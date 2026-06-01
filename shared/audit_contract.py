"""The audit_log row contract — one source for SQL + column order.

Previously the ``INSERT INTO audit_log`` statement and its 12-value parameter
tuple were hand-copied into three plugins (hermes-smd-audit/emit.py,
hermes-smd-webhook-router/__init__.py, hermes-smd-trust/outbound.py). A column
reorder in one place silently corrupted the others. This module is the single
definition; all writers build their row through ``build_audit_params`` so the
positional tuple can never drift from ``INSERT_SQL``.

The canonical audit_log schema lives ss-console-side in
``docs/specs/operator/d1-schema.md``; ``COLUMNS`` here mirrors it and is
pinned by the schema-snapshot CI guard.
"""

from __future__ import annotations

import json
from typing import Any

from shared.ids import iso_utc, sha256, ulid

# Column order is the contract. Keep in lockstep with d1-schema.md and the
# schema-snapshot test. The VALUES placeholder count is derived from this.
COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "action_type",
    "actor",
    "actor_role",
    "skill_name",
    "matter_ref",
    "input_digest",
    "output_digest",
    "diff_digest",
    "trust_ceiling",
    "metadata",
)

INSERT_SQL = (
    "INSERT INTO audit_log (" + ", ".join(COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")"
)


def _dumps(metadata: dict | None) -> str | None:
    """Deterministic metadata serialization (sorted keys, no whitespace).

    Returns ``None`` for empty/absent metadata so the column stores SQL NULL.
    """
    if not metadata:
        return None
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"))


def build_audit_params(
    *,
    row_id: str,
    ts: str,
    action_type: str,
    actor: str | None = None,
    actor_role: str | None = None,
    skill_name: str | None = None,
    matter_ref: str | None = None,
    input_digest: str | None = None,
    output_digest: str | None = None,
    diff_digest: str | None = None,
    trust_ceiling: str | None = None,
    metadata: dict | None = None,
) -> list[Any]:
    """Build the positional parameter list for ``INSERT_SQL``.

    Keyword-only by design: callers name every column, so a future column
    insertion is a compile-time-visible change at each call site rather than a
    silently-misaligned positional tuple. ``metadata`` is serialized here.
    """
    return [
        row_id,
        ts,
        action_type,
        actor,
        actor_role,
        skill_name,
        matter_ref,
        input_digest,
        output_digest,
        diff_digest,
        trust_ceiling,
        _dumps(metadata),
    ]


# Actor-role literal for agent-authored event rows. The canonical ActorRole
# enum lives in hermes-smd-audit/schemas.py (the plugin layer); shared/ is the
# lower layer and must not import upward, so the agent literal is pinned here
# and asserted equal to ActorRole.AGENT.value by the audit plugin's tests.
ACTOR_AGENT = "agent"


def agent_event_params(
    *,
    action_type: str,
    metadata: dict | None = None,
    skill_name: str | None = None,
    now_ms: int | None = None,
    now=None,
) -> list[Any]:
    """Convenience builder for an agent-actor event row (the webhook-router /
    outbound-gate shape): fresh ULID + ISO-Z timestamp, ``actor="agent"`` /
    ``actor_role="agent"``, all digest columns NULL. ``now_ms``/``now`` are
    injectable for deterministic tests.
    """
    return build_audit_params(
        row_id=ulid(now_ms=now_ms),
        ts=iso_utc(now),
        action_type=action_type,
        actor=ACTOR_AGENT,
        actor_role=ACTOR_AGENT,
        skill_name=skill_name,
        metadata=metadata,
    )


__all__ = [
    "COLUMNS",
    "INSERT_SQL",
    "ACTOR_AGENT",
    "build_audit_params",
    "agent_event_params",
    "sha256",
]
