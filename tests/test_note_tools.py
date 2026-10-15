"""An SMD note's agent tool (hermes-smd-medchron/note.py).

Root records a note on SMD's command; there is no submit tool. The one status
read hands the completion wake the recorded text, or a shortfall when there is
none. Each test fails if the guard it names is removed.
"""

from __future__ import annotations

import json

import pytest

from shared import provenance
from shared.action_classes import ActionClass, classify_tool

JOB = "01J0000000000000000000000Z"
NOTE = {
    "job_id": JOB,
    "state": "delivered",
    "text": "The rep letter for matter 203795 is filed to the matter.",
    "requester": "christa@firm.example",
    "request_ref": "AAMkSOURCEMESSAGE0001=",
    "approved_by": "scott@smd.services",
    "reply_sent_at": None,
}


class _Status:
    def __init__(self, resp: dict) -> None:
        self.resp = resp
        self.asked: list[str] = []

    def note_status(self, job_id):
        self.asked.append(job_id)
        return self.resp


@pytest.fixture
def note():
    from tests.conftest import load_plugin

    return load_plugin("hermes-smd-medchron").note


def test_there_is_no_submit_tool(note) -> None:
    """FALSIFIER: add a submit tool and a turn could record a note SMD did not."""
    assert set(note.TOOLS) == {"note_job_status"}
    assert classify_tool("note_job_status").action_class is ActionClass.READ


def test_the_read_projects_the_text_and_nothing_else(note, monkeypatch) -> None:
    """FALSIFIER: project the whole job and the requester, the anchor's id and
    the approver reach the model."""
    status = _Status({"ok": True, "job": NOTE})
    monkeypatch.setattr(note, "MedchronBrokerClient", lambda: status)
    out = json.loads(note.note_job_status({"job_id": JOB}))
    assert out == {"job": {"job_id": JOB, "state": "delivered", "text": NOTE["text"]}}
    assert status.asked == [JOB]


def test_a_note_with_text_is_an_ordinary_read(note, monkeypatch) -> None:
    from tests.conftest import load_plugin

    monkeypatch.setattr(note, "MedchronBrokerClient", lambda: _Status({"job": NOTE}))
    emit = load_plugin("hermes-smd-audit").emit
    assert emit._outcome_from_result(note.note_job_status({"job_id": JOB})) == ("ok", None)


@pytest.mark.parametrize(
    "resp",
    [
        {"ok": False, "error": "unknown note job"},
        {"job": None},
        {"job": {"job_id": JOB, "state": "delivered", "text": "  "}},
    ],
)
def test_no_text_to_send_is_a_shortfall_for_smd(note, monkeypatch, resp) -> None:
    """FALSIFIER: drop the empty-text branch and a turn holding nothing to send
    reads as an ordinary success, so SMD never hears the note did not go."""
    from tests.conftest import load_plugin

    monkeypatch.setattr(note, "MedchronBrokerClient", lambda: _Status(resp))
    result = note.note_job_status({"job_id": JOB})
    emit = load_plugin("hermes-smd-audit").emit
    assert emit._outcome_from_result(result) == ("shortfall", "note_job_unavailable")
    assert "Send the firm nothing" in json.loads(result)["message"]


def test_no_job_id_asks_nothing(note, monkeypatch) -> None:
    class _Never:
        def note_status(self, job_id):
            raise AssertionError("asked the broker")

    monkeypatch.setattr(note, "MedchronBrokerClient", lambda: _Never())
    assert json.loads(note.note_job_status({}))["job"] is None


def test_the_note_read_is_a_tenant_source() -> None:
    """Every identifier in the text was corroborated against the colleague's own
    email when root recorded it, so the identifier gate may verify against it.
    FALSIFIER: drop it from the read list and a note naming its matter is held."""
    assert provenance.seeds_provenance("note_job_status") is True
