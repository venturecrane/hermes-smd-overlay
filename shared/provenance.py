"""Per-session identifier provenance register (A1 — the runtime register).

The identifier-integrity gate (``shared.identifier_filter``) decides whether an
identifier in an outbound draft was actually READ from a source this session.
That requires a per-session record of what the agent read — this module is it,
the runtime analogue of ``shared.inbound.PENDING``.

Two collaborating points in ``hermes-smd-trust``:

* ``post_tool_call`` — for a READ-class tool, :func:`record_read` extracts the
  structured identifiers from the tool RESULT and adds them to that session's
  register.
* ``pre_tool_call`` — when the outbound gate evaluates a draft, it consults
  :func:`register_for` to ask "is each identifier in this body one we read?"

Process-local + bounded. One customer Machine = one agent process, so a
module-level dict keyed by ``session_id`` is the right scope (same shape as
``inbound.PENDING``). The dict is LRU-bounded so a long-lived Machine cannot
grow it without limit; an evicted session simply yields an empty register (the
report-only gate over-reports rather than under-reports — the safe direction).

This module holds NO names. Party/recipient names cannot be reliably scanned
from free read-text (the whole reason body name-checks are greeting-slot only),
so names are out of the runtime register v1; the gate reports on the
structured-shape kinds it can verify from reads (dates, A-numbers, receipts,
SSNs, case numbers) and leaves names to a structured-metadata seeding follow-on.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from shared.identifier_filter import ProvenanceRegister

logger = logging.getLogger(__name__)

# Bound the number of live session registers. A Machine handles one agent at a
# time; a handful of concurrent sessions is the realistic ceiling. 256 is far
# above that and caps memory if session_ids churn (each register is a small set
# of canonical strings).
_MAX_SESSIONS = 256

_registers: OrderedDict[str, ProvenanceRegister] = OrderedDict()


def record_read(session_id: str, text: str) -> None:
    """Add the structured identifiers found in a read-tool RESULT to the
    session's register. Best-effort: a bad/oversized blob is logged and skipped,
    never raised — provenance recording must never break the tool path."""
    if not session_id or not isinstance(text, str) or not text:
        return
    try:
        reg = _registers.get(session_id)
        if reg is None:
            reg = ProvenanceRegister()
            _registers[session_id] = reg
            _evict_if_needed()
        else:
            _registers.move_to_end(session_id)  # LRU touch
        reg.add_read_text(text)
    except Exception:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.debug("provenance: record_read failed for session %s", session_id, exc_info=True)


def register_for(session_id: str) -> ProvenanceRegister:
    """Return the session's register, or an empty one if nothing was recorded.

    An empty register means the gate cannot verify any identifier this session —
    in report-only mode that surfaces everything (the safe over-report
    direction), distinguishable downstream via ``register_was_empty``."""
    reg = _registers.get(session_id)
    if reg is None:
        return ProvenanceRegister()
    _registers.move_to_end(session_id)
    return reg


def drop(session_id: str) -> None:
    """Forget a session's register (e.g. at session end). Idempotent."""
    _registers.pop(session_id, None)


def _evict_if_needed() -> None:
    while len(_registers) > _MAX_SESSIONS:
        evicted, _ = _registers.popitem(last=False)  # oldest
        logger.debug(
            "provenance: evicted oldest session register %s (cap %d)", evicted, _MAX_SESSIONS
        )


def _reset_for_tests() -> None:
    """Clear all registers — test hook only."""
    _registers.clear()


__all__ = ["record_read", "register_for", "drop"]
