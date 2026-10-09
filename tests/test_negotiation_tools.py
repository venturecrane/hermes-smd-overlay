"""The negotiation watch's agent tool (hermes-smd-medchron/negotiation.py).

Scheduled only: there is no submit tool. The one status read hands the
completion wake a notice's composed message, or a failed job's shortfall.
Each test fails if the guard it names is removed.
"""

from __future__ import annotations

import json

import pytest

from shared import provenance
from shared.action_classes import ActionClass, classify_tool

JOB = "01J0000000000000000000000Z"
NOTICE = {
    "job_id": JOB,
    "kind": "notice",
    "state": "delivered",
    "matter_number": "200123",
    "status": "entered",
    "message": "New offer on matter 200123, Doe v. Example.",
    "subject": "New offer, matter 200123",
    "reply_sent_at": None,
}


class _Status:
    def __init__(self, job: dict) -> None:
        self.job = job

    def negotiation_status(self, job_id):
        return {"ok": True, "job": self.job}


@pytest.fixture
def neg():
    from tests.conftest import load_plugin

    return load_plugin("hermes-smd-medchron").negotiation


def test_there_is_no_submit_tool(neg) -> None:
    """FALSIFIER: add a submit tool and a turn could start a run the firm's
    schedule did not."""
    assert set(neg.TOOLS) == {"negotiation_job_status"}
    assert classify_tool("negotiation_job_status").action_class is ActionClass.READ


def test_a_notice_reads_back_its_message_and_subject_only(neg, monkeypatch) -> None:
    monkeypatch.setattr(neg, "MedchronBrokerClient", lambda: _Status(NOTICE))
    out = json.loads(neg.negotiation_job_status({"job_id": JOB}))
    assert out["job"]["message"] == NOTICE["message"] and out["job"]["subject"] == NOTICE["subject"]
    assert "reply_sent_at" not in out["job"] and "status" not in {k for k in out if k != "job"}


def test_a_failed_job_is_a_shortfall_for_smd(neg, monkeypatch) -> None:
    from tests.conftest import load_plugin

    monkeypatch.setattr(
        neg,
        "MedchronBrokerClient",
        lambda: _Status({"job_id": JOB, "kind": "job", "state": "failed"}),
    )
    result = neg.negotiation_job_status({"job_id": JOB})
    emit = load_plugin("hermes-smd-audit").emit
    assert emit._outcome_from_result(result) == ("shortfall", "negotiation_job_failed")
    assert "Send the firm nothing" in json.loads(result)["message"]


def test_a_delivered_notice_is_an_ordinary_read(neg, monkeypatch) -> None:
    from tests.conftest import load_plugin

    monkeypatch.setattr(neg, "MedchronBrokerClient", lambda: _Status(NOTICE))
    emit = load_plugin("hermes-smd-audit").emit
    assert emit._outcome_from_result(neg.negotiation_job_status({"job_id": JOB})) == ("ok", None)


def test_no_job_id_asks_nothing(neg, monkeypatch) -> None:
    class _Never:
        def negotiation_status(self, job_id):
            raise AssertionError("asked the broker")

    monkeypatch.setattr(neg, "MedchronBrokerClient", lambda: _Never())
    assert json.loads(neg.negotiation_job_status({}))["job"] is None


def test_the_notice_read_is_a_tenant_source() -> None:
    """The message relays the firm's own records (its matter number, the figures
    as entered and read back), so the identifier gate may verify against it."""
    assert provenance.seeds_provenance("negotiation_job_status") is True
