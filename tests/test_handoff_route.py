"""The async handoff door, made real (ss-console #2616): translate materializes
a `handoff` route whenever the MCP bearer exists, the prompt carries the task,
the gate stops masking non-2xx forwards, and the medchron submit tool passes
the append selection through."""

from __future__ import annotations

import json

import pytest

from bootstrap import translate as _wh
from tests.conftest import FakePluginContext, load_plugin

# ---------------------------------------------------------------------------
# translate: the handoff route
# ---------------------------------------------------------------------------


def test_handoff_route_materialized_whenever_the_mcp_secret_exists(monkeypatch):
    # Deliberately NOT gated on mcp_connector.enabled: the console's
    # operator_handoff_task and the seat's runner daemon are wired at
    # provision, not authored per-connector.
    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "shh-mcp")
    out = _wh._materialize_webhook_platform({"connectors": {}, "webhook_triggers": []})
    route = out["webhook"]["extra"]["routes"]["handoff"]
    assert route["secret"] == "shh-mcp"
    assert route["events"] == [] and route["skills"] == []
    assert "{task}" in route["prompt"] and "{handoff_id}" in route["prompt"]
    # The provenance claim and the initiation grant the deliver mode rests on.
    assert "DATA" in route["prompt"]
    assert "IS the initiation" in route["prompt"]


def test_handoff_route_fail_closed_without_the_secret(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET_MCP", raising=False)
    out = _wh._materialize_webhook_platform({"connectors": {}, "webhook_triggers": []})
    assert out == {}


def test_handoff_route_rides_beside_the_mcp_route(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "shh-mcp")
    cust = {"connectors": {}, "webhook_triggers": [], "mcp_connector": {"enabled": True}}
    routes = _wh._materialize_webhook_platform(cust)["webhook"]["extra"]["routes"]
    assert set(routes) == {"mcp", "handoff"}
    assert routes["handoff"]["prompt"] != routes["mcp"]["prompt"]


# ---------------------------------------------------------------------------
# gate: any non-2xx forward is a retryable failure
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self) -> bytes:
        return b""


class _FakeConnection:
    status = 200

    def __init__(self, *args, **kwargs) -> None:
        pass

    def request(self, *args, **kwargs) -> None:
        pass

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(_FakeConnection.status)

    def close(self) -> None:
        pass


class _FakeHandler:
    """Just enough of the BaseHTTPRequestHandler surface for _handle_handoff."""

    def __init__(self, body: dict, bearer: str) -> None:
        import io

        raw = json.dumps(body).encode()
        self.headers = {"Authorization": f"Bearer {bearer}", "Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.answers: list[tuple[int, dict]] = []

    def _json(self, status: int, payload: dict) -> None:
        self.answers.append((status, payload))


@pytest.fixture
def gate(monkeypatch):
    import webhook_gate

    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "shh-mcp")
    monkeypatch.setattr(webhook_gate.http.client, "HTTPConnection", _FakeConnection)
    return webhook_gate


def _handoff(gate_mod, status: int) -> tuple[int, dict]:
    _FakeConnection.status = status
    h = _FakeHandler({"handoff_id": "medchron-01A", "task": "run deliver mode"}, "shh-mcp")
    gate_mod._Handler._handle_handoff(h)
    return h.answers[-1]


def test_a_2xx_forward_is_accepted(gate):
    code, payload = _handoff(gate, 200)
    assert code == 202 and payload["accepted"] is True


def test_a_404_forward_is_a_retryable_failure_not_a_silent_202(gate):
    # The exact window between a config change and its reprovision: the route
    # is not materialized, the adapter 404s, and before #2616 the gate
    # swallowed that into a 202 — a lost wake indistinguishable from a
    # delivered one.
    code, payload = _handoff(gate, 404)
    assert code == 503 and payload["retry"] is True


def test_a_500_forward_stays_a_retryable_failure(gate):
    code, payload = _handoff(gate, 500)
    assert code == 503 and payload["retry"] is True


# ---------------------------------------------------------------------------
# medchron submit: the append selection passes through
# ---------------------------------------------------------------------------


def test_submit_passes_include_file_ids_through(monkeypatch):
    mod = load_plugin("hermes-smd-medchron")
    sent: dict = {}

    class _Client:
        def submit(self, envelope):
            sent.update(envelope)
            return {"accepted": True, "job_id": "01J", "state": "submitted"}

    monkeypatch.setattr(mod, "MedchronBrokerClient", _Client)
    ctx = FakePluginContext()
    mod.register(ctx)
    out = json.loads(
        ctx.tools["medchron_job_submit"]["handler"](
            {
                "matter_id": "m-1",
                "matter_number": "10006",
                "units": [{"client_name": "A B", "surname": "B", "dob": "01/02/1980"}],
                "incident_date": "2026-01-15",
                "incident_source": "administrator_request",
                "selection": {"include_file_ids": ["f-9", "f-10"]},
            }
        )
    )
    assert out["accepted"] is True
    assert sent["selection"] == {"include_file_ids": ["f-9", "f-10"]}
    schema = ctx.tools["medchron_job_submit"]["schema"]["parameters"]
    assert "include_file_ids" in schema["properties"]["selection"]["properties"]


def test_submit_omits_selection_when_not_given(monkeypatch):
    mod = load_plugin("hermes-smd-medchron")
    sent: dict = {}

    class _Client:
        def submit(self, envelope):
            sent.update(envelope)
            return {"accepted": True, "job_id": "01J", "state": "submitted"}

    monkeypatch.setattr(mod, "MedchronBrokerClient", _Client)
    ctx = FakePluginContext()
    mod.register(ctx)
    ctx.tools["medchron_job_submit"]["handler"](
        {
            "matter_id": "m-1",
            "matter_number": "10006",
            "units": [{"client_name": "A B", "surname": "B", "dob": "01/02/1980"}],
            "incident_date": "2026-01-15",
            "incident_source": "administrator_request",
        }
    )
    assert "selection" not in sent
