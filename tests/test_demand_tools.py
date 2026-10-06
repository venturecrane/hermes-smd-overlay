"""The demand job's agent tools (hermes-smd-medchron/demand.py).

Who asked is never the model's to say: requested_by, request_ref and the
request text come from the turn's verified inbound email, and a turn no email
opened submits nothing. Each test fails if the guard it names is removed.
"""

from __future__ import annotations

import json

import pytest

from shared import inbound, provenance
from shared.action_classes import ActionClass, classify_tool

ADMIN = "admin@firm.example"
IMID = "<CA1x2y3z@mail.firm.example>"
SESSION = "mail-turn-1"
MATTER = {"matter_id": "b041dd06-30a4-4c1f-912b-27724bd77a64", "matter_number": "900201"}


class _Client:
    def __init__(self) -> None:
        self.envelopes: list[dict] = []

    def demand_submit(self, envelope):
        self.envelopes.append(envelope)
        return {
            "ok": True,
            "accepted": True,
            "job_id": "01J0000000000000000000000Z",
            "state": "submitted",
        }


@pytest.fixture
def demand(monkeypatch):
    from tests.conftest import load_plugin

    mod = load_plugin("hermes-smd-medchron").demand
    client = _Client()
    monkeypatch.setattr(mod, "MedchronBrokerClient", lambda: client)
    provenance._reset_for_tests()
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    yield mod, client
    provenance._reset_for_tests()
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()


def _email(
    text: str = "Admin here: gap audit and draft demand on 900201, please.", imid: str = IMID
) -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        SESSION,
        inbound.InboundOrigin(
            sender_address=ADMIN,
            message_id="AAMkGRAPH=",
            inbox_id="op@x",
            internet_message_id=imid,
            reply_text=text,
        ),
    )
    provenance.note_session(SESSION)


def test_the_requester_and_request_come_from_the_email(demand) -> None:
    mod, client = demand
    _email()
    out = json.loads(
        mod.demand_job_submit(
            {**MATTER, "requested_by": "someone@else.example", "request_ref": "<x@y>"}
        )
    )
    assert out["accepted"] is True
    env = client.envelopes[0]
    assert env["requested_by"] == ADMIN
    assert env["request_ref"] == IMID
    assert env["request_text"].startswith("Admin here")
    assert env["deliverables"] == ["gap_audit", "demand"]
    assert (
        env["matter"] == {"id": MATTER["matter_id"], "number": "900201"} and env["file_to"] is None
    )


def test_the_schema_has_no_requester_field(demand) -> None:
    mod, _ = demand
    props = mod.TOOLS["demand_job_submit"][1]["properties"]
    assert not {"requested_by", "request_ref", "request_text"} & set(props)
    assert mod.TOOLS["demand_job_submit"][1]["additionalProperties"] is False


def test_a_turn_no_email_opened_submits_nothing(demand) -> None:
    mod, client = demand
    provenance.note_session("cron_abc_20261006_120000")
    out = json.loads(mod.demand_job_submit(MATTER))
    assert out["accepted"] is False and client.envelopes == []


def test_an_email_without_its_message_id_submits_nothing(demand) -> None:
    mod, client = demand
    _email(imid="")
    assert json.loads(mod.demand_job_submit(MATTER))["accepted"] is False
    assert client.envelopes == []


def test_an_email_whose_words_could_not_be_read_submits_nothing(demand) -> None:
    mod, client = demand
    _email(text="   ")
    assert json.loads(mod.demand_job_submit(MATTER))["accepted"] is False
    assert client.envelopes == []


def test_a_rehearsal_files_elsewhere(demand) -> None:
    mod, client = demand
    _email()
    mod.demand_job_submit(
        {
            **MATTER,
            "file_to_matter_id": "1dad2f6b-7c5b-4cee-a06d-aab9e1e91a23",
            "file_to_matter_number": "LIB",
        }
    )
    assert client.envelopes[0]["file_to"] == {
        "id": "1dad2f6b-7c5b-4cee-a06d-aab9e1e91a23",
        "number": "LIB",
    }


def test_the_tools_are_classified() -> None:
    assert classify_tool("demand_job_submit").action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool("demand_job_status").action_class is ActionClass.READ
    assert classify_tool("demand_allowance").action_class is ActionClass.READ
