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

# ---------------------------------------------------------------------------
# Session resolver (overlay #141)
#
# Hermes core passes session_id to post_tool_call and pre_llm_call but NOT to
# pre_tool_call — all three fire sites in run_agent.py (10930, 11093, 11471 at
# the pinned ref) pass task_id only. Forensic proof: every tier3
# IDENTIFIER_UNVERIFIED row ever emitted lacked a session_id key and carried
# register_was_empty=true, while reads were recorded under the REAL id nobody
# consulted. One Machine = one agent process = sequential sessions, so the
# plugin keeps the last REAL id any hook observed and consulting hooks resolve
# a missing id to it. The note lands at turn start (pre_llm_call carries the
# real id) before any tool pre-hook fires, so the cross-session leak window is
# nil in practice; a resolver miss degrades to the OLD behavior (empty
# register: over-report / no exemption), never a widened one.
# ---------------------------------------------------------------------------

_last_seen_session: str = ""


def note_session(session_id: str | None) -> None:
    """Record the most recent REAL session id any hook observed."""
    global _last_seen_session
    if session_id:
        _last_seen_session = session_id


def resolve_session(session_id: str | None) -> str:
    """The given id when present; otherwise the last real id seen this
    process (core drops session_id on the pre_tool_call path — #141)."""
    return session_id or _last_seen_session


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
        _record_captions(reg, text)
    except Exception:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.debug("provenance: record_read failed for session %s", session_id, exc_info=True)


def _record_captions(reg: ProvenanceRegister, text: str) -> None:
    """Harvest case CAPTIONS from a read blob into the register (ss #1758).

    A caption the agent actually read this session is quotable — the tier-2
    citation gate exempts provenance-verified captions from its case-name
    pattern (fabricated-authority patterns are never exempted). The case-name
    regex greedily swallows adjacent prose into its parties, so for each raw
    match we register every left-suffix × right-prefix token combination
    (bounded at 5×5 by the regex itself): the true caption is always among
    them, and the gate's boundary-bounded containment does the rest. "In re"
    forms register whole. Best-effort by construction of the caller.
    """
    from shared.citation_filter import CASE_NAME_RE, canonical_caption

    for m in CASE_NAME_RE.finditer(text):
        canon = canonical_caption(m.group(0))
        if " v. " not in canon:
            reg.add_caption(canon)  # "in re ..." — register the whole form
            continue
        left, right = canon.split(" v. ", 1)
        ltoks, rtoks = left.split(), right.split()
        for i in range(len(ltoks)):
            for j in range(1, len(rtoks) + 1):
                reg.add_caption(" ".join(ltoks[i:]) + " v. " + " ".join(rtoks[:j]))


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
