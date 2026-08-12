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
import threading
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
# Session resolver (overlay #141, rescoped ss-console #2288)
#
# Hermes core passes session_id to post_tool_call and pre_llm_call but NOT to
# pre_tool_call — all three fire sites in run_agent.py (10930, 11093, 11471 at
# the pinned ref) pass task_id only. Forensic proof: every tier3
# IDENTIFIER_UNVERIFIED row ever emitted lacked a session_id key and carried
# register_was_empty=true, while reads were recorded under the REAL id nobody
# consulted. So the plugin keeps the REAL id hooks do observe and consulting
# hooks resolve a missing id to it. A resolver miss degrades to the OLD
# behavior (empty register: over-report / no exemption), never a widened one.
#
# WHY THE FALLBACK IS PER-THREAD AND NOT PROCESS-GLOBAL
# -----------------------------------------------------
#
# The original version of this resolver kept ONE module-global last-seen id,
# on the premise that "One Machine = one agent process = sequential sessions".
# ``shared/trust_decision.py:55-72`` refutes that premise for the module next
# door, and it is the same premise: core keeps a long-lived event loop per
# worker thread (``/opt/hermes/model_tools.py:66-80``, ``_get_worker_loop`` —
# "Each worker thread (e.g., delegate_task's ThreadPoolExecutor threads) gets
# its own long-lived loop stored in thread-local storage"), and ADR 0021 has us
# using ``delegate_task`` as a native primitive. Concurrent agent threads in one
# process are a live configuration, not a hypothetical.
#
# What the resolved value KEYS makes that fatal rather than untidy. It is the
# primary key of every per-session safety register: the provenance register
# above, the matter gate's party sets (``matter_gate.evaluate`` ->
# ``matter_binding.membership_for``), the authored-spec read marks
# (``spec_status``), the voice live-gate mark (``voice_status``), and the
# read-capture windows establishment stages from. A shared last-value slot hands
# thread A's key to thread B, so A's reads certify B's citations and A's party
# set gates B's send. ``hermes-smd-establishment`` already names this failure in
# its own words — "one session's read would then satisfy another session's
# stage" — and then depended on a resolver that allowed it.
#
# So the fallback is thread-local, mirroring core's own idiom and the module
# next door. Three tiers, and the resolution SAYS which one answered:
#
#   keyed             the caller had a real id; nothing was inferred
#   thread            this thread's own noted id
#   process_singleton this thread never noted, and no two threads have ever
#                     held DIFFERENT sessions in this process — so there is
#                     exactly one session it could mean. This is the
#                     configuration the original comment described, and it
#                     keeps resolving exactly as it does today.
#   ambiguous         this thread never noted, and threads HAVE held different
#                     sessions. There is no basis to pick one, so it picks
#                     none. Degrades to the empty register (over-report / no
#                     exemption / no spec mark), never to a peer's state.
#   none              nothing has been noted anywhere yet
#
# The ``process_singleton`` tier is what makes this behavior-preserving: a
# thread whose FIRST hook is a consult has never noted (core drops the id
# there), and on a single-agent Machine it must still find the one live session.
# The tier switches itself off the moment two threads disagree — which is
# exactly when guessing would cross-attribute — and never switches back, because
# a process that has run a fan-out can run another.
#
# AND THE RESOLUTION IS DECLARED. ``trust_decision`` stamps
# ``trust_decision_match`` on every audit row so an auditor never has to guess
# how the join was made; session resolution had no equivalent, so a
# cross-attribution left no trace at all. That is why this was invisible rather
# than merely unfixed. :func:`resolve_session_with_mode` returns the mode, the
# trust gate carries it onto the per-tool audit row as ``session_resolution``,
# and :func:`last_resolution` exposes this thread's most recent one for logging.
# ---------------------------------------------------------------------------

#: How a session id was resolved. Stamped onto the per-tool audit row so a
#: keyed resolution is distinguishable from an inferred one after the fact.
MODE_KEYED = "keyed"
MODE_THREAD = "thread"
MODE_PROCESS = "process_singleton"
MODE_AMBIGUOUS = "ambiguous"
MODE_NONE = "none"

_local = threading.local()
_scope_lock = threading.Lock()

#: The single session every noting thread has agreed on, and the thread that
#: last noted it. Guarded by ``_scope_lock``.
_process_session: str = ""
_process_owner: int = 0
#: Sticky: set once two threads have held DIFFERENT sessions. From then on a
#: thread that never noted resolves to nothing rather than to a peer's session.
_process_ambiguous: bool = False


def note_session(session_id: str | None) -> None:
    """Record the REAL session id this hook observed, for THIS thread.

    Also maintains the process-wide singleton used by threads that have never
    noted one (see the module comment). A thread re-noting is succession, not
    concurrency: only a DIFFERENT thread holding a DIFFERENT session marks the
    process ambiguous.
    """
    global _process_session, _process_owner, _process_ambiguous
    if not session_id:
        return
    _local.session = session_id
    ident = threading.get_ident()
    with _scope_lock:
        if _process_session and _process_session != session_id and _process_owner != ident:
            _process_ambiguous = True
        _process_session = session_id
        _process_owner = ident


def resolve_session_with_mode(session_id: str | None) -> tuple[str, str]:
    """``(resolved_id, mode)`` — the id to key registers under, and how it was
    reached. ``mode`` is one of the ``MODE_*`` constants above.

    The result is also stashed for :func:`last_resolution` on this thread.
    """
    if session_id:
        return _record(str(session_id), MODE_KEYED)
    mine = getattr(_local, "session", "")
    if mine:
        return _record(mine, MODE_THREAD)
    with _scope_lock:
        shared_session, ambiguous = _process_session, _process_ambiguous
    if ambiguous:
        # Two sessions have been live on two threads. Whichever spoke last is
        # not evidence about this one.
        return _record("", MODE_AMBIGUOUS)
    if shared_session:
        return _record(shared_session, MODE_PROCESS)
    return _record("", MODE_NONE)


def resolve_session(session_id: str | None) -> str:
    """The given id when present; otherwise the id this thread is working under
    (core drops session_id on the pre_tool_call path — #141).

    Kept as the one-value entry point every consulting hook already calls. Use
    :func:`resolve_session_with_mode` where the resolution itself must be
    recorded.
    """
    return resolve_session_with_mode(session_id)[0]


def _record(resolved: str, mode: str) -> tuple[str, str]:
    _local.last_resolution = (resolved, mode)
    return resolved, mode


def last_resolution() -> tuple[str, str]:
    """This thread's most recent ``(resolved_id, mode)``.

    Per-thread and read by the thread that resolved, so it carries none of the
    cross-attribution risk the resolver itself had. ``("", MODE_NONE)`` before
    anything has resolved on this thread.
    """
    return getattr(_local, "last_resolution", ("", MODE_NONE))


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
    """Clear all registers and THIS thread's resolver state — test hook only.

    Another thread's noted session is unreachable from here, which is the
    property that makes the slot safe; a fixture must reset on the thread it
    noted on (same contract as ``TrustDecisionRegister.clear``).
    """
    global _process_session, _process_owner, _process_ambiguous
    _registers.clear()
    _local.session = ""
    _local.last_resolution = ("", MODE_NONE)
    with _scope_lock:
        _process_session = ""
        _process_owner = 0
        _process_ambiguous = False


__all__ = [
    "MODE_AMBIGUOUS",
    "MODE_KEYED",
    "MODE_NONE",
    "MODE_PROCESS",
    "MODE_THREAD",
    "drop",
    "last_resolution",
    "note_session",
    "record_read",
    "register_for",
    "resolve_session",
    "resolve_session_with_mode",
]
