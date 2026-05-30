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
        # Subagent lifecycle emitted by the subagent_stop hook
        # (ADR 0021 Stream C — one row per delegated child).
        "SUBAGENT_STOPPED",
        # Parent-side refusal when a subagent return fails the assembly-time
        # schema contract (ADR 0021 Stream C — emitted by the ss-console
        # delegate_task skills; the overlay accepts the action type so the
        # parent's row writes through the same per-customer D1 binding).
        "SUBAGENT_INCOMPLETE",
        # Agent-authored skill creation observation emitted by post_tool_call
        # when the dispatched tool is `skill_manage` (ADR 0017 §40 — mirror-
        # don't-gate observation of the Hermes-native Skill Curator surface).
        "AGENT_SKILL_CREATED",
        # Inbound webhook routed to a skill by hermes-smd-webhook-router
        # (ADR 0021 Stream E). One row per successful route; the payload
        # source + event_type land in metadata. Observation only; the
        # router does not gate dispatch.
        "WEBHOOK_ROUTED",
        # Untrusted inbound content received + attributed by
        # hermes-smd-webhook-router (ADR 0027 inbound convergence). One row per
        # dispatched inbound item; the provenance envelope (item_id,
        # trust_class, source, surface, verification, content_digest) lands in
        # metadata — never the content itself. Canonical type added ss-console-
        # side by PR-B; mirrored here so the overlay's AuditLogWriter accepts it.
        "INBOUND_RECEIVED",
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
# Agent-authored skill inventory — ADR 0022 Stream 2 (skill body persistence)
#
# Mirrors ss-console/ai-employee/migrations/0008 + 0009 combined. Lives in
# the per-customer D1 audit binding (SMD_D1_AUDIT_BINDING). The audit
# plugin owns the writes; skill_capture.py implements the write-ahead
# pattern (D1 row first with r2_status='pending', R2 PUT follows, UPDATE
# to persisted/failed). Boot-time reconciler retries pending/failed rows.
#
# Schema is the authoritative copy from
# docs/specs/ai-employee/skill-body-persistence.md (ss-console).
# Idempotent CREATE TABLE IF NOT EXISTS / ALTER on each Machine boot so
# the schema converges without a separate migration tool.
# ---------------------------------------------------------------------------


AGENT_SKILLS_INVENTORY_DDL: str = """
CREATE TABLE IF NOT EXISTS agent_skills_inventory (
    customer_slug       TEXT NOT NULL,
    persona_slug        TEXT NOT NULL,
    skill_name          TEXT NOT NULL,
    skill_content_hash  TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    source_turn_id      TEXT NOT NULL,
    archived_at         TEXT,
    archived_reason     TEXT,
    removed_at          TEXT,
    removed_by          TEXT,
    r2_key              TEXT,
    r2_status           TEXT NOT NULL DEFAULT 'unknown'
        CHECK (r2_status IN ('unknown', 'pending', 'persisted', 'failed')),
    r2_write_error      TEXT,
    PRIMARY KEY (customer_slug, persona_slug, skill_name, skill_content_hash)
);
"""

AGENT_SKILLS_INVENTORY_ACTIVE_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS agent_skills_inventory_active
    ON agent_skills_inventory (persona_slug, created_at)
    WHERE archived_at IS NULL AND removed_at IS NULL;
"""

AGENT_SKILLS_INVENTORY_BY_PERSONA_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS agent_skills_inventory_by_persona
    ON agent_skills_inventory (persona_slug, created_at);
"""

AGENT_SKILLS_INVENTORY_BY_HASH_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS agent_skills_inventory_by_hash
    ON agent_skills_inventory (skill_content_hash);
"""

AGENT_SKILLS_INVENTORY_R2_PENDING_INDEX_DDL: str = """
CREATE INDEX IF NOT EXISTS agent_skills_inventory_r2_pending
    ON agent_skills_inventory (r2_status, created_at)
    WHERE r2_status IN ('pending', 'failed');
"""


AUDIT_PLUGIN_DDLS: tuple[str, ...] = (
    AGENT_SKILLS_INVENTORY_DDL,
    AGENT_SKILLS_INVENTORY_ACTIVE_INDEX_DDL,
    AGENT_SKILLS_INVENTORY_BY_PERSONA_INDEX_DDL,
    AGENT_SKILLS_INVENTORY_BY_HASH_INDEX_DDL,
    AGENT_SKILLS_INVENTORY_R2_PENDING_INDEX_DDL,
)


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
    actor_role: ActorRole | None = None
    skill_name: str | None = None
    matter_ref: str | None = None
    input_payload: bytes | None = None
    output_payload: bytes | None = None
    diff_payload: bytes | None = None
    trust_ceiling: str | None = None
    metadata: dict | None = field(default=None)


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
