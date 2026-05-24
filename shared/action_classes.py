"""Action-class vocabulary shared by the audit and trust plugins.

Both ``hermes-smd-audit`` and ``hermes-smd-trust`` need the same closed-vocabulary
view of every Hermes tool call: the action class (read / internal_write /
external_send / commitment / destructive), the banned-tool set (Pattern A / B
forbidden capabilities), and the tool-name → action-class registry. This module
is the single source of truth.

Layering:

  - The plugins import these names. Plugins never redefine them. Drift between
    plugins is the failure mode this consolidation prevents (filed as task #33,
    follow-on to the §7 adapter port).
  - Tests assert the registry is closed-vocabulary, disjoint from
    ``BANNED_TOOLS``, and runtime-immutable (``MappingProxyType`` raises
    ``TypeError`` on any mutation attempt).
  - Audit tags every per-tool row with the action class; trust uses the action
    class to decide whether the call clears the resolved ceiling.

Banned reasons:

  ``BANNED_REASON`` maps each banned tool name to a closed-vocabulary category
  code (e.g. ``"banned_tool_pattern_a"``, ``"banned_tool_destructive"``). This
  is the substrate-level classification — audit rows persist it verbatim in
  ``metadata.banned_reason``. The trust plugin renders its own user-visible
  refusal sentence at the policy boundary; it does NOT consume the categorical
  code as message text.
"""

import enum
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocabulary action class
#
# String values match the ss-console adapter exactly so the two enforcement
# surfaces (TS validators on the authoring side, Python enforcement here)
# round-trip through their string representations.
# ---------------------------------------------------------------------------


class ActionClass(str, enum.Enum):
    """Categorization of every tool call by reversibility / blast radius."""

    READ = "read"  # Always allowed
    INTERNAL_WRITE = "internal_write"  # Notes, drafts, internal state — autonomous OK
    EXTERNAL_SEND = "external_send"  # Email, SMS, posts — gated
    COMMITMENT = "commitment"  # Sign, accept terms, agree to dates — never autonomous
    DESTRUCTIVE = "destructive"  # Delete, drop, irreversible — explicit per-call approval


# ---------------------------------------------------------------------------
# Banned tools — Pattern A / Pattern B forbidden capabilities
#
# A tool name in this set NEVER reaches trust-ceiling enforcement. The
# ``classify_tool()`` helper raises ``BannedToolError`` immediately; both
# plugins translate that into a refusal at their respective seams.
# ---------------------------------------------------------------------------


BANNED_TOOLS: frozenset[str] = frozenset(
    {
        # Pattern A — autonomous outbound from the agent identity. ADR 0005
        # locks reviewer-as-sender; the agent NEVER sends from its own
        # identity. The draft-creation path is allowed (DRAFT_CREATE);
        # the send path is permanently banned at this layer.
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


# Reason classification for BANNED tool names. Closed vocabulary of category
# codes. The audit plugin persists this verbatim in ``metadata.banned_reason``;
# the trust plugin renders its own sentence-form message at the policy
# boundary and does not consume this code as message text.
BANNED_REASON: Mapping[str, str] = MappingProxyType(
    {
        "email_send": "banned_tool_pattern_a",
        "email_send_message": "banned_tool_pattern_a",
        "email_reply": "banned_tool_pattern_a",
        "email_reply_all": "banned_tool_pattern_a",
        "email_forward": "banned_tool_pattern_a",
        "sms_send": "banned_tool_pattern_a",
        "sms_send_message": "banned_tool_pattern_a",
        "payments_initiate_transfer": "banned_tool_destructive",
        "payments_send_payment": "banned_tool_destructive",
        "payments_refund": "banned_tool_destructive",
        "payments_authorize_charge": "banned_tool_destructive",
        "payments_void_authorization": "banned_tool_destructive",
        "calendar_delete_event": "banned_tool_destructive",
        "practice_management_delete_matter": "banned_tool_destructive",
        "practice_management_close_matter_permanent": "banned_tool_destructive",
        "connector_revoke_oauth": "banned_tool_destructive",
        "connector_unbind_permanent": "banned_tool_destructive",
    }
)


# ---------------------------------------------------------------------------
# Tool-name → action_class registry
#
# Keys: every tool name the v1 capability surface exposes (read /
# internal-write / commitment). Email send + money movement are DELIBERATELY
# ABSENT from this map and present in BANNED_TOOLS instead — adding them here
# is a P0 doctrine violation.
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


# Public read-only view. Callers must not mutate the registry at runtime;
# changes ship as a PR + test + spec update. MappingProxyType raises
# TypeError on any mutation attempt, making the constraint enforceable.
TOOL_ACTION_CLASS_MAP: Mapping[str, ActionClass] = MappingProxyType(
    _RAW_TOOL_ACTION_CLASS_MAP
)


# ---------------------------------------------------------------------------
# Refusal types
# ---------------------------------------------------------------------------


class BannedToolError(Exception):
    """Raised when a tool name appears in ``BANNED_TOOLS``.

    Never reaches policy. The dispatch path catches this and translates to a
    refusal audit row via the per-tool emit helper. ``tool_name`` carries the
    offending name for metadata; ``reason`` is the closed-set category code
    from ``BANNED_REASON`` (``"banned_tool_pattern_a"`` /
    ``"banned_tool_destructive"``).
    """

    def __init__(self, *, tool_name: str, reason: str = "banned_tool") -> None:
        super().__init__(f"tool {tool_name!r} is banned: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass(frozen=True)
class ToolClassification:
    """Outcome of ``classify_tool()``.

    ``action_class`` is the action class the trust-ceiling enforcer should
    use for this tool call. ``unmapped`` is True if the tool name was not
    in ``TOOL_ACTION_CLASS_MAP`` (the helper returned the READ default).
    """

    action_class: ActionClass
    unmapped: bool


def classify_tool(tool_name: str) -> ToolClassification:
    """Map a tool name to its ``ActionClass``.

    - Empty / missing tool name → ``ValueError``.
    - ``tool_name`` in ``BANNED_TOOLS`` → ``BannedToolError`` (the exception
      carries the categorical ``reason`` code from ``BANNED_REASON``).
    - ``tool_name`` in registry → mapped action class, ``unmapped=False``.
    - Otherwise → default to ``ActionClass.READ``, ``unmapped=True``.

    The unmapped fallback is conservative: an unmapped tool is treated as
    read-only, so the unconfigured surface cannot drive a write.
    """
    if not tool_name:
        raise ValueError("tool_name is required")

    if tool_name in BANNED_TOOLS:
        reason = BANNED_REASON.get(tool_name, "banned_tool")
        raise BannedToolError(tool_name=tool_name, reason=reason)

    mapped = _RAW_TOOL_ACTION_CLASS_MAP.get(tool_name)
    if mapped is not None:
        return ToolClassification(action_class=mapped, unmapped=False)

    logger.warning(
        "classify_tool: tool_name=%s not in TOOL_ACTION_CLASS_MAP; "
        "defaulting to READ and tagging metadata.unmapped_tool=true",
        tool_name,
    )
    return ToolClassification(action_class=ActionClass.READ, unmapped=True)


__all__ = [
    "ActionClass",
    "BANNED_REASON",
    "BANNED_TOOLS",
    "BannedToolError",
    "TOOL_ACTION_CLASS_MAP",
    "ToolClassification",
    "classify_tool",
]
