"""Tests for mediated Workspace classification, grants, and tool dispatch."""

import json

from shared.action_classes import ActionClass, classify_tool
from shared.workspace_broker import GRANT_ARG
from tests.conftest import load_plugin

WORKSPACE_CLASSES = {
    "workspace_gmail_search": ActionClass.READ,
    "workspace_gmail_get": ActionClass.READ,
    "workspace_gmail_create_draft": ActionClass.INTERNAL_WRITE,
    "workspace_gmail_modify": ActionClass.INTERNAL_WRITE,
    "workspace_gmail_archive": ActionClass.INTERNAL_WRITE,
    "workspace_calendar_list": ActionClass.READ,
    "workspace_calendar_get": ActionClass.READ,
    "workspace_calendar_create_draft": ActionClass.INTERNAL_WRITE,
    "workspace_calendar_update_draft": ActionClass.INTERNAL_WRITE,
    "workspace_drive_list": ActionClass.READ,
    "workspace_drive_get": ActionClass.READ,
    "workspace_drive_export": ActionClass.READ,
    "workspace_docs_create": ActionClass.INTERNAL_WRITE,
    "workspace_docs_get": ActionClass.READ,
    "workspace_docs_append": ActionClass.INTERNAL_WRITE,
    "workspace_sheets_create": ActionClass.INTERNAL_WRITE,
    "workspace_sheets_get_values": ActionClass.READ,
    "workspace_sheets_update_values": ActionClass.INTERNAL_WRITE,
}


class ToolContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, **kwargs) -> None:
        self.tools[kwargs["name"]] = kwargs


def test_every_workspace_tool_is_explicitly_classified() -> None:
    for name, expected in WORKSPACE_CLASSES.items():
        classification = classify_tool(name)
        assert classification.action_class is expected
        assert classification.unmapped is False


def test_plugin_registers_only_the_reviewed_surface() -> None:
    plugin = load_plugin("hermes-smd-workspace")
    ctx = ToolContext()
    plugin.register(ctx)
    assert set(ctx.tools) == set(WORKSPACE_CLASSES)
    assert "workspace_gmail_send" not in ctx.tools
    assert "workspace_drive_share" not in ctx.tools


def test_gmail_tools_expose_managed_mailbox_targeting() -> None:
    plugin = load_plugin("hermes-smd-workspace")
    ctx = ToolContext()
    plugin.register(ctx)
    for name in (
        "workspace_gmail_search",
        "workspace_gmail_get",
        "workspace_gmail_create_draft",
        "workspace_gmail_modify",
        "workspace_gmail_archive",
    ):
        props = ctx.tools[name]["schema"]["properties"]
        assert "mailbox" in props, f"{name} should accept a managed mailbox target"
    # send-as From is offered only where a draft is composed; mailbox stays optional.
    draft_props = ctx.tools["workspace_gmail_create_draft"]["schema"]["properties"]
    assert "from" in draft_props
    assert "mailbox" not in ctx.tools["workspace_gmail_create_draft"]["schema"]["required"]
    assert "from" not in ctx.tools["workspace_gmail_create_draft"]["schema"]["required"]


def test_handler_forwards_managed_mailbox_payload(monkeypatch) -> None:
    plugin = load_plugin("hermes-smd-workspace")
    captured = {}

    def fake_execute(operation, payload, grant):
        captured.update(operation=operation, payload=payload, grant=grant)
        return {"ok": True, "result": {"id": "draft-1"}, "receipt": {"signature": "s"}}

    monkeypatch.setattr(plugin, "execute", fake_execute)
    monkeypatch.setattr(plugin, "write_execution", lambda **_: None)
    handler = plugin._handler("workspace_gmail_create_draft")

    handler(
        {
            "to": "client@example.com",
            "subject": "Re: scheduling",
            "body": "Confirming.",
            "mailbox": "owner@firm.com",
            "from": "team@firm.com",
            GRANT_ARG: "grant-1",
        }
    )

    # mailbox/from ride in the payload so they are covered by the grant digest.
    assert captured["payload"]["mailbox"] == "owner@firm.com"
    assert captured["payload"]["from"] == "team@firm.com"
    assert GRANT_ARG not in captured["payload"]


def test_handler_requires_grant_and_strips_it_before_execute(monkeypatch) -> None:
    plugin = load_plugin("hermes-smd-workspace")
    captured = {}

    def fake_execute(operation, payload, grant):
        captured.update(operation=operation, payload=payload, grant=grant)
        return {
            "ok": True,
            "result": {"id": "doc-1"},
            "receipt": {"payload_digest": "abc", "signature": "signed"},
        }

    monkeypatch.setattr(plugin, "execute", fake_execute)
    monkeypatch.setattr(plugin, "write_execution", lambda **_: None)
    handler = plugin._handler("workspace_docs_create")

    result = handler({"title": "Test", GRANT_ARG: "grant-1"})

    assert json.loads(result) == {"id": "doc-1"}
    assert captured == {
        "operation": "workspace_docs_create",
        "payload": {"title": "Test"},
        "grant": "grant-1",
    }


def test_trust_hook_mints_grant_only_after_ceiling_allows(monkeypatch) -> None:
    plugin = load_plugin("hermes-smd-trust")
    args = {"title": "Test"}
    monkeypatch.setattr(plugin.enforce, "evaluate_tool_call", lambda *_: None)
    monkeypatch.setattr(plugin.outbound, "check_outbound_draft", lambda **_: None)
    monkeypatch.setattr(
        plugin,
        "authorize",
        lambda *_, **__: {"grant": "grant-1", "payload_digest": "digest-1"},
    )
    decisions = []
    monkeypatch.setattr(plugin, "write_decision", lambda **kwargs: decisions.append(kwargs))

    result = plugin.on_pre_tool_call(
        tool_name="workspace_docs_create",
        args=args,
        customer_slug="acme",
        session_id="session-1",
        tool_call_id="call-1",
    )

    assert result is None
    assert args[GRANT_ARG] == "grant-1"
    assert decisions[0]["payload_digest"] == "digest-1"


def test_trust_hook_fails_closed_when_broker_is_unavailable(monkeypatch) -> None:
    plugin = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(plugin.enforce, "evaluate_tool_call", lambda *_: None)
    monkeypatch.setattr(plugin, "authorize", lambda *_, **__: (_ for _ in ()).throw(OSError()))

    result = plugin.on_pre_tool_call(
        tool_name="workspace_drive_list",
        args={},
        customer_slug="acme",
    )

    assert result["action"] == "block"
