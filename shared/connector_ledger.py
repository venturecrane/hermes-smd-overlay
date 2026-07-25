"""Per-MCP-server failure ledger — the agent→gate bridge for connector health.

ss#1990 (connector-outage alerting). The agent-process connector-health
plugin records the outcome of every MCP tool call here; the gate's
heartbeat emitter reads it back through :mod:`shared.connector_check` each
60s tick and ships a per-server map to the console. Two processes, one
tmpfs file — the same producer/consumer shape as
:mod:`shared.mcp_result_store` (same-uid processes, atomic
temp-file+rename publish).

**Path.** ``SMD_CONNECTOR_LEDGER_PATH`` (default
``/run/smd-connector-health/ledger.json``). ``/run`` is tmpfs: fast,
ephemeral, wiped on Machine restart. The wipe is CORRECT for this signal —
stale pre-restart failure counts must not survive into a fresh boot; the
console HOLDS any open alert until post-restart calls rebuild the run
(never a false RECOVERED from absence, ADR 0079 doctrine).

**Semantics.** Per server: ``consecutive_failures`` (the only alert input),
``first_error_ts`` (start of the current unbroken failure run, set on the
0→1 transition), ``last_ok_ts``, ``last_error_ts``, ``last_conn_error_ts``
(last failure whose message matched :mod:`shared.connector_signatures`),
``last_error_message`` (truncated at write). A success resets the count to
0 and clears the run fields but KEEPS the key — the console resolves an
alert only on a proven success, never on key-absence. Timestamps are epoch
seconds (float): pure aware-clock arithmetic, no tz parsing class of bugs.

**Bounded.** The map is capped at :data:`MAX_SERVERS`. Eviction priority
protects the alerting signal: failing entries are kept first, then entries
with a recent error (an open console alert is waiting on their recovery
signal — shedding a just-recovered entry would strand that alert open
forever), and never-errored healthy entries are shed first.

Fail-soft throughout: every writer/reader error logs at WARNING and
degrades (a failed record is one lost sample; the next call records again).
Concurrent one-shot agent processes can race the read-modify-write —
last-writer-wins loses at most an increment (undercount, never a false
page), accepted and documented in ADR 0080.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("hermes_smd.connector_ledger")

_DEFAULT_LEDGER_PATH = "/run/smd-connector-health/ledger.json"

MAX_SERVERS = 32
MAX_ERROR_CHARS = 200


def ledger_path() -> Path:
    """The ledger file path (``SMD_CONNECTOR_LEDGER_PATH`` or the default)."""
    return Path(os.environ.get("SMD_CONNECTOR_LEDGER_PATH") or _DEFAULT_LEDGER_PATH)


def _load(path: Path) -> dict:
    """Read the raw ledger document. Missing file → fresh empty document.
    A corrupt document is REPLACED fresh (the writer must keep writing so
    detection recovers; the reader independently reports the corruption
    window as check-not-ok)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"mapping_ok": True, "servers": {}}
    except OSError as exc:
        logger.warning("connector_ledger: read failed (%s); starting fresh", exc)
        return {"mapping_ok": True, "servers": {}}
    try:
        doc = json.loads(raw)
    except ValueError:
        logger.warning("connector_ledger: corrupt ledger; starting fresh")
        return {"mapping_ok": True, "servers": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("servers"), dict):
        return {"mapping_ok": True, "servers": {}}
    doc.setdefault("mapping_ok", True)
    return doc


def _store(path: Path, doc: dict) -> bool:
    """Atomically publish ``doc`` (temp file + rename, mcp_result_store shape)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.warning("connector_ledger: cannot create %s: %s", path.parent, exc)
        return False
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("connector_ledger: write failed: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _evict(servers: dict[str, dict]) -> dict[str, dict]:
    """Enforce :data:`MAX_SERVERS` with alert-preserving priority."""
    if len(servers) <= MAX_SERVERS:
        return servers

    def keep_rank(item: tuple[str, dict]) -> tuple[int, float]:
        entry = item[1]
        failing = 1 if entry.get("consecutive_failures", 0) else 0
        last_error = entry.get("last_error_ts") or 0.0
        try:
            last_error = float(last_error)
        except (TypeError, ValueError):
            last_error = 0.0
        # Sort descending by (failing, recency-of-error): failing entries
        # first, then recently-errored (a console alert may be waiting on
        # their recovery), never-errored healthy entries last → shed first.
        return (failing, last_error)

    ranked = sorted(servers.items(), key=keep_rank, reverse=True)
    return dict(ranked[:MAX_SERVERS])


def record_call(
    server: str,
    *,
    ok: bool,
    error_message: str | None = None,
    conn_class: bool = False,
    now: float | None = None,
) -> bool:
    """Record one MCP tool-call outcome for ``server``. Returns True on a
    successful ledger publish; False (logged) on any failure."""
    if not server or not isinstance(server, str):
        return False
    ts = time.time() if now is None else now
    path = ledger_path()
    doc = _load(path)
    servers: dict[str, dict] = doc["servers"]
    entry = servers.get(server)
    if not isinstance(entry, dict):
        entry = {}
    if ok:
        entry["consecutive_failures"] = 0
        entry["last_ok_ts"] = ts
        # End of the failure run: clear the run fields, keep the key (the
        # console resolves only on this proven-success state) and keep the
        # historical last_error_* for the admin staleness display.
        entry.pop("first_error_ts", None)
        entry.pop("last_conn_error_ts", None)
    else:
        count = entry.get("consecutive_failures")
        count = count if isinstance(count, int) and count >= 0 else 0
        if count == 0:
            entry["first_error_ts"] = ts
        entry["consecutive_failures"] = count + 1
        entry["last_error_ts"] = ts
        if isinstance(error_message, str) and error_message:
            entry["last_error_message"] = error_message[:MAX_ERROR_CHARS]
        if conn_class:
            entry["last_conn_error_ts"] = ts
    servers[server] = entry
    doc["servers"] = _evict(servers)
    return _store(path, doc)


def mark_mapping_broken() -> bool:
    """Record that the tool→server mapping is unavailable (Hermes pin moved
    ``_mcp_tool_server_names``). With no mapping, NOTHING is being counted —
    the reader reports check-not-ok so the console pages instead of the
    whole alert class going silently dark."""
    path = ledger_path()
    doc = _load(path)
    if doc.get("mapping_ok") is False:
        return True
    doc["mapping_ok"] = False
    return _store(path, doc)


__all__ = [
    "MAX_ERROR_CHARS",
    "MAX_SERVERS",
    "ledger_path",
    "mark_mapping_broken",
    "record_call",
]
