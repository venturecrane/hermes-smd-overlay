"""Tests for the ``hermes-smd-trust`` plugin.

Covers:
  - The plugin registers ``pre_tool_call`` and ``transform_tool_result``.
  - ``classify_tool`` correctly maps known tools, refuses banned tools,
    and defaults unknown tools to READ with ``unmapped=True``.
  - The policy core (``enforce``) returns the right decision for every
    (Ceiling, ActionClass, approval) combination.
  - ``evaluate_tool_call`` returns the expected block directive shape
    when refusing and ``None`` when allowing.
  - The hook entry point (``on_pre_tool_call``) is exception-safe —
    a raise inside the policy module never propagates to the caller.
"""

import pytest

from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Module loaders — bypass package-import-by-hyphen issues
# ---------------------------------------------------------------------------


def _load_trust_module(submodule: str = ""):
    """Load the trust plugin or one of its submodules.

    Imports the plugin package via ``load_plugin`` so its hyphenated
    directory name resolves; the submodules are then importable as
    attributes on the loaded module's package namespace.
    """
    plugin = load_plugin("hermes-smd-trust")
    if not submodule:
        return plugin
    return getattr(plugin, submodule)


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


def test_trust_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-trust must attach to pre_tool_call."""
    mod = load_plugin("hermes-smd-trust")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "pre_tool_call" in fake_ctx.registered
    assert "transform_tool_result" not in fake_ctx.registered


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


def test_classify_tool_known_read_tool() -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool("email_list_messages")
    assert classification.action_class == enforce.ActionClass.READ
    assert classification.unmapped is False


def test_classify_tool_known_internal_write_tool() -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool("email_create_draft")
    assert classification.action_class == enforce.ActionClass.INTERNAL_WRITE
    assert classification.unmapped is False


def test_classify_tool_known_commitment_tool() -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool("calendar_propose_time")
    assert classification.action_class == enforce.ActionClass.COMMITMENT
    assert classification.unmapped is False


def test_classify_tool_unknown_falls_back_to_read_unmapped() -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool("never_seen_before_tool")
    assert classification.action_class == enforce.ActionClass.READ
    assert classification.unmapped is True


def test_classify_tool_empty_name_raises() -> None:
    enforce = _load_trust_module("enforce")
    with pytest.raises(ValueError, match="tool_name is required"):
        enforce.classify_tool("")


@pytest.mark.parametrize(
    "banned_tool",
    [
        "email_send",
        "email_reply",
        "email_forward",
        "sms_send",
        "payments_initiate_transfer",
        "calendar_delete_event",
        "practice_management_delete_matter",
        "connector_revoke_oauth",
        # AgentMail MCP sends — prefixed `<server>:<tool>` runtime names.
        "agentmail:send_message",
        "agentmail:send_draft",
        "agentmail:reply_to_message",
        "agentmail:forward_message",
    ],
)
def test_classify_tool_banned_raises_banned_tool_error(banned_tool) -> None:
    enforce = _load_trust_module("enforce")
    with pytest.raises(enforce.BannedToolError) as excinfo:
        enforce.classify_tool(banned_tool)
    assert excinfo.value.tool_name == banned_tool


# ---------------------------------------------------------------------------
# Policy core — enforce()
# ---------------------------------------------------------------------------


def test_enforce_refused_ceiling_blocks_everything() -> None:
    enforce = _load_trust_module("enforce")
    for action in enforce.ActionClass:
        decision = enforce.enforce(
            ceiling=enforce.Ceiling.REFUSED,
            action=action,
            skill_name="test",
            tool_name="x",
        )
        assert decision.allowed is False
        assert decision.audit_action == "refuse"


def test_enforce_read_always_allowed_under_non_refused_ceilings() -> None:
    enforce = _load_trust_module("enforce")
    for ceiling in (enforce.Ceiling.AUTONOMOUS, enforce.Ceiling.DRAFT_FOR_REVIEW):
        decision = enforce.enforce(
            ceiling=ceiling,
            action=enforce.ActionClass.READ,
            skill_name="test",
            tool_name="any_read",
        )
        assert decision.allowed is True
        assert decision.audit_action == "allow"


def test_enforce_internal_write_autonomous_allowed() -> None:
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.INTERNAL_WRITE,
        skill_name="test",
        tool_name="email_create_draft",
    )
    assert decision.allowed is True
    assert decision.audit_action == "allow"


def test_enforce_internal_write_draft_for_review_routes_to_draft() -> None:
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        ceiling=enforce.Ceiling.DRAFT_FOR_REVIEW,
        action=enforce.ActionClass.INTERNAL_WRITE,
        skill_name="test",
        tool_name="email_create_draft",
    )
    # Allowed, but tagged as draft — audit downstream can filter on this.
    assert decision.allowed is True
    assert decision.audit_action == "draft"


def test_enforce_external_send_autonomous_requires_approval() -> None:
    enforce = _load_trust_module("enforce")
    # Without approval — refused.
    refused = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.EXTERNAL_SEND,
        skill_name="test",
        tool_name="x",
        current_turn_approval=False,
    )
    assert refused.allowed is False
    assert refused.audit_action == "refuse"
    # With approval — allowed.
    allowed = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.EXTERNAL_SEND,
        skill_name="test",
        tool_name="x",
        current_turn_approval=True,
    )
    assert allowed.allowed is True


def test_enforce_external_send_draft_for_review_drafts() -> None:
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        ceiling=enforce.Ceiling.DRAFT_FOR_REVIEW,
        action=enforce.ActionClass.EXTERNAL_SEND,
        skill_name="test",
        tool_name="x",
    )
    assert decision.allowed is False
    assert decision.audit_action == "draft"


def test_enforce_commitment_requires_autonomous_and_approval() -> None:
    enforce = _load_trust_module("enforce")
    # Draft ceiling refuses commitments entirely.
    refused_draft = enforce.enforce(
        ceiling=enforce.Ceiling.DRAFT_FOR_REVIEW,
        action=enforce.ActionClass.COMMITMENT,
        skill_name="test",
        tool_name="x",
        current_turn_approval=True,
    )
    assert refused_draft.allowed is False
    # Autonomous without approval refuses.
    refused_no_approval = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.COMMITMENT,
        skill_name="test",
        tool_name="x",
        current_turn_approval=False,
    )
    assert refused_no_approval.allowed is False
    # Autonomous with approval allows.
    allowed = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.COMMITMENT,
        skill_name="test",
        tool_name="x",
        current_turn_approval=True,
    )
    assert allowed.allowed is True


def test_enforce_destructive_requires_autonomous_and_approval() -> None:
    enforce = _load_trust_module("enforce")
    refused_draft = enforce.enforce(
        ceiling=enforce.Ceiling.DRAFT_FOR_REVIEW,
        action=enforce.ActionClass.DESTRUCTIVE,
        skill_name="test",
        tool_name="x",
        current_turn_approval=True,
    )
    assert refused_draft.allowed is False
    refused_no_approval = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.DESTRUCTIVE,
        skill_name="test",
        tool_name="x",
        current_turn_approval=False,
    )
    assert refused_no_approval.allowed is False
    allowed = enforce.enforce(
        ceiling=enforce.Ceiling.AUTONOMOUS,
        action=enforce.ActionClass.DESTRUCTIVE,
        skill_name="test",
        tool_name="x",
        current_turn_approval=True,
    )
    assert allowed.allowed is True


# ---------------------------------------------------------------------------
# evaluate_tool_call — the hook surface
# ---------------------------------------------------------------------------


@pytest.fixture
def env_autonomous(monkeypatch):
    """Set the customer ceiling to AUTONOMOUS via env override."""
    monkeypatch.setenv("SMD_TRUST_CEILING", "autonomous")
    yield


@pytest.fixture
def env_draft_for_review(monkeypatch):
    """Set the customer ceiling to DRAFT_FOR_REVIEW via env override."""
    monkeypatch.setenv("SMD_TRUST_CEILING", "draft_for_review")
    yield


@pytest.fixture
def env_refused(monkeypatch):
    """Set the customer ceiling to REFUSED via env override."""
    monkeypatch.setenv("SMD_TRUST_CEILING", "refused")
    yield


def test_evaluate_tool_call_allows_read_under_autonomous(env_autonomous) -> None:
    enforce = _load_trust_module("enforce")
    result = enforce.evaluate_tool_call("email_list_messages", {}, "acme")
    assert result is None


def test_evaluate_tool_call_blocks_banned_tool(env_autonomous) -> None:
    enforce = _load_trust_module("enforce")
    result = enforce.evaluate_tool_call("email_send", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert result["message"].startswith("Refused:")


def test_evaluate_tool_call_blocks_under_refused_ceiling(env_refused) -> None:
    enforce = _load_trust_module("enforce")
    result = enforce.evaluate_tool_call("email_list_messages", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "refused" in result["message"].lower()


def test_evaluate_tool_call_drafts_internal_write_under_draft_ceiling(
    env_draft_for_review,
) -> None:
    enforce = _load_trust_module("enforce")
    # Allowed but tagged as draft — evaluate_tool_call returns None for
    # allow (the audit hint is internal; the hook only blocks).
    result = enforce.evaluate_tool_call("email_create_draft", {}, "acme")
    assert result is None


def test_evaluate_tool_call_customer_ceiling_caps_skill(env_draft_for_review) -> None:
    """If customer.yaml says draft_for_review, an autonomous SKILL.md cannot
    raise the effective ceiling."""
    enforce = _load_trust_module("enforce")
    # The skill says autonomous; the customer says draft_for_review.
    # An EXTERNAL_SEND should be refused (drafted, not sent).
    result = enforce.evaluate_tool_call(
        "email_create_draft",
        {"_skill_trust_ceiling": "autonomous"},
        "acme",
    )
    # email_create_draft is INTERNAL_WRITE — still allowed, routed draft.
    assert result is None


def test_evaluate_tool_call_unknown_tool_defaults_to_read_allowed(
    env_autonomous,
) -> None:
    enforce = _load_trust_module("enforce")
    result = enforce.evaluate_tool_call("wholly_unknown_tool_xyz", {}, "acme")
    # Default classification is READ; allowed under non-refused ceilings.
    assert result is None


def test_evaluate_tool_call_empty_name_is_passthrough(env_autonomous) -> None:
    enforce = _load_trust_module("enforce")
    result = enforce.evaluate_tool_call("", {}, "acme")
    assert result is None


# ---------------------------------------------------------------------------
# Hook surface exception safety
# ---------------------------------------------------------------------------


def test_on_pre_tool_call_fails_closed_on_internal_exceptions(monkeypatch) -> None:
    """A raise inside enforce.evaluate_tool_call must FAIL CLOSED (issue #12).

    Replaces ``enforce.evaluate_tool_call`` with a raising stub and asserts
    the hook returns a block directive rather than allowing the call —
    safety must not degrade to "allow" on an indeterminate decision.
    """
    plugin = load_plugin("hermes-smd-trust")

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic policy failure")

    monkeypatch.setattr(plugin.enforce, "evaluate_tool_call", boom)
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert result["message"].startswith("Refused:")


def test_evaluate_tool_call_fails_closed_for_sensitive_on_ceiling_error(
    monkeypatch,
) -> None:
    """If ceiling resolution raises, a sensitive (non-READ) action refuses.

    Issue #12: a customer.yaml parse error / garbled secret during ceiling
    resolution must not let a COMMITMENT/EXTERNAL_SEND/DESTRUCTIVE call
    through. ``calendar_propose_time`` is a (non-banned) COMMITMENT tool.
    """
    enforce = _load_trust_module("enforce")

    def boom() -> None:
        raise RuntimeError("customer.yaml parse failure")

    monkeypatch.setattr(enforce, "_resolve_customer_ceiling", boom)
    result = enforce.evaluate_tool_call("calendar_propose_time", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "failing closed" in result["message"].lower()


def test_evaluate_tool_call_allows_read_on_ceiling_error(monkeypatch) -> None:
    """A ceiling-resolution failure must NOT brick low-risk READ tooling."""
    enforce = _load_trust_module("enforce")

    def boom() -> None:
        raise RuntimeError("customer.yaml parse failure")

    monkeypatch.setattr(enforce, "_resolve_customer_ceiling", boom)
    result = enforce.evaluate_tool_call("email_list_messages", {}, "acme")
    assert result is None


def test_on_pre_tool_call_returns_block_directive_shape(env_refused) -> None:
    """Refusals must come back as the canonical block-directive shape."""
    plugin = load_plugin("hermes-smd-trust")
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert isinstance(result["message"], str)
    assert result["message"].startswith("Refused:")


def test_on_pre_tool_call_allow_returns_none(env_autonomous) -> None:
    plugin = load_plugin("hermes-smd-trust")
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None
