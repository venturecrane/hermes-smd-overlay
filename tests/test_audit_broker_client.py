"""Broker-mediated audit transport: selection, wire behavior, and a CI guard.

Covers the OP-P1-4 hardening seam:

* :func:`audit_client_from_env` selects the broker transport iff
  ``SMD_AUDIT_BROKER_SOCKET`` is set, else a direct ``D1Client``.
* :class:`BrokerAuditClient` is a drop-in for ``D1Client.execute`` — it ships
  the row over a Unix socket, drops the agent-supplied ``id``/``ts`` (the
  broker re-derives them), and raises ``AuditWriteError`` on refusal/outage.
* A source-level guard fails CI if any new audit writer constructs a client on
  ``SMD_D1_AUDIT_BINDING`` directly instead of routing through the factory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from shared.audit_client import (
    AuditWriteError,
    BrokerAuditClient,
    audit_client_from_env,
)
from shared.audit_contract import COLUMNS, INSERT_SQL, build_audit_params

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A tiny in-test Unix-socket broker that records what it receives.
# ---------------------------------------------------------------------------


class _FakeBroker:
    """One-shot AF_UNIX broker that captures the request and replies."""

    def __init__(self, path: str, *, ok: bool = True) -> None:
        self._path = path
        self._ok = ok
        self.received: dict | None = None
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65_536)
                if not chunk:
                    break
                buf.extend(chunk)
            try:
                self.received = json.loads(buf)
            except ValueError:
                self.received = None
            reply = (
                {"ok": True, "id": "01TESTULID0000000000000000"}
                if self._ok
                else {
                    "ok": False,
                    "message": "refused for test",
                }
            )
            conn.sendall(json.dumps(reply).encode() + b"\n")

    def close(self) -> None:
        self._srv.close()
        self._thread.join(timeout=1)


@pytest.fixture
def short_tmpdir():
    # AF_UNIX paths are capped at ~104 chars on macOS; pytest's tmp_path is too
    # long. Bind sockets under a short /tmp dir instead.
    d = tempfile.mkdtemp(dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def broker_socket(short_tmpdir):
    path = os.path.join(short_tmpdir, "audit.sock")
    broker = _FakeBroker(path)
    try:
        yield path, broker
    finally:
        broker.close()


# ---------------------------------------------------------------------------
# audit_client_from_env selection
# ---------------------------------------------------------------------------


def test_factory_returns_direct_client_when_socket_unset(monkeypatch):
    monkeypatch.delenv("SMD_AUDIT_BROKER_SOCKET", raising=False)
    monkeypatch.setenv("CUSTOMER_SLUG", "smd")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "/tmp/whatever.db")
    client = audit_client_from_env(customer_slug="smd")
    # Direct path is a D1Client (duck-typed by attribute, no import needed).
    assert not isinstance(client, BrokerAuditClient)
    assert hasattr(client, "execute")


def test_factory_returns_broker_client_when_socket_set(monkeypatch):
    monkeypatch.setenv("SMD_AUDIT_BROKER_SOCKET", "/run/x/audit.sock")
    client = audit_client_from_env(customer_slug="smd")
    assert isinstance(client, BrokerAuditClient)


def test_broker_client_unset_socket_raises(monkeypatch):
    monkeypatch.delenv("SMD_AUDIT_BROKER_SOCKET", raising=False)
    with pytest.raises(AuditWriteError):
        BrokerAuditClient()


# ---------------------------------------------------------------------------
# BrokerAuditClient wire behavior
# ---------------------------------------------------------------------------


def test_execute_ships_row_and_drops_id_ts(broker_socket):
    path, broker = broker_socket
    client = BrokerAuditClient(socket_path=path)
    params = build_audit_params(
        row_id="01LOCALULID000000000000000",
        ts="2026-06-11T00:00:00Z",
        action_type="TOOL_CALL_COMPLETED",
        actor="agent",
        actor_role="agent",
        skill_name="inbox-triage",
        metadata={"k": "v"},
    )
    assert len(params) == len(COLUMNS)

    rows = client.execute(INSERT_SQL, *params)
    assert rows == 1

    assert broker.received is not None
    assert broker.received["action"] == "audit_append"
    row = broker.received["row"]
    # id/ts are dropped — the broker re-derives them.
    assert "id" not in row
    assert "ts" not in row
    # the agent-supplied columns survive.
    assert row["action_type"] == "TOOL_CALL_COMPLETED"
    assert row["actor"] == "agent"
    assert row["skill_name"] == "inbox-triage"
    # metadata arrives already-serialized (build_audit_params dumps it).
    assert json.loads(row["metadata"]) == {"k": "v"}


def test_execute_wrong_param_count_raises(broker_socket):
    path, _ = broker_socket
    client = BrokerAuditClient(socket_path=path)
    with pytest.raises(AuditWriteError):
        client.execute(INSERT_SQL, "only", "three", "params")


def test_execute_broker_refusal_raises(short_tmpdir):
    path = os.path.join(short_tmpdir, "audit.sock")
    broker = _FakeBroker(path, ok=False)
    try:
        client = BrokerAuditClient(socket_path=path)
        params = build_audit_params(
            row_id="x",
            ts="t",
            action_type="TOOL_CALL_COMPLETED",
            actor="agent",
            actor_role="agent",
        )
        with pytest.raises(AuditWriteError):
            client.execute(INSERT_SQL, *params)
    finally:
        broker.close()


def test_execute_socket_outage_raises(short_tmpdir):
    # No broker listening at this path → connect fails → AuditWriteError.
    client = BrokerAuditClient(socket_path=os.path.join(short_tmpdir, "absent.sock"))
    params = build_audit_params(
        row_id="x",
        ts="t",
        action_type="TOOL_CALL_COMPLETED",
        actor="agent",
        actor_role="agent",
    )
    with pytest.raises(AuditWriteError):
        client.execute(INSERT_SQL, *params)


# ---------------------------------------------------------------------------
# CI guard — every audit writer must route through the factory
# ---------------------------------------------------------------------------


_BYPASS = re.compile(
    r"(?:d1_client_from_env|D1Client)\s*\([^)]*SMD_D1_AUDIT_BINDING",
    re.DOTALL,
)


def test_no_audit_binding_client_bypasses_the_factory():
    """Fail if any source file constructs a client bound to the audit ledger
    directly. The ONLY sanctioned path is ``audit_client_from_env`` (in
    ``shared/audit_client.py``); everything else must go through it so the
    broker transport (OP-P1-4) cannot be silently bypassed by a new writer.
    """
    offenders: list[str] = []
    for base in ("shared", "plugins"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            if rel == "shared/audit_client.py":
                continue  # the sanctioned factory itself
            if _BYPASS.search(path.read_text(encoding="utf-8")):
                offenders.append(rel)
    assert not offenders, (
        "audit-ledger client constructed outside audit_client_from_env "
        f"(route these through the broker-aware factory): {offenders}"
    )
