"""The kinds of job proven on this seat, as the request cards read them.

The seat's lane-proof gate lives in ss-console's medchron runner
(``operator/runners/medchron/medchron/lane_proof.py``): the root daemon holds a
job whose kind is not proven, and notes ``lane_unproven: <key>`` on its ledger
row. This module is the READ side the gate's heartbeat leg needs to page SMD
(``request_cards``): the same two root-owned files, read the same way.

* ``lane-proofs.yaml``          the authored seed and revocations, from the vault;
* ``lane-proofs.runtime.json``  events ``medchron lane-proof accept|revoke`` appends.

A key is proven when the authored file does not revoke it and either its last
runtime event is ``proven`` or it has none and the authored file proves it.
Both repos pin that rule to the same cases
(``tests/contract/lane_proofs_cases.json``, mirrored byte for byte with
ss-console's ``operator/contracts/lane-proofs-cases.json``).

Read-only, never raises: an unreadable file is a ``problem`` and proves
nothing, so a hold still pages (as ``unreadable``) and an in-turn ask is not
paged off a record the gate could not read.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_DIR = "/run/smd-medchron"
DIR_ENV = "SMD_LANE_PROOFS_DIR"
AUTHORED_NAME = "lane-proofs.yaml"
RUNTIME_NAME = "lane-proofs.runtime.json"
HOLD_PREFIX = "lane_unproven:"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}(?:\.[a-z0-9_]{1,40})?$")


@dataclass
class Proofs:
    proven: dict[str, dict[str, Any]] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    def is_proven(self, key: str) -> bool:
        return key in self.proven and key not in self.revoked

    def skill_proven(self, skill: str) -> bool:
        """An in-turn skill is proven when it, or any of its job kinds, is."""
        return any(
            self.is_proven(k) for k in self.proven if k == skill or k.startswith(skill + ".")
        )

    def why(self, key: str) -> str:
        """The card's closed reason vocabulary."""
        if key in self.revoked:
            return "revoked"
        return "unreadable" if self.problems else "never_proven"


def _authored(path: Path, out: Proofs) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        out.problems.append(f"{AUTHORED_NAME} absent")
        return {}
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        lanes, revoked = doc.get("lanes") or {}, doc.get("revoked") or []
        if not isinstance(lanes, dict) or not isinstance(revoked, list):
            raise ValueError("shape")
    except Exception as exc:  # noqa: BLE001 - see the module note
        out.problems.append(f"{AUTHORED_NAME} unreadable: {type(exc).__name__}")
        return {}
    for item in revoked:
        key = str(item.get("key") or "") if isinstance(item, dict) else ""
        if KEY_RE.match(key):
            out.revoked.add(key)
    return {str(k): dict(v or {}) for k, v in lanes.items() if KEY_RE.match(str(k))}


def load(directory: str | Path | None = None) -> Proofs:
    base = Path(directory or os.environ.get(DIR_ENV) or DEFAULT_DIR)
    out = Proofs()
    proven = _authored(base / AUTHORED_NAME, out)
    runtime = base / RUNTIME_NAME
    try:
        doc = (
            json.loads(runtime.read_text(encoding="utf-8")) if runtime.is_file() else {"events": []}
        )
        events = doc.get("events") if isinstance(doc, dict) else None
        if not isinstance(events, list):
            raise ValueError("no events list")
    except Exception as exc:  # noqa: BLE001 - see the module note
        out.problems.append(f"{RUNTIME_NAME} unreadable: {type(exc).__name__}")
        return out
    for e in events:
        key = str(e.get("key") or "") if isinstance(e, dict) else ""
        if not KEY_RE.match(key):
            continue
        if e.get("event") == "proven":
            proven[key] = dict(e)
        elif e.get("event") == "revoked":
            proven.pop(key, None)
    out.proven = proven
    return out


def held_key(reason: object) -> str | None:
    """The lane key a ledger row's hold note names, or None."""
    if not isinstance(reason, str) or not reason.startswith(HOLD_PREFIX):
        return None
    key = reason[len(HOLD_PREFIX) :].strip()
    return key if KEY_RE.match(key) else None


__all__ = ["DIR_ENV", "HOLD_PREFIX", "KEY_RE", "Proofs", "held_key", "load"]
