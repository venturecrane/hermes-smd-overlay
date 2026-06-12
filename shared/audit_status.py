"""Boot-scoped audit-wiring status sentinel + no-audit-mode rate limiter (#64).

Two independent paths let audit emission die silently while enforcement keeps
running: the audit plugin's ``register()`` can fail and leave every hook a
no-op, and the trust plugin's lazy ``_audit_client()`` caches a resolution
failure and skips every row. Blocking still works in both cases — the failure
is in the *accountability* sense, which for a compliance-grade ledger is its
own failure class.

This module gives both paths a visible health surface:

1. **Status sentinel.** The audit plugin writes
   ``$HERMES_HOME/.smd/audit_status.json`` at registration — first
   ``wired: false`` ("registration in progress"), then the outcome. The
   config-seam snapshot (``shared/config_snapshot.py``) reads it back and
   surfaces ``audit.writer_wired`` in ``operator.runtime.config/v1`` so the
   console's verify gate / drift audit sees a no-writer Machine.

   The sentinel records the writing process's PID. A handler can't sentinel
   its own non-execution: if the plugin never loads, the file is absent or
   carries a DEAD pid from a previous boot. The snapshot compares the recorded
   pid against the live agent pid and degrades on mismatch — staleness is
   detected, never misread as current state.

2. **Rate-limited no-audit warning.** ``NoAuditWarner`` lets per-evaluation
   skip paths log at WARNING without spamming: at most one warning per
   ``interval_seconds`` per process, so a Machine running dark says so in its
   logs continuously, not once at init.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = "smd.audit_status/1"

# Relative to HERMES_HOME. The agent process (which hosts the audit plugin)
# owns HERMES_HOME; the webhook gate reads it the same way config_snapshot
# already reads profiles/.
_STATUS_RELPATH = Path(".smd") / "audit_status.json"

_DEFAULT_HERMES_HOME = "/opt/data"


def _status_path(hermes_home: str | None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _STATUS_RELPATH


def write_audit_status(
    *,
    wired: bool,
    transport: str | None,
    reason: str | None,
    hermes_home: str | None = None,
) -> bool:
    """Persist the audit-wiring outcome for this boot. Best-effort, never raises.

    Returns True when the sentinel was written. The write is atomic
    (tmp + rename) so the gate never reads a torn file. ``reason`` must never
    carry a secret value — callers pass exception *types/messages* from the
    env-resolution layer, which by the shared.secrets contract name vars, not
    values.
    """
    path = _status_path(hermes_home)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "wired": wired,
        "transport": transport,
        "reason": reason,
        "pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("audit_status: sentinel write failed (%s): %s", path, exc)
        return False


def read_audit_status(hermes_home: str | None = None) -> dict[str, Any] | None:
    """Read the sentinel. ``None`` when absent/unparseable/wrong shape."""
    path = _status_path(hermes_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    return data


def evaluate_status(
    status: dict[str, Any] | None,
    live_agent_pid: int | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Turn a raw sentinel + the live agent pid into a snapshot fact (pure).

    Returns ``(audit_fact, degraded_entries)`` per the config-snapshot
    truthful-or-degraded contract:

    - sentinel absent            → ``writer_wired: None`` + degraded (the
      plugin may never have loaded — unknown, not "unwired")
    - pid mismatch vs live agent → value reported but degraded (previous boot)
    - live pid unknowable        → value reported but degraded (staleness
      can't be ruled out)
    - pid match                  → current-boot fact, no degradation
    """
    if status is None:
        return (
            {"writer_wired": None, "transport": None, "reason": None},
            [{"field": "audit.writer_wired", "reason": "status sentinel absent or unreadable"}],
        )

    fact = {
        "writer_wired": status.get("wired") if isinstance(status.get("wired"), bool) else None,
        "transport": status.get("transport"),
        "reason": status.get("reason"),
    }
    sentinel_pid = status.get("pid")

    if live_agent_pid is None:
        return fact, [
            {
                "field": "audit.writer_wired",
                "reason": "agent pid unknown; sentinel staleness undetectable",
            }
        ]
    if sentinel_pid != live_agent_pid:
        return fact, [
            {
                "field": "audit.writer_wired",
                "reason": f"sentinel pid {sentinel_pid} != live agent pid (previous boot?)",
            }
        ]
    return fact, []


class NoAuditWarner:
    """Rate-limited WARNING for code paths that skip an audit row.

    One instance per module (process-scoped). ``warn()`` logs at WARNING at
    most once per ``interval_seconds``; suppressed calls log at DEBUG so a
    verbose trace still shows every skip. Returns True when the WARNING fired.
    """

    def __init__(self, interval_seconds: float = 300.0) -> None:
        self._interval = interval_seconds
        self._last_warned: float | None = None

    def warn(self, log: logging.Logger, context: str) -> bool:
        now = time.monotonic()
        if self._last_warned is not None and (now - self._last_warned) < self._interval:
            log.debug("NO-AUDIT MODE (suppressed warning): %s", context)
            return False
        self._last_warned = now
        log.warning(
            "NO-AUDIT MODE: %s — decision enforced but NOT recorded in the audit ledger "
            "(audit client unconfigured); this warning repeats at most every %ds",
            context,
            int(self._interval),
        )
        return True


__all__ = [
    "SCHEMA",
    "write_audit_status",
    "read_audit_status",
    "evaluate_status",
    "NoAuditWarner",
]
