"""Per-thread conversation transcript for the MCP channel's continuity.

Why this exists (read first): the MCP channel (Claude as an inbound channel,
docs/design/operator/03-mcp-server-exposure.md) is a LIVE bidirectional
conversation — the worker should remember what was said earlier in the same
exchange, exactly as it would on the Hermes CLI. Hermes core DOES support that
(``gateway/session.py:get_or_create_session`` reuses one ``session_id`` whenever
the ``chat_id`` recurs). But the MCP channel rides Hermes' *webhook adapter*,
and that adapter (``gateway/platforms/webhook.py``) sets
``chat_id = webhook:{route}:{delivery_id}`` where ``delivery_id`` is ALSO its
idempotency/dedup key — forced unique per delivery. So every webhook-delivered
turn is its own Hermes session by the adapter's design, and native session
memory cannot carry the thread. Decoupling chat_id from the dedup key is a
Hermes-core change, which the pin-only-fork / plugin-only overlay rule
(ADR 0015) forbids.

This store gives the conversation its continuity in the overlay instead: the
gate appends each (operator message, worker reply) pair under a thread key, and
re-injects the recent transcript into the next turn's prompt. The worker sees
the whole conversation in-context; the felt result is identical to native
continuity, with no core fork.

**Identity is the boundary (security).** The thread key is
``<hash(clerk_subject)>:<thread_id>`` — built by the gate from the AUTHENTICATED
principal (never a bare caller-supplied value). Two different identities that
pass the same ``thread_id`` land on DIFFERENT keys, so one principal can never
read or resume another's conversation. Your conversation is yours, the same way
no one else can pick up your phone call. ``thread_id`` absent => no continuity
(a one-shot turn), which is the safe default.

**Cross-process by necessity.** Like ``mcp_result_store``, the gate process
writes/reads this; it cannot be an in-process dict. One JSON file per thread
key under a tmpfs dir, atomically written, capped and TTL'd.

**Path.** ``SMD_MCP_STORE_DIR`` (default ``/run/smd-mcp``) — same tmpfs home as
the result store, NOT under ``/opt/data`` (so it survives the gateway's mid-boot
``chmod 0700`` of the data home, OP-P1-4). Created ``0700``, single Machine uid.

**Bounded.** Each thread keeps only the last ``_MAX_TURNS`` exchanges; every
append prunes thread files idle longer than ``_TTL_SECONDS``. A conversation
that goes quiet ages out; an active one stays warm.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("hermes_smd.mcp_thread_store")

_DEFAULT_STORE_DIR = "/run/smd-mcp"
# A thread key is gate-built as "<16-hex principal hash>:<caller thread_id>".
# Restrict to a filesystem-safe charset and bound the length so it can only ever
# name a single flat file inside the store dir — never traverse out of it.
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")
# A caller-supplied thread_id is bounded and charset-restricted independently,
# before it is composed into the key, so a hostile client cannot smuggle path
# segments or unbounded data through it.
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Keep the recent exchanges; older turns age out of context. Bounds both the
# file size and the prompt budget the history injection spends.
_MAX_TURNS = 12
# An idle thread is pruned after this long. Long enough that a real
# back-and-forth stays warm, short enough that abandoned threads do not linger.
_TTL_SECONDS = 7200  # 2 hours


def store_dir() -> Path:
    """The store directory (``SMD_MCP_STORE_DIR`` or the ``/run`` default)."""
    return Path(os.environ.get("SMD_MCP_STORE_DIR") or _DEFAULT_STORE_DIR)


def thread_key(principal_subject: str, thread_id: str) -> str | None:
    """Build the principal-namespaced thread key, or None when not threadable.

    ``<sha256(principal_subject)[:16]>:<thread_id>``. The principal hash is the
    isolation boundary: the caller chooses ``thread_id`` only WITHIN their own
    authenticated identity, so they can never name another identity's thread.
    Returns None when the principal or thread_id is missing/unsafe (=> the turn
    runs one-shot, the safe default).
    """
    if not principal_subject or not thread_id:
        return None
    if not _SAFE_THREAD_ID_RE.match(thread_id):
        logger.warning("mcp_thread_store: refusing unsafe thread_id")
        return None
    digest = hashlib.sha256(principal_subject.encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{thread_id}"


def _safe_key(key: str) -> str | None:
    return key if key and _SAFE_KEY_RE.match(key) else None


def _path_for(directory: Path, key: str) -> Path | None:
    safe = _safe_key(key)
    return (directory / f"thread-{safe}.json") if safe else None


def _ensure_dir(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return True
    except OSError as exc:
        logger.warning("mcp_thread_store: cannot create %s: %s", directory, exc)
        return False


def history(key: str) -> list[dict]:
    """Return the recent transcript for ``key`` (oldest first), or [].

    Each entry is ``{"role": "operator"|"worker", "text": str}``. Fail-soft: any
    read/parse error returns [] (a missing transcript just means no continuity
    for this turn, never an error).
    """
    directory = store_dir()
    path = _path_for(directory, key)
    if path is None:
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("mcp_thread_store: history read failed: %s", exc)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list):
        return []
    return [t for t in turns if isinstance(t, dict) and "role" in t and "text" in t]


def append(key: str, operator_message: str, worker_reply: str, *, now: float | None = None) -> bool:
    """Append one (operator → worker) exchange to thread ``key``.

    Keeps only the last ``_MAX_TURNS`` exchanges. Atomic write; opportunistic
    TTL prune of idle threads. Fail-soft: a write error is logged and returns
    False (the turn already happened; a failed append just means the next turn
    lacks this much history).
    """
    directory = store_dir()
    if not _ensure_dir(directory):
        return False
    path = _path_for(directory, key)
    if path is None:
        logger.warning("mcp_thread_store: refusing unsafe thread key")
        return False

    _prune(directory, now=now)

    turns = history(key)
    turns.append({"role": "operator", "text": operator_message})
    turns.append({"role": "worker", "text": worker_reply})
    # Cap to the last _MAX_TURNS exchanges (2 entries per exchange).
    if len(turns) > _MAX_TURNS * 2:
        turns = turns[-_MAX_TURNS * 2 :]

    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps({"turns": turns}), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("mcp_thread_store: append failed: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def render(turns: list[dict], *, max_chars: int = 6000) -> str:
    """Render a transcript as a framed, prompt-ready history block.

    Returns "" for an empty transcript (the prompt's ``{history}`` slot then
    renders to nothing). Otherwise returns a labeled block ending in a blank
    line so it sits cleanly before the new message. The transcript is bounded to
    ``max_chars`` (oldest turns dropped first) so a long thread cannot blow the
    prompt budget. The turn is tainted regardless, so the history rides the same
    untrusted-origin treatment as the new message.
    """
    if not turns:
        return ""
    lines = []
    for t in turns:
        who = "Operator" if t.get("role") == "operator" else "You (Crane)"
        lines.append(f"{who}: {t.get('text', '')}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "…\n" + text[-max_chars:]
    return f"Earlier in this same conversation:\n{text}\n\n"


def _prune(directory: Path, *, now: float | None = None) -> None:
    """Remove thread files idle longer than the TTL. Best-effort, never raises."""
    reference = time.time() if now is None else now
    try:
        entries = list(directory.glob("thread-*.json"))
    except OSError:
        return
    for entry in entries:
        try:
            if reference - entry.stat().st_mtime > _TTL_SECONDS:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
