"""Tests for the overlay BrokerJobClient (B1, ADR 0051).

Exercises the real Unix-socket transport against a stub broker that mimics the
wire contract, plus the request/response marshalling for each verb. The broker
*logic* (fencing, idempotency) is covered on the console side; here we prove the
client speaks the protocol and unwraps responses correctly.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading

import pytest

from shared.job_ledger_client import BrokerJobClient, JobLedgerError


class _StubBroker:
    """A one-shot-per-connection Unix-socket broker that replies to each
    newline-delimited JSON request with a canned response keyed by action."""

    def __init__(self, sock_path: str, responses: dict[str, dict]) -> None:
        self._responses = responses
        self.requests: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(sock_path)
        self._srv.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65_536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                if not buf:
                    continue
                req = json.loads(buf)
                self.requests.append(req)
                resp = self._responses.get(req.get("action"), {"ok": False, "error": "no stub"})
                conn.sendall(json.dumps(resp).encode() + b"\n")

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self._srv.close()


@pytest.fixture
def broker():
    responses = {
        "job_create": {"ok": True, "id": "JOB123"},
        "job_read": {"ok": True, "job": {"id": "JOB123", "status": "queued"}},
        "job_list_claimable": {"ok": True, "jobs": [{"id": "A"}, {"id": "B"}]},
        "job_claim": {"ok": True, "lease_epoch": 3},
        "job_heartbeat": {"ok": True, "result": True},
        "job_record": {"ok": True, "result": False},  # ok=processed; result=fenced out
        "job_cancel": {"ok": True, "result": True},
        "job_idem_begin": {"ok": True, "decision": "review"},
        "job_idem_complete": {"ok": True, "result": True},
    }
    # AF_UNIX paths are capped (~104 chars on macOS); pytest's tmp_path is too
    # long, so use a short /tmp dir.
    tmpdir = tempfile.mkdtemp(prefix="b1", dir="/tmp")
    sock_path = os.path.join(tmpdir, "b.sock")
    b = _StubBroker(sock_path, responses)
    try:
        yield b, BrokerJobClient(socket_path=sock_path, timeout=2.0)
    finally:
        b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_create_returns_id_and_sends_row(broker):
    b, client = broker
    assert (
        client.create({"customer_slug": "demo", "brief": "x", "budget_cents": 1, "persona_id": "p"})
        == "JOB123"
    )
    assert b.requests[-1]["action"] == "job_create"
    assert b.requests[-1]["row"]["customer_slug"] == "demo"


def test_read_unwraps_job(broker):
    _, client = broker
    job = client.read("JOB123")
    assert job["status"] == "queued"


def test_list_claimable_unwraps_jobs(broker):
    _, client = broker
    assert [j["id"] for j in client.list_claimable()] == ["A", "B"]


def test_claim_returns_epoch_and_omits_clock(broker):
    b, client = broker
    assert client.claim("JOB123", "worker-1") == 3
    sent = b.requests[-1]
    # The client never sends a clock value — the broker stamps lease timing.
    assert "now" not in sent and "lease_expiry_cutoff" not in sent
    assert sent["worker_id"] == "worker-1"


def test_record_returns_false_when_fenced_out(broker):
    b, client = broker
    assert client.record("JOB123", 2, {"spent_cents": 9}) is False
    assert b.requests[-1]["lease_epoch"] == 2
    assert b.requests[-1]["fields"] == {"spent_cents": 9}


def test_idem_begin_returns_decision(broker):
    _, client = broker
    assert client.idem_begin("JOB123", "send:x", 3) == "review"


def test_cancel_and_heartbeat(broker):
    _, client = broker
    assert client.cancel("JOB123") is True
    assert client.heartbeat("JOB123", 3) is True


def test_broker_refusal_raises(broker):
    _, client = broker
    # No stub for this action → {"ok": False} → JobLedgerError.
    with pytest.raises(JobLedgerError):
        client._request({"action": "job_unknown"})


def test_missing_socket_env_raises(monkeypatch):
    monkeypatch.delenv("SMD_WORKSPACE_BROKER_SOCKET", raising=False)
    with pytest.raises(JobLedgerError):
        BrokerJobClient()
