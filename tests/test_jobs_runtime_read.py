"""Tests for the ``jobs`` observability seam (B1, ADR 0051).

Covers the overlay side of the durable-job observability lane:

  * ``BrokerJobClient.list_all`` speaks the ``job_list`` verb and unwraps rows.
  * The ``jobs`` runtime-read kind (``runtime_read.read_runtime``) reads over
    the broker socket, projects rows to ``_JOBS_COLUMNS``, and fails safe to an
    honest empty page when the broker is unreachable / unconfigured.
  * The ``job_status`` / ``job_cancel`` MCP verbs route correctly through
    ``webhook_gate._mcp_dispatch`` — read-only, no agent turn — and fail safe.

The broker *logic* (fencing, idempotency, list filtering) is covered console
side; here we prove the overlay speaks the protocol and the seams route.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading

import pytest

import webhook_gate as gate
from shared import runtime_read
from shared.job_ledger_client import SOCKET_ENV, BrokerJobClient, JobLedgerError


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


# Two ledger rows the stub returns; the second exercises every projected field.
_ROW_A = {
    "id": "JOB_A",
    "created_at": "2026-06-18T00:00:00.000Z",
    "updated_at": "2026-06-18T00:05:00.000Z",
    "customer_slug": "demo-law",
    "persona_id": "intake-coordinator",
    "model": "claude-sonnet-4-6",
    "status": "running",
    "deliver_to": "telegram:123",
    "lease_owner": "worker-1",
    "lease_epoch": 2,
    "attempts": 1,
    "budget_cents": 500,
    "spent_cents": 120,
    "cancel_requested": 0,
    "result_ref": None,
    "error": None,
    # A column NOT in the projection — must be dropped by _read_jobs.
    "brief": "review the three production documents",
}
_ROW_B = {
    "id": "JOB_B",
    "created_at": "2026-06-18T01:00:00.000Z",
    "updated_at": "2026-06-18T01:10:00.000Z",
    "customer_slug": "demo-law",
    "persona_id": "intake-coordinator",
    "model": "claude-sonnet-4-6",
    "status": "done",
    "deliver_to": "telegram:123",
    "lease_owner": "worker-2",
    "lease_epoch": 5,
    "attempts": 2,
    "budget_cents": 500,
    "spent_cents": 410,
    "cancel_requested": 0,
    "result_ref": "r2://results/job_b.json",
    "error": None,
    "brief": "second job",
}


@pytest.fixture
def broker(monkeypatch):
    """Stand up a stub broker socket and point the client env at it."""
    responses = {
        "job_list": {"ok": True, "jobs": [_ROW_B, _ROW_A]},
        "job_read": {"ok": True, "job": _ROW_A},
        "job_cancel": {"ok": True, "result": True},
    }
    # AF_UNIX paths are capped (~104 chars on macOS); pytest's tmp_path is too
    # long, so use a short /tmp dir.
    tmpdir = tempfile.mkdtemp(prefix="b1jobs", dir="/tmp")
    sock_path = os.path.join(tmpdir, "b.sock")
    b = _StubBroker(sock_path, responses)
    monkeypatch.setenv(SOCKET_ENV, sock_path)
    try:
        yield b, sock_path
    finally:
        b.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


# -- BrokerJobClient.list_all ---------------------------------------------------


def test_list_all_speaks_job_list_and_unwraps(broker):
    b, sock_path = broker
    rows = BrokerJobClient(socket_path=sock_path, timeout=2.0).list_all()
    assert [r["id"] for r in rows] == ["JOB_B", "JOB_A"]
    assert b.requests[-1]["action"] == "job_list"


# -- jobs runtime-read kind -----------------------------------------------------


def test_jobs_kind_is_supported_and_real():
    assert "jobs" in runtime_read.SUPPORTED_KINDS
    assert "jobs" in runtime_read._REAL_KINDS


def test_jobs_runtime_read_projects_rows(broker):
    _, _sock = broker
    result = runtime_read.read_runtime("jobs", db_path=None)
    ids = [e["id"] for e in result["entries"]]
    assert ids == ["JOB_B", "JOB_A"]
    assert result["cursor"] is None
    # Projected to the stable column set — the non-projected ``brief`` is dropped.
    first = result["entries"][0]
    assert set(first) == set(runtime_read._JOBS_COLUMNS)
    assert "brief" not in first
    assert first["status"] == "done"
    assert first["spent_cents"] == 410
    assert first["result_ref"] == "r2://results/job_b.json"


def test_jobs_runtime_read_fails_safe_when_broker_unreachable(monkeypatch):
    # Socket env unset → BrokerJobClient() raises JobLedgerError → honest empty.
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    result = runtime_read.read_runtime("jobs", db_path=None)
    assert result == {"entries": [], "cursor": None}


# -- MCP job_status / job_cancel verbs -----------------------------------------


def test_mcp_job_status_projects_control_facts(broker):
    _, _sock = broker
    status, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "id": 11, "method": "job_status", "params": {"job_id": "JOB_A"}}
    )
    assert status == 200
    result = body["result"]
    assert result["found"] is True
    assert result["job_id"] == "JOB_A"
    # Exactly the operator-visible status fields (plus found/job_id).
    assert set(result) == {"found", "job_id", *gate._JOB_STATUS_FIELDS}
    assert result["status"] == "running"
    assert result["budget_cents"] == 500
    assert result["spent_cents"] == 120


def test_mcp_job_status_requires_job_id(broker):
    _, _sock = broker
    _, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 12, "method": "job_status", "params": {}})
    assert body["error"]["code"] == gate._JSON_RPC_INVALID_PARAMS


def test_mcp_job_status_not_found_when_broker_unreachable(monkeypatch):
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    _, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "id": 13, "method": "job_status", "params": {"job_id": "JOB_X"}}
    )
    assert body["result"] == {"found": False, "job_id": "JOB_X"}


def test_mcp_job_cancel_routes_and_returns_outcome(broker):
    b, _sock = broker
    status, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "id": 14, "method": "job_cancel", "params": {"job_id": "JOB_A"}}
    )
    assert status == 200
    assert body["result"] == {"job_id": "JOB_A", "cancelled": True}
    assert b.requests[-1]["action"] == "job_cancel"
    assert b.requests[-1]["job_id"] == "JOB_A"


def test_mcp_job_cancel_errors_when_broker_unreachable(monkeypatch):
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    _, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "id": 15, "method": "job_cancel", "params": {"job_id": "JOB_X"}}
    )
    assert body["error"]["code"] == gate._JSON_RPC_INTERNAL_ERROR
