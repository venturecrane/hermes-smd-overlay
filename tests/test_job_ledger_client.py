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


# -- broker respawn gap (2026-10-05, SMD-OPERATOR-2G) --------------------------
# A job-ledger write cannot be held in memory like the gateway's audit rows
# (overlay#416): it is synchronous and its fenced answer matters. It rides the
# respawn out instead, re-trying ONLY a connect() the broker refused, because
# that proves nothing was sent.


def test_record_lands_when_the_broker_comes_back_inside_the_retry_window():
    """The broker is not listening when the write starts and comes up during
    the client's second back-off: the write lands and returns the broker's
    answer, instead of raising and dropping the segment's spend."""
    tmpdir = tempfile.mkdtemp(prefix="b1", dir="/tmp")
    sock_path = os.path.join(tmpdir, "b.sock")
    sleeps: list[float] = []
    brokers: list[_StubBroker] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 2:
            brokers.append(_StubBroker(sock_path, {"job_record": {"ok": True, "result": True}}))

    client = BrokerJobClient(socket_path=sock_path, timeout=2.0, sleep=sleep)
    try:
        assert client.record("J", 4, {"spent_cents": 12}) is True
        assert len(sleeps) == 2
        assert brokers[0].requests == [
            {"action": "job_record", "job_id": "J", "lease_epoch": 4, "fields": {"spent_cents": 12}}
        ]
    finally:
        for b in brokers:
            b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_a_broker_still_down_at_the_deadline_raises_as_unreachable():
    """Past the window the write raises, with the refusal kept as __cause__ so
    the worker loop still classifies it as broker-unreachable."""
    from shared.job_worker_runtime import _is_broker_unreachable

    tmpdir = tempfile.mkdtemp(prefix="b1", dir="/tmp")
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    client = BrokerJobClient(
        socket_path=os.path.join(tmpdir, "absent.sock"),
        connect_retry_seconds=3.0,
        sleep=sleep,
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(JobLedgerError, match="job broker socket error") as info:
            client.record("J", 1, {"spent_cents": 1})
        assert isinstance(info.value.__cause__, FileNotFoundError)
        assert _is_broker_unreachable(info.value) is True
        assert len(sleeps) >= 2, "a refused connect must be re-tried before giving up"
        assert now[0] >= 3.0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_a_failure_after_connect_is_never_retried(monkeypatch):
    """Once connect() succeeded the broker may have applied the request, so an
    error past that point raises at once, even a reset that shares its type
    with a connect-time refusal: a retry could double-apply the write."""
    import shared.job_ledger_client as mod

    connects: list[str] = []

    class _ResetAfterConnect:
        def __init__(self, *_a: object) -> None: ...
        def __enter__(self) -> _ResetAfterConnect:
            return self

        def __exit__(self, *_a: object) -> None: ...
        def settimeout(self, _t: float) -> None: ...
        def connect(self, path: str) -> None:
            connects.append(path)

        def sendall(self, _b: bytes) -> None:
            raise ConnectionResetError(54, "Connection reset by peer")

    monkeypatch.setattr(mod.socket, "socket", _ResetAfterConnect)
    client = BrokerJobClient(
        socket_path="/tmp/x.sock",
        sleep=lambda _s: pytest.fail("a post-connect failure must not back off"),
    )
    with pytest.raises(JobLedgerError, match="reset") as info:
        client.record("J", 1, {"status": "delivered"})
    assert connects == ["/tmp/x.sock"]
    assert isinstance(info.value.__cause__, ConnectionResetError)
