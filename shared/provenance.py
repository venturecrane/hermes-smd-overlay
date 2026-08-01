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

It DOES hold associations (2026-08-01). Atom provenance answers "was this value
read?", which cannot catch a *mispairing*: on 2026-08-01 the Operator wrote
"matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026" when the
event carried ``matterNumber=2026-PI-101``. Both values had been read that
session, so every atom verified and the line passed clean. :func:`_record_associations`
seeds (matter, date) pairs one record at a time — per record, never per blob,
because pairing everything in a listing registers the cross-product and verifies
exactly the defect this catches.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any

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
        _record_associations(reg, text)
    except Exception:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.debug("provenance: record_read failed for session %s", session_id, exc_info=True)


# Bound the per-blob walk. A listing returns at most a few hundred rows and the
# gate must not become the expensive part of a read.
_MAX_SEEDED_RECORDS = 200


def _iter_records(payload: Any, depth: int = 0) -> Iterator[dict]:
    """Yield the record-shaped dicts in a decoded tool result.

    Handles the three shapes the connectors actually return: a HATEOAS envelope
    (``{"value": [...]}``), a bare list, and a single record. Depth-bounded so a
    pathological payload cannot walk forever.
    """
    if depth > 3:
        return
    if isinstance(payload, dict):
        inner = payload.get("value")
        if isinstance(inner, list):
            for item in inner:
                yield from _iter_records(item, depth + 1)
            return
        yield payload
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_records(item, depth + 1)


def _record_associations(reg: ProvenanceRegister, text: str) -> None:
    """Seed (matter, date) associations from a read blob, ONE RECORD AT A TIME.

    This is what turns pair-keyed provenance on: until something seeds an
    association the gate reports no pairs at all, by design (an unseeded register
    cannot judge a pairing, and flagging every matter+date line would be worse
    than silence).

    **Per record is the whole point.** ``add_read_text`` deliberately registers
    no pairs because a tool result is a *collection*: pairing every matter in the
    blob with every date in the blob registers the cross-product and verifies
    precisely the mispairings this exists to catch. Here the matter binding was
    resolved in connector code (``matterNumber``, attached by the smokeball
    connector's ``_attach_matter_ref``), so "this date belongs to this matter" is
    a fact about one object rather than an inference across a page.

    Every string field of the record is offered as a date candidate rather than a
    hardcoded field list. Two reasons: the vendor's date fields differ per entity
    (``dueDate`` on a task, ``startTime``/``endTime`` on an event) and a guessed
    field name that does not exist is the authored-not-captured failure this
    codebase has already been bitten by. ``add_record`` extracts what is actually
    a date and ignores the rest.

    Over-registering a true association is the safe direction here: it makes the
    gate more permissive, never wrong. Under-registering would flag correct lines,
    and a gate that flags correct lines is worse than no gate.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return  # not JSON — atoms and captions already handled the text form
    seeded = 0
    for record in _iter_records(payload):
        if seeded >= _MAX_SEEDED_RECORDS:
            return
        number = record.get("matterNumber")
        if not isinstance(number, str) or not number:
            continue
        candidates = [v for v in record.values() if isinstance(v, str)]
        reg.add_record(number, candidates)
        seeded += 1


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
