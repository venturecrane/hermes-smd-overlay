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


def test_append_derives_item_key_and_token_from_components(escalation):
    """The tool hashes the identity tuple ITSELF (the first live probe proved
    a model-authored item_key forks the pre_run join: it wrote a colon-joined
    composite the sha256 join never matched)."""
    plugin, requests = escalation
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "client-verification-tracker",
                "matter_id": "m-1",
                "source_id": "task-1",
                "label": "client-verification",
                "authored_date": None,
                "event": "chased",
                "attempt": 2,
            }
        )
    )
    expected_key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    expected_token = escalation_ledger.token_for(expected_key)
    assert out["ok"] is True
    assert out["item_key"] == expected_key  # echoed for the turn
    assert out["token"] == expected_token
    assert len(requests) == 1
    req = requests[0]
    assert req["action"] == "escalation_event_append"
    event = req["event"]
    assert event["item_key"] == expected_key  # EXACTLY the pre_run gate's key
    assert event["token"] == expected_token
    assert event["event"] == "chased"
    assert event["attempt"] == 2
    assert event["ts"] is None  # broker stamps server-side; agent cannot backdate
    assert event["v"] == escalation_ledger.SCHEMA_VERSION


def test_derive_only_returns_identity_and_writes_nothing(escalation):
    """ss #1935: the alert body must quote real broker-derived ACK codes, but the
    safe failure direction (send fails -> nothing recorded -> re-fires next run)
    requires the raise to be appended AFTER the send. derive_only=true is the
    first step of that ordering: identity out, zero events written."""
    plugin, requests = escalation
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-1",
                "source_id": "task-1",
                "label": "records-outstanding",
                "authored_date": "2026-07-11",
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
            }
        )
    )
    expected_key = escalation_ledger.item_key("m-1", "task-1", "records-outstanding", "2026-07-11")
    assert out["ok"] is True
    assert out["written"] is False
    assert out["item_key"] == expected_key
    assert out["token"] == escalation_ledger.token_for(expected_key)
    assert requests == []  # NOTHING reached the broker


def test_derive_only_matches_the_later_real_append(escalation):
    """Determinism guard: the token quoted in the alert (derive_only) and the
    token recorded by the post-send append are the same value."""
    plugin, requests = escalation
    components = {
        "skill": "deadline-miss-escalator",
        "matter_id": "m-1",
        "source_id": "task-9",
        "label": "lien-payoff",
        "authored_date": None,
        "event": "fired",
        "attempt": 1,
    }
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    appended = json.loads(plugin._escalation_append(components))
    assert derived["token"] == appended["token"]
    assert derived["item_key"] == appended["item_key"]
    assert len(requests) == 1  # only the second call wrote


def test_derive_only_rejects_ack_token(escalation):
    plugin, requests = escalation
    with pytest.raises(ValueError, match="one or the other"):
        plugin._escalation_append(
            {
                "skill": "s",
                "event": "acked",
                "attempt": 1,
                "ack_token": "ACK-ABCDEF",
                "derive_only": True,
            }
        )
    assert requests == []


def test_append_idless_item_gets_no_token(escalation):
    plugin, requests = escalation
    json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-1",
                "source_id": None,
                "label": "sol-date",
                "authored_date": "2026-08-01",
                "event": "fired",
                "attempt": 1,
            }
        )
    )
    assert requests[0]["event"]["token"] is None  # blanket-ack-only group


def test_acked_resolves_identity_from_token(escalation, tmp_path, monkeypatch):
    """The acker knows the ACK code from the reply, not the identity tuple —
    the tool resolves the token against the ledger's prior raises."""
    plugin, requests = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "ledger.jsonl"
    fired = escalation_ledger.make_event(
        skill="deadline-miss-escalator",
        matter_id="m-1",
        item_key=key,
        event="fired",
        attempt=1,
        token=token,
        ts="2026-07-14T09:00:00.000Z",
    )
    ledger_file.write_text(json.dumps(fired) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "acked",
                "attempt": 1,
                "ack_token": token,
            }
        )
    )
    assert out["ok"] is True
    assert requests[0]["event"]["item_key"] == key
    assert requests[0]["event"]["matter_id"] == "m-1"


def test_acked_unknown_token_is_rejected_before_the_broker(escalation, tmp_path, monkeypatch):
    plugin, requests = escalation
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(tmp_path / "empty.jsonl"))
    with pytest.raises(ValueError, match="never rang"):
        plugin._escalation_append(
            {"skill": "s", "event": "acked", "attempt": 1, "ack_token": "ACK-XXXXXX"}
        )
    assert requests == []  # nothing shipped


def test_append_returns_broker_rejection_verbatim(escalation, monkeypatch):
    plugin, _ = escalation
    monkeypatch.setattr(
        plugin,
        "_broker_request",
        lambda payload: {"ok": False, "error": "ValueError", "message": "no prior raise"},
    )
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "s",
                "matter_id": "m-1",
                "source_id": "t-1",
                "label": "x",
                "event": "fired",
                "attempt": 1,
            }
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
