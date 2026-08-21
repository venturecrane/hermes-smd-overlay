"""Per-session identifier provenance register (A1 — the runtime register).

The identifier-integrity gate (``shared.identifier_filter``) decides whether an
identifier in an outbound draft was actually READ from a source this session.
That requires a per-session record of what the agent read — this module is it,
the runtime analogue of ``shared.inbound.PENDING``.

Two collaborating points in ``hermes-smd-trust``:

* ``post_tool_call`` — for a TENANT-SOURCE read tool (:func:`seeds_provenance`),
  :func:`record_read` extracts the structured identifiers from the tool RESULT
  and adds them to that session's register.
* ``pre_tool_call`` — when the outbound gate evaluates a draft, it consults
  :func:`register_for` to ask "is each identifier in this body one we read?"

Not every READ-class tool is a source (ss-console#2511). See
:data:`TENANT_SOURCE_READ_TOOLS` below for the line and the incident that drew
it.

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

from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from shared.identifier_filter import ProvenanceRegister

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# What counts as a source (ss-console#2511)
#
# The register answers "did the Operator READ this identifier, or compose it?"
# That answer is only as good as what was allowed to seed it. Until 2026-08-21
# the seeding rule was the action class alone: any READ-class tool result went
# into the register, and ``read_file`` is READ-class.
#
# The cost, measured. During the A&P stand-up rehearsal on
# ``hermes-ashton-price``, the ``operator-self-test`` skill asked the Operator
# to prove this gate by drafting a memo citing the sentinel case number
# ZZ-9999-0001. The Operator read the skill text with ``read_file`` first, so
# the sentinel was in the register before the draft gate looked; the gate found
# nothing unverified and said nothing, because a verified identifier emits no
# row. The sentinel matter 404d, the Operator retried against a real matter,
# and the memo landed in the firm's production Smokeball. The gate was not
# broken. It was fed a register poisoned with the seat's own prose.
#
# So the rule is about WHERE the text came from, not what class the tool is:
#
#   A read establishes provenance only when it reaches the tenant's own system
#   of record — their mail, their calendar, their practice-management data,
#   their documents.
#
# Everything the seat holds about itself is excluded, because none of it is a
# record of anything the firm did: its files and skills (the incident), its
# memory store, its self-description, its run and connector metadata, its own
# unsent drafts, and any model-produced text about its own composition
# (``voice_score_draft`` hands the draft straight back). The open web is
# excluded for a different reason of the same shape: a page is a source, but not
# the firm's, and it is writable by anyone who would like a number believed.
#
# THE SET IS AN ALLOWLIST, and that direction is the safety property. A read
# tool nobody has classified here does not seed, so the gate over-reports — a
# refusal a human can clear by re-reading — rather than under-reports, which is
# a composed identifier delivered in silence. That is the same direction the
# empty-register and eviction paths already take. ``tests/test_provenance_
# sources.py`` pins every READ tool in the registry to one side or the other, so
# a new tool cannot join either side without someone deciding.
# ---------------------------------------------------------------------------

#: Read tools whose results are the TENANT's records. Only these seed.
TENANT_SOURCE_READ_TOOLS: frozenset[str] = frozenset(
    {
        # AgentMail — the mailbox. Drafts are excluded on purpose: a draft is
        # the Operator's own unsent sentence, and reading one back must not
        # certify the numbers it contains. ``auth_me`` is seat identity.
        "mcp_agentmail_list_inboxes",
        "mcp_agentmail_get_inbox",
        "mcp_agentmail_list_threads",
        "mcp_agentmail_search_threads",
        "mcp_agentmail_get_thread",
        "mcp_agentmail_get_attachment",
        "mcp_agentmail_list_messages",
        "mcp_agentmail_search_messages",
        # Microsoft Graph mail — the firm's own M365 mailbox.
        "mcp_msgraph_mail_list_messages",
        "mcp_msgraph_mail_read_message",
        "mcp_msgraph_mail_poll_delta",
        # Generic connector surface (mail, SMS, calendar, practice management).
        "email_list_messages",
        "email_get_message",
        "email_search",
        "email_get_thread",
        "email_list_labels",
        "sms_list_messages",
        "sms_get_message",
        "calendar_list_events",
        "calendar_get_event",
        "calendar_search_events",
        "calendar_check_availability",
        "practice_management_search_matters",
        "practice_management_get_matter",
        "practice_management_list_documents",
        "practice_management_get_document",
        "practice_management_list_tasks",
        # Smokeball — the law wedge's system of record. ``auth_status`` is
        # excluded (credential metadata, no tenant content). ``get_memos_on_
        # matter`` IS included: a committed memo is part of the matter the firm
        # can see, unlike a draft.
        "mcp_smokeball_list_matters",
        "mcp_smokeball_get_matter",
        "mcp_smokeball_list_matter_types",
        "mcp_smokeball_get_stage_sets",
        "mcp_smokeball_get_stage_to_matter_mappings",
        "mcp_smokeball_get_contacts",
        "mcp_smokeball_get_contact",
        "mcp_smokeball_get_contact_relations",
        "mcp_smokeball_list_tasks",
        "mcp_smokeball_get_task",
        "mcp_smokeball_search_staff",
        "mcp_smokeball_get_staff",
        "mcp_smokeball_get_roles_on_matter",
        "mcp_smokeball_get_relationships_on_matter",
        "mcp_smokeball_get_files_on_matter",
        "mcp_smokeball_get_file",
        "mcp_smokeball_get_download_url",
        "mcp_smokeball_read_document",
        "mcp_smokeball_get_memos_on_matter",
        "mcp_smokeball_get_bank_accounts",
        "mcp_smokeball_get_matter_balances",
        "mcp_smokeball_get_matter_billing_config",
        "mcp_smokeball_get_fees",
        "mcp_smokeball_get_expenses",
        "mcp_smokeball_get_webhook_subscriptions",
        "mcp_smokeball_get_event_types",
        "mcp_smokeball_list_events",
        "mcp_smokeball_list_folders",
        # Clio.
        "mcp_clio_oktopeak_list_matters",
        "mcp_clio_oktopeak_get_matter",
        "mcp_clio_oktopeak_search_contacts",
        "mcp_clio_oktopeak_get_contact",
        "mcp_clio_oktopeak_list_documents",
        "mcp_clio_oktopeak_get_document",
        "mcp_clio_oktopeak_list_tasks",
        "mcp_clio_oktopeak_list_calendars",
        "mcp_clio_oktopeak_list_calendar_entries",
        "mcp_clio_oktopeak_list_time_entries",
        "mcp_clio_oktopeak_get_billing_summary",
        "mcp_clio_oktopeak_list_users",
        "mcp_clio_oktopeak_get_user",
        "mcp_clio_oktopeak_export_audit_log",
        # Mediated Google Workspace — the principal's real mail, calendar and
        # files, reached through the broker.
        "workspace_gmail_search",
        "workspace_gmail_get",
        "workspace_calendar_list",
        "workspace_calendar_get",
        "workspace_drive_list",
        "workspace_drive_get",
        "workspace_drive_export",
        "workspace_docs_get",
        "workspace_sheets_get_values",
    }
)


def seeds_provenance(tool_name: str) -> bool:
    """True iff this tool's result may establish identifier provenance.

    Total and fail-safe on every input a hook can hand it: an empty name, a
    name nobody has registered, a write tool, or a read of the seat's own
    artifacts all return False, which means the register stays unseeded and the
    gate over-reports. Name matching uses the same trim + lowercase
    normalization as ``action_classes.classify_tool`` so a runtime that
    surfaces a differently-cased name cannot slip past this or land in it.

    The action class is re-checked here rather than trusted from the caller, so
    the predicate cannot certify a tool that was reclassified out of READ while
    its name stayed in the set above.
    """
    if not tool_name:
        return False
    normalized = tool_name.strip().lower()
    if normalized not in TENANT_SOURCE_READ_TOOLS:
        return False
    return TOOL_ACTION_CLASS_MAP.get(normalized) is ActionClass.READ


# Bound the number of live session registers. A Machine handles one agent at a
# time; a handful of concurrent sessions is the realistic ceiling. 256 is far
# above that and caps memory if session_ids churn (each register is a small set
# of canonical strings).
_MAX_SESSIONS = 256

_registers: OrderedDict[str, ProvenanceRegister] = OrderedDict()

# ---------------------------------------------------------------------------
# The NEGATIVE register (ss-console#2511)
#
# Excluding the seat's own reads from the positive register is subtraction, and
# subtraction alone loosens the gate. ``_check_identifiers`` carves out an EMPTY
# register on the draft path, on the reasoning that a refusal with no source to
# re-read is a brick. Today ``read_file`` makes a register almost never empty;
# with the allowlist above, a turn whose only reads were local has an empty one,
# and every composed identifier on it would be ALLOWED with a report row. The
# sentinel would go through again by a different door, and whether the kill test
# passes would depend on whether ``list_matters`` happened to run first.
#
# So the seat's reads are not discarded. They are recorded HERE, and this
# register means the opposite of the other one: an identifier in it was found in
# text the seat produced or holds — a skill body, a spec, a config file, its own
# scored draft. When a draft cites a value that appears ONLY here, that is not
# "nothing was read", it is "you got this from your own instructions", and the
# empty-register carve has nothing to say about it. Those are different states
# and the gate now distinguishes them: the audit row carries ``source=seat_text``
# and the refusal says where the value came from.
#
# Same shape and the same LRU bound as the positive register, keyed the same
# way, so a session's two registers evict together in practice and a missing one
# degrades to empty (no seat-sourced hits, i.e. today's behavior).
#
# It is a ``ProvenanceRegister`` rather than a bare set on purpose: membership is
# decided by ``register.verifies(hit)``, so the seeder and the checker
# canonicalize through exactly the same code as the positive path. A parallel
# set with its own normalization would drift, and a key the checker never looks
# up reads as "not seat-sourced" — the silent direction.
# ---------------------------------------------------------------------------

_seat_registers: OrderedDict[str, ProvenanceRegister] = OrderedDict()

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
# next door. Three rungs resolve and two decline, and the resolution SAYS which
# one answered:
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
    forms register whole. A caption the record spells in longhand ("Espinoza
    versus Kaviani") is harvested through CASE_NAME_VERSUS_RE and canonicalizes
    to the "v." form, so the read registers the same caption either way.
    Best-effort by construction of the caller.
    """
    from shared.citation_filter import (
        CASE_NAME_RE,
        CASE_NAME_VERSUS_RE,
        canonical_caption,
    )

    matches = list(CASE_NAME_RE.finditer(text)) + list(CASE_NAME_VERSUS_RE.finditer(text))
    for m in matches:
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


def record_seat_text(session_id: str, text: str) -> None:
    """Add the identifiers in a NON-seeding read result to the seat register.

    The mirror of :func:`record_read`, and deliberately the same extractor: what
    makes a value "seat-sourced" has to be decided by the same canonicalization
    that decides whether a draft's value is verified, or the two sets are about
    different strings.

    Best-effort on the same terms as :func:`record_read` — a bad or oversized
    blob is logged and skipped. A failure here loses a BLOCK the gate would
    otherwise have made, so it degrades toward today's behavior rather than
    toward a refusal nobody can explain.
    """
    if not session_id or not isinstance(text, str) or not text:
        return
    try:
        reg = _seat_registers.get(session_id)
        if reg is None:
            reg = ProvenanceRegister()
            _seat_registers[session_id] = reg
            _evict_if_needed()
        else:
            _seat_registers.move_to_end(session_id)  # LRU touch
        # Atoms only. Associations are seeded per connector RECORD and captions
        # feed the citation allowlist; neither means anything for text the seat
        # wrote about itself, and seeding them here would widen two gates this
        # change is not about.
        reg.add_read_text(text)
    except Exception:  # noqa: BLE001 — recording is best-effort, never fatal
        logger.debug(
            "provenance: record_seat_text failed for session %s", session_id, exc_info=True
        )


def seat_sourced_for(session_id: str) -> ProvenanceRegister:
    """The identifiers this session read from the SEAT's own text.

    An unknown session yields an empty register, which verifies nothing, which
    means no hit is seat-sourced — the pre-2511 behavior. Absence of the
    negative register can only lose a block, never invent one.
    """
    reg = _seat_registers.get(session_id)
    if reg is None:
        return ProvenanceRegister()
    _seat_registers.move_to_end(session_id)
    return reg


def drop(session_id: str) -> None:
    """Forget a session's registers (e.g. at session end). Idempotent."""
    _registers.pop(session_id, None)
    _seat_registers.pop(session_id, None)


def _evict_if_needed() -> None:
    for store, label in ((_registers, "read"), (_seat_registers, "seat")):
        while len(store) > _MAX_SESSIONS:
            evicted, _ = store.popitem(last=False)  # oldest
            logger.debug(
                "provenance: evicted oldest %s register %s (cap %d)",
                label,
                evicted,
                _MAX_SESSIONS,
            )


def _reset_for_tests() -> None:
    """Clear all registers and THIS thread's resolver state — test hook only.

    Another thread's noted session is unreachable from here, which is the
    property that makes the slot safe; a fixture must reset on the thread it
    noted on (same contract as ``TrustDecisionRegister.clear``).
    """
    global _process_session, _process_owner, _process_ambiguous
    _registers.clear()
    _seat_registers.clear()
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
    "TENANT_SOURCE_READ_TOOLS",
    "drop",
    "last_resolution",
    "note_session",
    "record_read",
    "record_seat_text",
    "register_for",
    "resolve_session",
    "resolve_session_with_mode",
    "seat_sourced_for",
    "seeds_provenance",
]
