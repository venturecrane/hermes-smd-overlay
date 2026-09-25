"""Deleting calendar events as an administrator-confirmed act (ss-console,
2026-09-25).

The shape is the matter act's (``test_commitment_acts.py``) with one
difference: the payload is the withheld call's own list of events, because
which events to remove is the request itself and no config could author it.
Each test below tries to break one invariant:

* ``delete_events`` never fires without an administrator's written yes, and
  it may only be PROPOSED on a turn an administrator opened by email;
* at ``destructive: confirm`` ONLY a tool with an act shape is withheld as a
  proposal; every other destructive tool still refuses (``delete_file``);
* what executes on the confirming turn is the STORED list, whatever the model
  sends the second time;
* the approval is spent once and the commit names the approving message.

THE FALSIFIER, run against the parent commit 48726ab: every gate test fails
there, because ``_decide_approval_class`` has no confirm branch for DESTRUCTIVE
and the call is simply refused; the classification tests fail because the two
tools are unclassified (and so refused as unknown).
"""

from __future__ import annotations

import json

import pytest

from shared import act_broker, customer_config
from shared.action_classes import ActionClass, classify_tool
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT, InboundOrigin
from shared.pending_acts import PENDING_ACTS, ConfirmedAct
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

ADMIN = "admin@firm.example"
PARALEGAL = "staff@firm.example"
TOOL = "mcp_smokeball_delete_events"
PREPARE = "mcp_smokeball_prepare_event_deletion"
SESSION = "sess-delete"
PROPOSAL = "7e3a90b1"
MESSAGE_ID = "msg-inbound-7"
M1 = "8d7c2a4e-1f3b-4c5d-9e6f-0a1b2c3d4e5f"

EVENTS = [
    {
        "event_id": "e1",
        "matter_id": M1,
        "matter_number": "200213",
        "subject": "Old deadline",
        "start_time": "2026-01-05T00:00:00Z",
    },
    {
        "event_id": "e2",
        "matter_id": M1,
        "matter_number": "200213",
        "subject": "Trial",
        "start_time": "2026-02-01T09:00:00Z",
    },
]
READBACK = (
    f"[act {PROPOSAL}] Delete 2 Smokeball calendar events: 2 on matter 200213 "
    '(2026-01-05 "Old deadline"; 2026-02-01 "Trial"). Reply "yes, delete them" to proceed.'
)


class _FakeConfig:
    def __init__(self, admins: list[str]) -> None:
        self._admins = [a.lower() for a in admins]
        self.connectors: dict = {}

    @property
    def admins(self) -> list[str]:
        return list(self._admins)

    def sender_is_admin(self, sender: object) -> bool:
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender: object) -> bool:
        return True

    @property
    def raw(self) -> dict:
        return {}


class _FakeCustomerConfig:
    admins: list[str] = [ADMIN]

    @classmethod
    def from_volume(cls, path=None):  # noqa: ANN001 - mirrors the real signature
        return _FakeConfig(cls.admins)


@pytest.fixture(autouse=True)
def _clean_state():
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    _FakeCustomerConfig.admins = [ADMIN]
    yield
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()


def _gate(monkeypatch, exposure: dict):
    trust = load_plugin("hermes-smd-trust")
    enforce = trust.enforce
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": exposure(enforce))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "operator")
    monkeypatch.setattr(customer_config, "CustomerConfig", _FakeCustomerConfig)
    calls: list[dict] = []

    def fake_verdict(payload: dict) -> dict:
        calls.append(payload)
        if payload.get("action") == act_broker.ACTION_PROPOSE:
            return {
                "ok": True,
                "proposal_id": PROPOSAL,
                "kind": act_broker.KIND_TOOL_CALL,
                "tool": payload.get("tool"),
                "for_admin": True,
                "expires_at": "2026-09-26T09:00:00Z",
                "readback": READBACK,
            }
        return {"ok": True, "committed": True}

    monkeypatch.setattr(act_broker, "verdict", fake_verdict)
    return trust, enforce, calls


@pytest.fixture
def gate(monkeypatch):
    """A seat that authors `destructive: confirm`."""
    return _gate(monkeypatch, lambda e: {e.ActionClass.DESTRUCTIVE: e.Ceiling.CONFIRM})


def _admin_turn(session: str = SESSION, sender: str = ADMIN) -> None:
    SESSION_INBOUND_ORIGIN.record(
        session, InboundOrigin(sender_address=sender, message_id=MESSAGE_ID, inbox_id="inbox-1")
    )


def _proposals(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("action") == act_broker.ACTION_PROPOSE]


def _commits(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("action") == act_broker.ACTION_COMMIT]


def _args() -> dict:
    return {"events": json.loads(json.dumps(EVENTS))}


def _confirm_on_seat() -> None:
    PENDING_ACTS.mark_confirmed(
        SESSION,
        ConfirmedAct(
            proposal_id=PROPOSAL,
            tool=TOOL,
            payload={"events": json.loads(json.dumps(EVENTS))},
            instructed_by=ADMIN,
            confirmed_by=ADMIN,
            confirmed_message_id=MESSAGE_ID,
            confirmed_at=1_000_000.0,
        ),
    )


# ---- classification ---------------------------------------------------------


def test_the_two_tools_are_classified():
    assert classify_tool(TOOL).action_class is ActionClass.DESTRUCTIVE
    assert classify_tool(PREPARE).action_class is ActionClass.READ
    assert act_broker.is_act_tool(TOOL) and act_broker.is_call_payload_act(TOOL)
    assert not act_broker.is_call_payload_act("mcp_smokeball_delete_file")
    assert not act_broker.is_call_payload_act("mcp_smokeball_create_matter")


# ---- proposing ----------------------------------------------------------------


def test_an_administrators_request_is_withheld_and_proposed_with_the_calls_list(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result is not None and result["action"] == "block"
    assert READBACK in result["message"] and "Nothing was done" in result["message"]
    (proposal,) = _proposals(calls)
    assert proposal["tool"] == TOOL
    assert proposal["payload"] == {"events": EVENTS}
    assert proposal["instructed_by"] == ADMIN and proposal["source_ref"] == MESSAGE_ID
    assert PENDING_ACTS.peek(SESSION).confirmed is None


def test_only_the_closed_key_set_is_proposed(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(
        TOOL, {**_args(), "also": "delete_everything"}, "smd", session_id=SESSION
    )
    assert _proposals(calls)[0]["payload"] == {"events": EVENTS}


def test_a_call_without_the_list_proposes_nothing(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, {"events": []}, "smd", session_id=SESSION)
    assert result["action"] == "block" and PREPARE in result["message"]
    assert _proposals(calls) == [] and not PENDING_ACTS.has_open(SESSION)


def test_a_colleague_who_is_not_an_administrator_proposes_nothing(gate):
    _trust, enforce, calls = gate
    _admin_turn(sender=PARALEGAL)
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert _proposals(calls) == [] and not PENDING_ACTS.has_open(SESSION)


def test_a_turn_nobody_opened_by_email_proposes_nothing(gate):
    _trust, enforce, calls = gate
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result["action"] == "block" and _proposals(calls) == []


def test_delete_file_at_destructive_confirm_still_refuses(gate):
    """No act shape, no withhold: the confirm ceiling does not make every
    destructive tool approvable."""
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(
        "mcp_smokeball_delete_file", {"matter_id": M1, "file_id": "f1"}, "smd", session_id=SESSION
    )
    assert result["action"] == "block"
    assert "[act " not in result["message"]
    assert _proposals(calls) == [] and not PENDING_ACTS.has_open(SESSION)


def test_a_seat_authoring_only_commitment_confirm_cannot_propose_a_deletion(monkeypatch):
    _trust, enforce, calls = _gate(
        monkeypatch, lambda e: {e.ActionClass.COMMITMENT: e.Ceiling.CONFIRM}
    )
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result["action"] == "block" and _proposals(calls) == []


def test_destructive_autonomous_still_needs_an_approval(monkeypatch):
    """The hard floor stands: an autonomous destructive exposure never fires
    without a current-turn approval, and it is not proposed either."""
    _trust, enforce, calls = _gate(
        monkeypatch, lambda e: {e.ActionClass.DESTRUCTIVE: e.Ceiling.AUTONOMOUS}
    )
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result["action"] == "block" and _proposals(calls) == []


# ---- the confirming turn ----------------------------------------------------------


def test_the_stored_list_replaces_whatever_the_model_sends(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    live = {"events": [{**EVENTS[0], "event_id": "someone-elses"}], "extra": 1}
    assert enforce.evaluate_tool_call(TOOL, live, "smd", session_id=SESSION) is None
    assert live == {"events": EVENTS}


def test_the_approval_is_spent_by_the_call_it_authorized(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    assert enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION) is None
    second = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert second is not None and second["action"] == "block"


def test_a_deletion_approval_does_not_authorize_delete_file(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    result = enforce.evaluate_tool_call(
        "mcp_smokeball_delete_file", {"matter_id": M1, "file_id": "f1"}, "smd", session_id=SESSION
    )
    assert result is not None and result["action"] == "block"


def test_the_commit_names_the_message_and_carries_the_outcome_counts(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=_args(),
        result=json.dumps({"deleted": [], "ref": "deleted=2 pending=0 skipped=0 failed=0"}),
        session_id=SESSION,
        status="ok",
    )
    (commit,) = _commits(calls)
    assert commit["tool"] == TOOL and commit["payload"] == {"events": EVENTS}
    assert commit["confirmed_by"] == ADMIN and commit["confirmed_message_id"] == MESSAGE_ID
    assert commit["outcome"] == {"ok": True, "ref": "deleted=2 pending=0 skipped=0 failed=0"}


def test_the_commit_reads_the_ref_through_hermes_result_wrapper(gate, monkeypatch):
    """Read live on pilot-smokeball 2026-09-25: Hermes hands post_tool_call the
    MCP result as {"result": "<json text>"}, and the committed rows carried an
    empty reference because the unwrap stopped at that string."""
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    inner = json.dumps({"deleted": [], "ref": "deleted=2 pending=0 skipped=1 failed=0"})
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=_args(),
        result=json.dumps({"result": inner}),
        session_id=SESSION,
        status="ok",
    )
    (commit,) = _commits(calls)
    assert commit["outcome"]["ref"] == "deleted=2 pending=0 skipped=1 failed=0"


def test_the_withheld_line_tells_the_model_to_send_it_unchanged(gate):
    """Read live 2026-09-25: told only to "put this line in your reply", the
    model ended the turn with it as final text, sent nothing, and restored the
    line's neutralized brackets."""
    _trust, enforce, _calls = gate
    _admin_turn()
    message = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)["message"]
    assert "SEND your email reply" in message
    assert "character for character" in message
    assert message.endswith(READBACK)
