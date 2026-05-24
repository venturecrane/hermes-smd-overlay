"""Audit row shape, accepted action_type vocabulary, and tool registry.

Ported from ss-console/ai-employee/adapter/audit_log.py (the ACCEPTED_ACTION_TYPES
frozenset + ActorRole enum + AuditEvent dataclass) and from
ss-console/ai-employee/adapter/audit_emit_points.py (the BANNED_TOOLS set, the
TOOL_ACTION_CLASS_MAP registry, and the HookActionClass enum that the registry
keys against).

GEPA removal
------------

The original ACCEPTED_ACTION_TYPES carried ``GEPA_DISABLED_VERIFIED`` to record
boot-time evidence that the GEPA self-evolution subsystem was disabled inside
each customer Machine. ADR 0018 is superseded by ADR 0015 (pin-only fork posture)
and the May 2026 architectural realignment: GEPA does not exist in upstream
Hermes, so there is nothing to disable, no boot-check to emit, and no need for
the audit row. The action_type is removed here. Boot-checks for non-existent
subsystems were a P0 doctrine bug; not carrying the action_type forward closes
the doctrine surface.

Audit table column shape (mirrors the per-customer D1 schema):

    id            TEXT PRIMARY KEY    -- ULID, sortable by time
    ts            TEXT NOT NULL       -- ISO 8601 UTC with millisecond precision
    action_type   TEXT NOT NULL       -- one of ACCEPTED_ACTION_TYPES
    actor         TEXT NOT NULL       -- 'agent' | 'captain' | person_mappings.id
    actor_role    TEXT                -- ActorRole value (string for forward-compat)
    skill_name    TEXT                -- originating skill identifier
    matter_ref    TEXT                -- opaque per-vertical reference
    input_digest  TEXT                -- SHA-256 of caller-supplied input bytes
    output_digest TEXT                -- SHA-256 of caller-supplied output bytes
    diff_digest   TEXT                -- SHA-256 of caller-supplied diff bytes
    trust_ceiling TEXT                -- skill ceiling at action time
    metadata      TEXT                -- canonical JSON dict (sort_keys, no whitespace)
"""

import enum
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional


# ---------------------------------------------------------------------------
# Accepted action_type vocabulary
#
# Adding or removing an action type means updating any fabrication-filter or
# compliance-evidence-packet consumers in the same PR; the dashboard surfaces
# unknown action_types as warnings.
# ---------------------------------------------------------------------------


ACCEPTED_ACTION_TYPES: frozenset[str] = frozenset(
    {
        # Draft lifecycle
        "DRAFT_CREATED",
        "DRAFT_APPROVED",
        "DRAFT_REJECTED",
        "DRAFT_EXPIRED",
        # Memory rules
        "MEMORY_RULE_ADDED",
        "MEMORY_RULE_EDITED",
        "MEMORY_RULE_DELETED",
        # Trust ceiling
        "TRUST_PROMOTED",
        "TRUST_DEMOTED",
        # Skill activation
        "SKILL_ENABLED",
        "SKILL_DISABLED",
        # Agent lifecycle
        "AGENT_STOPPED",
        "AGENT_RESUMED",
        # Connector lifecycle
        "CONNECTOR_BOUND",
        "CONNECTOR_UNBOUND",
        "CONNECTOR_AUTH_EXPIRED",
        "CONNECTOR_AUTH_RESTORED",
        "CONNECTOR_TOKEN_REFRESHED",
        "CONNECTOR_HEALTH_PROBE_FAILED",
        # Scope changes
        "SCOPE_CHANGED",
        # Sent-folder watching
        "SENT_DETECTED",
        "SENT_DIFF_INDEXED",
        # Safety substrate
        "INVARIANT_VIOLATION",
        "INVARIANT_BOOT_CHECK_FAILED",
        # RBAC and compliance
        "RBAC_EVENT",
        "COMPLIANCE_PACKET_EXPORTED",
        # Voice gate
        "VOICE_GATE_PASSED",
        "VOICE_GATE_NEAR_PASS",
        "VOICE_GATE_FAILED",
        # Fabrication and escalation
        "FABRICATION_FILTER_TRIGGERED",
        "ESCALATION_FIRED",
        "ESCALATION_ACKNOWLEDGED",
        # Decommission lifecycle
        "DECOMMISSION_INITIATED",
        "DECOMMISSION_DRAIN_COMPLETE",
        "DECOMMISSION_FINAL",
        # Honcho overlay (ADR 0016) — proposer-only persona observations.
        "HONCHO_OBSERVATION",
        "HONCHO_PROMOTION",
        "HONCHO_DISMISSAL",
        # Skill Curator overlay (ADR 0017) — observer-only skill drafts.
        "CURATOR_DRAFT",
        "CURATOR_PROMOTION",
        "CURATOR_DISMISSAL",
        # LLM-turn audit emitted by the post_llm_call hook.
        "LLM_TURN_COMPLETED",
        # Per-tool audit emitted by the post_tool_call hook.
        "TOOL_CALL_COMPLETED",
    }
)


class ActorRole(str, enum.Enum):
    """Caller's role at the time of the audited action."""

    PRINCIPAL = "principal"
    OPERATOR = "operator"
    COMPLIANCE = "compliance"
    AGENT = "agent"
    CAPTAIN = "captain"


# ---------------------------------------------------------------------------
# Hook action classes — the trust-ceiling enforcer keys on these.
#
# Ported from the original adapter.hermes_hook module so the audit plugin can
# tag every per-tool row with the action class the substrate considers the
# tool to belong to. The trust-ceiling plugin (hermes-smd-trust) owns the
# enforcement decision; this module owns the vocabulary.
# ---------------------------------------------------------------------------


class HookActionClass(str, enum.Enum):
    """Closed vocabulary of action classes a tool call can belong to."""

    READ = "read"
    INTERNAL_WRITE = "internal_write"
    COMMITMENT = "commitment"
    EXTERNAL_SEND = "external_send"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class AuditEvent:
    """Strongly-typed event payload accepted by the emission helpers.

    Required:
        action_type — one of ACCEPTED_ACTION_TYPES
        actor       — 'agent' | 'captain' | person_mappings.id

    Optional:
        actor_role     — ActorRole enum (or string for forward-compat)
        skill_name     — name of the skill that originated the action
        matter_ref     — opaque per-vertical reference (matter id, lead id)
        input_payload  — bytes to digest; never stored
        output_payload — bytes to digest; never stored
        diff_payload   — bytes to digest; never stored
        trust_ceiling  — value of the skill's trust_ceiling at action time
        metadata       — JSON-serializable dict; merged then json.dumps()ed
    """

    action_type: str
    actor: str
    actor_role: Optional[ActorRole] = None
    skill_name: Optional[str] = None
    matter_ref: Optional[str] = None
    input_payload: Optional[bytes] = None
    output_payload: Optional[bytes] = None
    diff_payload: Optional[bytes] = None
    trust_ceiling: Optional[str] = None
    metadata: Optional[dict] = field(default=None)


# ---------------------------------------------------------------------------
# Banned tools - Pattern A / Pattern B forbidden capabilities
#
# A tool name in this set NEVER reaches trust-ceiling enforcement. The
# `classify_tool()` helper in emit.py raises ``BannedToolError`` immediately;
# the overlay's dispatch path translates that into a refusal audit row.
# ---------------------------------------------------------------------------


BANNED_TOOLS: frozenset[str] = frozenset(
    {
        # Pattern A - autonomous outbound from the agent identity. ADR 0005
        # locks reviewer-as-sender; the agent NEVER sends from its own
        # identity. The draft-creation path is allowed (DRAFT_CREATE);
        # the send path is permanently banned at this layer.
        "email_send",
        "email_send_message",
        "email_reply",
        "email_reply_all",
        "email_forward",
        # SMS / messaging - same rationale as email_send. Pattern A.
        "sms_send",
        "sms_send_message",
        # Money movement - never autonomous.
        "payments_initiate_transfer",
        "payments_send_payment",
        "payments_refund",
        "payments_authorize_charge",
        "payments_void_authorization",
        # Calendar / matter destructive - irreversible state changes.
        "calendar_delete_event",
        "practice_management_delete_matter",
        "practice_management_close_matter_permanent",
        # Connector-level destructive operations.
        "connector_revoke_oauth",
        "connector_unbind_permanent",
    }
)


# Reason classification for BANNED tool names. The dispatch path uses this to
# render a more specific customer message ("autonomous send is disabled" vs
# "destructive operation is disabled") without needing a second lookup.
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
# Tool-name -> action_class registry
#
# Keys: every tool name the v1 capability surface exposes (read /
# internal-write / commitment). Email send + money movement are DELIBERATELY
# ABSENT from this map and present in BANNED_TOOLS instead - adding them here
# is a P0 doctrine violation.
# ---------------------------------------------------------------------------


_RAW_TOOL_ACTION_CLASS_MAP: dict[str, HookActionClass] = {
    # Email - read-only + draft-creation only. SEND is BANNED.
    "email_list_messages": HookActionClass.READ,
    "email_get_message": HookActionClass.READ,
    "email_search": HookActionClass.READ,
    "email_get_thread": HookActionClass.READ,
    "email_list_labels": HookActionClass.READ,
    "email_create_draft": HookActionClass.INTERNAL_WRITE,
    "email_update_draft": HookActionClass.INTERNAL_WRITE,
    "email_delete_draft": HookActionClass.INTERNAL_WRITE,
    # SMS - read-only + draft-creation only. SEND is BANNED.
    "sms_list_messages": HookActionClass.READ,
    "sms_get_message": HookActionClass.READ,
    "sms_create_draft": HookActionClass.INTERNAL_WRITE,
    # Calendar - read + non-destructive scheduling state changes.
    "calendar_list_events": HookActionClass.READ,
    "calendar_get_event": HookActionClass.READ,
    "calendar_search_events": HookActionClass.READ,
    "calendar_check_availability": HookActionClass.READ,
    "calendar_create_event_draft": HookActionClass.INTERNAL_WRITE,
    "calendar_propose_time": HookActionClass.COMMITMENT,
    "calendar_respond_invitation_draft": HookActionClass.INTERNAL_WRITE,
    # Practice management - read + non-destructive matter updates.
    "practice_management_search_matters": HookActionClass.READ,
    "practice_management_get_matter": HookActionClass.READ,
    "practice_management_list_documents": HookActionClass.READ,
    "practice_management_get_document": HookActionClass.READ,
    "practice_management_list_tasks": HookActionClass.READ,
    "practice_management_create_note": HookActionClass.INTERNAL_WRITE,
    "practice_management_create_task_draft": HookActionClass.INTERNAL_WRITE,
    "practice_management_update_matter_field": HookActionClass.INTERNAL_WRITE,
    "practice_management_open_matter_draft": HookActionClass.COMMITMENT,
    # Memory - read-only via this registry.
    "memory_search": HookActionClass.READ,
    "memory_get_rule": HookActionClass.READ,
    "memory_list_rules": HookActionClass.READ,
    # Voice gate - read-only against the voice corpus.
    "voice_score_draft": HookActionClass.READ,
    "voice_list_judge_history": HookActionClass.READ,
    # Connector lifecycle - read-only here.
    "connector_get_status": HookActionClass.READ,
    "connector_list_bindings": HookActionClass.READ,
}


# Public read-only view. Callers must not mutate the registry at runtime;
# changes ship as a PR + test + spec update. MappingProxyType raises
# TypeError on any mutation attempt, making the constraint enforceable.
TOOL_ACTION_CLASS_MAP: Mapping[str, HookActionClass] = MappingProxyType(
    _RAW_TOOL_ACTION_CLASS_MAP
)


# Scope keys lifted into per-tool audit metadata.
SCOPE_KEYS: tuple[str, ...] = ("matter_id", "customer_segment")


__all__ = [
    "ACCEPTED_ACTION_TYPES",
    "ActorRole",
    "AuditEvent",
    "BANNED_REASON",
    "BANNED_TOOLS",
    "HookActionClass",
    "SCOPE_KEYS",
    "TOOL_ACTION_CLASS_MAP",
]
