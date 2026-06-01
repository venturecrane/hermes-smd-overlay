"""Guard tests for the single-sourced audit_log row contract.

These pin the properties that keep the three audit writers (hermes-smd-audit,
hermes-smd-webhook-router, hermes-smd-trust) from ever desyncing on the
audit_log row shape. A column reorder, a placeholder-count change, or a drift
between the ``ACTOR_AGENT`` literal and the canonical ``ActorRole.AGENT`` enum
will fail here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from shared.audit_contract import (
    ACTOR_AGENT,
    COLUMNS,
    INSERT_SQL,
    agent_event_params,
    build_audit_params,
)

# Canonical column order — this list IS the contract. If you change it you must
# change it in lockstep with ss-console docs/specs/ai-employee/d1-schema.md and
# the ss-console schema-snapshot guard. This test deliberately hard-codes the
# expected order so a silent reorder fails.
_EXPECTED_COLUMNS = (
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


def test_columns_frozen_order():
    assert COLUMNS == _EXPECTED_COLUMNS


def test_insert_sql_matches_columns():
    # SQL column list and placeholder count both derive from COLUMNS.
    assert INSERT_SQL.startswith("INSERT INTO audit_log (")
    assert ", ".join(_EXPECTED_COLUMNS) in INSERT_SQL
    assert INSERT_SQL.count("?") == len(_EXPECTED_COLUMNS)


def test_build_audit_params_length_matches_columns():
    params = build_audit_params(
        row_id="01TESTULID",
        ts="2026-05-31T00:00:00.000Z",
        action_type="TOOL_CALL",
    )
    assert len(params) == len(_EXPECTED_COLUMNS)


def test_build_audit_params_positional_alignment():
    params = build_audit_params(
        row_id="ID",
        ts="TS",
        action_type="AT",
        actor="a",
        actor_role="r",
        skill_name="s",
        matter_ref="m",
        input_digest="i",
        output_digest="o",
        diff_digest="d",
        trust_ceiling="t",
        metadata={"k": "v"},
    )
    by_col = dict(zip(_EXPECTED_COLUMNS, params, strict=True))
    assert by_col["id"] == "ID"
    assert by_col["ts"] == "TS"
    assert by_col["action_type"] == "AT"
    assert by_col["actor"] == "a"
    assert by_col["actor_role"] == "r"
    # metadata is serialized deterministically (sorted keys, no whitespace).
    assert by_col["metadata"] == '{"k":"v"}'


def test_empty_metadata_is_null():
    params = build_audit_params(row_id="X", ts="Y", action_type="Z", metadata=None)
    assert params[_EXPECTED_COLUMNS.index("metadata")] is None
    params2 = build_audit_params(row_id="X", ts="Y", action_type="Z", metadata={})
    assert params2[_EXPECTED_COLUMNS.index("metadata")] is None


def test_agent_event_params_shape():
    params = agent_event_params(
        action_type="WEBHOOK_ROUTED",
        skill_name="inbox-triage",
        metadata={"a": 1},
        now_ms=0,
    )
    by_col = dict(zip(_EXPECTED_COLUMNS, params, strict=True))
    assert by_col["actor"] == ACTOR_AGENT
    assert by_col["actor_role"] == ACTOR_AGENT
    assert by_col["skill_name"] == "inbox-triage"
    assert by_col["action_type"] == "WEBHOOK_ROUTED"
    # digest columns are NULL for an agent event
    for col in ("input_digest", "output_digest", "diff_digest", "matter_ref", "trust_ceiling"):
        assert by_col[col] is None
    # ULID is 26 Crockford chars
    assert len(by_col["id"]) == 26


def test_actor_agent_literal_matches_canonical_enum():
    """ACTOR_AGENT in shared/ must equal ActorRole.AGENT.value in the audit
    plugin's schemas (shared/ cannot import upward, so the literal is pinned
    here and asserted equal to the enum)."""
    schemas_path = (
        Path(__file__).resolve().parent.parent / "plugins" / "hermes-smd-audit" / "schemas.py"
    )
    spec = importlib.util.spec_from_file_location("_audit_schemas_for_test", schemas_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert ACTOR_AGENT == mod.ActorRole.AGENT.value
