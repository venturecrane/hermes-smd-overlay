"""Connector-health self-check — read the call-outcome ledger for the beat.

ss#1990 (connector-outage alerting), the connector analogue of
:mod:`shared.scheduler_check`. Runs INSIDE the gate's heartbeat emitter
each tick: reads the tmpfs ledger the agent-side connector-health plugin
maintains (:mod:`shared.connector_ledger`) and shapes it into the per-server
map the heartbeat ships to the console.

Design rules (ADR 0080):

* **Read-only.** This module never writes the ledger.
* **Ages, not timestamps.** Every time field in the emitted entries is an
  age in seconds computed HERE, writer-side, against the beat clock:
  ``run_age_seconds`` (current failure run), ``last_ok_age_seconds``,
  ``last_error_age_seconds``. The console's alerter evaluates STORED values
  only — a frozen row from a dead seat can never self-activate a page by
  wall-clock passage, and seat-vs-console clock skew never enters any
  predicate.
* **Absence is a hold, corruption is a page.** Missing ledger (fresh boot,
  no MCP calls yet) → ``ok=True`` with an empty map: nothing to conclude,
  the console holds. Unreadable/corrupt ledger, or the plugin's
  ``mapping_ok=False`` flag (tool→server mapping unavailable after a Hermes
  pin bump — nothing is being counted) → ``ok=False`` with ``servers=None``:
  the check itself is broken and the console pages ``connector_check_error``
  rather than the whole alert class going silently dark.
* **Malformed entries are dropped, not coerced.** A valid entry's meaning
  does not depend on its neighbors; dropping one (absence = hold for that
  server) is strictly safer than guessing at a half-trusted entry.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from shared.connector_ledger import ledger_path

logger = logging.getLogger("hermes_smd.connector_check")


@dataclass(frozen=True)
class ConnectorCheck:
    """Outcome of one connector-health read.

    ``ok`` is the health of the CHECK ITSELF (ledger readable, mapping
    alive), not of any connector. ``servers`` maps sanitized MCP server
    name → payload entry; ``None`` when the check is broken (never emit a
    map you cannot trust).
    """

    ok: bool
    servers: dict[str, dict] | None


def _age(now: float, ts: object) -> int | None:
    """Non-negative whole-second age of epoch ``ts``, or None."""
    try:
        value = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    age = now - value
    return int(age) if age >= 0 else 0


def _shape_entry(raw: object, *, now: float) -> dict | None:
    """One ledger entry → one payload entry, or None to drop it."""
    if not isinstance(raw, dict):
        return None
    count = raw.get("consecutive_failures")
    if not isinstance(count, int) or count < 0:
        return None
    entry: dict[str, object] = {"consecutive_failures": count}
    if count > 0:
        run_age = _age(now, raw.get("first_error_ts"))
        if run_age is None:
            # A failure run with no parseable start cannot satisfy any
            # age-gated condition; drop the entry (hold) rather than guess.
            return None
        entry["run_age_seconds"] = run_age
        first = raw.get("first_error_ts")
        last_conn = raw.get("last_conn_error_ts")
        try:
            entry["conn_evidence"] = (
                last_conn is not None and float(last_conn) >= float(first)  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            entry["conn_evidence"] = False
    last_ok_age = _age(now, raw.get("last_ok_ts"))
    if last_ok_age is not None:
        entry["last_ok_age_seconds"] = last_ok_age
    last_error_age = _age(now, raw.get("last_error_ts"))
    if last_error_age is not None:
        entry["last_error_age_seconds"] = last_error_age
    message = raw.get("last_error_message")
    if isinstance(message, str) and message:
        entry["last_error_message"] = message
    return entry


# ---------------------------------------------------------------------------
# Durable-credential age (ss#2148). Some connectors hold a durable credential
# on the volume whose lifetime is finite (Smokeball refresh token: 30 days,
# vendor-confirmed). The file's mtime is the last write — initial consent or a
# rotation persist — so its age IS the credential's age. The console's
# pre-expiry condition (connector_token_expiring) fires on this field.
#
# Shipped as a SEPARATE heartbeat field, never synthesized into the health
# map: fabricating a ``consecutive_failures: 0`` entry for an idle connector
# would falsely RESOLVE an open connector_down alert (resolve only on proven
# success — ADR 0080). Ages, not timestamps, same as everything here.
# ---------------------------------------------------------------------------

_TOKEN_FILE_ENV: dict[str, tuple[str, str]] = {
    # server name → (env var override, default path)
    "smokeball": ("SMOKEBALL_REFRESH_TOKEN_FILE", "/opt/data/.smokeball-mcp/refresh_token"),
}


def token_ages(*, now: float | None = None) -> dict[str, int]:
    """Age in seconds of each connector's durable credential file.

    A server appears only when its token file exists and stats cleanly —
    absence means "nothing to report" (hold), never zero. Read-only,
    fail-soft: a stat error drops the key.
    """
    import os
    from pathlib import Path

    reference = time.time() if now is None else now
    ages: dict[str, int] = {}
    for server, (env_var, default) in _TOKEN_FILE_ENV.items():
        path = Path(os.environ.get(env_var) or default)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        age = _age(reference, mtime)
        if age is not None:
            ages[server] = age
    return ages


def check(*, now: float | None = None) -> ConnectorCheck:
    """Read the ledger and shape the heartbeat's connector map."""
    reference = time.time() if now is None else now
    path = ledger_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Distinguish two very different absences (2026-07-25 live finding on
        # smd-staging: /run is root-owned tmpfs, the ledger DIR was never
        # boot-created, every record_call silently failed, and a REAL 401
        # outage read as legit-empty green — the exact class this system
        # exists to kill). entrypoint.sh now creates the dir root-side before
        # the privilege drop, so:
        #   dir present, file missing → genuinely no calls yet: legit-empty.
        #   dir MISSING → the writer cannot possibly record; the check is
        #     broken and must PAGE (connector_check_error), not hold green.
        if not path.parent.is_dir():
            logger.warning(
                "connector_check: ledger dir %s missing — writer cannot "
                "record; boot contract broken (entrypoint mkdir absent?)",
                path.parent,
            )
            return ConnectorCheck(ok=False, servers=None)
        return ConnectorCheck(ok=True, servers={})
    except OSError as exc:
        logger.warning("connector_check: ledger unreadable: %s", exc)
        return ConnectorCheck(ok=False, servers=None)
    try:
        doc = json.loads(raw)
    except ValueError:
        logger.warning("connector_check: ledger corrupt")
        return ConnectorCheck(ok=False, servers=None)
    if not isinstance(doc, dict) or not isinstance(doc.get("servers"), dict):
        logger.warning("connector_check: ledger has unexpected shape")
        return ConnectorCheck(ok=False, servers=None)
    if doc.get("mapping_ok") is False:
        logger.warning(
            "connector_check: tool→server mapping unavailable (Hermes pin "
            "moved _mcp_tool_server_names?) — nothing is being counted"
        )
        return ConnectorCheck(ok=False, servers=None)

    servers: dict[str, dict] = {}
    for name, raw_entry in doc["servers"].items():
        if not isinstance(name, str) or not name:
            continue
        shaped = _shape_entry(raw_entry, now=reference)
        if shaped is not None:
            servers[name] = shaped
    return ConnectorCheck(ok=True, servers=servers)


__all__ = ["ConnectorCheck", "check", "token_ages"]
