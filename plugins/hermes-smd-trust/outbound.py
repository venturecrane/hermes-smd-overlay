"""Outbound provenance gate wiring — ADR 0028.

Bridges the pure policy core (``shared.outbound_gate.evaluate``) into the trust
plugin's ``pre_tool_call`` hook. Responsibilities that DON'T belong in the
policy core live here:

* **Draft-tool detection.** Which tool names are draft-creating body-bearing
  writes (``email_create_draft``, ``practice_management_create_note``,
  ``email_update_draft``, ...). Derived from ``shared.action_classes`` so the
  gated set tracks the registry, not a hand-maintained copy.
* **Body extraction.** Resolve the draft body from the tool ``args`` using the
  real arg keys. If a draft-creating tool carries NO recognizable body key →
  BLOCK (fail-closed); never skip.
* **Vertical resolution.** Read the customer vertical from
  ``shared.customer_config`` (falling back to ``SMD_VERTICAL`` env). An
  indeterminate vertical makes the policy core run the most-restrictive tiers.
* **Audit emission.** On block, write a ``FABRICATION_FILTER_TRIGGERED`` row
  directly via the shared ``D1Client`` against the per-customer audit binding —
  the same pattern ``hermes-smd-webhook-router`` uses for ``WEBHOOK_ROUTED``.
  This depends on the shared D1 primitive + the canonical audit_log schema, NOT
  the audit *plugin*'s hook surface — the trust/audit loose-coupling (AGENTS.md)
  is preserved and the hyphenated-package dynamic-import dance is avoided.

Fail-closed everywhere. A draft-creating tool whose body cannot be resolved, a
gate that cannot evaluate, an audit writer that raises — none of these let the
draft through. The audit write is best-effort relative to the BLOCK: if the
write fails we still block (and log), because the safety decision is the block,
not the row.
"""

import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from shared import (
    identifier_filter,
    output_checklist,
    provenance,
    report_render,
    send_dispatch,
    spec_gate,
)
from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from shared.audit_contract import CANONICAL_TOOL_CALL_KEY, agent_event_params
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_status import NoAuditWarner
from shared.outbound_gate import GateDecision, evaluate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Draft-tool detection
#
# A tool is gated by the outbound provenance gate iff it is INTERNAL_WRITE AND
# it can carry an authored prose body the agent could fabricate into. The
# substrate registry (shared.action_classes) is the source of truth for the
# action class; the gate covers the prose-bearing INTERNAL_WRITE subset.
#
# Within the gated set, two fail-modes differ:
#
#   * BODY-REQUIRED tools (``email_create_draft``, ``email_update_draft``,
#     ``sms_create_draft``, ``practice_management_create_note``) ALWAYS author a
#     prose body. If a recognizable body key is ABSENT, that is a malformed /
#     unexpected-shape call and we BLOCK (fail-closed) — we must not let a draft
#     past the scan because we couldn't find its body.
#
#   * BODY-OPTIONAL tools (the calendar drafts,
#     ``practice_management_create_task_draft``,
#     ``practice_management_update_matter_field``) may carry a prose body OR
#     purely structured fields (a date, a time, an enum). When a body IS
#     present it is scanned; when none is present there is no fabrication
#     surface and the call is ALLOWED. This avoids bricking legitimate
#     structured-only operations while still scanning any free-text the agent
#     authored.
#
# Two tools are excluded entirely: ``email_delete_draft`` (a delete authors
# nothing) and ``establish_stage_document`` (its payload is the FIRM's own
# document, read in place and copied byte for byte — see the note on the set
# below).
#
# The sets are data-driven from the registry (so a new INTERNAL_WRITE tool is
# at least body-optional-gated by default, never silently un-gated); the
# body-required and excluded lists are the only hand-maintained surfaces and a
# test pins them.
# ---------------------------------------------------------------------------


# INTERNAL_WRITE tools that author NO content — excluded from the gate entirely.
#
# ``establish_stage_document`` (ss #2247, 2026-08-11). This gate scans text the
# AGENT COMPOSED for fabricated markers, citations, and unverified identifiers.
# A staged establishment document is the opposite of that: it is the firm's own
# work product, read in place through the connector this session and copied byte
# for byte, and the establishment skill's safety invariant 2 makes staging it
# UNEDITED a hard requirement. Scanning it for fabrication is a category error —
# it asks whether the firm fabricated its own letter.
#
# The gate also could not have protected anything here. The model already holds
# the text: it came back from ``read_document`` on an earlier call in the same
# turn, so refusing the staging call closes a door the content already walked
# through. What the refusal DID accomplish was worse than nothing. On the first
# live run (pilot-smokeball, 2026-08-11) it refused two of the three documents
# the admin had blessed — a policy-limits demand letter for its dollar figures
# and a trial binder index for its dates, which for a PI firm are precisely the
# flagship voice exemplars — and the agent responded by deleting the wage rates
# and billing totals from the letter so it would stage. A gate that cannot be
# satisfied honestly teaches the model to satisfy it dishonestly, and an edit is
# invisible in the record where a refusal would have been visible.
#
# The real controls on this path are downstream, server-side, and purpose-built:
# ``establish_submit``'s spec_body stays gated here (it IS agent-composed), the
# intake's leak_check refuses copied client prose in the installed spec, and the
# digit invariant refuses any figure the profile did not compute. Both passed on
# the run described above. The staged corpus itself never leaves the seat and is
# purged on pass and on fail.
_NON_AUTHORING_INTERNAL_WRITE: frozenset[str] = frozenset(
    {
        "email_delete_draft",  # delete — nothing authored to scan
        "establish_stage_document",  # the firm's own document, verbatim (ss #2247)
    }
)

# The record-store write (plugins/hermes-smd-record-store, ss-console#2793,
# 2026-09-29). Excluded from this gate, and the reasoning is the opposite of
# "nothing authored": the record IS composed, and that is the point.
#
# A record store is the seat's own system of record for a vertical that has no
# other (the open-house visitor store). A record carries a rostered person's
# dictation verbatim plus the fields the product derives from it, and the one
# thing it must derive is a follow-up schedule: day 2, 7 and 30 after the visit
# date. Those dates are COMPUTED by design. The identifier gate exists to refuse
# a computed legal deadline in an outbound letter; here it refused the schedule
# at the moment of writing it down, seven times on the first live capture, and
# its refusal text ("re-read the source record") sent the model to re-fetch the
# inbound email through a fenced read, which tainted the turn and closed the
# write for good. A gate that cannot be satisfied honestly on the store's own
# purpose teaches the model to satisfy it dishonestly, the ss #2247 lesson.
#
# What still stands, and why this is narrow: the store is never outbound. Every
# reply, recall answer and follow-up draft composed FROM a record goes through
# the draft tools, which stay gated, and record_store_read seeds provenance, so
# a date the seat wrote and read back is traceable while a date it never wrote
# is not. The write itself is refused on a tainted session by its own hook, so
# nothing an outsider sent can be written here to launder it. The fabrication
# marker scan is also skipped: a dictation is the agent's words, and an em dash
# their phone typed is not shipped copy.
_RECORD_STORE_WRITES: frozenset[str] = frozenset({"record_store_write"})

# Gated tools that MUST carry a prose body. A missing body on these is a
# fail-closed BLOCK (a create-draft / create-note with no body is malformed).
_BODY_REQUIRED_DRAFT_TOOLS: frozenset[str] = frozenset(
    {
        "email_create_draft",
        "email_update_draft",
        "sms_create_draft",
        "practice_management_create_note",
        # The two live mail-draft names, for the same reason as the generic ones
        # above: a create_draft with no recognizable body is a malformed call,
        # not an empty one, and letting it through unscanned is how a draft
        # reaches a mailbox without ever meeting the gate. Both connectors make
        # the body schema-required, so a missing one is a shape surprise, which
        # is exactly the case that must fail closed (ss-console#2511).
        "mcp_msgraph_mail_create_draft",
        "mcp_agentmail_create_draft",
    }
)


# ---------------------------------------------------------------------------
# Gated tools that REPORT rather than refuse (ss-console#2511)
#
# ``mcp_smokeball_add_file`` (content_text) and
# ``mcp_smokeball_render_docx_draft`` (draft_markdown) carry the firm's real
# documents: a demand letter, discovery responses, a settlement statement.
# Neither key was in the scan lists, so both tools reached
# ``check_outbound_draft``, matched nothing, and exited unscanned. The
# identifier surface with the largest blast radius on the seat was the one
# surface the gate never saw. Those keys are in ``_DRAFT_SCAN_KEYS`` now.
#
# Both tools REPORTED at first, and the reason was the ss#2247 note up this
# file rather than timidity. A demand letter is dense with figures and dates the
# firm authored elsewhere; flipping a gate straight to BLOCK on that content,
# with no measurement of how often it fires on correct work, is how the
# establishment gate ended up teaching the model to delete wage rates from a
# letter so it would stage. So the rate got measured before anything flipped.
#
# ``mcp_smokeball_render_docx_draft`` BLOCKS as of this change, because that
# number is now in hand. Four pilot drafting lanes on 2026-08-21
# (ss-console#2511, ``vfy_01M0JG54ATP5ZA1TDTQJ6CEVWA``) put ten render calls
# through the gate and produced zero false positives and one genuine catch. The
# catch argues the flip on its own: computed response deadlines reached a filed
# Word draft, while the same values were refused on the memo and on the email in
# the same turn. A document the firm files is the last surface that should be
# the permissive one. So this tool falls through to ``_identifier_gate_mode()``
# below, like every other draft tool.
#
# ``mcp_smokeball_add_file`` stays report-only. No lane has exercised it yet, so
# its false-positive rate is unmeasured and the ss#2247 reasoning still applies
# to it unchanged. Flipping it is the same decision again, with its own number,
# not a follow-on to this one.
#
# PER TOOL, never the env lever. ``SMD_IDENTIFIER_GATE_MODE=report`` downgrades
# every gate on the seat and is the incident rollback; this downgrades exactly
# one tool and is the authored posture. The audit row says which one applied
# (``mode=report_tool`` vs ``mode=report``) so a ledger reader is never left
# guessing whether a seat was in rollback.
# ---------------------------------------------------------------------------

_REPORT_ONLY_DRAFT_TOOLS: frozenset[str] = frozenset(
    {
        "mcp_smokeball_add_file",
    }
)

#: Audit/mode value for the per-tool carve above. Deliberately distinct from the
#: operator-set ``report``, which means the whole seat is in rollback.
MODE_REPORT_TOOL = "report_tool"


def _is_gated_draft_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` is a prose-bearing INTERNAL_WRITE draft tool."""
    if not tool_name:
        return False
    action = TOOL_ACTION_CLASS_MAP.get(tool_name)
    if action is not ActionClass.INTERNAL_WRITE:
        return False
    if tool_name in _RECORD_STORE_WRITES:
        return False
    return tool_name not in _NON_AUTHORING_INTERNAL_WRITE


def _body_is_required(tool_name: str) -> bool:
    """True iff a missing body on this gated tool must fail closed (block)."""
    return tool_name in _BODY_REQUIRED_DRAFT_TOOLS


# The frozen set of gated tool names, computed once from the registry. Exposed
# for the test that asserts the expected draft tools are covered.
GATED_DRAFT_TOOLS: frozenset[str] = frozenset(
    name for name in TOOL_ACTION_CLASS_MAP if _is_gated_draft_tool(name)
)


# ---------------------------------------------------------------------------
# Body extraction
#
# Draft-creating tools carry the authored prose under one of a few arg keys.
# We try them in priority order. A draft tool that carries NONE of them is a
# fail-closed BLOCK: we must not let an unrecognized-shape draft slip past the
# scan because we couldn't find its body.
# ---------------------------------------------------------------------------


_BODY_ARG_KEYS: tuple[str, ...] = (
    "body",
    "body_plain",
    "body_text",
    "content",
    "html_body",
    "html",  # AgentMail draft/send bodies use the bare "html" key
    "text",
    "note",
    "message",
)


def _extract_body(args: dict | None) -> str | None:
    """Resolve the draft body from tool args. ``None`` if no body key present.

    Returns the first non-empty string value found among ``_BODY_ARG_KEYS``.
    A key present but holding a non-string (e.g. a structured block) is
    coerced via ``str()`` so the scan still sees the content; an entirely
    absent body returns ``None`` so the caller fails closed.
    """
    if not isinstance(args, dict):
        return None
    for key in _BODY_ARG_KEYS:
        if key not in args:
            continue
        value = args[key]
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
            # Present-but-empty body on a draft tool: treat as no usable body
            # so the caller fails closed rather than scanning "" and passing.
            return ""
        # Non-string body content — coerce so the markers/citation scan runs.
        coerced = str(value)
        if coerced.strip():
            return coerced
    return None


# ---------------------------------------------------------------------------
# Identifier scan text (#2132 / ss#2171)
#
# The identifier provenance check must see MORE than the prose body: Smokeball
# structured writes carry their fabrication surface in structured args — a
# hearing date in create_event's start_time, a deadline in create_task's
# due_date, a matter number in a subject line. Before this existed,
# mcp_smokeball_create_event matched zero body keys and exited the gate
# unscanned (ss#2132), which is exactly where a fabricated hearing date would
# land on the firm's calendar.
#
# Mirrors _extract_send_body's concatenate-everything approach (first-match
# _BODY_ARG_KEYS semantics truncated create_task scanning at `note`). This
# text feeds ONLY the identifier check — the Tier-1/2 marker/citation gate
# (`evaluate`) keeps scanning the prose body alone, deliberately: widening an
# already-blocking gate to subject lines is a separate, measured decision.
# ---------------------------------------------------------------------------

_DRAFT_SCAN_KEYS: tuple[str, ...] = (
    # prose bodies (superset of _BODY_ARG_KEYS)
    "body",
    "body_plain",
    "body_text",
    "content",
    "html_body",
    "html",
    "text",
    "note",
    "message",
    # The document bodies (ss-console#2511). ``content_text`` is add_file's
    # plain-text payload and ``draft_markdown`` is render_docx_draft's source;
    # each is a whole document, and neither was scanned by anything.
    "content_text",
    "draft_markdown",
    # add_workbook's structured payload: a list of sheets whose cells carry the
    # figures, dates and case numbers. Non-string, so it is FLATTENED to its
    # scalar leaves below rather than repr'd.
    "sheets",
    # structured, identifier-bearing args (#2132)
    "subject",
    "title",
    "name",
    "description",
    "location",
    "due_date",
    "start_time",
    "end_time",
)


def _flatten_scalars(value: object, out: list[str]) -> None:
    """Append every scalar leaf of a nested list/dict to ``out`` as text.

    A workbook's ``sheets`` arrives as nested lists and dicts; ``str()`` of that
    is a Python repr whose quoting and brackets sit between an identifier and
    its neighbours. Walking the leaves hands the scanner each cell on its own
    line, as it would see a prose body. ``None`` is skipped (an empty cell).
    """
    if value is None:
        return
    if isinstance(value, dict):
        for item in value.values():
            _flatten_scalars(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _flatten_scalars(item, out)
    else:
        out.append(str(value))


def _extract_draft_scan_text(args: dict | None) -> str:
    """Concatenate every present ``_DRAFT_SCAN_KEYS`` value for the identifier
    scan. Returns ``""`` when nothing scannable is present (a structured-only
    call carrying no identifier-bearing args has no fabrication surface).
    A non-string value (``sheets``) is flattened to its scalar leaves."""
    if not isinstance(args, dict):
        return ""
    parts: list[str] = []
    for key in _DRAFT_SCAN_KEYS:
        value = args.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value
        else:
            leaves: list[str] = []
            _flatten_scalars(value, leaves)
            text = "\n".join(leaves)
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Vertical resolution
#
# Source order:
#   1. shared.customer_config.CustomerConfig.from_volume().vertical
#   2. SMD_VERTICAL env override (dev / test)
#   3. None → the policy core runs the most-restrictive (law) tier.
# Any failure resolving the vertical yields None, which is the most-restrictive
# input to the gate — consistent with fail-closed.
# ---------------------------------------------------------------------------


def _resolve_vertical() -> str | None:
    """Best-effort customer vertical. ``None`` → most-restrictive evaluation."""
    try:
        from shared.customer_config import CustomerConfig

        cfg = CustomerConfig.from_volume()
        vertical = cfg.vertical
        if isinstance(vertical, str) and vertical.strip():
            return vertical.strip()
    except Exception:
        logger.debug(
            "outbound gate: customer_config vertical unavailable; falling back to env",
            exc_info=True,
        )
    env_value = os.environ.get("SMD_VERTICAL")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()
    return None


def _resolve_cohort() -> str | None:
    """Best-effort customer cohort (reserved; not load-bearing in v1)."""
    env_value = os.environ.get("SMD_COHORT")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()
    return None


# ---------------------------------------------------------------------------
# Audit emission — FABRICATION_FILTER_TRIGGERED
#
# Writes one audit_log row directly via the shared ``D1Client``, the same
# pattern hermes-smd-webhook-router uses for WEBHOOK_ROUTED. This sidesteps a
# dynamic-import of the sibling audit plugin's AuditLogWriter (the plugin dir
# name is hyphenated) AND preserves the trust/audit loose coupling (AGENTS.md):
# we depend on the shared D1 primitive + the canonical audit_log schema, not
# the audit plugin's hook surface. The INSERT SQL and column order MUST agree
# with ``hermes-smd-audit/emit.py`` ``_INSERT_SQL`` (and the webhook-router
# copy); the canonical schema lives in ss-console.
#
# The audit write is best-effort RELATIVE TO THE BLOCK: a write failure logs a
# warning, but the BLOCK still stands — the safety decision is the block, not
# the row.
# ---------------------------------------------------------------------------


# ULID, ISO-Z timestamps, and the audit_log INSERT contract are single-sourced
# in shared.ids / shared.audit_contract (imported above). Row params are built
# via agent_event_params so this writer's column order can never drift from
# hermes-smd-audit/emit.py.


_AUDIT_CLIENT: Any = None
_AUDIT_CUSTOMER_SLUG: str | None = None
_AUDIT_WIRED: bool = False

# #64: a gate that blocks/reports without recording is running dark in the
# accountability sense. Warn per-evaluation on a rate limit, not once at init.
_NO_AUDIT_WARNER = NoAuditWarner()


def _audit_client() -> tuple[Any, str | None]:
    """Lazily resolve (D1Client, customer_slug). Cached across calls.

    Returns ``(None, None)`` when the audit env is not configured — the gate
    still blocks; the row is simply skipped (rate-limited WARNING at each
    skip site, #64). Tests can reset the cache by setting the module globals
    back to their initial values.
    """
    global _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG, _AUDIT_WIRED
    if _AUDIT_WIRED:
        return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG
    _AUDIT_WIRED = True
    try:
        from shared.audit_client import audit_client_from_env
        from shared.secrets import require

        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        slug = secrets_map["SMD_CUSTOMER_SLUG"]
        # Broker-aware (OP-P1-4): FABRICATION_FILTER_TRIGGERED /
        # IDENTIFIER_UNVERIFIED rows route through the append-only broker when
        # SMD_AUDIT_BROKER_SOCKET is set; direct D1Client otherwise.
        _AUDIT_CLIENT = audit_client_from_env(customer_slug=slug)
        _AUDIT_CUSTOMER_SLUG = slug
    except Exception as exc:  # noqa: BLE001 — audit is best-effort vs the block
        logger.debug("outbound gate: audit client unconfigured (%s); blocks won't emit a row", exc)
        _AUDIT_CLIENT = None
        _AUDIT_CUSTOMER_SLUG = None
    return _AUDIT_CLIENT, _AUDIT_CUSTOMER_SLUG


def _emit_fabrication_audit(
    *,
    tool_name: str,
    decision: GateDecision,
    session_id: str,
    tool_call_id: str,
    vertical: str | None,
    cohort: str | None,
) -> None:
    """Write one ``FABRICATION_FILTER_TRIGGERED`` row. Best-effort, never raises.

    The draft body is NEVER written — only the marker ids / citation labels
    that hit land in metadata (never the matched prose substring or the body).
    """
    client, slug = _audit_client()
    if client is None or slug is None:
        _NO_AUDIT_WARNER.warn(
            logger, f"FABRICATION_FILTER_TRIGGERED on tool={tool_name} not recorded"
        )
        return
    try:
        metadata: dict = {
            "fabrication_filter": True,
            "customer": slug,
            "tool": tool_name,
            "gate_tier": decision.tier,
            "vertical": vertical or "(unknown)",
            "evaluated_law_tier": decision.evaluated_law_tier,
        }
        if cohort:
            metadata["cohort"] = cohort
        if decision.marker_hits:
            # marker IDS only — never the matched prose substring.
            metadata["marker_hits"] = list(decision.marker_hits)
        if decision.citation_hits:
            metadata["citation_hits"] = list(decision.citation_hits)
        if session_id:
            metadata["session_id"] = session_id
        if tool_call_id:
            metadata[CANONICAL_TOOL_CALL_KEY] = tool_call_id

        # body is never persisted — only marker ids / citation labels in metadata
        params = agent_event_params(
            action_type="FABRICATION_FILTER_TRIGGERED",
            metadata=metadata,
        )
        client.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — audit row is best-effort vs block
        logger.warning(
            "outbound gate: FABRICATION_FILTER_TRIGGERED emission failed "
            "(tool=%s tier=%s err=%s); the BLOCK still stands",
            tool_name,
            decision.tier,
            exc,
        )


# ---------------------------------------------------------------------------
# A1 identifier-integrity gate — REFUSING (ss #2171)
#
# Distinct from the blocking Tier-1/Tier-2 fabrication gate above. After a draft
# body clears that gate, scan it for identifier-shaped tokens (dates, A-numbers,
# receipts, SSNs, case numbers) NOT in this session's provenance register — i.e.
# an identifier the agent composed without READING it from a source. That is the
# runtime signature of a fabricated/garbled identifier, and the never-computes
# backstop for a computed legal date (a computed SOL won't be in the register).
#
# The gate REFUSES (Captain directive 2026-08-02: every seat blocking, pre-live)
# with these deliberate carve-outs:
#
#   * NAME hits never block or report — the runtime register holds no names
#     (structured-name seeding is a follow-on), so every NAME hit would be an FP.
#   * Ambient dates: today's and yesterday's UTC date verify against the system
#     clock rather than a read ("As of <today>..." is legitimate composition; a
#     US-local "today" is always utc-today or utc-today-1). Ambience is
#     DATE-kind only — a (matter, date) PAIR claim is never ambient.
#   * Empty register, DRAFT gate only: allow + report. A refusal with no source
#     to re-read is a brick for conversational work, and drafts reach a human.
#     The SEND gate gets NO such carve: an autonomous external send composed
#     with an empty register is exactly "cannot verify" — it blocks.
#
# Rollback lever: SMD_IDENTIFIER_GATE_MODE=report downgrades to report-only
# (operator-only env, never client-authorable; unset or any other value =
# block, fail-closed). The gate keeps emitting IDENTIFIER_UNVERIFIED rows in
# either mode — telemetry continuity through an incident.
# ---------------------------------------------------------------------------


# Every kind except NAME blocks. Defined by exclusion so a future IdKind added
# to the vendored filter defaults to BLOCKING (fail-closed), not report-only.
_BLOCKING_KINDS: frozenset[identifier_filter.IdKind] = frozenset(identifier_filter.IdKind) - {
    identifier_filter.IdKind.NAME
}


def _identifier_gate_mode() -> str:
    """Resolve the gate mode from env, failing closed.

    ONLY the literal string ``report`` (case-insensitive) downgrades; unset,
    typos, ``off``, ``disabled`` — anything else — mean BLOCK.
    """
    if os.getenv("SMD_IDENTIFIER_GATE_MODE", "").strip().lower() == "report":
        return "report"
    return "block"


def _ambient_dates() -> frozenset[str]:
    """Canonical dates verified by the system clock rather than a session read.

    UTC today and yesterday: a US-local "today" is always one of the two, and
    "as of today" composition is legitimate without a read. Anything further
    out (a computed deadline, a hearing date) must come from a read — a
    computed date is a true positive under the read-not-compute doctrine
    (ss #2115), not an FP.
    """
    today = datetime.now(timezone.utc).date()
    return frozenset({today.isoformat(), (today - timedelta(days=1)).isoformat()})


def _is_ambient(hit: identifier_filter.IdentifierHit, ambient: frozenset[str]) -> bool:
    """True when the clock, not a read, vouches for this hit.

    A bare DATE that is today or yesterday is exempt (see :func:`_ambient_dates`).
    So is a PAIR whose date half is: a matter number beside today's date asserts
    when the Operator is writing, not what the record says, so there is no
    record on which the two could have been seen together, and demanding one
    refuses every "alert fired on <today>, matter <n>" line. The matter number
    itself is still checked as its own hit; only the association is exempt.

    Found 2026-09-21: pilot-smokeball's deadline-miss-escalator wrote five
    unroutable-matter memos, each "fired on <today>. Matter <n> has ...", and
    all five were refused on the pair twice before a third draft dropped the
    number. Those ten refusals were half of the 20 that tripped the seat's
    refusal-cascade HARD_STOP.
    """
    if hit.kind is identifier_filter.IdKind.DATE:
        return hit.canonical in ambient
    if hit.kind is identifier_filter.IdKind.PAIR:
        _, _, date_half = hit.canonical.rpartition("|")
        return date_half in ambient
    return False


def _days_from_today_bucket(canonical: str) -> str:
    """Value-free distance bucket for a canonical YYYY-MM-DD (FP triage axis)."""
    try:
        delta = (date.fromisoformat(canonical) - date.today()).days
    except ValueError:
        return "unparsed"
    if delta < 0:
        return "past"
    if delta == 0:
        return "today"
    if delta <= 7:
        return "1-7d"
    if delta <= 30:
        return "8-30d"
    if delta <= 365:
        return "31-365d"
    return ">365d"


def _identifier_refusal_message(
    unverified: list, *, seat_sourced: bool = False, register_was_empty: bool = False
) -> str:
    """What the model sees on a refusal. Names kinds only — never raw values,
    never scan mechanics (which would teach evasion routes).

    ``seat_sourced`` adds one sentence when at least one of the hits was found in
    the seat's OWN text this session rather than in a record. Without it the
    refusal reads "not traceable to anything read this session" to a model that
    can see it read the value ten minutes ago, in a skill file. A refusal whose
    reason the reader can disprove is a refusal the reader works around; naming
    the actual distinction is what makes it followable.

    ``register_was_empty`` REMOVES the remove-the-value escape hatch. On
    2026-08-24 the pilot's escalator was refused three times on an empty
    register, complied with "or remove the unverified value and state that it
    needs confirmation" fifteen items at a time, and shipped a digest where
    every line read "matter number unavailable" — an artifact worse than the one
    refused, passed by the gate that refused it. Removing one stray unverified
    value from an otherwise-sourced draft is a legitimate path; removing EVERY
    value because nothing was read is laundering a failed run into a deliverable.
    When nothing was read this session, the only honest moves are to read the
    source records or to stop and report the run failed — so that is all the
    refusal offers.
    """
    kinds = sorted({h.kind.value for h in unverified})
    if register_was_empty:
        message = (
            "Refused: this content contains identifier(s) not traceable to anything "
            f"read this session ({', '.join(kinds)}), and nothing has been read from "
            "the firm's records this session, so none of it can be verified. Do NOT "
            "send a version with the unverified values removed — a report that names "
            "no records is not a deliverable. Read the source records and include "
            "each value exactly as it appears there, or stop without sending and "
            "state that this run failed and needs attention. "
            "Do not guess, derive, or reformat identifiers."
        )
    else:
        message = (
            "Refused: this content contains identifier(s) not traceable to anything "
            f"read this session ({', '.join(kinds)}). Re-read the source record that "
            "contains the correct value and include it exactly as it appears there — "
            "or remove the unverified value and state that it needs confirmation. "
            "Do not guess, derive, or reformat identifiers."
        )
    if seat_sourced:
        message += (
            " At least one of these appears in your own instructions, skills, or "
            "configuration rather than in a record from the firm's systems. Text "
            "the seat carries is not a source; only a read of the firm's own "
            "records is."
        )
    return message


def _emit_identifier_audit(
    *,
    unverified: list,
    mode: str,
    blocked: bool,
    block_bypass: str | None,
    register_was_empty: bool,
    seat_sourced: bool,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    vertical: str | None,
    cohort: str | None,
) -> None:
    """Write the IDENTIFIER_UNVERIFIED row. Raises to the caller's guard — the
    caller treats emission as best-effort and a failure NEVER rescinds a block."""
    client, slug = _audit_client()
    if client is None or slug is None:
        _NO_AUDIT_WARNER.warn(logger, f"IDENTIFIER_UNVERIFIED on tool={tool_name} not recorded")
        return
    by_kind: dict[str, int] = {}
    date_distance: dict[str, int] = {}
    for h in unverified:
        by_kind[h.kind.value] = by_kind.get(h.kind.value, 0) + 1
        if h.kind is identifier_filter.IdKind.DATE:
            bucket = _days_from_today_bucket(h.canonical)
            date_distance[bucket] = date_distance.get(bucket, 0) + 1
    metadata: dict = {
        "gate_tier": "tier3_identifier",
        "mode": mode,
        "blocked": blocked,
        "customer": slug,
        "tool": tool_name,
        "vertical": vertical or "(unknown)",
        "register_was_empty": register_was_empty,
        "unverified_counts": by_kind,
        # redacted shapes only — never the raw identifier value
        "shapes": sorted({identifier_filter._redact(h) for h in unverified}),
    }
    if seat_sourced:
        # The kill test on ss-console#2511 reads this exact key and value. It is
        # the difference between "the register was empty" and "the value came out
        # of the seat's own text", which is the whole point of the negative
        # register — so it is a field, not a phrase inside another field.
        metadata["source"] = "seat_text"
    if block_bypass:
        metadata["block_bypass"] = block_bypass
    if date_distance:
        # value-free: distance buckets, not dates (post-flip FP triage axis)
        metadata["date_distance"] = date_distance
    if cohort:
        metadata["cohort"] = cohort
    if session_id:
        metadata["session_id"] = session_id
    if tool_call_id:
        metadata[CANONICAL_TOOL_CALL_KEY] = tool_call_id
    params = agent_event_params(action_type="IDENTIFIER_UNVERIFIED", metadata=metadata)
    client.execute(_INSERT_SQL, *params)


def _check_identifiers(
    *,
    body: str,
    gate: str,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    vertical: str | None,
    cohort: str | None,
    fail_closed: bool = False,
) -> dict | None:
    """Identifier-integrity check: block directive on an unverified identifier.

    ``gate`` is ``"draft"`` or ``"send"`` — the empty-register carve applies to
    the draft gate only (see the section comment above).

    ``fail_closed`` turns a scanner crash into a refusal instead of an allow.
    The attachment scan passes it: a workbook is a surface no person composed
    in front of the model, and "could not look" must not send it.

    Three zones, deliberately separated:
      1. scan   — guarded: a scanner CRASH is an infra fault, not evidence of
                  fabrication; allow loudly (the Tier-1/2 gate already ran).
      2. decide — pure, unguarded: nothing to swallow.
      3. audit  — best-effort: an emission failure never rescinds a block.
    """
    # -- zone 1: scan --
    try:
        register = provenance.register_for(session_id)
        result = identifier_filter.check(body, register)
        ambient = _ambient_dates()
        unverified = [
            h
            for h in result.unverified
            if h.kind is not identifier_filter.IdKind.NAME and not _is_ambient(h, ambient)
        ]
        register_was_empty = result.register_was_empty
        # ss-console#2511 — the negative register. A hit the SEAT's own text
        # supplied is a different finding from a hit nothing supplied, and
        # ``verifies`` is the same membership test the positive path uses, so
        # both sides canonicalize identically by construction.
        seat_register = provenance.seat_sourced_for(session_id)
        seat_sourced_hits = [h for h in unverified if seat_register.verifies(h)]
    except Exception as exc:  # noqa: BLE001 — infra fault, not fabrication evidence
        if fail_closed:
            logger.error(
                "outbound gate: identifier scan CRASHED (tool=%s err=%s); refusing",
                tool_name,
                exc,
            )
            return {"action": "block", "message": "The attachment could not be checked."}
        logger.error(
            "outbound gate: identifier scan CRASHED (tool=%s err=%s); allowing",
            tool_name,
            exc,
        )
        return None
    if not unverified:
        return None

    # -- zone 2: decide --
    #
    # Two carves and they do not compose. The empty-register carve exists so a
    # conversational turn that read nothing is not bricked by a refusal with no
    # source to go re-read. That reasoning does not reach a value the seat read
    # out of its own skill body: there IS a thing to say about it, namely that
    # it is not a record. So a seat-sourced hit refuses through the carve.
    #
    # The per-tool report carve is unconditional in the other direction: a tool
    # on that list measures and never refuses, seat-sourced or not, until its
    # false-positive rate has been read (see _REPORT_ONLY_DRAFT_TOOLS, which is
    # down to add_file alone now that render_docx_draft has its number).
    mode = MODE_REPORT_TOOL if tool_name in _REPORT_ONLY_DRAFT_TOOLS else _identifier_gate_mode()
    seat_sourced = bool(seat_sourced_hits)
    empty_carve = register_was_empty and gate == "draft" and not seat_sourced
    should_block = (
        mode == "block" and not empty_carve and any(h.kind in _BLOCKING_KINDS for h in unverified)
    )
    block_bypass = (
        "register_empty" if (mode == "block" and empty_carve and not should_block) else None
    )

    # -- zone 3: audit --
    try:
        _emit_identifier_audit(
            unverified=unverified,
            mode=mode,
            blocked=should_block,
            block_bypass=block_bypass,
            register_was_empty=register_was_empty,
            seat_sourced=seat_sourced,
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
        )
    except Exception as exc:  # noqa: BLE001 — audit is best-effort vs the decision
        logger.warning(
            "outbound gate: identifier audit emit failed (tool=%s err=%s); decision stands",
            tool_name,
            exc,
        )

    if should_block:
        return {
            "action": "block",
            "message": _identifier_refusal_message(
                unverified,
                seat_sourced=seat_sourced,
                register_was_empty=register_was_empty,
            ),
        }
    return None


# ---------------------------------------------------------------------------
# The gate entry point — called from on_pre_tool_call after ceiling passes
# ---------------------------------------------------------------------------


def check_outbound_draft(
    *,
    tool_name: str,
    args: dict | None,
    session_id: str = "",
    tool_call_id: str = "",
) -> dict | None:
    """Second pre_tool_call evaluation: scan a draft body for fabrication.

    Called ONLY after the trust-ceiling check has allowed the call, and ONLY
    for body-bearing draft tools. Returns a block directive
    ``{"action": "block", "message": ...}`` to refuse, or ``None`` to allow.

    Fail-closed:
      * A gated draft tool whose body can't be resolved → BLOCK.
      * The policy core blocks (marker / citation / load error) → BLOCK + audit.
    """
    if not _is_gated_draft_tool(tool_name):
        # Not a body-bearing draft tool; the outbound gate doesn't apply.
        return None

    body = _extract_body(args)
    if body is None or not body.strip():
        if not _body_is_required(tool_name):
            # Body-optional tool with no prose body (structured-only call: a
            # calendar time, a matter-field date). The Tier-1/2 marker/citation
            # gate has no prose to scan — but the structured args are still an
            # identifier surface (#2132: a fabricated hearing date in
            # create_event.start_time was invisible here). Run the identifier
            # gate over the concatenated scannable args — an unverified
            # identifier here REFUSES the write (ss #2171).
            scan_text = _extract_draft_scan_text(args)
            if scan_text.strip():
                directive = _check_identifiers(
                    body=scan_text,
                    gate="draft",
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    vertical=_resolve_vertical(),
                    cohort=_resolve_cohort(),
                )
                if directive is not None:
                    return directive
            markdown = args.get("draft_markdown") if isinstance(args, dict) else None
            if tool_name in CHECKLIST_DOCX_TOOLS and isinstance(markdown, str):
                return _check_checklist(
                    tool_name=tool_name,
                    texts=[markdown],
                    surface=output_checklist.DOCX,
                    output_class="work_product",
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
            return None
        # A BODY-REQUIRED draft tool with no recognizable / empty body. We
        # cannot scan what we can't find — BLOCK rather than skip (fail-closed).
        logger.warning(
            "outbound gate: body-required draft tool %r carried no recognizable "
            "body key; BLOCKING (fail-closed, ADR 0028)",
            tool_name,
        )
        decision = GateDecision(
            allowed=False,
            reason=(
                f"Refused: draft tool {tool_name} carried no recognizable body to "
                "scan for fabrication; failing closed (ADR 0028)"
            ),
            audit_action="fabrication_block",
            tier="load_error",
        )
        _emit_fabrication_audit(
            tool_name=tool_name,
            decision=decision,
            session_id=session_id,
            tool_call_id=tool_call_id,
            vertical=_resolve_vertical(),
            cohort=_resolve_cohort(),
        )
        return {"action": "block", "message": decision.reason}

    vertical = _resolve_vertical()
    cohort = _resolve_cohort()
    # Provenance-verified captions (ss #1758): a case name the agent READ this
    # session is quotable; empty register = no exemption (fail-closed).
    # Provenance-verified MONEY (ss-console#2258): same shape, same fail-closed
    # default. The skill authorizes a figure that exists in an authored source on
    # the matter; without this the gate refused what the skill permitted.
    _register = provenance.register_for(session_id)
    decision = evaluate(
        body,
        cohort,
        vertical,
        allowed_case_names=_register.captions(),
        allowed_money=_register.money(),
    )
    if decision.allowed:
        # A1 identifier gate: refuse any identifier not traceable to a source
        # read this session (ss #2171). Scans the concatenated draft surface
        # (prose body PLUS structured args, #2132) — a wider net than the
        # evaluate() call above, which deliberately stays prose-only.
        directive = _check_identifiers(
            body=_extract_draft_scan_text(args) or body,
            gate="draft",
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
        )
        if directive is not None:
            return directive
        if tool_name in CHECKLIST_MEMO_TOOLS:
            # The file note, raw: the connector normalizes headings, emphasis
            # and quotes after this hook, so the MEMO set does not refuse them.
            return _check_checklist(
                tool_name=tool_name,
                texts=[body],
                surface=output_checklist.MEMO,
                output_class="record",
                session_id=session_id,
                tool_call_id=tool_call_id,
            )
        return None

    _emit_fabrication_audit(
        tool_name=tool_name,
        decision=decision,
        session_id=session_id,
        tool_call_id=tool_call_id,
        vertical=vertical,
        cohort=cohort,
    )
    return {"action": "block", "message": decision.reason}


# ---------------------------------------------------------------------------
# EXTERNAL_SEND gate (ADR 0028 / EFF-01)
#
# An autonomous EXTERNAL_SEND delivers content to the outside world with NO
# human review, so it is the highest-stakes fabrication surface — yet the draft
# gate above only covers INTERNAL_WRITE drafts. This path runs the same
# provenance gate on the send body so a fabricated marker / legal citation
# blocks the send. Sends are scanned across ALL scannable fields (a fabricated
# cite can hide in an html body while the plaintext field is empty), unlike
# drafts which take the first recognized body key.
# ---------------------------------------------------------------------------


_SEND_SCAN_KEYS: tuple[str, ...] = (
    "subject",
    "text",
    "html",
    "html_body",
    "body",
    # msgraph-mail (ADR 0078) send/reply bodies ride ``body_text`` (flat args, D4).
    # WITHOUT this key the scanner found no body on an msgraph send, scanned "",
    # and silently ALLOWED — a fabricated citation in body_text would sail through
    # the fabrication/citation gate. body_plain is added for symmetry with the
    # draft path's _BODY_ARG_KEYS.
    "body_text",
    "body_plain",
    "content",
    "message",
    "note",
)


# EXTERNAL_SEND tools that ALWAYS author a prose body (a mail send with no body
# is malformed). For these, an unlocatable body fails CLOSED (block) rather than
# the default "no content → no fabrication surface → allow": a send-class tool
# whose body the scanner cannot find must not ship un-scanned. Scoped to the
# msgraph sends so the established AgentMail send behavior (text/html, already in
# the scan keys) is unchanged.
_BODY_REQUIRED_SEND_TOOLS: frozenset[str] = frozenset(
    {"mcp_msgraph_mail_send_message", "mcp_msgraph_mail_reply_message"}
)


# ---------------------------------------------------------------------------
# Staff-class dash normalization (ss-console#2547)
#
# The em dash is a Tier-1 fabrication marker because the tone rules ban it on
# shipped user-facing copy — copy that carries the FIRM's voice to someone
# outside it. On 2026-08-19 that rule refused the deadline escalator's fifth and
# last attempt to tell Scott about a court date seven days away. The recipient
# was Scott. The consequence of the marker reaching him would have been a dash
# in an ops email; the consequence of the refusal was silence about a deadline.
#
# The rule is not wrong, it was pointed at the wrong class. So on the STAFF class
# alone the dash is normalized instead of refused, and every outbound class keeps
# the marker exactly as it was — client, vendor and unrostered-external copy is
# what the tone rule was written about, and there the refusal is the point.
#
# NORMALIZE BEFORE THE SCAN, AND SEND WHAT WAS SCANNED. The args dict is mutated
# in place, which is the established way this hook reaches the tool (the
# workspace broker's grant and the report renderer's html half both do it), and
# it means the gate cannot end up scanning one body while the transport carries
# another. Anything else would put a difference between the inspected text and
# the sent text, which is the shape of every bypass this file exists to close.
#
# Horizontal whitespace around the dash is absorbed so "a — b" becomes "a, b"
# rather than "a ,  b". NEWLINES ARE NOT: a line beginning with a dash is a list
# item, and eating the newline would silently join two bullets into one
# sentence — a change to what the message SAYS, which a punctuation normalizer
# has no business making.
# ---------------------------------------------------------------------------

_DASH_RE = re.compile(r"[ \t]*[—–][ \t]*")

#: The output class whose sends are normalized rather than refused.
_DASH_NORMALIZED_CLASS = "staff"

#: Which send args are rewritten. The scan keys minus nothing: every field the
#: scanner reads is a field the recipient sees, so normalizing a subset would
#: leave a marker in the half that was not rewritten and refuse anyway.
_DASH_NORMALIZE_KEYS = _SEND_SCAN_KEYS


def _normalize_staff_dashes(tool_name: str, args: dict | None, session_id: str) -> bool:
    """Rewrite em/en dashes in a STAFF-class send, in place. True iff anything
    changed.

    Fail-quiet and fail-STRICT: any failure to resolve the class leaves the body
    untouched, so the send meets the marker gate exactly as it does today. The
    worst case of this function not running is the refusal that already happens.
    """
    if not isinstance(args, dict):
        return False
    try:
        from . import enforce  # local: avoids an import cycle at package load

        resolved = enforce.resolved_send_class(tool_name, args, session_id)
        if resolved is None:
            return False
        if spec_gate.resolve_output_class(resolved.value) != _DASH_NORMALIZED_CLASS:
            return False
    except Exception:  # noqa: BLE001 — see docstring
        logger.debug(
            "outbound gate: staff-class resolution failed for %s", tool_name, exc_info=True
        )
        return False
    changed = False
    for key in _DASH_NORMALIZE_KEYS:
        value = args.get(key)
        if not isinstance(value, str) or not value:
            continue
        rewritten = _DASH_RE.sub(", ", value)
        if rewritten != value:
            args[key] = rewritten
            changed = True
    if changed:
        logger.info(
            "outbound gate: normalized dashes in a staff-class %s before the marker scan (ss#2547)",
            tool_name,
        )
    return changed


def _is_gated_send_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` delivers content externally (EXTERNAL_SEND)."""
    if not tool_name:
        return False
    return TOOL_ACTION_CLASS_MAP.get(tool_name) is ActionClass.EXTERNAL_SEND


def _extract_send_body(args: dict | None) -> str:
    """Concatenate every scannable field of a send so fabrication in any one of
    them (e.g. an html-only body) is scanned. Empty string if none present."""
    if not isinstance(args, dict):
        return ""
    parts: list[str] = []
    for key in _SEND_SCAN_KEYS:
        value = args.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def check_outbound_send(
    *,
    tool_name: str,
    args: dict | None,
    session_id: str = "",
    tool_call_id: str = "",
) -> dict | None:
    """Fabrication/citation gate for EXTERNAL_SEND tools (ADR 0028 / EFF-01).

    Called after the trust-ceiling check has allowed an autonomous send. Scans
    the combined send body through the same provenance gate the draft path uses
    and returns a block directive on a fabricated marker / legal citation, else
    ``None``. A send with no scannable content has no fabrication surface and is
    allowed (the ceiling + content floor already governed whether it may fire).
    """
    if not _is_gated_send_tool(tool_name):
        return None
    # Staff-class dash normalization runs FIRST and mutates ``args``, so the body
    # extracted below — the body every tier scans — is byte-for-byte the body the
    # transport will carry (ss#2547).
    _normalize_staff_dashes(tool_name, args, session_id)
    body = _extract_send_body(args)
    if not body.strip():
        if tool_name in _BODY_REQUIRED_SEND_TOOLS:
            # A body-required send whose body the scanner cannot locate — fail
            # CLOSED rather than skip the fabrication scan (the msgraph
            # body-key-omission bypass class). Report the block for the audit trail.
            logger.warning(
                "outbound gate: body-required send tool %r carried no recognizable "
                "body key; BLOCKING (fail-closed, ADR 0028/0078)",
                tool_name,
            )
            decision = GateDecision(
                allowed=False,
                reason=(
                    f"Refused: send tool {tool_name} carried no recognizable body to "
                    "scan for fabrication; failing closed (ADR 0028)"
                ),
                audit_action="fabrication_block",
                tier="load_error",
            )
            _emit_fabrication_audit(
                tool_name=tool_name,
                decision=decision,
                session_id=session_id,
                tool_call_id=tool_call_id,
                vertical=_resolve_vertical(),
                cohort=_resolve_cohort(),
            )
            return {"action": "block", "message": decision.reason}
        return None
    vertical = _resolve_vertical()
    cohort = _resolve_cohort()
    # Provenance-verified captions (ss #1758): a case name the agent READ this
    # session is quotable; empty register = no exemption (fail-closed).
    # Provenance-verified MONEY (ss-console#2258): same shape, same fail-closed
    # default. The skill authorizes a figure that exists in an authored source on
    # the matter; without this the gate refused what the skill permitted.
    _register = provenance.register_for(session_id)
    decision = evaluate(
        body,
        cohort,
        vertical,
        allowed_case_names=_register.captions(),
        allowed_money=_register.money(),
    )
    if decision.allowed:
        # A1 identifier gate on the send surface — NO empty-register carve
        # here: an autonomous external send composed with nothing read is
        # exactly "cannot verify", and no human sits downstream (ss #2171).
        directive = _check_identifiers(
            body=body,
            gate="send",
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
        )
        if directive is not None:
            return directive
        return _check_send_checklist(
            tool_name=tool_name, args=args, session_id=session_id, tool_call_id=tool_call_id
        )
    _emit_fabrication_audit(
        tool_name=tool_name,
        decision=decision,
        session_id=session_id,
        tool_call_id=tool_call_id,
        vertical=vertical,
        cohort=cohort,
    )
    return {"action": "block", "message": decision.reason}


# ---------------------------------------------------------------------------
# The outbound attachment scan (statute-watch v2, 2026-10-01)
#
# A workbook attached to an out-of-turn send is read by a person exactly like
# the body is, so its text meets the same two send-gate questions: does it carry
# a fabrication marker or an unread legal citation, and is every identifier in it
# (file numbers, dates, the pairs a row puts side by side) traceable to a source
# this session read. The output checklist is NOT run: it governs prose shape
# (local times, sentence form, length), and a table of records is not prose.
#
# The text arrives already extracted (``shared.outbound_attachment``), dates
# rendered as the ISO dates a reader sees. Everything here fails CLOSED: the
# caller refuses the attachment on a raise as well as on a block.
# ---------------------------------------------------------------------------

#: The audit tool name for an attachment scan: the send it rides.
ATTACHMENT_SCAN_TOOL = "smd_send_message"


def check_outbound_attachment(text: str, *, session_id: str, tool_call_id: str = "") -> dict | None:
    """Scan an outbound attachment's extracted ``text`` on the send gate.

    Returns ``{"action": "block", "message": ...}`` to refuse the attachment,
    else ``None``. Same register, same citation/fabrication policy and the same
    identifier gate (``gate="send"``, no empty-register carve) as
    :func:`check_outbound_send`, minus the prose checklist; the identifier scan
    fails closed here.
    """
    if not isinstance(text, str):
        return {"action": "block", "message": "The attachment could not be read."}
    if not text.strip():
        return None
    vertical = _resolve_vertical()
    cohort = _resolve_cohort()
    register = provenance.register_for(session_id)
    decision = evaluate(
        text,
        cohort,
        vertical,
        allowed_case_names=register.captions(),
        allowed_money=register.money(),
    )
    if not decision.allowed:
        _emit_fabrication_audit(
            tool_name=ATTACHMENT_SCAN_TOOL,
            decision=decision,
            session_id=session_id,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
        )
        return {"action": "block", "message": decision.reason}
    return _check_identifiers(
        body=text,
        gate="send",
        session_id=session_id,
        tool_name=ATTACHMENT_SCAN_TOOL,
        tool_call_id=tool_call_id,
        vertical=vertical,
        cohort=cohort,
        fail_closed=True,
    )


# ---------------------------------------------------------------------------
# The output checklist (ss-console Option B, 2026-09-29)
#
# The standard is one question: would a good paralegal have sent, filed or
# handed this over as is? ``shared/output_checklist`` decides the part of it a
# machine can decide (local times, no internal ids, plain sentences, length);
# this is where each surface meets it, after the fabrication and identifier
# gates have allowed the text:
#
#   * every send, at ``check_outbound_send``, which the in-turn hook, the
#     out-of-turn dispatcher and send_as all reach. A code-rendered body
#     (``TEMPLATED_BODY_ARG``) is skipped: the repo wrote it, not the model, and
#     the model can no longer set that key (``on_pre_tool_call`` pops it).
#   * a file note (create_memo / update_memo), raw, on the MEMO set;
#   * a Word document (render_docx_draft), on ``draft_markdown``, ids and
#     ISO/UTC stamps only.
#
# DISPOSITION. Refuse with the reason, then the remedy, quoting the fragment:
# the model holds the text, so the quote discloses nothing. The report-only
# rules write a row and proceed. The THIRD refusal of the same text in one
# session holds it instead: the message is withheld, never sent, and the turn is
# told to say so. A wrong court time sent on the third try is the 2026-09-29
# defect itself, and a scheduled turn refused forever is the 2026-08-19 loop;
# holding is the one answer that is neither.
# ---------------------------------------------------------------------------

#: The file-note writes. ``update_memo`` is the in-place twin of ``create_memo``
#: (one note per routine per matter, checklist item 11) and meets the same rules.
CHECKLIST_MEMO_TOOLS: frozenset[str] = frozenset(
    {"mcp_smokeball_create_memo", "mcp_smokeball_update_memo"}
)

#: The Word-document write whose markdown source is a whole filed document.
CHECKLIST_DOCX_TOOLS: frozenset[str] = frozenset({"mcp_smokeball_render_docx_draft"})

#: Send fields a reader sees as text. The html fields are scanned for their
#: visible text (tags removed), since a reader never sees the tags.
_CHECKLIST_TEXT_KEYS: tuple[str, ...] = (
    "subject",
    "text",
    "body",
    "body_text",
    "body_plain",
    "content",
    "message",
    "note",
)
_CHECKLIST_HTML_KEYS: tuple[str, ...] = ("html", "html_body")

_CHECKLIST_COUNTER = output_checklist.RefusalCounter()

#: Plain names for the rules, for the withheld notice the run output repeats.
_RULE_PLAIN: dict[str, str] = {
    "internal_id": "an internal id",
    "iso_timestamp": "a machine timestamp",
    "utc_time": "a UTC time",
    "bare_clock": "a clock time with no a.m. or p.m.",
    "pipe_table": "a table",
    "heading_marks": "pound-sign headings",
    "emphasis_marks": "asterisk or backtick markup",
    "html_tag": "an html tag",
    "em_dash": "a dash",
    "exclamation": "an exclamation mark",
    "caps_emphasis": "capitals for emphasis",
    "max_lines": "too many lines",
}

# ---------------------------------------------------------------------------
# The withheld notice: a held staff message is never silence
#
# Two rulings meet here. 2026-08-19 (ss-console#2547): the escalator's last
# warning about a court date was refused over a dash and the person heard
# nothing, so staff mail must not go silent. 2026-09-29: a wrong court time must
# never go out. Holding the model's text satisfies the second; this notice
# satisfies the first. When a STAFF message is withheld on its third refusal, a
# fixed, code-rendered note goes to the same staff recipients saying a message
# was withheld and why, in plain words, with none of the model's text in it.
#
# It goes through the seat's own out-of-turn sender (``shared.send_dispatch``,
# the trust plugin's ``_dispatch_internal_message``), so the ceiling, the taint
# gate and the transport's CONFIRM_SEND_DISPATCHED row are the ones any internal
# dispatch gets. It is marked templated, so the checklist never rescans it and it
# can never itself be held. A thread-local guard means a notice being dispatched
# can never produce another notice. It is sent once per message per session: on
# the refusal that exhausts the count, not on any later one.
# ---------------------------------------------------------------------------

#: What each rule looked like to the person, for the notice body. Every phrase
#: passes the staff checklist itself (pinned by a test).
_NOTICE_PLAIN: dict[str, str] = {
    "internal_id": "an internal reference code",
    "iso_timestamp": "a time that was not in the firm's local clock",
    "utc_time": "a time that was not in the firm's local clock",
    "bare_clock": "a time that was not in the firm's local clock",
    "pipe_table": "a table",
    "heading_marks": "formatting marks",
    "emphasis_marks": "formatting marks",
    "html_tag": "formatting marks",
    "em_dash": "a dash",
    "exclamation": "an exclamation mark",
}

_NOTICE_SUBJECT_FALLBACK = "an Operator message"
_NOTICE_SUBJECT_MAX = 120

_NOTICE_GUARD = threading.local()


class code_fixed_scope:  # noqa: N801 - reads as a phrase at the call site
    """The out-of-turn dispatcher's verified code-fixed capability, for this block.

    The date-prep brief is composed by the model but its recipients are fixed by
    code (``shared.send_dispatch.CodeFixedRecipients``), which lifts the taint
    gate for it. It is also the message most likely to carry a bad clock. If
    its withheld notice met the taint gate without that capability, the notice
    would be refused and the person would hear nothing: the 2026-08-19 failure
    again. So the dispatcher opens this scope around its outbound scans ONLY
    when it has verified the capability, and a notice raised inside it goes to
    exactly the capability's recipients, carrying the capability, so the
    dispatcher stamps the same ``taint_exempt`` marker on its row. The reasoning
    that justifies the exemption holds unchanged: the recipients are the
    code-fixed ones byte for byte and the body is fixed text. A send that never
    had the capability (every in-turn send) never opens this scope.
    """

    def __init__(self, capability: send_dispatch.CodeFixedRecipients | None) -> None:
        self._capability = capability

    def __enter__(self) -> None:
        self._prior = getattr(_NOTICE_GUARD, "code_fixed", None)
        _NOTICE_GUARD.code_fixed = self._capability

    def __exit__(self, *_exc: object) -> None:
        _NOTICE_GUARD.code_fixed = self._prior


class notices_suppressed:  # noqa: N801 - reads as a phrase at the call site
    """Within this block the checklist sends no withheld notice of its own.

    For a caller that sends its own notice to a wider set of people (the
    approved-send lane adds the approver), so one refusal is one notice.
    """

    def __enter__(self) -> None:
        self._prior = getattr(_NOTICE_GUARD, "suppressed", False)
        _NOTICE_GUARD.suppressed = True

    def __exit__(self, *_exc: object) -> None:
        _NOTICE_GUARD.suppressed = self._prior


def _notice_body(rules: list[str]) -> str:
    phrases: list[str] = []
    for rule in rules:
        phrase = _NOTICE_PLAIN.get(rule, "something a paralegal would correct")
        if phrase not in phrases:
            phrases.append(phrase)
    if len(phrases) > 1:
        carried = ", ".join(phrases[:-1]) + " and " + phrases[-1]
    else:
        carried = phrases[0] if phrases else "something a paralegal would correct"
    return (
        "The Operator prepared a message about this and withheld it.\n"
        f"It carried {carried}.\n"
        "Please check the matter's calendar entry or task directly.\n"
        "This is an internal note; no client was contacted."
    )


def _notice_subject(original: str) -> str:
    """``Withheld: <original subject>``, unless the subject itself would not pass.

    The notice is never rescanned, so a subject that carries the very time the
    body was withheld for is replaced rather than repeated.
    """
    flat = " ".join(original.split()) if isinstance(original, str) else ""
    if flat and not output_checklist.refusing(
        output_checklist.check(flat, output_checklist.STAFF_SEND)
    ):
        return ("Withheld: " + flat)[:_NOTICE_SUBJECT_MAX]
    return "Withheld: " + _NOTICE_SUBJECT_FALLBACK


def _recipients(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _send_withheld_notice(
    *,
    to: list[str],
    cc: list[str],
    subject: str,
    rules: list[str],
    session_id: str,
    code_fixed_recipients: send_dispatch.CodeFixedRecipients | None = None,
) -> send_dispatch.DispatchResult:
    """Dispatch the fixed withheld notice. Never raises; never recurses."""
    if getattr(_NOTICE_GUARD, "active", False):
        return send_dispatch.DispatchResult(sent=False, reason="a notice cannot raise a notice")
    _NOTICE_GUARD.active = True
    try:
        return send_dispatch.dispatch(
            to=to,
            cc=cc,
            subject=_notice_subject(subject),
            text=_notice_body(rules),
            session_id=session_id,
            templated=True,
            **(
                {"code_fixed_recipients": code_fixed_recipients}
                if code_fixed_recipients is not None
                else {}
            ),
        )
    except Exception as exc:  # noqa: BLE001 - dispatch is already exception-safe
        logger.warning("outbound gate: withheld notice raised (%s)", exc)
        return send_dispatch.DispatchResult(sent=False, reason=str(exc))
    finally:
        _NOTICE_GUARD.active = False


_NOUNS: dict[str, tuple[str, str]] = {
    output_checklist.MEMO: ("file note", "written"),
    output_checklist.DOCX: ("document", "written"),
}


def _merge(
    violations_per_text: list[list[output_checklist.Violation]],
) -> list[output_checklist.Violation]:
    """One violation per rule across the scanned fields, the first one found."""
    seen: dict[str, output_checklist.Violation] = {}
    for violations in violations_per_text:
        for violation in violations:
            seen.setdefault(violation.rule, violation)
    return list(seen.values())


def _check_checklist(
    *,
    tool_name: str,
    texts: list[str],
    surface: str,
    output_class: str,
    session_id: str,
    tool_call_id: str,
    notice: dict | None = None,
) -> dict | None:
    """Scan ``texts`` on ``surface``; refuse, hold, or record and proceed.

    ``notice`` (staff sends only) carries ``to``, ``cc`` and ``subject`` for the
    withheld notice sent when this call exhausts the refusal count.

    A scanner fault allows loudly, as the identifier scan does: the checklist is
    a quality gate that runs after the safety gates have already allowed the
    text, so an infra fault in it is not evidence about the text.
    """
    try:
        found = _merge([output_checklist.check(text, surface) for text in texts if text])
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error("outbound gate: output checklist CRASHED (tool=%s err=%s)", tool_name, exc)
        return None
    if not found:
        return None
    blocking = output_checklist.refusing(found)
    exhausted = False
    first_exhaustion = False
    if blocking:
        key = output_checklist.fingerprint("\x00".join(texts))
        count = _CHECKLIST_COUNTER.bump(session_id, key)
        exhausted = count >= output_checklist.HOLD_AFTER
        first_exhaustion = count == output_checklist.HOLD_AFTER
    spec_gate.audit_output_checklist(
        tool_name=tool_name,
        output_class=output_class,
        rules=output_checklist.rule_names(found),
        session_id=session_id,
        tool_call_id=tool_call_id,
        exhausted=exhausted,
    )
    if not blocking:
        return None
    noun, verb = _NOUNS.get(surface, ("message", "sent"))
    detail = output_checklist.describe(blocking)
    if exhausted:
        plain = ", ".join(_RULE_PLAIN.get(v.rule, v.rule) for v in blocking)
        told = ""
        suppressed = getattr(_NOTICE_GUARD, "suppressed", False)
        if notice and first_exhaustion and notice.get("to") and not suppressed:
            capability = getattr(_NOTICE_GUARD, "code_fixed", None)
            if capability is not None:
                # Exactly the code-fixed recipients, so the capability matches.
                notice_to, notice_cc = list(capability.to), list(capability.cc)
            else:
                notice_to, notice_cc = notice["to"], notice.get("cc") or []
            result = _send_withheld_notice(
                to=notice_to,
                cc=notice_cc,
                subject=notice.get("subject") or "",
                rules=[v.rule for v in blocking],
                session_id=session_id,
                code_fixed_recipients=capability,
            )
            told = (
                " A short internal notice that a message was withheld went to "
                f"{', '.join(result.recipients or notice['to'])}."
                if result.sent
                else f" The internal notice that it was withheld did not go ({result.reason})."
            )
        return {
            "action": "block",
            "message": (
                f"Withheld: this {noun} was refused {output_checklist.HOLD_AFTER} times "
                f"with the same text and has not been {verb}. It will not go out. Do not "
                f"call {tool_name} with it again.{told} In your run output, say that the "
                f"{noun} was withheld because it still carried {plain}, so a person can "
                f"correct what produced it. {detail}"
            ),
        }
    return {
        "action": "block",
        "message": (
            f"Refused: this {noun} would reach a person with {len(blocking)} thing(s) a "
            f"paralegal would correct, and nothing was {verb}. {detail} Correct each one "
            f"and call {tool_name} again with the corrected text."
        ),
    }


def _send_output_class(tool_name: str, args: dict, session_id: str) -> str | None:
    """The send's output class ("staff", "outbound_client", ...) or ``None``.

    The same resolution ``_normalize_staff_dashes`` makes; ``None`` means the
    class could not be resolved and the stricter every-output set applies.
    """
    try:
        from . import enforce  # local: avoids an import cycle at package load

        code_fixed = getattr(_NOTICE_GUARD, "code_fixed", None) is not None
        resolved = enforce.resolved_send_class(tool_name, args, session_id, code_fixed=code_fixed)
        if resolved is None:
            return None
        return spec_gate.resolve_output_class(resolved.value)
    except Exception:  # noqa: BLE001 - unresolved reads as the stricter set
        logger.debug("outbound gate: send class unresolved for %s", tool_name, exc_info=True)
        return None


def _send_texts(args: dict) -> list[str]:
    """The reader-visible text fields of a send, html reduced to its words."""
    texts: list[str] = []
    for key in _CHECKLIST_TEXT_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    for key in _CHECKLIST_HTML_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(report_render.html_text_content(value))
    return texts


def send_checklist_rules(tool_name: str, args: dict | None, session_id: str) -> list[str]:
    """The refusing checklist rules a send carries, read only (no count, no row).

    For a caller that already has a refusal in hand and needs to say, in plain
    words, what the refused message carried (the approved-send lane's notice).
    Empty when the send meets the checklist, or when the refusal came from a
    different gate.
    """
    if not isinstance(args, dict):
        return []
    output_class = _send_output_class(tool_name, args, session_id)
    surface = (
        output_checklist.STAFF_SEND if output_class == "staff" else output_checklist.EVERY_OUTPUT
    )
    try:
        found = _merge([output_checklist.check(t, surface) for t in _send_texts(args)])
    except Exception:  # noqa: BLE001 - a naming aid, never a decision
        return []
    return [v.rule for v in output_checklist.refusing(found)]


def send_is_staff(tool_name: str, args: dict | None, session_id: str) -> bool:
    """True when this send resolves to the staff output class."""
    return isinstance(args, dict) and _send_output_class(tool_name, args, session_id) == "staff"


def _check_send_checklist(
    *, tool_name: str, args: dict | None, session_id: str, tool_call_id: str
) -> dict | None:
    """The checklist on a send: STAFF_SEND for staff, EVERY_OUTPUT otherwise."""
    if not isinstance(args, dict) or args.get(spec_gate.TEMPLATED_BODY_ARG):
        return None
    texts = _send_texts(args)
    if not texts:
        return None
    output_class = _send_output_class(tool_name, args, session_id)
    surface = (
        output_checklist.STAFF_SEND if output_class == "staff" else output_checklist.EVERY_OUTPUT
    )
    notice = None
    if surface == output_checklist.STAFF_SEND:
        notice = {
            "to": _recipients(args.get("to")),
            "cc": _recipients(args.get("cc")),
            "subject": args.get("subject") if isinstance(args.get("subject"), str) else "",
        }
    return _check_checklist(
        tool_name=tool_name,
        texts=texts,
        surface=surface,
        output_class=output_class or "unresolved",
        session_id=session_id,
        tool_call_id=tool_call_id,
        notice=notice,
    )


__all__ = [
    "GATED_DRAFT_TOOLS",
    "check_outbound_draft",
    "check_outbound_send",
]
