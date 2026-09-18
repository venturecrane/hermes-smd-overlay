"""The chronology-package seam (routine 11, ss#2614): the three agent tools are
function-shaped and speak the broker's medchron_* verbs; the runtime-read
kind projects the ledger and fails safe; the action-class rows exist."""

from __future__ import annotations

import json
import os
import socket
import threading

import pytest

from shared import runtime_read
from shared.action_classes import ActionClass, classify_tool
from shared.medchron_client import SOCKET_ENV, MedchronBrokerClient, MedchronBrokerError
from tests.conftest import FakePluginContext, load_plugin


class _FakeBroker:
    """A one-request-per-connection broker that records what it was asked and
    answers from a table, the way the real one does."""

    def __init__(self, sock_path: str) -> None:
        self.requests: list[dict] = []
        self.answers: dict[str, dict] = {
            "medchron_job_submit": {
                "ok": True,
                "accepted": True,
                "job_id": "01J",
                "state": "submitted",
                "allowance_remaining_documents": 40,
            },
            "medchron_job_status": {
                "ok": True,
                "job": {"id": "01J", "state": "running", "pages": 12},
                "matter": {
                    "matter_number": "200454",
                    "covered_document_ids": ["a", "b"],
                    "uncovered_document_ids": ["c"],
                },
            },
            "medchron_allowance": {
                "ok": True,
                "month": "2026-08",
                "allowance": 100,
                "used": 60,
                "remaining": 40,
                "authored": True,
            },
            "medchron_job_list": {
                "ok": True,
                "jobs": [
                    {
                        "id": "B",
                        "created_at": "2",
                        "updated_at": "2",
                        "state": "delivered",
                        "matter_number": "2",
                        "documents": 9,
                        "pages": 90,
                        "cents": 900,
                        "reason": None,
                        "folder_id": "f",
                        "secret": "no",
                    },
                    {
                        "id": "A",
                        "created_at": "1",
                        "updated_at": "1",
                        "state": "held",
                        "matter_number": "1",
                        "documents": 0,
                        "pages": 0,
                        "cents": 0,
                        "reason": "seat paused",
                        "folder_id": None,
                    },
                ],
            },
        }
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                raw = conn.makefile("rb").readline()
                req = json.loads(raw)
                self.requests.append(req)
                ans = self.answers.get(
                    req.get("action"), {"ok": False, "error": "ValueError", "message": "nope"}
                )
                conn.sendall(json.dumps(ans).encode() + b"\n")

    def close(self) -> None:
        self._srv.close()


@pytest.fixture
def broker(monkeypatch):
    # A short path: AF_UNIX caps sun_path at ~104 bytes and pytest's tmp_path
    # is longer than that on macOS.
    import tempfile

    sock = os.path.join(tempfile.mkdtemp(prefix="mc-", dir="/tmp"), "s")
    b = _FakeBroker(sock)
    monkeypatch.setenv(SOCKET_ENV, sock)
    yield b
    b.close()


def _tools() -> dict:
    ctx = FakePluginContext()
    load_plugin("hermes-smd-medchron").register(ctx)
    return ctx.tools


def test_the_three_tools_register_function_shaped_and_require_the_socket():
    tools = _tools()
    assert set(tools) == {"medchron_job_submit", "medchron_job_status", "medchron_allowance"}
    for name, t in tools.items():
        assert t["requires_env"] == ["SMD_WORKSPACE_BROKER_SOCKET"], name
        assert t["schema"]["parameters"]["type"] == "object", name
    submit = tools["medchron_job_submit"]["schema"]["parameters"]
    assert set(submit["required"]) == {
        "matter_id",
        "matter_number",
        "units",
        "incident_date",
        "incident_source",
    }


def test_submit_builds_the_envelope_and_relays_the_ticket(broker):
    tools = _tools()
    out = json.loads(
        tools["medchron_job_submit"]["handler"](
            {
                "matter_id": "m-1",
                "matter_number": "2026-PI-102",
                "units": [
                    {"client_name": "Alpha Example", "surname": "Example", "dob": "01/02/1980"}
                ],
                "incident_date": "2026-01-15",
                "incident_source": "administrator_request",
                "request_ref": "t-9",
            }
        )
    )
    assert out["accepted"] and out["job_id"] == "01J" and out["allowance_remaining_documents"] == 40
    req = broker.requests[-1]
    assert req["action"] == "medchron_job_submit"
    env = req["envelope"]
    assert env["matter"] == {"id": "m-1", "number": "2026-PI-102", "title": ""}
    assert env["incident"] == {"date": "2026-01-15", "source": "administrator_request"}
    assert env["request_ref"] == "t-9" and "injuries" not in env


def test_submit_relays_a_refusal_as_prose(broker):
    broker.answers["medchron_job_submit"] = {
        "ok": True,
        "accepted": False,
        "reason": "the monthly allowance is spent",
    }
    out = json.loads(
        _tools()["medchron_job_submit"]["handler"](
            {
                "matter_id": "m",
                "matter_number": "n",
                "units": [],
                "incident_date": "2026-01-01",
                "incident_source": "matter_layout",
            }
        )
    )
    assert out == {"accepted": False, "reason": "the monthly allowance is spent"}


def test_status_and_allowance_are_thin(broker):
    tools = _tools()
    assert (
        json.loads(tools["medchron_job_status"]["handler"]({"job_id": "01J"}))["job"]["state"]
        == "running"
    )
    assert broker.requests[-1] == {"action": "medchron_job_status", "job_id": "01J"}
    assert json.loads(tools["medchron_allowance"]["handler"]({}))["remaining"] == 40


def test_status_by_matter_asks_for_the_matter_and_never_smuggles_a_job_id(broker):
    """The UPDATE path: a matter id in, that matter's cumulative coverage out.

    The two request assertions are the point. `matter_id` must appear ONLY when
    it was supplied, because a key added unconditionally would change the wire
    shape of every existing status call (the assertion above pins it), and
    `job_id` must be absent, because the broker refuses both at once and a
    smuggled empty string would make every matter lookup fail as a bad job id.
    """
    tools = _tools()
    out = json.loads(
        tools["medchron_job_status"]["handler"](
            {"matter_id": "00268f88-f469-4f24-8d50-92fd4e732f4e"}
        )
    )
    assert out["matter"]["covered_document_ids"] == ["a", "b"]
    assert broker.requests[-1] == {
        "action": "medchron_job_status",
        "matter_id": "00268f88-f469-4f24-8d50-92fd4e732f4e",
    }

    # Neither argument: the payload carries neither key, so the recent-list
    # call is byte-identical to what it was before the matter path existed.
    json.loads(tools["medchron_job_status"]["handler"]({}))
    assert broker.requests[-1] == {"action": "medchron_job_status"}


def test_client_raises_on_a_broker_refusal_or_no_socket(broker, monkeypatch):
    with pytest.raises(MedchronBrokerError):
        MedchronBrokerClient()._request({"action": "medchron_job_record"})
    monkeypatch.delenv(SOCKET_ENV)
    with pytest.raises(MedchronBrokerError):
        MedchronBrokerClient()


# -- runtime-read kind ---------------------------------------------------------


def test_medchron_jobs_kind_is_supported_and_real():
    assert "medchron_jobs" in runtime_read.SUPPORTED_KINDS
    assert "medchron_jobs" in runtime_read._REAL_KINDS


def test_medchron_jobs_runtime_read_projects_rows(broker):
    result = runtime_read.read_runtime("medchron_jobs", db_path=None)
    assert [e["id"] for e in result["entries"]] == ["B", "A"] and result["cursor"] is None
    first = result["entries"][0]
    assert set(first) == set(runtime_read._MEDCHRON_JOBS_COLUMNS) and "secret" not in first
    # 2026-09-16: the console groups attempts of one chronology by the broker's
    # work key. Dropping the column would silently return the console to
    # counting every launch as its own debit.
    assert "work_digest" in runtime_read._MEDCHRON_JOBS_COLUMNS
    assert broker.requests[-1]["action"] == "medchron_job_list"


def test_medchron_jobs_runtime_read_fails_safe_when_broker_unreachable(monkeypatch):
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    assert runtime_read.read_runtime("medchron_jobs", db_path=None) == {
        "entries": [],
        "cursor": None,
    }


# -- action classes ------------------------------------------------------------


def test_the_tools_are_classified():
    assert classify_tool("medchron_job_submit").action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool("medchron_job_status").action_class is ActionClass.READ
    assert classify_tool("medchron_allowance").action_class is ActionClass.READ
    assert not classify_tool("medchron_job_submit").unmapped


def test_env_is_only_the_broker_socket():
    src = open(
        os.path.join(
            os.path.dirname(__file__), "..", "plugins", "hermes-smd-medchron", "__init__.py"
        )
    ).read()
    assert "os.environ" not in src


# -- what the model is told about these tools ----------------------------------


def _descriptions() -> dict[str, str]:
    plugin = load_plugin("hermes-smd-medchron")
    return {name: spec[0] for name, spec in plugin.TOOLS.items()}


def test_the_descriptions_speak_pages_and_the_cycle():
    """The description is the ONE surface always in front of the model. On the
    mail channel the skill body is not loaded until ``read_file`` runs, so until
    then this text is all the model knows about the tool -- and for two days it
    said "monthly document allowance" while the skill body said "pages, in the
    cycle". The unit moved to pages 2026-09-08 (ss-console#2723) and the window
    moved to the billing cycle 2026-09-11 (ss-console#2747); ss-console's
    medchron-skill-allowance-unit.test.ts guards the SKILL BODY against exactly
    this wording and cannot reach here.
    """
    for name, text in _descriptions().items():
        low = text.lower()
        assert "document allowance" not in low, f"{name} still describes a document allowance"
        assert "monthly" not in low, f"{name} still calls the window monthly"
        assert "this month" not in low, f"{name} still calls the window a month"
    assert "page allowance" in _descriptions()["medchron_allowance"].lower()
    assert "billing cycle" in _descriptions()["medchron_allowance"].lower()


def test_the_submit_description_carries_the_ask_and_the_reservation():
    """On the mail channel this text is the trigger phrasing and the guard rail,
    because the skill body arrives only if the model already decided to fetch
    it. Both the recognized ask and the two refusals the broker will actually
    perform belong where the model always sees them."""
    text = _descriptions()["medchron_job_submit"].lower()
    assert "build the chronology" in text
    assert "named administrator" in text
    assert "requested_by" in text
    assert "medchron_allowance" in text
