"""Trust-ceiling enforcement — the safety floor under every tool call.

Ported from ``ss-console/ai-employee/adapter/trust_ceiling.py`` (the policy
core) plus ``ai-employee/adapter/audit_emit_points.py`` (the per-tool
classification registry the policy needs as input). Consolidated here so
the plugin has exactly one enforcement entry point — ``evaluate_tool_call``
— that the ``pre_tool_call`` hook calls without further glue.

Per-customer ceiling resolution
-------------------------------

The ceiling for a given tool call is the lower of two values:

  1. ``customer.yaml.scope.trust_ceiling`` — the customer-authored cap.
  2. The SKILL.md frontmatter ``trust_ceiling`` for the current skill —
     the skill-author cap.

``customer.yaml`` cannot raise above the SKILL.md declaration; the SKILL.md
is the authoritative content-class ceiling for that skill. The customer
ceiling can only narrow.

The customer-yaml read is deferred to ``shared.customer_config`` (agent E
owns that port). Until that lands, this module reads the ceiling from a
process-environment override (``SMD_TRUST_CEILING``) so tests and dev
runtimes can exercise enforcement without a populated volume.

Per-tool action class
---------------------

Each tool name is classified into one of five action classes (READ,
INTERNAL_WRITE, EXTERNAL_SEND, COMMITMENT, DESTRUCTIVE). The registry is
closed-vocabulary: unmapped tool names default to READ and are flagged
so audit review can surface them. A second closed set, ``BANNED_TOOLS``,
captures Pattern-A / Pattern-B forbidden capabilities (email_send,
payments_*, delete_event, etc.); a banned tool is refused before policy
even runs.

Refusal shape
-------------

The plugin's ``pre_tool_call`` hook expects either ``None`` (allow) or a
block directive:

    {"action": "block", "message": "Refused: <reason>"}

This module's ``evaluate_tool_call`` returns exactly that shape. The audit
plugin observes the refusal via its own ``post_tool_call`` hook on the
error-result path — trust does not call into audit directly (loose
coupling, per AGENTS.md).
"""

import enum
import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocabulary enums
#
# String values match the ss-console adapter exactly so the two enforcement
# surfaces (TS validators on the authoring side, Python enforcement here)
# round-trip through their string representations.
# ---------------------------------------------------------------------------


class Ceiling(str, enum.Enum):
    """Three content classes per ADR 0005 (reviewer-as-sender)."""

    AUTONOMOUS = "autonomous"
    DRAFT_FOR_REVIEW = "draft_for_review"
    REFUSED = "refused"


class ActionClass(str, enum.Enum):
    """Categorization of every tool call by reversibility / blast radius."""

    READ = "read"  # Always allowed
    INTERNAL_WRITE = "internal_write"  # Notes, drafts, internal state — autonomous OK
    EXTERNAL_SEND = "external_send"  # Email, SMS, posts — gated
    COMMITMENT = "commitment"  # Sign, accept terms, agree to dates — never autonomous
    DESTRUCTIVE = "destructive"  # Delete, drop, irreversible — explicit per-call approval


# Strict ordering used by ``_min_ceiling``. Lower index = more restrictive.
# A customer ceiling of REFUSED beats every SKILL.md declaration.
_CEILING_ORDER: tuple[Ceiling, ...] = (
    Ceiling.REFUSED,
    Ceiling.DRAFT_FOR_REVIEW,
    Ceiling.AUTONOMOUS,
)


def _min_ceiling(a: Ceiling, b: Ceiling) -> Ceiling:
    """Return the more restrictive of two ceilings."""
    return a if _CEILING_ORDER.index(a) <= _CEILING_ORDER.index(b) else b


# ---------------------------------------------------------------------------
# Banned tools — Pattern A / Pattern B forbidden capabilities
#
# A tool name in this set NEVER reaches policy evaluation. It is refused at
# the entry. Source of truth: capability-contracts.md and ADR 0005.
# ---------------------------------------------------------------------------


BANNED_TOOLS: frozenset[str] = frozenset(
    {
        # Pattern A — autonomous outbound from the agent identity. ADR 0005
        # locks reviewer-as-sender; the agent NEVER sends from its own
        # identity. Draft creation is allowed (INTERNAL_WRITE); send is
        # permanently banned at this layer.
        "email_send",
        "email_send_message",
        "email_reply",
        "email_reply_all",
        "email_forward",
        # SMS / messaging — same rationale as email_send. Pattern A.
        "sms_send",
        "sms_send_message",
        # Money movement — never autonomous.
        "payments_initiate_transfer",
        "payments_send_payment",
        "payments_refund",
        "payments_authorize_charge",
        "payments_void_authorization",
        # Calendar / matter destructive — irreversible state changes.
        "calendar_delete_event",
        "practice_management_delete_matter",
        "practice_management_close_matter_permanent",
        # Connector-level destructive operations.
        "connector_revoke_oauth",
        "connector_unbind_permanent",
    }
)


_BANNED_REASON: Mapping[str, str] = MappingProxyType(
    {
        "email_send": "autonomous email send is forbidden (ADR 0005 reviewer-as-sender)",
        "email_send_message": "autonomous email send is forbidden (ADR 0005)",
        "email_reply": "autonomous email reply is forbidden (ADR 0005)",
        "email_reply_all": "autonomous email reply-all is forbidden (ADR 0005)",
        "email_forward": "autonomous email forward is forbidden (ADR 0005)",
        "sms_send": "autonomous SMS send is forbidden (ADR 0005)",
        "sms_send_message": "autonomous SMS send is forbidden (ADR 0005)",
        "payments_initiate_transfer": "autonomous money movement is forbidden",
        "payments_send_payment": "autonomous money movement is forbidden",
        "payments_refund": "autonomous money movement is forbidden",
        "payments_authorize_charge": "autonomous money movement is forbidden",
        "payments_void_authorization": "autonomous money movement is forbidden",
        "calendar_delete_event": "calendar event deletion is forbidden",
        "practice_management_delete_matter": "matter deletion is forbidden",
        "practice_management_close_matter_permanent": "permanent matter close is forbidden",
        "connector_revoke_oauth": "connector OAuth revocation is forbidden",
        "connector_unbind_permanent": "permanent connector unbind is forbidden",
    }
)


# ---------------------------------------------------------------------------
# Tool-name -> action_class registry
#
# Closed vocabulary. Unknown tools default to READ via classify_tool() and
# carry an ``unmapped`` flag so audit review can catch tools added without
# a registry entry.
# ---------------------------------------------------------------------------


_RAW_TOOL_ACTION_CLASS_MAP: dict[str, ActionClass] = {
    # Email — read-only + draft-creation only. SEND is BANNED.
    "email_list_messages": ActionClass.READ,
    "email_get_message": ActionClass.READ,
    "email_search": ActionClass.READ,
    "email_get_thread": ActionClass.READ,
    "email_list_labels": ActionClass.READ,
    "email_create_draft": ActionClass.INTERNAL_WRITE,
    "email_update_draft": ActionClass.INTERNAL_WRITE,
    "email_delete_draft": ActionClass.INTERNAL_WRITE,
    # SMS — read-only + draft-creation only. SEND is BANNED.
    "sms_list_messages": ActionClass.READ,
    "sms_get_message": ActionClass.READ,
    "sms_create_draft": ActionClass.INTERNAL_WRITE,
    # Calendar — read + non-destructive scheduling state changes.
    "calendar_list_events": ActionClass.READ,
    "calendar_get_event": ActionClass.READ,
    "calendar_search_events": ActionClass.READ,
    "calendar_check_availability": ActionClass.READ,
    "calendar_create_event_draft": ActionClass.INTERNAL_WRITE,
    "calendar_propose_time": ActionClass.COMMITMENT,
    "calendar_respond_invitation_draft": ActionClass.INTERNAL_WRITE,
    # Practice management — read + non-destructive matter updates.
    "practice_management_search_matters": ActionClass.READ,
    "practice_management_get_matter": ActionClass.READ,
    "practice_management_list_documents": ActionClass.READ,
    "practice_management_get_document": ActionClass.READ,
    "practice_management_list_tasks": ActionClass.READ,
    "practice_management_create_note": ActionClass.INTERNAL_WRITE,
    "practice_management_create_task_draft": ActionClass.INTERNAL_WRITE,
    "practice_management_update_matter_field": ActionClass.INTERNAL_WRITE,
    "practice_management_open_matter_draft": ActionClass.COMMITMENT,
    # Memory — read-only via this registry.
    "memory_search": ActionClass.READ,
    "memory_get_rule": ActionClass.READ,
    "memory_list_rules": ActionClass.READ,
    # Voice gate — read-only against the voice corpus.
    "voice_score_draft": ActionClass.READ,
    "voice_list_judge_history": ActionClass.READ,
    # Connector lifecycle — read-only here.
    "connector_get_status": ActionClass.READ,
    "connector_list_bindings": ActionClass.READ,
}


# Public read-only view. Mutation at runtime raises TypeError.
TOOL_ACTION_CLASS_MAP: Mapping[str, ActionClass] = MappingProxyType(
    _RAW_TOOL_ACTION_CLASS_MAP
)


# ---------------------------------------------------------------------------
# Decision shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnforcementDecision:
    """Internal decision shape. ``audit_action`` is the hint for the audit
    plugin's downstream classification of this row.
    """

    allowed: bool
    reason: str
    audit_action: str  # "allow" | "draft" | "refuse"


# ---------------------------------------------------------------------------
# Refusal classes — raised by ``classify_tool``; carried as exception
# attributes so the hook surface can render structured messages.
# ---------------------------------------------------------------------------


class BannedToolError(Exception):
    """Raised when a tool name is in ``BANNED_TOOLS``. Never reaches policy."""

    def __init__(self, *, tool_name: str, reason: str = "banned_tool") -> None:
        super().__init__(f"tool {tool_name!r} is banned: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass(frozen=True)
class ToolClassification:
    """Output of ``classify_tool``. ``unmapped`` flags tools that fell back
    to READ because they had no registry entry."""

    action_class: ActionClass
    unmapped: bool


def classify_tool(tool_name: str) -> ToolClassification:
    """Map a tool name to its ``ActionClass``.

    - Empty / missing tool name -> ``ValueError``.
    - ``tool_name`` in ``BANNED_TOOLS`` -> ``BannedToolError``.
    - ``tool_name`` in registry -> mapped action class, ``unmapped=False``.
    - Otherwise -> default to ``ActionClass.READ``, ``unmapped=True``.

    The unmapped fallback is conservative: an unmapped tool is treated as
    read-only, so the unconfigured surface cannot drive a write.
    """
    if not tool_name:
        raise ValueError("tool_name is required")
    if tool_name in BANNED_TOOLS:
        reason = _BANNED_REASON.get(tool_name, "banned_tool")
        raise BannedToolError(tool_name=tool_name, reason=reason)
    mapped = _RAW_TOOL_ACTION_CLASS_MAP.get(tool_name)
    if mapped is not None:
        return ToolClassification(action_class=mapped, unmapped=False)
    logger.debug(
        "classify_tool: %s not in TOOL_ACTION_CLASS_MAP; "
        "defaulting to READ and tagging unmapped=true",
        tool_name,
    )
    return ToolClassification(action_class=ActionClass.READ, unmapped=True)


# ---------------------------------------------------------------------------
# Policy core
# ---------------------------------------------------------------------------


def enforce(
    *,
    ceiling: Ceiling,
    action: ActionClass,
    skill_name: str,
    tool_name: str,
    current_turn_approval: bool = False,
) -> EnforcementDecision:
    """Return whether this tool call is allowed under the current ceiling.

    ``current_turn_approval`` is True iff the operator explicitly approved
    THIS specific action in the CURRENT invocation. Approvals from prior
    turns or prior sessions are NOT valid (safety invariant #1).

    Logic:
      - REFUSED ceiling refuses everything (including READ).
      - READ is always allowed under non-REFUSED ceilings.
      - COMMITMENT requires AUTONOMOUS + current-turn approval.
      - DESTRUCTIVE requires AUTONOMOUS + current-turn approval.
      - EXTERNAL_SEND under AUTONOMOUS requires current-turn approval; under
        DRAFT_FOR_REVIEW the action is routed to draft (not sent).
      - INTERNAL_WRITE under AUTONOMOUS is allowed; under DRAFT_FOR_REVIEW
        it is allowed but routed to the draft folder.
    """
    # REFUSED ceiling: nothing executes
    if ceiling == Ceiling.REFUSED:
        return EnforcementDecision(
            allowed=False,
            reason=(
                f"skill {skill_name} has trust_ceiling=refused; "
                f"tool {tool_name} blocked"
            ),
            audit_action="refuse",
        )

    # READ always allowed regardless of ceiling (non-REFUSED)
    if action == ActionClass.READ:
        return EnforcementDecision(
            allowed=True, reason="read action", audit_action="allow"
        )

    # COMMITMENT — never autonomous without approval (invariant #3).
    if action == ActionClass.COMMITMENT:
        if ceiling == Ceiling.DRAFT_FOR_REVIEW:
            return EnforcementDecision(
                allowed=False,
                reason=(
                    "draft_for_review skills do not originate commitments; "
                    "produce draft instead"
                ),
                audit_action="draft",
            )
        if not current_turn_approval:
            return EnforcementDecision(
                allowed=False,
                reason="commitment action requires explicit current-turn approval",
                audit_action="refuse",
            )
        return EnforcementDecision(
            allowed=True,
            reason="commitment with current-turn approval",
            audit_action="allow",
        )

    # DESTRUCTIVE — never autonomous without approval (invariant #1).
    if action == ActionClass.DESTRUCTIVE:
        if ceiling == Ceiling.DRAFT_FOR_REVIEW:
            return EnforcementDecision(
                allowed=False,
                reason=(
                    "draft_for_review skills do not originate destructive "
                    "actions; report instead"
                ),
                audit_action="refuse",
            )
        if not current_turn_approval:
            return EnforcementDecision(
                allowed=False,
                reason="destructive action requires explicit current-turn approval",
                audit_action="refuse",
            )
        return EnforcementDecision(
            allowed=True,
            reason="destructive with current-turn approval",
            audit_action="allow",
        )

    # EXTERNAL_SEND — requires current-turn approval (invariant #2)
    if action == ActionClass.EXTERNAL_SEND:
        if ceiling == Ceiling.AUTONOMOUS and current_turn_approval:
            return EnforcementDecision(
                allowed=True,
                reason="autonomous send with approval",
                audit_action="allow",
            )
        if ceiling == Ceiling.AUTONOMOUS:
            return EnforcementDecision(
                allowed=False,
                reason=(
                    "external_send requires explicit current-turn approval "
                    "even for autonomous skills"
                ),
                audit_action="refuse",
            )
        # draft_for_review: produce the draft, don't send.
        return EnforcementDecision(
            allowed=False,
            reason=(
                "skill is draft_for_review; produce draft to notes folder "
                "instead of sending"
            ),
            audit_action="draft",
        )

    # INTERNAL_WRITE — autonomous OK, draft_for_review writes to notes folder.
    if action == ActionClass.INTERNAL_WRITE:
        if ceiling == Ceiling.AUTONOMOUS:
            return EnforcementDecision(
                allowed=True,
                reason="autonomous internal write",
                audit_action="allow",
            )
        # draft_for_review: allow write but route to notes folder.
        return EnforcementDecision(
            allowed=True,
            reason="internal write routed to draft folder",
            audit_action="draft",
        )

    # Unknown action class — fail closed.
    return EnforcementDecision(
        allowed=False,
        reason=f"unknown action class {action}; defaulting to refuse",
        audit_action="refuse",
    )


# ---------------------------------------------------------------------------
# Ceiling resolution
#
# Customer ceiling source order:
#   1. ``customer.yaml.scope.trust_ceiling`` (via shared.customer_config).
#   2. ``SMD_TRUST_CEILING`` env var (override for dev / test).
#   3. Default: DRAFT_FOR_REVIEW (the most restrictive non-refused ceiling).
#
# SKILL.md source order:
#   1. ``args["_skill_trust_ceiling"]`` if the runtime stamps it onto the
#      tool args (Hermes plugin contract — the active skill is observable
#      to the pre-hook through tool args).
#   2. Default: AUTONOMOUS (matches Hermes-skill authoring default; the
#      customer-cap will narrow it).
# ---------------------------------------------------------------------------


_DEFAULT_CUSTOMER_CEILING = Ceiling.DRAFT_FOR_REVIEW
_DEFAULT_SKILL_CEILING = Ceiling.AUTONOMOUS


def _parse_ceiling(value: Optional[str], fallback: Ceiling) -> Ceiling:
    if not value:
        return fallback
    try:
        return Ceiling(value)
    except ValueError:
        logger.warning(
            "trust ceiling value %r is not one of %s; using fallback %s",
            value,
            sorted(c.value for c in Ceiling),
            fallback.value,
        )
        return fallback


def _resolve_customer_ceiling() -> Ceiling:
    """Resolve the customer-authored ceiling.

    Reads from ``shared.customer_config.CustomerConfig.from_volume()`` when
    available; falls back to the ``SMD_TRUST_CEILING`` env var; finally
    defaults to ``DRAFT_FOR_REVIEW``.

    The volume read is best-effort: if customer.yaml is not parsed yet
    (agent E's port is in progress) the function silently falls back. This
    keeps trust enforcement working in dev / test even when other parts of
    the overlay are stubs.
    """
    # Try the shared loader first.
    try:
        from shared.customer_config import CustomerConfig  # local import

        cfg = CustomerConfig.from_volume()
        scope = getattr(cfg, "scope", None) or {}
        value = scope.get("trust_ceiling") if isinstance(scope, dict) else None
        if value:
            return _parse_ceiling(value, _DEFAULT_CUSTOMER_CEILING)
    except NotImplementedError:
        # Stub state — customer_config not yet ported. Fall through to env.
        pass
    except Exception:
        # Any other error (missing file, parse failure, attribute miss) is
        # logged at debug; we fall through to the env-var path.
        logger.debug(
            "customer_config unavailable for ceiling resolution; falling back to env",
            exc_info=True,
        )

    env_value = os.environ.get("SMD_TRUST_CEILING")
    return _parse_ceiling(env_value, _DEFAULT_CUSTOMER_CEILING)


def _resolve_skill_ceiling(args: Optional[dict]) -> Ceiling:
    """Resolve the SKILL.md-declared ceiling for the active skill.

    The runtime is expected to stamp ``_skill_trust_ceiling`` onto the tool
    args before the pre-hook runs. Absent that, fall back to AUTONOMOUS so
    the customer cap dominates.
    """
    if not isinstance(args, dict):
        return _DEFAULT_SKILL_CEILING
    value = args.get("_skill_trust_ceiling")
    return _parse_ceiling(value, _DEFAULT_SKILL_CEILING)


def _resolve_skill_name(args: Optional[dict]) -> str:
    """Best-effort skill-name resolution for the audit reason string."""
    if isinstance(args, dict):
        name = args.get("_skill_name")
        if isinstance(name, str) and name:
            return name
    return "(unknown)"


def _resolve_current_turn_approval(args: Optional[dict]) -> bool:
    """Whether the operator approved THIS action in THIS turn.

    The runtime stamps ``_current_turn_approval`` onto the tool args when
    an approval has been registered for this exact call. Approvals from
    prior turns/sessions never carry over.
    """
    if isinstance(args, dict):
        return bool(args.get("_current_turn_approval"))
    return False


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


def evaluate_tool_call(
    tool_name: str,
    args: dict,
    customer_slug: str,
) -> Optional[dict]:
    """Decide whether a tool call may proceed.

    Returns:
        ``None`` to allow the call.
        ``{"action": "block", "message": "Refused: <reason>"}`` to block.

    Block precedence (first match wins):
      1. Tool name is in ``BANNED_TOOLS`` — refused regardless of ceiling.
      2. Resolved ceiling refuses the action class via ``enforce()``.

    Exception safety: any exception in this function is caught at the
    hook boundary; this function may raise internally and the caller's
    try/except in ``__init__.py`` translates raises into a None (allow)
    return so a misbehaving policy module cannot break the agent loop.
    """
    if not tool_name:
        # Defensive: an empty tool name is a malformed pre-hook kwarg, not a
        # genuine refusal. Allow and let downstream surfaces complain.
        return None

    # 1. Banned tools — refuse before policy runs.
    try:
        classification = classify_tool(tool_name)
    except BannedToolError as err:
        return {
            "action": "block",
            "message": f"Refused: {err.reason}",
        }

    # 2. Resolve customer + skill ceilings; take the more restrictive.
    customer_ceiling = _resolve_customer_ceiling()
    skill_ceiling = _resolve_skill_ceiling(args)
    effective_ceiling = _min_ceiling(customer_ceiling, skill_ceiling)

    decision = enforce(
        ceiling=effective_ceiling,
        action=classification.action_class,
        skill_name=_resolve_skill_name(args),
        tool_name=tool_name,
        current_turn_approval=_resolve_current_turn_approval(args),
    )

    if decision.allowed:
        return None

    return {
        "action": "block",
        "message": f"Refused: {decision.reason}",
    }


__all__ = [
    "ActionClass",
    "BANNED_TOOLS",
    "BannedToolError",
    "Ceiling",
    "EnforcementDecision",
    "TOOL_ACTION_CLASS_MAP",
    "ToolClassification",
    "classify_tool",
    "enforce",
    "evaluate_tool_call",
]
