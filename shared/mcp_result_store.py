"""Cross-process result store for the MCP channel's synchronous return.

Why this exists (read first): the MCP channel (Claude as an inbound channel,
docs/design/operator/03-mcp-server-exposure.md) must answer a `tools/call`
**synchronously** — the client waits for the answer as the call's return value.
But Hermes' webhook dispatch is fire-and-forget: the adapter spawns the agent
turn as a background task and returns ``202 Accepted`` immediately
(``gateway/platforms/webhook.py`` L570-583), delivering the agent's eventual
answer out-of-band. So there is no built-in request→turn→response path.

This store bridges that gap **without touching Hermes core**:

  * The agent-process result-sink plugin (``hermes-smd-mcp-result-sink``)
    captures the completed turn's answer in its ``post_llm_call`` hook and
    ``put()``s it here, keyed by the request's correlation id.
  * The gate-process ``/mcp`` route (``webhook_gate.py``) ``take()``s it in a
    bounded long-poll and returns it as the MCP ``tools/call`` result.

**Cross-process by necessity.** The webhook gate and the Hermes agent run as
two separate (same-uid ``hermes``) processes, so the store cannot be an
in-process dict — the writer and the reader live in different processes. A
small file store under a tmpfs dir is the simplest correct bridge: one JSON
file per correlation id, atomically written, one-shot consumed.

**Path.** ``SMD_MCP_STORE_DIR`` (default ``/run/smd-mcp``). ``/run`` is tmpfs —
fast, ephemeral, and NOT under ``/opt/data`` (so it is unaffected by the
gateway's mid-boot ``chmod 0700`` of the data home that the audit ledger had to
bind-mount around, OP-P1-4). Created ``0700`` and owned by the single Machine
uid.

**Bounded.** Every ``put()`` opportunistically prunes files older than
``_TTL_SECONDS`` so an answer the gate never collected (client disconnected,
timed out) cannot accumulate. Correlation ids are sanitised to a safe charset
before they touch the filesystem (they are gate-generated, but defence in depth
against path traversal is cheap).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("hermes_smd.mcp_result_store")

_DEFAULT_STORE_DIR = "/run/smd-mcp"
# A correlation id is gate-generated (a ULID or the JSON-RPC id). Restrict to a
# filesystem-safe charset and bound the length so it can only ever name a single
# flat file inside the store dir — never traverse out of it.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# An uncollected answer is pruned after this long. Comfortably longer than the
# gate's long-poll budget, so a result is never pruned out from under a poll
# that is still waiting for it.
_TTL_SECONDS = 300


def store_dir() -> Path:
    """The store directory (``SMD_MCP_STORE_DIR`` or the ``/run`` default)."""
    return Path(os.environ.get("SMD_MCP_STORE_DIR") or _DEFAULT_STORE_DIR)


def _safe_id(correlation_id: str) -> str | None:
    """Return ``correlation_id`` iff it is a safe single path segment, else None."""
    if correlation_id and _SAFE_ID_RE.match(correlation_id):
        return correlation_id
    return None


def _path_for(directory: Path, correlation_id: str) -> Path | None:
    safe = _safe_id(correlation_id)
    return (directory / f"{safe}.json") if safe else None


def _ensure_dir(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return True
    except OSError as exc:
        logger.warning("mcp_result_store: cannot create %s: %s", directory, exc)
        return False


def put(correlation_id: str, payload: dict, *, now: float | None = None) -> bool:
    """Atomically write the turn's answer for ``correlation_id``.

    Returns True on success. Fail-soft: a write error is logged and returns
    False (the agent turn already happened; a failed store just means the gate
    long-poll will time out and the client can retry). Opportunistically prunes
    stale results on the way in.
    """
    directory = store_dir()
    if not _ensure_dir(directory):
        return False
    path = _path_for(directory, correlation_id)
    if path is None:
        logger.warning("mcp_result_store: refusing unsafe correlation id")
        return False

    _prune(directory, now=now)

    tmp = path.with_suffix(".json.tmp")
    try:
        # Atomic publish: write the temp file fully, then rename into place so a
        # concurrent reader never observes a half-written result.
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("mcp_result_store: put failed for one result: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def take(correlation_id: str) -> dict | None:
    """Read-and-remove the answer for ``correlation_id`` (one-shot).

    Returns the stored payload dict, or None if not present yet. Removing on
    read keeps the store self-cleaning under the normal (collected) path; the
    TTL prune only catches answers that were never collected.
    """
    directory = store_dir()
    path = _path_for(directory, correlation_id)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("mcp_result_store: take read failed: %s", exc)
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _prune(directory: Path, *, now: float | None = None) -> None:
    """Remove result files older than the TTL. Best-effort, never raises."""
    reference = time.time() if now is None else now
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    for entry in entries:
        try:
            if reference - entry.stat().st_mtime > _TTL_SECONDS:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
