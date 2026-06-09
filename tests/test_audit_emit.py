"""Tests for the hermes-smd-audit plugin.

Ported from ss-console/operator/adapter/tests/test_audit_log.py +
test_audit_emit_points.py. Covers:

  * Registration: ``register(ctx)`` is callable and wires both hooks.
  * ULID + ISO-8601 helpers behave per spec.
  * AuditLogWriter writes every column correctly through a fake D1Client.
  * Action type validation: ValueError on unknown types.
  * Metadata is serialized deterministically (sort_keys, no whitespace).
  * Failure path: D1Client exception is wrapped in AuditWriteError.
  * Registry invariants: tools map to HookActionClass, registry is
    disjoint from BANNED_TOOLS, runtime-immutable.
  * classify_tool: known / unknown / banned cases.
  * ToolCallTimer: monotonic, single-shot, raises on misuse.
  * extract_scope_metadata: lifts matter_id + customer_segment.
  * build_per_tool_metadata: canonical key set, unmapped + banned flags.
  * Per-hook helpers: emit_tool_event and emit_llm_event drive the
    writer end-to-end via a fake D1Client.
  * The Hermes-dispatcher exception-safety contract: hook callbacks
    never raise even when the writer fails.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports (`from . import emit`) work.

    The shared ``tests.conftest.load_plugin`` calls ``exec_module`` without first
    registering the parent module in ``sys.modules``; relative imports inside the
    plugin's ``__init__.py`` then fail with ``ModuleNotFoundError``. Tests in
    this file need ``mod.emit`` / ``mod.schemas`` / ``mod.immutability`` /
    ``mod.integrity`` access, so we use a local loader that does the right
    sequencing.
    """
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake D1Client — captures the executed SQL + params for inspection.
# Mirrors the contract of shared.d1_client.D1Client.execute(sql, *params).
# ---------------------------------------------------------------------------


class FakeD1Client:
    """Records every execute call. Tests inspect ``rows`` after writes."""

    def __init__(self, *, raise_on_execute: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._raise = raise_on_execute

    def execute(self, sql: str, *params) -> None:
        if self._raise is not None:
            raise self._raise
        self.calls.append((sql, tuple(params)))

    def rows(self) -> list[dict]:
        """Parse the recorded INSERT params into a list of column dicts."""
        cols = [
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
        ]
        out: list[dict] = []
        for _sql, params in self.calls:
            out.append(dict(zip(cols, params, strict=False)))
        return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_audit_registers_expected_hooks(fake_ctx, monkeypatch) -> None:
    """hermes-smd-audit must attach to post_tool_call and post_llm_call."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")

    mod = load_plugin("hermes-smd-audit")
    assert callable(mod.register)

    mod.register(fake_ctx)
    assert "post_tool_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered


def test_audit_registers_even_when_env_missing(fake_ctx, monkeypatch) -> None:
    """Missing env logs a warning but still registers the callbacks."""
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    monkeypatch.delenv("SMD_D1_AUDIT_BINDING", raising=False)

    mod = load_plugin("hermes-smd-audit")
    mod.register(fake_ctx)
    assert "post_tool_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered


# ---------------------------------------------------------------------------
# ULID + ISO timestamp helpers
# ---------------------------------------------------------------------------


def test_ulid_is_26_chars() -> None:
    mod = load_plugin("hermes-smd-audit")
    ulid = mod.emit._ulid()
    assert len(ulid) == 26
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in ulid)


def test_ulid_sorts_by_time() -> None:
    mod = load_plugin("hermes-smd-audit")
    early = mod.emit._ulid(now_ms=1_000_000_000_000)
    later = mod.emit._ulid(now_ms=2_000_000_000_000)
    assert early < later


def test_ulid_unique_within_same_ms() -> None:
    mod = load_plugin("hermes-smd-audit")
    ulids = {mod.emit._ulid(now_ms=1_700_000_000_000) for _ in range(100)}
    assert len(ulids) == 100


def test_iso_utc_format() -> None:
    from datetime import datetime

    mod = load_plugin("hermes-smd-audit")
    dt = datetime(2026, 5, 21, 12, 34, 56, 789_000, tzinfo=UTC)
    assert mod.emit._iso_utc(dt) == "2026-05-21T12:34:56.789Z"


def test_sha256_none_passes_through() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit._sha256(None) is None


def test_sha256_known_value() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit._sha256(b"").startswith("e3b0c442")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def test_writer_inserts_row_with_all_fields() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)

    event = mod.schemas.AuditEvent(
        action_type="DRAFT_CREATED",
        actor="agent",
        actor_role=mod.schemas.ActorRole.AGENT,
        skill_name="inbox-triage",
        matter_ref="matter-123",
        input_payload=b"raw email body",
        output_payload=b"draft response",
        diff_payload=None,
        trust_ceiling="draft_for_review",
        metadata={"recipient_cohort_id": "anxious-client", "priority": 5},
    )
    ulid = writer.write(event)

    assert len(client.calls) == 1
    sql, params = client.calls[0]
    assert "INSERT INTO audit_log" in sql

    row = client.rows()[0]
    assert row["id"] == ulid
    assert row["action_type"] == "DRAFT_CREATED"
    assert row["actor"] == "agent"
    assert row["actor_role"] == "agent"
    assert row["skill_name"] == "inbox-triage"
    assert row["matter_ref"] == "matter-123"
    assert row["input_digest"] == mod.emit._sha256(b"raw email body")
    assert row["output_digest"] == mod.emit._sha256(b"draft response")
    assert row["diff_digest"] is None
    assert row["trust_ceiling"] == "draft_for_review"
    parsed = json.loads(row["metadata"])
    assert parsed == {"priority": 5, "recipient_cohort_id": "anxious-client"}
    assert row["ts"].endswith("Z")
    assert "T" in row["ts"]


def test_ensure_schema_creates_table_then_write_reads_back(tmp_path) -> None:
    """End-to-end against a REAL D1Client: on a fresh DB with no audit_log,
    ensure_schema() creates the table and a real write lands a readable row.
    Closes the table-creation gap in ss-console#1285 (the Machine's bootstrap
    never applied the per-customer migrations, so the table was missing)."""
    from shared.d1_client import D1Client

    mod = load_plugin("hermes-smd-audit")
    db = str(tmp_path / "audit.db")
    client = D1Client(binding_name=db, customer_slug="acme")  # direct-path binding (#41)
    writer = mod.emit.AuditLogWriter(client)
    writer.ensure_schema()  # the table did not exist before this call

    event = mod.schemas.AuditEvent(
        action_type="DRAFT_CREATED",
        actor="agent",
        actor_role=mod.schemas.ActorRole.AGENT,
        skill_name="inbox-triage",
    )
    ulid = writer.write(event)

    rows = client.query("SELECT id, action_type, actor FROM audit_log")
    assert rows == [{"id": ulid, "action_type": "DRAFT_CREATED", "actor": "agent"}]


def test_writer_with_minimal_event() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    event = mod.schemas.AuditEvent(action_type="AGENT_STOPPED", actor="captain")
    writer.write(event)
    row = client.rows()[0]
    assert row["actor"] == "captain"
    assert row["skill_name"] is None
    assert row["metadata"] is None


def test_writer_rejects_unknown_action_type() -> None:
    mod = load_plugin("hermes-smd-audit")
    writer = mod.emit.AuditLogWriter(FakeD1Client())
    with pytest.raises(ValueError, match="not in ACCEPTED_ACTION_TYPES"):
        writer.write(mod.schemas.AuditEvent(action_type="MADE_UP_TYPE", actor="agent"))


def test_writer_wraps_executor_failure_as_audit_write_error() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    writer = mod.emit.AuditLogWriter(client)
    with pytest.raises(mod.emit.AuditWriteError):
        writer.write(mod.schemas.AuditEvent(action_type="DRAFT_CREATED", actor="agent"))


def test_metadata_is_deterministic_json() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    md_a = {"b": 1, "a": 2}
    md_b = {"a": 2, "b": 1}
    writer.write(mod.schemas.AuditEvent(action_type="DRAFT_CREATED", actor="agent", metadata=md_a))
    writer.write(mod.schemas.AuditEvent(action_type="DRAFT_CREATED", actor="agent", metadata=md_b))
    rows = client.rows()
    assert rows[0]["metadata"] == rows[1]["metadata"]
    assert rows[0]["metadata"] == '{"a":2,"b":1}'


def test_actor_role_accepts_plain_string_for_forward_compat() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    writer.write(
        mod.schemas.AuditEvent(
            action_type="RBAC_EVENT",
            actor="agent",
            actor_role="future_role",  # type: ignore[arg-type]
        )
    )
    assert client.rows()[0]["actor_role"] == "future_role"


# ---------------------------------------------------------------------------
# Accepted action_type set
# ---------------------------------------------------------------------------


def test_accepted_action_types_includes_safety_substrate_events() -> None:
    mod = load_plugin("hermes-smd-audit")
    must_have = {
        "DRAFT_CREATED",
        "INVARIANT_VIOLATION",
        "TRUST_PROMOTED",
        "ESCALATION_FIRED",
        "DECOMMISSION_FINAL",
        "COMPLIANCE_PACKET_EXPORTED",
    }
    assert must_have.issubset(mod.schemas.ACCEPTED_ACTION_TYPES)


def test_accepted_action_types_excludes_gepa() -> None:
    """ADR 0018 is superseded; GEPA action_type must not appear."""
    mod = load_plugin("hermes-smd-audit")
    assert "GEPA_DISABLED_VERIFIED" not in mod.schemas.ACCEPTED_ACTION_TYPES


def test_accepted_action_types_includes_new_hook_emission_types() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert "TOOL_CALL_COMPLETED" in mod.schemas.ACCEPTED_ACTION_TYPES
    assert "LLM_TURN_COMPLETED" in mod.schemas.ACCEPTED_ACTION_TYPES


# ---------------------------------------------------------------------------
# Tool registry invariants
# ---------------------------------------------------------------------------


def test_registry_is_nonempty() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert len(mod.schemas.TOOL_ACTION_CLASS_MAP) > 0


def test_registry_values_are_hook_action_class() -> None:
    mod = load_plugin("hermes-smd-audit")
    for name, value in mod.schemas.TOOL_ACTION_CLASS_MAP.items():
        assert isinstance(value, mod.schemas.HookActionClass), (
            f"registry value for {name!r} is not a HookActionClass"
        )


def test_registry_and_banned_sets_are_disjoint() -> None:
    """No tool name appears in both the registry and BANNED_TOOLS."""
    mod = load_plugin("hermes-smd-audit")
    overlap = set(mod.schemas.TOOL_ACTION_CLASS_MAP.keys()) & set(mod.schemas.BANNED_TOOLS)
    assert overlap == set(), f"tool names appear in BOTH map and BANNED_TOOLS: {sorted(overlap)}"


def test_registry_is_immutable_at_runtime() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(TypeError):
        mod.schemas.TOOL_ACTION_CLASS_MAP["new_tool"] = mod.schemas.HookActionClass.READ  # type: ignore[index]


def test_email_send_is_banned_not_in_registry() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert "email_send" in mod.schemas.BANNED_TOOLS
    assert "email_send" not in mod.schemas.TOOL_ACTION_CLASS_MAP


def test_email_create_draft_is_internal_write() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.schemas.TOOL_ACTION_CLASS_MAP["email_create_draft"]
        is mod.schemas.HookActionClass.INTERNAL_WRITE
    )


def test_payments_initiate_transfer_is_banned() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert "payments_initiate_transfer" in mod.schemas.BANNED_TOOLS
    assert "payments_initiate_transfer" not in mod.schemas.TOOL_ACTION_CLASS_MAP


def test_principal_send_tools_stay_banned_and_absent_from_registry() -> None:
    """Principal-identity sends are NEVER in the registry — they stay banned.

    Pre-ADR-0025 doctrine was "no send tool anywhere." ADR 0025 makes the
    PERSONA's own-identity sends (``agentmail:send_*``) a configurable
    EXTERNAL_SEND, so they legitimately appear in the registry and are governed
    by the trust ceiling. What stays absolute: the agent must never send from
    the PRINCIPAL's identity (``email_send`` / ``email_reply`` / ``sms_send``,
    "never send as Scott"). Those remain in BANNED_TOOLS and out of the map.
    """
    mod = load_plugin("hermes-smd-audit")
    principal_sends = (
        "email_send",
        "email_send_message",
        "email_reply",
        "email_reply_all",
        "email_forward",
        "sms_send",
        "sms_send_message",
    )
    for name in principal_sends:
        assert name in mod.schemas.BANNED_TOOLS, f"{name} must stay banned"
        assert name not in mod.schemas.TOOL_ACTION_CLASS_MAP, f"{name} must not be in the registry"


def test_send_tools_in_registry_are_classified_external_send() -> None:
    """Any send-capable tool that IS in the registry must be EXTERNAL_SEND.

    A send tool classified READ / INTERNAL_WRITE would slip past the exposure
    ceiling. The only sends allowed in the registry are the persona's own
    (``agentmail:send_*``); each must carry the EXTERNAL_SEND class so the
    ceiling governs it (ADR 0025). ``send_draft`` is a send (it dispatches a
    pre-composed draft), so it counts too; ``create_draft`` / ``update_draft``
    are authoring, not sending, and are excluded from this check.
    """
    mod = load_plugin("hermes-smd-audit")
    for name, action in mod.schemas.TOOL_ACTION_CLASS_MAP.items():
        looks_like_send = (
            ("send" in name)
            or name.endswith(("_reply", "_forward"))
            or ":reply_to_message" in name
            or ":forward_message" in name
        )
        is_draft_authoring = name.endswith(
            (
                "create_draft",
                "update_draft",
                "_create_draft",
                "_event_draft",
                "_task_draft",
                "invitation_draft",
            )
        )
        if looks_like_send and not is_draft_authoring:
            assert action is mod.schemas.HookActionClass.EXTERNAL_SEND, (
                f"send-capable tool {name!r} is classified {action}, expected EXTERNAL_SEND"
            )


# ---------------------------------------------------------------------------
# classify_tool
# ---------------------------------------------------------------------------


def test_classify_tool_returns_registry_value_for_known_tool() -> None:
    mod = load_plugin("hermes-smd-audit")
    cls = mod.emit.classify_tool("email_create_draft")
    assert cls.action_class is mod.schemas.HookActionClass.INTERNAL_WRITE
    assert cls.unmapped is False


def test_classify_tool_returns_read_default_for_unknown_tool() -> None:
    mod = load_plugin("hermes-smd-audit")
    cls = mod.emit.classify_tool("some_brand_new_tool")
    assert cls.action_class is mod.schemas.HookActionClass.READ
    assert cls.unmapped is True


def test_classify_tool_raises_banned_for_email_send() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(mod.emit.BannedToolError) as exc:
        mod.emit.classify_tool("email_send")
    assert exc.value.tool_name == "email_send"
    assert exc.value.reason == "banned_tool_pattern_a"


def test_classify_tool_raises_banned_for_payments_initiate_transfer() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(mod.emit.BannedToolError) as exc:
        mod.emit.classify_tool("payments_initiate_transfer")
    assert exc.value.reason == "banned_tool_destructive"


def test_classify_tool_raises_for_every_banned_tool() -> None:
    mod = load_plugin("hermes-smd-audit")
    for name in mod.schemas.BANNED_TOOLS:
        with pytest.raises(mod.emit.BannedToolError):
            mod.emit.classify_tool(name)


def test_classify_tool_rejects_empty_name() -> None:
    mod = load_plugin("hermes-smd-audit")
    with pytest.raises(ValueError, match="tool_name is required"):
        mod.emit.classify_tool("")


# ---------------------------------------------------------------------------
# ToolCallTimer
# ---------------------------------------------------------------------------


def test_timer_measures_elapsed_ms() -> None:
    mod = load_plugin("hermes-smd-audit")
    timer = mod.emit.ToolCallTimer().start()
    time.sleep(0.005)
    elapsed = timer.stop()
    assert elapsed >= 4.0
    assert elapsed < 200.0
    assert timer.duration_ms == elapsed


def test_timer_duration_is_none_before_stop() -> None:
    mod = load_plugin("hermes-smd-audit")
    timer = mod.emit.ToolCallTimer().start()
    assert timer.duration_ms is None
    timer.stop()


def test_timer_start_twice_raises() -> None:
    mod = load_plugin("hermes-smd-audit")
    timer = mod.emit.ToolCallTimer().start()
    with pytest.raises(RuntimeError, match="start called twice"):
        timer.start()


def test_timer_stop_before_start_raises() -> None:
    mod = load_plugin("hermes-smd-audit")
    timer = mod.emit.ToolCallTimer()
    with pytest.raises(RuntimeError, match="stop called before start"):
        timer.stop()


def test_timer_stop_twice_raises() -> None:
    mod = load_plugin("hermes-smd-audit")
    timer = mod.emit.ToolCallTimer().start()
    timer.stop()
    with pytest.raises(RuntimeError, match="stop called twice"):
        timer.stop()


# ---------------------------------------------------------------------------
# extract_scope_metadata
# ---------------------------------------------------------------------------


def test_extract_scope_metadata_returns_empty_when_no_arguments() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit.extract_scope_metadata(None) == {}


def test_extract_scope_metadata_returns_empty_when_arguments_lack_scope_keys() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit.extract_scope_metadata({"unrelated": "value", "subject": "Hi"}) == {}


def test_extract_scope_metadata_lifts_matter_id() -> None:
    mod = load_plugin("hermes-smd-audit")
    out = mod.emit.extract_scope_metadata({"matter_id": "matter-42", "body": "ignored"})
    assert out == {"matter_id": "matter-42"}


def test_extract_scope_metadata_lifts_customer_segment() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit.extract_scope_metadata({"customer_segment": "cohort-a"}) == {
        "customer_segment": "cohort-a"
    }


def test_extract_scope_metadata_lifts_both_keys() -> None:
    mod = load_plugin("hermes-smd-audit")
    out = mod.emit.extract_scope_metadata(
        {
            "matter_id": "matter-7",
            "customer_segment": "cohort-b",
            "to": "ignored@example.com",
        }
    )
    assert out == {"matter_id": "matter-7", "customer_segment": "cohort-b"}


def test_extract_scope_metadata_coerces_non_string_values() -> None:
    mod = load_plugin("hermes-smd-audit")
    out = mod.emit.extract_scope_metadata({"matter_id": 12345, "customer_segment": True})
    assert out == {"matter_id": "12345", "customer_segment": "True"}


def test_extract_scope_metadata_omits_none_values() -> None:
    mod = load_plugin("hermes-smd-audit")
    out = mod.emit.extract_scope_metadata({"matter_id": None, "customer_segment": "cohort-c"})
    assert out == {"customer_segment": "cohort-c"}


# ---------------------------------------------------------------------------
# build_per_tool_metadata
# ---------------------------------------------------------------------------


def test_build_metadata_canonical_keys_present() -> None:
    mod = load_plugin("hermes-smd-audit")
    md = mod.emit.build_per_tool_metadata(
        customer="acme",
        tool_name="email_create_draft",
        action_class=mod.schemas.HookActionClass.INTERNAL_WRITE,
        outcome="ok",
        duration_ms=12.5,
        trace_id="trace-test-0001",
    )
    for key in (
        "per_tool_audit",
        "customer",
        "skill",
        "skill_version",
        "tool",
        "action_class",
        "ceiling_level",
        "outcome",
        "error_type",
        "duration_ms",
        "trace_id",
    ):
        assert key in md, f"canonical key {key!r} missing from metadata"
    assert md["per_tool_audit"] is True
    assert md["customer"] == "acme"
    assert md["tool"] == "email_create_draft"
    assert md["action_class"] == "internal_write"
    assert md["outcome"] == "ok"
    assert md["duration_ms"] == 12.5
    assert md["trace_id"] == "trace-test-0001"


def test_build_metadata_no_unmapped_or_banned_flags_by_default() -> None:
    mod = load_plugin("hermes-smd-audit")
    md = mod.emit.build_per_tool_metadata(
        customer="acme",
        tool_name="email_create_draft",
        action_class=mod.schemas.HookActionClass.INTERNAL_WRITE,
        outcome="ok",
    )
    assert "unmapped_tool" not in md
    assert "banned_tool" not in md
    assert "banned_reason" not in md


def test_build_metadata_tags_unmapped_tool() -> None:
    mod = load_plugin("hermes-smd-audit")
    md = mod.emit.build_per_tool_metadata(
        customer="acme",
        tool_name="some_brand_new_tool",
        action_class=mod.schemas.HookActionClass.READ,
        outcome="ok",
        unmapped=True,
    )
    assert md["unmapped_tool"] is True


def test_build_metadata_tags_banned_tool() -> None:
    mod = load_plugin("hermes-smd-audit")
    md = mod.emit.build_per_tool_metadata(
        customer="acme",
        tool_name="email_send",
        action_class=mod.schemas.HookActionClass.EXTERNAL_SEND,
        outcome="blocked",
        banned_reason="banned_tool_pattern_a",
    )
    assert md["banned_tool"] is True
    assert md["banned_reason"] == "banned_tool_pattern_a"
    assert md["outcome"] == "blocked"


def test_build_metadata_merges_scope_keys() -> None:
    mod = load_plugin("hermes-smd-audit")
    md = mod.emit.build_per_tool_metadata(
        customer="acme",
        tool_name="email_create_draft",
        action_class=mod.schemas.HookActionClass.INTERNAL_WRITE,
        outcome="ok",
        arguments={"matter_id": "matter-42"},
    )
    assert md["matter_id"] == "matter-42"


# ---------------------------------------------------------------------------
# emit_tool_event + emit_llm_event
# ---------------------------------------------------------------------------


def test_emit_tool_event_known_tool_writes_tool_call_completed_row() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    ulid = mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_create_draft",
        args={"matter_id": "matter-42", "customer_segment": "cohort-a"},
        result="{}",
        task_id="task-1",
        session_id="sess-1",
        tool_call_id="trace-test-0001",
        duration_ms=8,
    )
    assert ulid
    row = client.rows()[0]
    assert row["action_type"] == "TOOL_CALL_COMPLETED"
    md = json.loads(row["metadata"])
    assert md["tool"] == "email_create_draft"
    assert md["action_class"] == "internal_write"
    assert md["matter_id"] == "matter-42"
    assert md["customer_segment"] == "cohort-a"
    assert md["session_id"] == "sess-1"
    assert md["task_id"] == "task-1"


def test_emit_tool_event_unknown_tool_tags_unmapped() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="some_brand_new_tool",
        args=None,
        result="{}",
        task_id="",
        session_id="sess-x",
        tool_call_id="",
        duration_ms=None,
    )
    md = json.loads(client.rows()[0]["metadata"])
    assert md["unmapped_tool"] is True
    assert md["action_class"] == "read"


def test_emit_tool_event_banned_tool_writes_invariant_violation_row() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_send",
        args=None,
        result="",
        task_id="",
        session_id="sess-x",
        tool_call_id="",
        duration_ms=None,
    )
    row = client.rows()[0]
    assert row["action_type"] == "INVARIANT_VIOLATION"
    md = json.loads(row["metadata"])
    assert md["banned_tool"] is True
    assert md["banned_reason"] == "banned_tool_pattern_a"
    assert md["outcome"] == "blocked"


def test_emit_llm_event_writes_llm_turn_completed_row() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    ulid = mod.emit.emit_llm_event(
        writer,
        customer="acme",
        session_id="sess-1",
        user_message="hello",
        assistant_response="hi",
        model="claude-opus-4-7",
        platform="cli",
    )
    assert ulid
    row = client.rows()[0]
    assert row["action_type"] == "LLM_TURN_COMPLETED"
    # The user / assistant text is digested, not stored verbatim
    assert row["input_digest"] == mod.emit._sha256(b"hello")
    assert row["output_digest"] == mod.emit._sha256(b"hi")
    md = json.loads(row["metadata"])
    assert md["session_id"] == "sess-1"
    assert md["model"] == "claude-opus-4-7"
    assert md["platform"] == "cli"


# ---------------------------------------------------------------------------
# Outcome inference (_outcome_from_result) — error-detecting, conservative.
# ---------------------------------------------------------------------------


def test_outcome_ok_for_empty_or_nonjson() -> None:
    mod = load_plugin("hermes-smd-audit")
    f = mod.emit._outcome_from_result
    assert f("") == ("ok", None)
    assert f(None) == ("ok", None)
    assert f("plain text, not json") == ("ok", None)
    assert f("{ not valid json") == ("ok", None)  # unparseable → never fabricate
    assert f("{}") == ("ok", None)
    assert f('{"result": "done", "rows": 3}') == ("ok", None)


def test_outcome_detects_structured_errors() -> None:
    mod = load_plugin("hermes-smd-audit")
    f = mod.emit._outcome_from_result
    assert f('{"error": "boom"}') == ("error", "boom")
    assert f('{"error": true, "code": "E_TIMEOUT"}') == ("error", "E_TIMEOUT")
    assert f('{"is_error": true}') == ("error", None)
    assert f('{"isError": true}') == ("error", None)
    assert f('{"status": "error"}') == ("error", "error")
    assert f('{"status": "FAILED", "type": "AuthError"}') == ("error", "AuthError")
    assert f('{"ok": false}') == ("error", None)
    assert f('{"success": false, "error_type": "Conflict"}') == ("error", "Conflict")
    # truthy non-error fields must NOT be read as errors
    assert f('{"ok": true, "status": "success"}') == ("ok", None)


def test_emit_tool_event_records_error_outcome_and_version() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_create_draft",
        args=None,
        result='{"error": "rate_limited", "code": "E_RATE"}',
        task_id="t",
        session_id="s",
        tool_call_id="c",
        duration_ms=5,
    )
    md = json.loads(client.rows()[0]["metadata"])
    assert md["outcome"] == "error"
    assert md["error_type"] == "E_RATE"
    # forward-only changepoint marker (v2 = error-detecting)
    assert md["outcome_semantics_version"] == 2


def test_emit_tool_event_ok_outcome_carries_version() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)
    mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_create_draft",
        args=None,
        result="{}",
        task_id="t",
        session_id="s",
        tool_call_id="c",
        duration_ms=5,
    )
    md = json.loads(client.rows()[0]["metadata"])
    assert md["outcome"] == "ok"
    assert md["outcome_semantics_version"] == 2


# ---------------------------------------------------------------------------
# Hook callbacks are exception-safe
# ---------------------------------------------------------------------------


def test_on_post_tool_call_swallows_writer_exception(fake_ctx, monkeypatch) -> None:
    """Per AGENTS.md hard rule #3, callbacks never raise."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")

    # Swap in a writer that always raises so we exercise the except clause.
    boom = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    mod._WRITER = mod.emit.AuditLogWriter(boom)
    mod._CUSTOMER_SLUG = "acme"

    # The callback must NOT raise even though the writer does.
    mod.on_post_tool_call(
        tool_name="email_create_draft",
        args={},
        result="{}",
        task_id="",
        session_id="sess-x",
        tool_call_id="",
        duration_ms=1,
    )


def test_on_post_llm_call_swallows_writer_exception(fake_ctx, monkeypatch) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    boom = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    mod._WRITER = mod.emit.AuditLogWriter(boom)
    mod._CUSTOMER_SLUG = "acme"

    mod.on_post_llm_call(
        session_id="sess-x",
        user_message="hello",
        assistant_response="hi",
        conversation_history=[],
        model="claude-opus-4-7",
        platform="cli",
    )


def test_on_post_tool_call_no_writer_is_noop(fake_ctx) -> None:
    """When env is missing the callback should noop, not crash."""
    mod = load_plugin("hermes-smd-audit")
    mod._WRITER = None
    mod._CUSTOMER_SLUG = None
    mod.on_post_tool_call(
        tool_name="email_create_draft",
        args={},
        result="{}",
        task_id="",
        session_id="sess-x",
        tool_call_id="",
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# subagent_stop hook + emit_subagent_stop_event (ADR 0021 Stream C)
# ---------------------------------------------------------------------------


def test_emit_subagent_stop_event_writes_row() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)

    ulid = mod.emit.emit_subagent_stop_event(
        writer,
        customer="acme",
        session_id="sess-child-1",
        parent_session_id="sess-parent-1",
        child_role="medicals_summary",
        child_status="ok",
        duration_ms=4200,
        task_id="task-1",
        skill_name="law-pi-demand-letter-draft",
    )
    assert ulid
    row = client.rows()[0]
    assert row["action_type"] == "SUBAGENT_STOPPED"
    assert row["skill_name"] == "law-pi-demand-letter-draft"
    md = json.loads(row["metadata"])
    assert md["per_subagent_audit"] is True
    assert md["customer"] == "acme"
    assert md["child_role"] == "medicals_summary"
    assert md["child_status"] == "ok"
    assert md["session_id"] == "sess-child-1"
    assert md["parent_session_id"] == "sess-parent-1"
    assert md["task_id"] == "task-1"
    assert md["duration_ms"] == 4200.0


def test_emit_subagent_stop_event_minimal_kwargs() -> None:
    """parent_session_id, task_id, duration_ms, skill_name are all optional."""
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)

    mod.emit.emit_subagent_stop_event(
        writer,
        customer="acme",
        session_id="sess-child-1",
        parent_session_id=None,
        child_role="liability_summary",
        child_status="failed",
        duration_ms=None,
    )
    row = client.rows()[0]
    md = json.loads(row["metadata"])
    assert md["child_status"] == "failed"
    assert "parent_session_id" not in md
    assert "task_id" not in md
    assert "duration_ms" not in md


def test_emit_subagent_stop_event_rejects_reserved_metadata_keys() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)

    with pytest.raises(ValueError):
        mod.emit.emit_subagent_stop_event(
            writer,
            customer="acme",
            session_id="s",
            parent_session_id=None,
            child_role="r",
            child_status="ok",
            duration_ms=1,
            extra_metadata={"child_role": "evil"},  # reserved by wrapper
        )


def test_on_subagent_stop_writes_through_writer(fake_ctx, monkeypatch) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    mod._WRITER = mod.emit.AuditLogWriter(client)
    mod._CUSTOMER_SLUG = "acme"

    mod.on_subagent_stop(
        session_id="sess-child-1",
        parent_session_id="sess-parent-1",
        child_role="damages_summary",
        child_status="ok",
        duration_ms=2100,
        task_id="task-x",
        skill_name="law-pi-settlement-prep",
    )
    rows = client.rows()
    assert len(rows) == 1
    assert rows[0]["action_type"] == "SUBAGENT_STOPPED"
    md = json.loads(rows[0]["metadata"])
    assert md["child_role"] == "damages_summary"


def test_on_subagent_stop_swallows_writer_exception(fake_ctx, monkeypatch) -> None:
    """Per AGENTS.md hard rule #3, callbacks never raise."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    boom = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    mod._WRITER = mod.emit.AuditLogWriter(boom)
    mod._CUSTOMER_SLUG = "acme"

    # Must not raise.
    mod.on_subagent_stop(
        session_id="s",
        parent_session_id=None,
        child_role="r",
        child_status="ok",
        duration_ms=1,
    )


def test_on_subagent_stop_no_writer_is_noop(fake_ctx) -> None:
    mod = load_plugin("hermes-smd-audit")
    mod._WRITER = None
    mod._CUSTOMER_SLUG = None
    # Must not raise.
    mod.on_subagent_stop(
        session_id="s",
        parent_session_id=None,
        child_role="r",
        child_status="ok",
        duration_ms=1,
    )


# ---------------------------------------------------------------------------
# skill_manage → AGENT_SKILL_CREATED detection + emission (ADR 0017 §40)
# ---------------------------------------------------------------------------


def test_detect_skill_manage_creation_basic_create_action() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.emit.detect_skill_manage_creation(
            tool_name="skill_manage",
            args={"action": "create", "slug": "my-new-skill"},
        )
        == "my-new-skill"
    )


def test_detect_skill_manage_creation_accepts_name_field() -> None:
    """The detector is permissive on the field name (`slug`, `name`, `skill_slug`)."""
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.emit.detect_skill_manage_creation(
            tool_name="skill_manage",
            args={"mode": "create", "name": "another-skill"},
        )
        == "another-skill"
    )


def test_detect_skill_manage_creation_returns_none_for_non_create_action() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.emit.detect_skill_manage_creation(
            tool_name="skill_manage",
            args={"action": "delete", "slug": "old-skill"},
        )
        is None
    )


def test_detect_skill_manage_creation_returns_none_for_other_tools() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.emit.detect_skill_manage_creation(
            tool_name="email_create_draft",
            args={"action": "create", "slug": "foo"},
        )
        is None
    )


def test_detect_skill_manage_creation_returns_none_for_missing_slug() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert (
        mod.emit.detect_skill_manage_creation(
            tool_name="skill_manage",
            args={"action": "create"},
        )
        is None
    )


def test_detect_skill_manage_creation_returns_none_for_non_dict_args() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert mod.emit.detect_skill_manage_creation(tool_name="skill_manage", args=None) is None
    assert (
        mod.emit.detect_skill_manage_creation(tool_name="skill_manage", args="not-a-dict") is None
    )  # type: ignore[arg-type]


def test_emit_agent_skill_created_event_writes_row() -> None:
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    writer = mod.emit.AuditLogWriter(client)

    ulid = mod.emit.emit_agent_skill_created_event(
        writer,
        customer="acme",
        session_id="sess-1",
        skill_name_created="newly-authored-skill",
        skill_manage_args={"action": "create", "slug": "newly-authored-skill"},
        tool_call_id="trace-xyz",
    )
    assert ulid
    row = client.rows()[0]
    assert row["action_type"] == "AGENT_SKILL_CREATED"
    assert row["skill_name"] == "newly-authored-skill"
    md = json.loads(row["metadata"])
    assert md["per_agent_skill_creation"] is True
    assert md["customer"] == "acme"
    assert md["skill_name_created"] == "newly-authored-skill"
    assert md["session_id"] == "sess-1"
    assert md["tool_call_id"] == "trace-xyz"
    assert md["skill_manage_args"]["action"] == "create"


def test_on_post_tool_call_fires_both_rows_for_skill_manage_create(fake_ctx, monkeypatch) -> None:
    """When skill_manage is invoked with a create action, TWO audit rows
    must land: TOOL_CALL_COMPLETED (the usual) AND AGENT_SKILL_CREATED
    (the ADR 0017 §40 observation surface)."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    mod._WRITER = mod.emit.AuditLogWriter(client)
    mod._CUSTOMER_SLUG = "acme"

    mod.on_post_tool_call(
        tool_name="skill_manage",
        args={"action": "create", "slug": "fresh-skill"},
        result="{}",
        task_id="",
        session_id="sess-1",
        tool_call_id="trace-1",
        duration_ms=10,
    )
    rows = client.rows()
    action_types = [r["action_type"] for r in rows]
    assert "TOOL_CALL_COMPLETED" in action_types
    assert "AGENT_SKILL_CREATED" in action_types


def test_on_post_tool_call_does_not_fire_agent_skill_created_for_other_tools(
    fake_ctx, monkeypatch
) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    mod._WRITER = mod.emit.AuditLogWriter(client)
    mod._CUSTOMER_SLUG = "acme"

    mod.on_post_tool_call(
        tool_name="email_create_draft",
        args={"action": "create"},
        result="{}",
        task_id="",
        session_id="sess-1",
        tool_call_id="trace-1",
        duration_ms=10,
    )
    rows = client.rows()
    action_types = [r["action_type"] for r in rows]
    assert "AGENT_SKILL_CREATED" not in action_types


def test_on_post_tool_call_does_not_fire_agent_skill_created_for_skill_manage_delete(
    fake_ctx, monkeypatch
) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    client = FakeD1Client()
    mod._WRITER = mod.emit.AuditLogWriter(client)
    mod._CUSTOMER_SLUG = "acme"

    mod.on_post_tool_call(
        tool_name="skill_manage",
        args={"action": "delete", "slug": "old-skill"},
        result="{}",
        task_id="",
        session_id="sess-1",
        tool_call_id="trace-1",
        duration_ms=10,
    )
    rows = client.rows()
    action_types = [r["action_type"] for r in rows]
    assert "AGENT_SKILL_CREATED" not in action_types


# ---------------------------------------------------------------------------
# ACCEPTED_ACTION_TYPES schema additions
# ---------------------------------------------------------------------------


def test_subagent_action_types_accepted() -> None:
    mod = load_plugin("hermes-smd-audit")
    assert "SUBAGENT_STOPPED" in mod.schemas.ACCEPTED_ACTION_TYPES
    assert "SUBAGENT_INCOMPLETE" in mod.schemas.ACCEPTED_ACTION_TYPES
    assert "AGENT_SKILL_CREATED" in mod.schemas.ACCEPTED_ACTION_TYPES


def test_register_wires_subagent_stop_hook(fake_ctx, monkeypatch) -> None:
    """register(ctx) MUST register subagent_stop alongside the existing hooks."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-audit")
    mod.register(fake_ctx)
    assert "post_tool_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered
    assert "subagent_stop" in fake_ctx.registered
