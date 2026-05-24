"""Audit row shape and accepted action_type vocabulary.

Ported from ss-console/ai-employee/adapter/audit_log.py (the ACCEPTED_ACTION_TYPES
frozenset + ActorRole enum + AuditEvent dataclass).

The tool-action-class vocabulary (``ActionClass``, ``BANNED_TOOLS``,
``BANNED_REASON``, ``TOOL_ACTION_CLASS_MAP``, ``BannedToolError``,
``ToolClassification``, ``classify_tool``) lives in ``shared.action_classes``
— the audit and trust plugins both import from there to keep one source of
truth (consolidation: task #33). ``HookActionClass`` is preserved here as a
deprecated alias for ``ActionClass`` so downstream audit consumers
(``emit.py``, ``test_audit_emit.py``) keep working without churn.

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
from typing import Optional

from shared.action_classes import (
    BANNED_REASON,
    BANNED_TOOLS,
    TOOL_ACTION_CLASS_MAP,
    ActionClass,
)


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
# Deprecated alias for the action-class enum.
#
# The vocabulary moved to ``shared.action_classes.ActionClass`` (task #33
# consolidation). ``HookActionClass`` is retained as a module-level alias
# so existing audit-side consumers (``emit.py``, ``test_audit_emit.py``,
# anything that imports ``schemas.HookActionClass``) keep working without
# touching every call site. New code should import ``ActionClass`` directly
# from ``shared.action_classes``.
# ---------------------------------------------------------------------------


HookActionClass = ActionClass


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


# Scope keys lifted into per-tool audit metadata.
SCOPE_KEYS: tuple[str, ...] = ("matter_id", "customer_segment")


__all__ = [
    "ACCEPTED_ACTION_TYPES",
    "ActionClass",
    "ActorRole",
    "AuditEvent",
    "BANNED_REASON",
    "BANNED_TOOLS",
    "HookActionClass",
    "SCOPE_KEYS",
    "TOOL_ACTION_CLASS_MAP",
]
