"""Tests for plugins/hermes-smd-escalation (ss #1915).

The append validation lives in the broker (console side); here we prove the
tool handlers marshal the event correctly (broker faked), the state read folds
a real ledger file via the vendored shared/escalation_ledger twin, both tools
register well-formed, and both carry TOOL_ACTION_CLASS_MAP entries (the
unmapped-tool REFUSED fallback is exactly how the execute_code gap this plugin
closes was surfaced).
"""

from __future__ import annotations

import json

import pytest

from shared import escalation_ledger
from shared.action_classes import ActionClass, classify_tool
from tests.conftest import load_plugin


@pytest.fixture
def escalation(monkeypatch):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "evt-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    return plugin, requests


def test_append_marshals_event_through_broker_verb(escalation):
    plugin, requests = escalation
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "client-verification-tracker",
                "matter_id": "m-1",
                "item_key": "a" * 64,
                "event": "chased",
                "attempt": 2,
                "token": "ACK-7Q3M2K",
            }
        )
    )
    assert out == {"ok": True, "id": "evt-1"}
    assert len(requests) == 1
    req = requests[0]
    assert req["action"] == "escalation_event_append"
    event = req["event"]
    assert event["event"] == "chased"
    assert event["attempt"] == 2
    assert event["ts"] is None  # broker stamps server-side; agent cannot backdate
    assert event["v"] == escalation_ledger.SCHEMA_VERSION


def test_append_returns_broker_rejection_verbatim(escalation, monkeypatch):
    plugin, _ = escalation
    monkeypatch.setattr(
        plugin,
        "_broker_request",
        lambda payload: {"ok": False, "error": "ValueError", "message": "no prior raise"},
    )
    out = json.loads(
        plugin._escalation_append(
            {"skill": "s", "item_key": "k", "event": "acked", "attempt": 1, "token": "ACK-X"}
        )
    )
    assert out["ok"] is False
    assert "no prior raise" in out["message"]


def test_state_folds_ledger_file(escalation, tmp_path, monkeypatch):
    plugin, _ = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "escalation-ledger.jsonl"
    events = [
        escalation_ledger.make_event(
            skill="client-verification-tracker",
            matter_id="m-1",
            item_key=key,
            event="chased",
            attempt=1,
            token=token,
            ts="2026-07-14T09:00:00.000Z",
        ),
    ]
    ledger_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(plugin._escalation_state({}))
    assert out["event_count"] == 1
    assert out["item_count"] == 1
    row = out["items"][key]
    assert row["attempts"] == 1
    assert row["token"] == token
    assert row["last_raised_date"] == "2026-07-14"


def test_state_filters_by_skill(escalation, tmp_path, monkeypatch):
    plugin, _ = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "deadline", None)
    lines = [
        json.dumps(
            escalation_ledger.make_event(
                skill=skill,
                matter_id="m-1",
                item_key=key + suffix,
                event="fired",
                attempt=1,
                ts="2026-07-14T09:00:00.000Z",
            )
        )
        for skill, suffix in (
            ("deadline-miss-escalator", ""),
            ("client-verification-tracker", "x"),
        )
    ]
    ledger_file = tmp_path / "ledger.jsonl"
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(plugin._escalation_state({"skill": "deadline-miss-escalator"}))
    assert out["event_count"] == 1
    assert out["item_count"] == 1


def test_state_missing_file_is_empty_not_error(escalation, monkeypatch, tmp_path):
    plugin, _ = escalation
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(tmp_path / "absent.jsonl"))
    out = json.loads(plugin._escalation_state({}))
    assert out == {"event_count": 0, "item_count": 0, "items": {}}


def test_register_registers_both_tools(escalation):
    plugin, _ = escalation
    registered: list[dict] = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    plugin.register(Ctx())
    names = {r["name"] for r in registered}
    assert names == {"escalation_append", "escalation_state"}
    for r in registered:
        assert "parameters" in r["schema"]  # function shape, not bare JSON-schema


def test_tools_are_mapped_in_action_class_registry():
    assert classify_tool("escalation_append").action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool("escalation_state").action_class is ActionClass.READ
    assert classify_tool("escalation_append").unmapped is False


def test_vendored_ledger_twin_matches_reference_shapes():
    # Guard the vendored twin's load-bearing API (the console-side sync test
    # guards byte-identity; this guards the plugin's import surface).
    for name in (
        "read_ledger",
        "derive_state",
        "token_for",
        "item_key",
        "make_event",
        "SCHEMA_VERSION",
        "DEFAULT_LEDGER_PATH",
    ):
        assert hasattr(escalation_ledger, name)
