"""A medical-records order as an administrator-confirmed act (ss-console,
2026-10-05).

The calendar deletion's shape (``test_event_deletion_act.py``) applied to a
COMMITMENT: the payload is the order the connector's ``prepare_records_order``
built, carried on the withheld ``place_records_order`` call. Each test tries to
break one invariant:

* ``place_order`` never fires without an administrator's written yes, and may
  only be PROPOSED on a turn an administrator opened by email;
* the proposal carries ONLY the ``order`` key the call held;
* what executes on the confirming turn is the STORED order, whatever the model
  sends the second time; a model-changed order never reaches the connector;
* with nothing confirmed (an empty register) the call is withheld again, never
  executed;
* a records-order approval authorizes no other commitment (create_matter).

THE FALSIFIER, run against origin/main (7bbc385): 7 of these 10 fail there,
because the three tools are unmapped and so refused as unknown before any
proposal, replay or commit can happen. The 3 that pass there are the refusal
tests, which an unknown tool also satisfies; they pin the behavior once mapped.
"""

from __future__ import annotations

import copy
import json

import pytest

from shared import act_broker
from shared.action_classes import ActionClass, classify_tool
from shared.pending_acts import PENDING_ACTS, ConfirmedAct
from tests.test_event_deletion_act import (  # noqa: F401 - the shared fixture and helpers
    ADMIN,
    MESSAGE_ID,
    PARALEGAL,
    PROPOSAL,
    SESSION,
    _admin_turn,
    _clean_state,
    _commits,
    _gate,
    _proposals,
)

TOOL = "mcp_smokeball_place_records_order"
PREPARE = "mcp_smokeball_prepare_records_order"
READ_BACK_TOOL = "mcp_smokeball_records_orders_for_matter"
MATTER = "8d7c2a4e-1f3b-4c5d-9e6f-0a1b2c3d4e5f"
ORDER = {
    "order_ref": "0123456789abcdef0123456789abcdef",
    "vendor_name": "Example Records",
    "matter_id": MATTER,
    "matter_number": "900101",
    "client_name": "Pat Example",
    "ssn_last4": "6789",
    "order_by_email": ADMIN,
    "language": "en",
    "hipaa_file_id": "f-hipaa",
    "hipaa_file_name": "HIPAA Authorization.pdf",
    "pre_approved_custodian_fee": 100.0,
    "order_certificate": "no_request",
    "locations": [
        {
            "custodian_id": "552211",
            "custodian_name": "Example Health Center",
            "custodian_address": "1 Main St, Springfield, CA 95811",
            "record_types": ["Medical", "Billing"],
            "service_start": "2021-10-05",
            "service_end": "2026-10-05",
        }
    ],
}


@pytest.fixture
def gate(monkeypatch):
    """A seat that authors `commitment: confirm`."""
    return _gate(monkeypatch, lambda e: {e.ActionClass.COMMITMENT: e.Ceiling.CONFIRM})


def _args() -> dict:
    return {"order": copy.deepcopy(ORDER)}


def _confirm_on_seat() -> None:
    PENDING_ACTS.mark_confirmed(
        SESSION,
        ConfirmedAct(
            proposal_id=PROPOSAL,
            tool=TOOL,
            payload={"order": copy.deepcopy(ORDER)},
            instructed_by=ADMIN,
            confirmed_by=ADMIN,
            confirmed_message_id=MESSAGE_ID,
            confirmed_at=1_000_000.0,
        ),
    )


def test_the_three_tools_are_classified():
    assert classify_tool(TOOL).action_class is ActionClass.COMMITMENT
    assert classify_tool(PREPARE).action_class is ActionClass.READ
    assert classify_tool(READ_BACK_TOOL).action_class is ActionClass.READ
    assert act_broker.is_act_tool(TOOL) and act_broker.is_call_payload_act(TOOL)
    assert act_broker.CALL_PAYLOAD_ACTS[TOOL] == "commitment"


def test_an_administrators_request_is_withheld_and_proposed_with_the_calls_order(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, {**_args(), "also": "x"}, "smd", session_id=SESSION)
    assert result is not None and result["action"] == "block"
    (proposal,) = _proposals(calls)
    assert proposal["tool"] == TOOL and proposal["payload"] == {"order": ORDER}
    assert proposal["instructed_by"] == ADMIN and proposal["source_ref"] == MESSAGE_ID
    assert PENDING_ACTS.peek(SESSION).confirmed is None


def test_a_call_without_an_order_proposes_nothing_and_names_prepare(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, {"order": {}}, "smd", session_id=SESSION)
    assert result["action"] == "block" and PREPARE in result["message"]
    assert "Nothing was ordered" in result["message"]
    assert _proposals(calls) == [] and not PENDING_ACTS.has_open(SESSION)


def test_a_colleague_who_is_not_an_administrator_proposes_nothing(gate):
    _trust, enforce, calls = gate
    _admin_turn(sender=PARALEGAL)
    result = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert result["action"] == "block" and _proposals(calls) == []


def test_the_stored_order_replaces_a_model_changed_order(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    changed = _args()
    changed["order"]["pre_approved_custodian_fee"] = 900.0
    changed["order"]["locations"][0]["custodian_id"] = "999999"
    assert enforce.evaluate_tool_call(TOOL, changed, "smd", session_id=SESSION) is None
    assert changed == {"order": ORDER}


def test_with_nothing_confirmed_the_call_is_withheld_never_executed(gate):
    """An empty register: a "yes" that never bound to the line, or a second
    call after the approval was spent, executes nothing."""
    _trust, enforce, calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    again = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert again is not None and again["action"] == "block"
    assert len(_proposals(calls)) == 1  # one outstanding act; never superseded


def test_the_approval_is_spent_once(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    assert enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION) is None
    second = enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    assert second is not None and second["action"] == "block"


def test_an_order_approval_does_not_authorize_create_matter(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, _args(), "smd", session_id=SESSION)
    _confirm_on_seat()
    result = enforce.evaluate_tool_call(
        "mcp_smokeball_create_matter", {"description": "x"}, "smd", session_id=SESSION
    )
    assert result is not None and result["action"] == "block"


def test_a_failed_placement_commits_no_act_row(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, "[act x] order")
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=_args(),
        result="The vendor refused",
        session_id=SESSION,
        status="error",
    )
    assert _commits(calls) == []


def test_a_placed_order_commits_with_the_vendor_order_id(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, "[act x] order")
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    oid = "11111111-2222-4333-8444-555555555555"
    inner = json.dumps({"id": oid, "status": "placed", "matter_id": MATTER})
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=_args(),
        result=json.dumps({"result": inner}),
        session_id=SESSION,
        status="ok",
    )
    (commit,) = _commits(calls)
    assert commit["payload"] == {"order": ORDER} and commit["outcome"] == {"ok": True, "ref": oid}
