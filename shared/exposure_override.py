"""Runtime exposure overrides — the client-owned entitlement dial (ss#2003 Q7).

A Named Administrator changes a routine's autonomy level in the client portal;
the console compiles the tier to per-action-class ceiling values and posts them
here through the gate (POST /entitlement/set, console-proxy bearer — the same
trust boundary as /sticky-stop/set). The override is RUNTIME POSTURE layered on
top of the authored exposure, exactly as an operator pause is runtime posture
layered on top of the authored schedule:

    effective(action) = override(action) if present else authored exposure

The authored ceiling stays in git. ``personas[].entitlements.exposure_ceiling``
(customer.yaml, optional map action_class -> ceiling) is the letter commitment
compiled per action class; an override may move FREELY in both directions at or
below that ceiling and may NEVER exceed it. When no exposure_ceiling is
authored for a class, the authored exposure value itself is the bound — absence
of an authored ceiling is absence of permission to raise (fail-closed, ADR
0056). The clamp runs HERE at write time (the gate refuses the set) and AGAIN
in the trust plugin at read time (defense in depth: a row that somehow exceeds
the ceiling is narrowed, never honored).

Persistence: Machine-local SQLite on the Fly volume (ADR 0062 posture, same as
``sticky_stop_state``), so an override survives restart AND reprovision by
design. One row per (customer, persona, action_class); the console is the sole
writer and audits who/when/why control-plane-side where the actor was
authenticated (same division of labor as the pause: the Machine enforces, the
control plane attributes). The actor/reason columns here are a debugging
convenience mirror, not the audit record.

Import direction: plugins import shared, never the reverse — so the ceiling
vocabulary is a local closed set that round-trips with the trust plugin's
``Ceiling`` enum through string values (same convention as
``shared.action_classes.ActionClass`` vs the ss-console TS validators).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from shared.action_classes import ActionClass
from shared.ids import iso_utc

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/opt/data/smd/exposure_override.db"

# Round-trips with plugins/hermes-smd-trust/enforce.Ceiling (string values are
# the contract; see module docstring).
CEILINGS: frozenset[str] = frozenset({"autonomous", "confirm", "draft_for_review", "refused"})

# Restrictiveness ordering: higher == more restrictive. Mirrors the trust
# plugin's _RESTRICTIVENESS table (same string contract).
RESTRICTIVENESS: dict[str, int] = {
    "autonomous": 0,
    "confirm": 1,
    "draft_for_review": 2,
    "refused": 3,
}

# Action classes an override may address. READ is never authored (enforcement
# always allows it); REFUSED is the fail-closed terminal class, not a dial.
_OVERRIDABLE_ACTIONS: frozenset[str] = frozenset(
    a.value for a in ActionClass if a not in (ActionClass.READ, ActionClass.REFUSED)
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS exposure_override (
  customer      TEXT NOT NULL,
  persona       TEXT NOT NULL,
  action_class  TEXT NOT NULL,
  ceiling       TEXT NOT NULL,
  actor_id      TEXT NOT NULL,
  reason        TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (customer, persona, action_class)
)
"""


def db_path() -> str:
    """Resolve the override state file path (env override for tests)."""
    return os.environ.get("SMD_EXPOSURE_OVERRIDE_DB_PATH") or DEFAULT_DB_PATH


def _customer_slug() -> str:
    return os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "_machine"


def _connect(path: str | None) -> sqlite3.Connection:
    resolved = Path(path or db_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def _authored_bounds(persona_slug: str) -> tuple[dict[str, str], dict[str, str]]:
    """Read (exposure, exposure_ceiling) for a persona from the trusted config.

    Raw string maps, invalid entries dropped with a warning (mirror of the
    trust plugin's ``_parse_exposure_map`` posture — a garbled entry falls to
    the fail-closed default, never silently grants autonomy). A missing
    customer.yaml (dev/test) yields two empty maps — with no authored exposure
    and no authored ceiling every raise is refused, which is the correct
    fail-closed answer on an unprovisioned box.

    Any OTHER read fault propagates: the caller (the gate handler) turns it
    into a 5xx and the console records nothing — a bound we cannot read is a
    bound we do not enforce against, so the set must not happen.
    """
    from shared.customer_config import CustomerConfig, CustomerConfigMissingError

    try:
        personas = CustomerConfig.from_volume().personas
    except NotImplementedError:
        return {}, {}
    except CustomerConfigMissingError:
        logger.debug("no customer.yaml on volume; empty bounds", exc_info=True)
        return {}, {}

    def _clean(raw: object) -> dict[str, str]:
        out: dict[str, str] = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            key, val = str(k), str(v)
            if key not in _OVERRIDABLE_ACTIONS:
                continue
            if val not in CEILINGS:
                logger.warning("exposure_override: invalid ceiling %r for %s; dropping", v, k)
                continue
            out[key] = val
        return out

    for persona in personas:
        if isinstance(persona, dict) and persona.get("slug") == persona_slug:
            ent = persona.get("entitlements")
            if not isinstance(ent, dict):
                return {}, {}
            return _clean(ent.get("exposure")), _clean(ent.get("exposure_ceiling"))
    return {}, {}


def max_allowed(action_class: str, exposure: dict[str, str], ceiling: dict[str, str]) -> str:
    """The most autonomous value an override may take for one action class.

    The authored ``exposure_ceiling`` when present; otherwise the authored
    ``exposure`` value; otherwise ``refused`` (unauthored = no permission,
    ADR 0056).
    """
    return ceiling.get(action_class) or exposure.get(action_class) or "refused"


def set_overrides(
    *,
    persona: str,
    changes: list[dict[str, Any]],
    actor_id: str,
    reason: str,
    path: str | None = None,
) -> dict[str, Any]:
    """Apply a batch of override rows atomically, clamped to authored bounds.

    ``changes`` is ``[{action_class, ceiling}, ...]`` — the compiled exposure
    delta of ONE tier change, applied all-or-nothing (a tier is a coherent
    bundle; half a tier is not a state the grid describes). Rejects the whole
    batch when any entry names an unknown action class, an unknown ceiling, or
    a ceiling more autonomous than :func:`max_allowed` for that class.

    Caller responsibility (same contract as ``pin_hard_stops``): authenticate
    the actor BEFORE invoking. Returns ``{applied: [...], persona, level}``
    on success; raises ``ValueError`` with a rejection payload on refusal.
    """
    if not persona or not actor_id or not reason:
        raise ValueError("persona, actor_id and reason are required")
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list")

    exposure, ceiling = _authored_bounds(persona)
    normalized: list[tuple[str, str]] = []
    rejections: list[str] = []
    for entry in changes:
        if not isinstance(entry, dict):
            rejections.append("malformed change entry")
            continue
        action = str(entry.get("action_class") or "")
        value = str(entry.get("ceiling") or "")
        if action not in _OVERRIDABLE_ACTIONS:
            rejections.append(f"unknown action class {action!r}")
            continue
        if value not in CEILINGS:
            rejections.append(f"unknown ceiling {value!r} for {action}")
            continue
        bound = max_allowed(action, exposure, ceiling)
        if RESTRICTIVENESS[value] < RESTRICTIVENESS[bound]:
            rejections.append(
                f"{action}: {value} exceeds the authored ceiling {bound} "
                "(raising a ceiling is a commitment change, not a settings change)"
            )
            continue
        normalized.append((action, value))
    if rejections:
        raise ValueError("; ".join(rejections))

    customer = _customer_slug()
    now = iso_utc()
    conn = _connect(path)
    try:
        for action, value in normalized:
            conn.execute(
                "INSERT INTO exposure_override "
                "(customer, persona, action_class, ceiling, actor_id, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(customer, persona, action_class) DO UPDATE SET "
                "ceiling = excluded.ceiling, actor_id = excluded.actor_id, "
                "reason = excluded.reason, updated_at = excluded.updated_at",
                (customer, persona, action, value, actor_id, reason, now),
            )
        conn.commit()
        return {
            "applied": [{"action_class": a, "ceiling": v} for a, v in normalized],
            "persona": persona,
            "updated_at": now,
        }
    finally:
        conn.close()


def read_overrides(persona: str, path: str | None = None) -> dict[str, str]:
    """Current override map ``{action_class: ceiling}`` for one persona.

    A missing state file means no override was ever set — empty map, authored
    exposure stands. Any sqlite fault PROPAGATES (the trust plugin's caller
    fails closed for sensitive actions rather than silently resolving the
    authored posture a client may have lowered).
    """
    resolved = Path(path or db_path())
    if not resolved.exists():
        return {}
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute(_CREATE_TABLE_SQL)
        rows = conn.execute(
            "SELECT action_class, ceiling FROM exposure_override WHERE customer = ? AND persona = ?",
            (_customer_slug(), persona),
        ).fetchall()
        return {
            str(action): str(value)
            for action, value in rows
            if str(action) in _OVERRIDABLE_ACTIONS and str(value) in CEILINGS
        }
    finally:
        conn.close()


def read_all(path: str | None = None) -> list[dict[str, Any]]:
    """Every override row on this Machine — the gate's read surface for the
    console display and the live probes. Missing file => empty list."""
    resolved = Path(path or db_path())
    if not resolved.exists():
        return []
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute(_CREATE_TABLE_SQL)
        rows = conn.execute(
            "SELECT customer, persona, action_class, ceiling, actor_id, reason, updated_at "
            "FROM exposure_override ORDER BY persona, action_class"
        ).fetchall()
        return [
            {
                "customer": r[0],
                "persona": r[1],
                "action_class": r[2],
                "ceiling": r[3],
                "actor_id": r[4],
                "reason": r[5],
                "updated_at": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


__all__ = [
    "CEILINGS",
    "DEFAULT_DB_PATH",
    "RESTRICTIVENESS",
    "db_path",
    "max_allowed",
    "read_all",
    "read_overrides",
    "set_overrides",
]
