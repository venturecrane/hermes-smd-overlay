"""Tests for the ``hermes-smd-trust`` plugin (ADR 0056 exposure model).

Covers:
  - The plugin registers ``pre_tool_call`` + ``post_tool_call``.
  - ``classify_tool`` maps known tools, refuses banned tools, and fails closed
    for unknown tools to REFUSED with ``unmapped=True`` (issue #1327).
  - The policy core (``enforce``) returns the right decision for every
    (exposure, ActionClass, approval) combination under the persona-exposure
    model — unauthored non-read classes are fail-closed (ADR 0056).
  - ``evaluate_tool_call`` reads the active persona's exposure from the trusted
    config and returns the canonical block directive / ``None``.
  - The hook entry point is exception-safe and fails closed (issue #12).
"""

import pytest

from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Module loaders — bypass package-import-by-hyphen issues
# ---------------------------------------------------------------------------


def _load_trust_module(submodule: str = ""):
    plugin = load_plugin("hermes-smd-trust")
    if not submodule:
        return plugin
    return getattr(plugin, submodule)


def _set_exposure(monkeypatch, enforce_mod, mapping, *, persona="marcus") -> None:
    """Fix the active persona's resolved exposure on ``enforce_mod`` for a test.

    Patches ``_resolve_persona_exposure`` to return ``mapping`` regardless of the
    persona slug, and sets the active-profile env so persona resolution is
    realistic for the audit line.
    """
    monkeypatch.setattr(enforce_mod, "_resolve_persona_exposure", lambda slug="": dict(mapping))
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", persona)


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


def test_trust_registers_expected_hooks(fake_ctx) -> None:
    mod = load_plugin("hermes-smd-trust")
    assert callable(mod.register)
    mod.register(fake_ctx)
    assert "pre_tool_call" in fake_ctx.registered
    assert "post_tool_call" in fake_ctx.registered  # A1 provenance recording
    assert "transform_tool_result" not in fake_ctx.registered


# ---------------------------------------------------------------------------
# Tool classification (unchanged by ADR 0056)
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


@pytest.mark.parametrize(
    "tool_name",
    [
        "read_file",
        "search_files",
        "skills_list",
        "skill_view",
        "session_search",
        "memory_search",
        "memory_get_rule",
        "memory_list_rules",
        "vision_analyze",
    ],
)
def test_mission_critical_native_reads_are_reachable(tool_name) -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool(tool_name)
    assert classification.action_class == enforce.ActionClass.READ
    assert classification.unmapped is False


@pytest.mark.parametrize(
    "tool_name",
    [
        "todo",
        "record_peer_preference",
        "clarify",
        "write_file",
        "patch",
    ],
)
def test_native_internal_writes_are_not_unmapped(tool_name) -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool(tool_name)
    assert classification.action_class == enforce.ActionClass.INTERNAL_WRITE
    assert classification.unmapped is False


@pytest.mark.parametrize(
    "tool_name",
    [
        "execute_code",
        "terminal",
        "process",
        "delegate_task",
        "computer_use",
        "cronjob",
        "skill_manage",
    ],
)
def test_high_power_native_tools_remain_code_execution(tool_name) -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool(tool_name)
    assert classification.action_class == enforce.ActionClass.CODE_EXECUTION
    assert classification.unmapped is False


def test_classify_tool_unknown_fails_closed_to_refused_unmapped() -> None:
    enforce = _load_trust_module("enforce")
    classification = enforce.classify_tool("never_seen_before_tool")
    assert classification.action_class == enforce.ActionClass.REFUSED
    assert classification.action_class != enforce.ActionClass.READ
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
    ],
)
def test_classify_tool_banned_raises_banned_tool_error(banned_tool) -> None:
    enforce = _load_trust_module("enforce")
    with pytest.raises(enforce.BannedToolError) as excinfo:
        enforce.classify_tool(banned_tool)
    assert excinfo.value.tool_name == banned_tool


# ---------------------------------------------------------------------------
# Policy core — enforce() under the exposure model
# ---------------------------------------------------------------------------


def test_enforce_read_always_allowed_regardless_of_exposure() -> None:
    enforce = _load_trust_module("enforce")
    for exposure in ({}, {enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.REFUSED}):
        decision = enforce.enforce(
            action=enforce.ActionClass.READ,
            exposure=exposure,
            tool_name="any_read",
        )
        assert decision.allowed is True
        assert decision.audit_action == "allow"


def test_enforce_unauthored_non_read_classes_are_fail_closed() -> None:
    """ADR 0056: every non-read class with no authored exposure is REFUSED."""
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    for action in (
        A.INTERNAL_WRITE,
        A.EXTERNAL_SEND,
        A.COMMITMENT,
        A.DESTRUCTIVE,
        A.CODE_EXECUTION,
    ):
        decision = enforce.enforce(action=action, exposure={}, tool_name="x")
        assert decision.allowed is False
        assert decision.audit_action == "refuse"
        assert decision.authored_ceiling is None
        assert decision.effective_ceiling == enforce.Ceiling.REFUSED


def test_enforce_internal_write_autonomous_allowed() -> None:
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        action=enforce.ActionClass.INTERNAL_WRITE,
        exposure={enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.AUTONOMOUS},
        tool_name="email_create_draft",
    )
    assert decision.allowed is True
    assert decision.audit_action == "allow"
    assert decision.authored_ceiling == enforce.Ceiling.AUTONOMOUS


def test_enforce_internal_write_draft_for_review_executes_and_says_so() -> None:
    """An internal write at draft_for_review is ALLOWED, and the record says so.

    The reason string used to read "internal write routed to draft folder",
    which described a routing step this branch does not perform. On 2026-08-21
    the ledger row for a ``mcp_smokeball_create_memo`` that executed against the
    firm's production Smokeball, and landed on a real matter, carried exactly
    that phrase (ss-console#2511). An auditor scanning the journal for writes
    would have read it as a draft and moved on.

    ``audit_action`` stays ``draft``: it is the ceiling's own vocabulary and
    other surfaces join on it. The human-readable half is what changed.
    """
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        action=enforce.ActionClass.INTERNAL_WRITE,
        exposure={enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.DRAFT_FOR_REVIEW},
        tool_name="email_create_draft",
    )
    assert decision.allowed is True
    assert decision.audit_action == "draft"
    assert "routed to draft folder" not in decision.reason
    assert "executed" in decision.reason


def test_no_allowed_decision_describes_itself_as_routed_to_a_draft_folder() -> None:
    """The general form, so the phrase cannot come back on a neighbouring branch.

    Every (action class x ceiling) pair the policy core will allow, checked for
    the one phrase that made an executed write unreadable in the journal. A
    WITHHELD decision may legitimately talk about drafting: nothing ran, and the
    content really is being held for review. It is the ALLOWED ones that must
    not.
    """
    enforce = _load_trust_module("enforce")
    ceilings = list(enforce.Ceiling)
    offenders = []
    for action in enforce.ActionClass:
        for ceiling in ceilings:
            try:
                decision = enforce.enforce(
                    action=action,
                    exposure={action: ceiling},
                    tool_name="email_create_draft",
                )
            except Exception:  # noqa: BLE001 - a class the core declines to evaluate
                continue
            if decision.allowed and "routed to draft folder" in decision.reason:
                offenders.append((action.value, ceiling.value, decision.reason))
    assert offenders == [], offenders


def test_enforce_external_send_exposure_governs() -> None:
    """ADR 0056: external_send is governed by the persona's authored exposure.
    Unauthored is fail-closed; explicit autonomous sends; explicit refused
    blocks; a vertical floor only narrows."""
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    C = enforce.Ceiling
    # Unauthored → fail-closed (refused, no draft).
    d = enforce.enforce(action=A.EXTERNAL_SEND, exposure={}, tool_name="x")
    assert d.allowed is False and d.audit_action == "refuse"
    # Authored autonomous → send.
    d = enforce.enforce(
        action=A.EXTERNAL_SEND, exposure={A.EXTERNAL_SEND: C.AUTONOMOUS}, tool_name="x"
    )
    assert d.allowed is True and d.audit_action == "allow"
    # Authored refused → refuse.
    d = enforce.enforce(
        action=A.EXTERNAL_SEND, exposure={A.EXTERNAL_SEND: C.REFUSED}, tool_name="x"
    )
    assert d.allowed is False and d.audit_action == "refuse"
    # Vertical floor narrows an autonomous exposure back to draft.
    d = enforce.enforce(
        action=A.EXTERNAL_SEND,
        exposure={A.EXTERNAL_SEND: C.AUTONOMOUS},
        tool_name="x",
        vertical_floors={A.EXTERNAL_SEND: C.DRAFT_FOR_REVIEW},
    )
    assert d.allowed is False and d.audit_action == "draft"
    assert d.vertical_floor == C.DRAFT_FOR_REVIEW
    assert d.effective_ceiling == C.DRAFT_FOR_REVIEW


def test_enforce_external_send_authored_draft_for_review_drafts() -> None:
    enforce = _load_trust_module("enforce")
    decision = enforce.enforce(
        action=enforce.ActionClass.EXTERNAL_SEND,
        exposure={enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
        tool_name="x",
    )
    assert decision.allowed is False
    assert decision.audit_action == "draft"


def test_enforce_external_send_confirm_ceiling() -> None:
    """ADR 0071: the confirm ceiling sends only with an explicit current-turn
    approval, else withholds pending approval (await_approval); a draft_for_review
    vertical floor narrows an authored confirm; the taint-gate still dominates."""
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    C = enforce.Ceiling
    # confirm without approval → withheld pending approval (not draft, not refuse).
    d = enforce.enforce(
        action=A.EXTERNAL_SEND, exposure={A.EXTERNAL_SEND: C.CONFIRM}, tool_name="x"
    )
    assert d.allowed is False and d.audit_action == "await_approval"
    assert d.effective_ceiling == C.CONFIRM
    # confirm WITH current-turn approval → send.
    d = enforce.enforce(
        action=A.EXTERNAL_SEND,
        exposure={A.EXTERNAL_SEND: C.CONFIRM},
        tool_name="x",
        current_turn_approval=True,
    )
    assert d.allowed is True and d.audit_action == "allow"
    # A draft_for_review vertical floor narrows an authored confirm to draft even
    # with approval (confirm < draft_for_review in the restrictiveness ordering).
    d = enforce.enforce(
        action=A.EXTERNAL_SEND,
        exposure={A.EXTERNAL_SEND: C.CONFIRM},
        tool_name="x",
        current_turn_approval=True,
        vertical_floors={A.EXTERNAL_SEND: C.DRAFT_FOR_REVIEW},
    )
    assert d.allowed is False and d.audit_action == "draft"
    # Taint-gate dominates: a tainted turn cannot reach the confirm allow-path even
    # with approval set (an inbound/injected "approval" must never turn into a send).
    d = enforce.enforce(
        action=A.EXTERNAL_SEND,
        exposure={A.EXTERNAL_SEND: C.CONFIRM},
        tool_name="x",
        current_turn_approval=True,
        inbound_trust_class="external_untrusted",
    )
    assert d.allowed is False and d.audit_action == "refuse"


def test_enforce_commitment_requires_autonomous_exposure_and_approval() -> None:
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    C = enforce.Ceiling
    # Unauthored → refuse even with approval.
    d = enforce.enforce(action=A.COMMITMENT, exposure={}, tool_name="x", current_turn_approval=True)
    assert d.allowed is False
    # Draft exposure refuses commitments entirely.
    d = enforce.enforce(
        action=A.COMMITMENT,
        exposure={A.COMMITMENT: C.DRAFT_FOR_REVIEW},
        tool_name="x",
        current_turn_approval=True,
    )
    assert d.allowed is False
    # Autonomous without approval refuses.
    d = enforce.enforce(
        action=A.COMMITMENT,
        exposure={A.COMMITMENT: C.AUTONOMOUS},
        tool_name="x",
        current_turn_approval=False,
    )
    assert d.allowed is False
    # Autonomous with approval allows.
    d = enforce.enforce(
        action=A.COMMITMENT,
        exposure={A.COMMITMENT: C.AUTONOMOUS},
        tool_name="x",
        current_turn_approval=True,
    )
    assert d.allowed is True


def test_enforce_destructive_requires_autonomous_exposure_and_approval() -> None:
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    C = enforce.Ceiling
    d = enforce.enforce(
        action=A.DESTRUCTIVE, exposure={}, tool_name="x", current_turn_approval=True
    )
    assert d.allowed is False
    d = enforce.enforce(
        action=A.DESTRUCTIVE,
        exposure={A.DESTRUCTIVE: C.DRAFT_FOR_REVIEW},
        tool_name="x",
        current_turn_approval=True,
    )
    assert d.allowed is False
    d = enforce.enforce(
        action=A.DESTRUCTIVE,
        exposure={A.DESTRUCTIVE: C.AUTONOMOUS},
        tool_name="x",
        current_turn_approval=False,
    )
    assert d.allowed is False
    d = enforce.enforce(
        action=A.DESTRUCTIVE,
        exposure={A.DESTRUCTIVE: C.AUTONOMOUS},
        tool_name="x",
        current_turn_approval=True,
    )
    assert d.allowed is True


def test_enforce_decision_carries_full_audit_trail() -> None:
    """ADR 0056: every decision records action class, authored ceiling, vertical
    floor, and effective ceiling alongside the allow/draft/refuse audit_action."""
    enforce = _load_trust_module("enforce")
    A = enforce.ActionClass
    C = enforce.Ceiling
    d = enforce.enforce(
        action=A.EXTERNAL_SEND,
        exposure={A.EXTERNAL_SEND: C.AUTONOMOUS},
        tool_name="x",
        vertical_floors={A.EXTERNAL_SEND: C.DRAFT_FOR_REVIEW},
    )
    assert d.action_class == A.EXTERNAL_SEND
    assert d.authored_ceiling == C.AUTONOMOUS
    assert d.vertical_floor == C.DRAFT_FOR_REVIEW
    assert d.effective_ceiling == C.DRAFT_FOR_REVIEW


# ---------------------------------------------------------------------------
# resolve_ceiling
# ---------------------------------------------------------------------------


def test_resolve_ceiling_unauthored_is_refused() -> None:
    enforce = _load_trust_module("enforce")
    eff = enforce.resolve_ceiling(enforce.ActionClass.EXTERNAL_SEND, {})
    assert eff == enforce.Ceiling.REFUSED


def test_resolve_ceiling_authored_autonomous() -> None:
    enforce = _load_trust_module("enforce")
    eff = enforce.resolve_ceiling(
        enforce.ActionClass.EXTERNAL_SEND,
        {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS},
    )
    assert eff == enforce.Ceiling.AUTONOMOUS


def test_resolve_ceiling_vertical_floor_narrows() -> None:
    enforce = _load_trust_module("enforce")
    eff = enforce.resolve_ceiling(
        enforce.ActionClass.EXTERNAL_SEND,
        {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS},
        {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    assert eff == enforce.Ceiling.DRAFT_FOR_REVIEW


# ---------------------------------------------------------------------------
# evaluate_tool_call — the hook surface (exposure resolved from trusted config)
# ---------------------------------------------------------------------------


def test_evaluate_tool_call_allows_read(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    assert enforce.evaluate_tool_call("email_list_messages", {}, "acme") is None


def test_evaluate_tool_call_blocks_banned_tool(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call("email_send", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert result["message"].startswith("Refused:")


def test_evaluate_tool_call_blocks_internal_write_unauthored(monkeypatch) -> None:
    """No authored internal_write exposure → fail-closed (refused)."""
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call("email_create_draft", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"


def test_evaluate_tool_call_allows_internal_write_when_authored_autonomous(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.AUTONOMOUS}
    )
    assert enforce.evaluate_tool_call("email_create_draft", {}, "acme") is None


def test_evaluate_tool_call_unknown_tool_fails_closed_blocked(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.AUTONOMOUS}
    )
    result = enforce.evaluate_tool_call("wholly_unknown_tool_xyz", {}, "acme")
    assert result is not None
    assert result["action"] == "block"


def test_evaluate_tool_call_empty_name_is_passthrough() -> None:
    enforce = _load_trust_module("enforce")
    assert enforce.evaluate_tool_call("", {}, "acme") is None


# ---------------------------------------------------------------------------
# Hook surface exception safety
# ---------------------------------------------------------------------------


def test_on_pre_tool_call_fails_closed_on_internal_exceptions(monkeypatch) -> None:
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


def test_evaluate_tool_call_fails_closed_for_sensitive_on_exposure_error(monkeypatch) -> None:
    """If exposure resolution raises, a sensitive (non-READ) action refuses."""
    enforce = _load_trust_module("enforce")

    def boom(_slug="") -> None:
        raise RuntimeError("customer.yaml parse failure")

    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", boom)
    result = enforce.evaluate_tool_call("calendar_propose_time", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "failing closed" in result["message"].lower()


def test_evaluate_tool_call_allows_read_on_exposure_error(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")

    def boom(_slug="") -> None:
        raise RuntimeError("customer.yaml parse failure")

    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", boom)
    assert enforce.evaluate_tool_call("email_list_messages", {}, "acme") is None


def test_on_pre_tool_call_returns_block_directive_shape(monkeypatch) -> None:
    """Refusals come back as the canonical block-directive shape. Empty exposure
    → a sensitive internal_write is refused."""
    plugin = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(plugin.enforce, "_resolve_persona_exposure", lambda slug="": {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    result = plugin.on_pre_tool_call(
        tool_name="email_create_draft",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert result["message"].startswith("Refused:")


def test_on_pre_tool_call_allow_returns_none(monkeypatch) -> None:
    plugin = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(plugin.enforce, "_resolve_persona_exposure", lambda slug="": {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Agentmail / Clio classification regressions (unchanged by ADR 0056)
# ---------------------------------------------------------------------------


def test_agentmail_sends_classify_external_send() -> None:
    enforce = _load_trust_module("enforce")
    for t in (
        "agentmail:send_message",
        "agentmail:send_draft",
        "agentmail:reply_to_message",
        "agentmail:forward_message",
    ):
        c = enforce.classify_tool(t)
        assert c.action_class == enforce.ActionClass.EXTERNAL_SEND
        assert c.unmapped is False


def test_agentmail_runtime_mcp_names_classify_external_send() -> None:
    enforce = _load_trust_module("enforce")
    for t in (
        "mcp_agentmail_send_message",
        "mcp_agentmail_send_draft",
        "mcp_agentmail_reply_to_message",
        "mcp_agentmail_forward_message",
    ):
        c = enforce.classify_tool(t)
        assert c.action_class == enforce.ActionClass.EXTERNAL_SEND, t
        assert c.unmapped is False, t


def test_clio_runtime_mcp_names_are_all_classified() -> None:
    enforce = _load_trust_module("enforce")
    AC = enforce.ActionClass
    reads = [
        "mcp_clio_oktopeak_list_matters",
        "mcp_clio_oktopeak_get_matter",
        "mcp_clio_oktopeak_search_contacts",
        "mcp_clio_oktopeak_get_contact",
        "mcp_clio_oktopeak_list_documents",
        "mcp_clio_oktopeak_get_document",
        "mcp_clio_oktopeak_list_tasks",
        "mcp_clio_oktopeak_list_calendars",
        "mcp_clio_oktopeak_list_calendar_entries",
        "mcp_clio_oktopeak_list_time_entries",
        "mcp_clio_oktopeak_get_billing_summary",
        "mcp_clio_oktopeak_list_users",
        "mcp_clio_oktopeak_get_user",
        "mcp_clio_oktopeak_export_audit_log",
    ]
    internal_writes = [
        "mcp_clio_oktopeak_create_note",
        "mcp_clio_oktopeak_create_task",
        "mcp_clio_oktopeak_update_task",
        "mcp_clio_oktopeak_complete_task",
        "mcp_clio_oktopeak_upload_document",
    ]
    commitments = [
        "mcp_clio_oktopeak_create_matter",
        "mcp_clio_oktopeak_create_calendar_entry",
        "mcp_clio_oktopeak_log_time_entry",
        "mcp_clio_oktopeak_create_activity",
    ]
    for t in reads:
        c = enforce.classify_tool(t)
        assert c.unmapped is False, f"{t} unmapped -> READ fail-open"
        assert c.action_class == AC.READ, t
    for t in internal_writes:
        c = enforce.classify_tool(t)
        assert c.unmapped is False, f"{t} unmapped -> READ fail-open"
        assert c.action_class == AC.INTERNAL_WRITE, t
    for t in commitments:
        c = enforce.classify_tool(t)
        assert c.unmapped is False, f"{t} unmapped -> READ fail-open"
        assert c.action_class == AC.COMMITMENT, t


def test_agentmail_runtime_drafts_classify_internal_write() -> None:
    enforce = _load_trust_module("enforce")
    for t in ("mcp_agentmail_create_draft", "mcp_agentmail_update_draft"):
        c = enforce.classify_tool(t)
        assert c.action_class == enforce.ActionClass.INTERNAL_WRITE, t
        assert c.unmapped is False, t


def test_agentmail_runtime_deletes_classify_destructive() -> None:
    enforce = _load_trust_module("enforce")
    for t in ("mcp_agentmail_delete_inbox", "mcp_agentmail_delete_thread"):
        c = enforce.classify_tool(t)
        assert c.action_class == enforce.ActionClass.DESTRUCTIVE, t
        assert c.unmapped is False, t


def test_agentmail_runtime_send_unauthored_is_blocked(monkeypatch) -> None:
    """End-to-end: a live runtime send name is fail-closed without an authored
    external_send exposure on the active persona."""
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call(
        "mcp_agentmail_reply_to_message", {"text": "someone will be in touch"}, "smd"
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"


# ---------------------------------------------------------------------------
# Content-sensitivity floor (ADR 0031) via evaluate_tool_call
# ---------------------------------------------------------------------------


def test_send_unauthored_is_blocked_fail_closed(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call("agentmail:send_message", {"text": "hi there"}, "smd")
    assert isinstance(result, dict)
    assert result["action"] == "block"


def test_autonomous_clean_send_is_allowed(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert enforce.evaluate_tool_call("agentmail:send_message", args, "smd") is None


def test_content_floor_downgrades_money_send(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"subject": "Invoice attached", "text": "Please remit payment of $500 by Friday."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()


def test_content_floor_downgrades_contract_send(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"text": "Attached is the contract, please sign and return."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "smd")
    assert isinstance(result, dict)
    assert result["action"] == "block"


def test_send_draft_with_no_body_fails_toward_draft(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"draft_id": "d_1"}
    result = enforce.evaluate_tool_call("agentmail:send_draft", args, "smd")
    assert isinstance(result, dict)
    assert result["action"] == "block"


# ---------------------------------------------------------------------------
# Vertical-pack floors (_resolve_vertical_floors)
#
# No production vertical declares a floor today: the law-firm
# external-send-draft-floor was removed 2026-07 (Captain decision, ADR 0035 —
# outside-send is a firm-authored dial; supervision is held by audit +
# attribution + fail-closed entitlement). The floor machinery stays live for
# any future regulation-compelled floor and is exercised below by injecting a
# synthetic floor map into the loaded module.
# ---------------------------------------------------------------------------


@pytest.fixture
def env_vertical_law(monkeypatch):
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    yield


def _inject_floor(monkeypatch, enforce, vertical: str, floors: dict) -> None:
    """Inject a synthetic vertical floor into a loaded enforce module."""
    monkeypatch.setattr(enforce, "_VERTICAL_FLOORS", {vertical: floors})


def test_resolve_vertical_floors_law_firm_has_no_floor(env_vertical_law) -> None:
    # REGRESSION GUARD for the 2026-07 removal: the law-firm vertical resolves
    # to NO floors. Re-adding an external_send floor requires a Captain
    # decision reversing the removal, not a drive-by edit.
    enforce = _load_trust_module("enforce")
    assert enforce._resolve_vertical_floors() == {}


def test_resolve_vertical_floors_declared_floor_resolves(monkeypatch) -> None:
    # Machinery coverage: a vertical that DOES declare a floor still resolves it.
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("SMD_VERTICAL", "floored-test-vertical")
    _inject_floor(
        monkeypatch,
        enforce,
        "floored-test-vertical",
        {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    floors = enforce._resolve_vertical_floors()
    assert floors == {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW}


def test_resolve_vertical_floors_mixed_vertical_is_empty(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("SMD_VERTICAL", "mixed")
    assert enforce._resolve_vertical_floors() == {}


def test_resolve_vertical_failure_falls_through_to_env(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    monkeypatch.delenv("SMD_VERTICAL", raising=False)
    assert enforce._resolve_vertical() == ""
    assert enforce._resolve_vertical_floors() == {}


def test_law_authored_autonomous_clean_send_is_not_floored(monkeypatch, env_vertical_law) -> None:
    """THE 2026-07 behavior change: a law customer whose persona authored
    external_send=autonomous SENDS (clean body, no floor downgrade). The firm's
    authored exposure governs outside-send (ADR 0035); the pack no longer pins
    it. The content-sensitivity floor and taint gate still narrow when they
    apply — this body triggers neither."""
    enforce = _load_trust_module("enforce")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert enforce.evaluate_tool_call("agentmail:send_message", args, "acme") is None


def test_declared_floor_narrows_authored_autonomous_send_to_draft(monkeypatch) -> None:
    """Machinery coverage: a vertical that DOES declare an external_send floor
    still narrows an authored autonomous send to draft — even on a clean body."""
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("SMD_VERTICAL", "floored-test-vertical")
    _inject_floor(
        monkeypatch,
        enforce,
        "floored-test-vertical",
        {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.DRAFT_FOR_REVIEW},
    )
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    result = enforce.evaluate_tool_call("agentmail:send_message", args, "floored-test")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "draft" in result["message"].lower()


def test_non_law_authored_autonomous_clean_send_is_not_floored(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("SMD_VERTICAL", "mixed")
    _set_exposure(
        monkeypatch, enforce, {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS}
    )
    args = {"subject": "Saw your note", "text": "Got it, that works on my end. Talk soon."}
    assert enforce.evaluate_tool_call("agentmail:send_message", args, "smd") is None


def test_law_unauthored_send_stays_fail_closed(monkeypatch, env_vertical_law) -> None:
    """A law persona with NO authored external_send exposure is fail-closed
    (refused) — pure ADR 0035, no floor involved. Removing the pack floor
    granted nothing: unauthored is still no send, no draft."""
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call("agentmail:send_message", {"text": "hi there"}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"


# ---------------------------------------------------------------------------
# Volume-fault fail-closed — exposure read from the trusted customer.yaml
# ---------------------------------------------------------------------------


def _patch_from_volume_raise(monkeypatch, exc: Exception) -> None:
    import shared.customer_config as cc

    def raiser(cls, path=cc.DEFAULT_VOLUME_PATH):
        raise exc

    monkeypatch.setattr(cc.CustomerConfig, "from_volume", classmethod(raiser))


def test_garbled_customer_yaml_fails_closed_for_sensitive_action(monkeypatch) -> None:
    """A YAML parse fault on a provisioned Machine propagates so a sensitive
    (non-READ) action refuses with the fail-closed message."""
    import shared.customer_config as cc

    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    _patch_from_volume_raise(monkeypatch, cc.CustomerConfigError("customer.yaml is not valid YAML"))
    result = enforce.evaluate_tool_call("email_create_draft", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "failing closed" in result["message"]


def test_permission_error_on_volume_fails_closed_for_sensitive_action(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    _patch_from_volume_raise(monkeypatch, PermissionError("denied: /opt/data/customer.yaml"))
    result = enforce.evaluate_tool_call("email_create_draft", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "failing closed" in result["message"]


def test_volume_fault_still_allows_read(monkeypatch) -> None:
    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    _patch_from_volume_raise(monkeypatch, PermissionError("denied"))
    assert enforce.evaluate_tool_call("email_list_messages", {}, "acme") is None


def test_missing_customer_yaml_is_fail_closed_for_sensitive(monkeypatch) -> None:
    """The absent-file state (dev / test) resolves empty exposure — every
    non-read class fail-closed (ADR 0056 has no env exposure override). READ
    still passes."""
    import shared.customer_config as cc

    enforce = _load_trust_module("enforce")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    _patch_from_volume_raise(monkeypatch, cc.CustomerConfigMissingError("customer.yaml not found"))
    blocked = enforce.evaluate_tool_call("email_create_draft", {}, "acme")
    assert isinstance(blocked, dict) and blocked["action"] == "block"
    assert enforce.evaluate_tool_call("email_list_messages", {}, "acme") is None


# ---------------------------------------------------------------------------
# Tool-name normalization (unchanged by ADR 0056)
# ---------------------------------------------------------------------------


def test_classify_tool_normalizes_case_and_whitespace() -> None:
    enforce = _load_trust_module("enforce")
    assert enforce.classify_tool("Execute_Code").action_class is enforce.ActionClass.CODE_EXECUTION
    assert enforce.classify_tool(" TERMINAL ").action_class is enforce.ActionClass.CODE_EXECUTION
    assert enforce.classify_tool("Execute_Code").unmapped is False


def test_classify_tool_banned_lookup_is_case_insensitive() -> None:
    enforce = _load_trust_module("enforce")
    with pytest.raises(enforce.BannedToolError):
        enforce.classify_tool("EMAIL_SEND")


def test_mixed_case_code_execution_blocked_via_evaluate(monkeypatch) -> None:
    """A mixed-case execute_code call cannot slip the CODE_EXECUTION gate
    (fail-closed when unauthored)."""
    enforce = _load_trust_module("enforce")
    _set_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call("Execute_Code", {}, "acme")
    assert isinstance(result, dict)
    assert result["action"] == "block"


# ---------------------------------------------------------------------------
# Operator-pause wall (ss#2003): at HARD_STOP every tool call refuses,
# whatever woke the agent (the chokepoint covering cron-fired wakes).
# ---------------------------------------------------------------------------


def test_on_pre_tool_call_refuses_everything_while_paused(tmp_path, monkeypatch) -> None:
    from shared.cost_breaker import pin_hard_stops

    plugin = load_plugin("hermes-smd-trust")
    db = str(tmp_path / "sticky_stop.db")
    monkeypatch.setenv("SMD_STICKY_STOP_DB_PATH", db)
    pin_hard_stops(actor_id="portal-admin", reason="client pause", path=db)
    plugin._pause_cache["at"] = 0.0  # bust the TTL cache for the fresh db

    # Even a plain READ refuses while paused — pause is total, not tiered.
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert isinstance(result, dict)
    assert result["action"] == "block"
    assert "paused" in result["message"].lower()


def test_on_pre_tool_call_pause_wall_fails_open_on_read_error(tmp_path, monkeypatch) -> None:
    """A broken level read must not brick a healthy Machine — the wall is an
    ADDITIONAL chokepoint; primary stop enforcement lives at the gate/jobs."""
    plugin = load_plugin("hermes-smd-trust")
    monkeypatch.setenv("SMD_STICKY_STOP_DB_PATH", str(tmp_path / "nonexistent" / "x.db"))
    plugin._pause_cache["at"] = 0.0
    result = plugin.on_pre_tool_call(
        tool_name="email_list_messages",
        args={},
        task_id="t",
        session_id="s",
        tool_call_id="c",
    )
    assert result is None
