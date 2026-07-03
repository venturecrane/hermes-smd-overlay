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
from typing import Any

from shared import identifier_filter, provenance
from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
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
# Only ``email_delete_draft`` is excluded entirely — a delete authors nothing.
#
# The sets are data-driven from the registry (so a new INTERNAL_WRITE tool is
# at least body-optional-gated by default, never silently un-gated); the
# body-required and excluded lists are the only hand-maintained surfaces and a
# test pins them.
# ---------------------------------------------------------------------------


# INTERNAL_WRITE tools that author NO content — excluded from the gate entirely.
_NON_AUTHORING_INTERNAL_WRITE: frozenset[str] = frozenset(
    {
        "email_delete_draft",  # delete — nothing authored to scan
    }
)

# Gated tools that MUST carry a prose body. A missing body on these is a
# fail-closed BLOCK (a create-draft / create-note with no body is malformed).
_BODY_REQUIRED_DRAFT_TOOLS: frozenset[str] = frozenset(
    {
        "email_create_draft",
        "email_update_draft",
        "sms_create_draft",
        "practice_management_create_note",
    }
)


def _is_gated_draft_tool(tool_name: str) -> bool:
    """True iff ``tool_name`` is a prose-bearing INTERNAL_WRITE draft tool."""
    if not tool_name:
        return False
    action = TOOL_ACTION_CLASS_MAP.get(tool_name)
    if action is not ActionClass.INTERNAL_WRITE:
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
            metadata["tool_call_id"] = tool_call_id

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
# A1 identifier-integrity gate — REPORT-ONLY (never blocks)
#
# Distinct from the blocking Tier-1/Tier-2 fabrication gate above. After a draft
# body clears that gate, scan it for identifier-shaped tokens (dates, A-numbers,
# receipts, SSNs, case numbers) NOT in this session's provenance register — i.e.
# an identifier the agent composed without READING it from a source. That is the
# runtime signature of a fabricated/garbled identifier, and the never-computes
# backstop for a computed legal date (a computed SOL won't be in the register).
#
# REPORT-ONLY: emits an IDENTIFIER_UNVERIFIED audit signal and ALLOWS the draft.
# It never blocks — a mismatched identifier is what a human reviewer should SEE
# (these are draft_for_review tools; the draft already reaches a human). Names
# are excluded (the runtime register holds none — structured-name seeding is a
# follow-on). Enforcement (report -> flag) flips only after the false-positive
# rate is measured on real traffic (the plan's tune-on-traffic discipline).
# ---------------------------------------------------------------------------


def _report_identifiers(
    *,
    body: str,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    vertical: str | None,
    cohort: str | None,
) -> None:
    """Report (never block) outbound identifiers not traceable to a session read.

    Best-effort: any failure is swallowed — the report must never perturb the
    allowed draft path or raise out of the hook.
    """
    try:
        register = provenance.register_for(session_id)
        result = identifier_filter.check(body, register)  # mode=REPORT default
        unverified = [h for h in result.unverified if h.kind is not identifier_filter.IdKind.NAME]
        if not unverified:
            return
        client, slug = _audit_client()
        if client is None or slug is None:
            _NO_AUDIT_WARNER.warn(logger, f"IDENTIFIER_UNVERIFIED on tool={tool_name} not recorded")
            return
        by_kind: dict[str, int] = {}
        for h in unverified:
            by_kind[h.kind.value] = by_kind.get(h.kind.value, 0) + 1
        metadata: dict = {
            "gate_tier": "tier3_identifier",
            "mode": "report",
            "customer": slug,
            "tool": tool_name,
            "vertical": vertical or "(unknown)",
            "register_was_empty": result.register_was_empty,
            "unverified_counts": by_kind,
            # redacted shapes only — never the raw identifier value
            "shapes": sorted({identifier_filter._redact(h) for h in unverified}),
        }
        if cohort:
            metadata["cohort"] = cohort
        if session_id:
            metadata["session_id"] = session_id
        if tool_call_id:
            metadata["tool_call_id"] = tool_call_id
        params = agent_event_params(action_type="IDENTIFIER_UNVERIFIED", metadata=metadata)
        client.execute(_INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001 — report is best-effort; never block the allow
        logger.debug(
            "outbound gate: identifier report failed (tool=%s err=%s); draft still allowed",
            tool_name,
            exc,
        )


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
            # Body-optional tool with no prose body (structured-only call:
            # a calendar time, a matter-field date). No fabrication surface —
            # allow rather than brick a legitimate structured operation.
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
    decision = evaluate(body, cohort, vertical)
    if decision.allowed:
        # A1 report-only identifier gate: signal (never block) any identifier in
        # the body not traceable to a source read this session.
        _report_identifiers(
            body=body,
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
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
    "content",
    "message",
    "note",
)


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
    body = _extract_send_body(args)
    if not body.strip():
        return None
    vertical = _resolve_vertical()
    cohort = _resolve_cohort()
    decision = evaluate(body, cohort, vertical)
    if decision.allowed:
        _report_identifiers(
            body=body,
            session_id=session_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            vertical=vertical,
            cohort=cohort,
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


__all__ = [
    "GATED_DRAFT_TOOLS",
    "check_outbound_draft",
    "check_outbound_send",
]
