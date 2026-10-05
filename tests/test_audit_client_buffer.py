"""The gateway's audit gap buffer: rows survive a broker that is not listening.

SMD-OPERATOR-9 / SS-WEB-6..9: every audit row the gateway wrote while the broker
was down (a respawn, a starved boot) was dropped and tallied, about 3 minutes
after every release on every seat. These tests run a REAL Unix-socket broker
that can be stopped (socket file left behind, so connect() gets ECONNREFUSED,
the exact production error) and started again.

Falsifier: on the pre-buffer client, ``test_gap_then_recovery_loses_nothing``
fails at the tally assertion, because the gap write raised and counted.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from shared import audit_client
from shared import audit_failure_counter as counter
from shared.audit_client import AuditWriteError, BrokerAuditClient, _GapBuffer
from shared.audit_contract import COLUMNS


class FakeBroker:
    """A line-JSON broker on a Unix socket that can go away and come back."""

    def __init__(self, path: str, *, reply: bool = True) -> None:
        self.path = path
        self.rows: list[dict] = []
        self.reply = reply
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        s.listen(16)
        self._sock = s
        self._thread = threading.Thread(target=self._serve, args=(s,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the listener but LEAVE the socket file: connect() now refuses."""
        assert self._sock is not None
        self._sock.close()
        self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self, s: socket.socket) -> None:
        while True:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            with conn:
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65_536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                payload = json.loads(buf)
                self.rows.append(payload["row"])
                if self.reply:
                    conn.sendall(b'{"ok":true}\n')


@pytest.fixture
def machine_home(tmp_path, monkeypatch):
    (tmp_path / ".smd").mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def broker():
    # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path is not.
    d = tempfile.mkdtemp(prefix="smdab", dir="/tmp")
    b = FakeBroker(os.path.join(d, "b.sock"))
    b.start()
    yield b
    try:
        b.stop()
    except AssertionError:
        pass
    if os.path.exists(b.path):
        os.unlink(b.path)
    os.rmdir(d)


@pytest.fixture
def as_gateway(monkeypatch):
    monkeypatch.setenv("SMD_GATEWAY_PID", str(os.getpid()))


def _params(n: int, metadata: str | None = '{"k":"v"}') -> tuple:
    vals = {c: None for c in COLUMNS}
    vals.update(
        id="ignored",
        ts="ignored",
        action_type="TOOL_CALL_COMPLETED",
        actor="agent",
        skill_name=f"row-{n}",
        metadata=metadata,
    )
    return tuple(vals[c] for c in COLUMNS)


def _fast_gap(**kw) -> _GapBuffer:
    kw.setdefault("sleep", lambda s: time.sleep(0.02))
    return _GapBuffer(**kw)


def _tally(home: Path) -> int:
    return counter.read_audit_write_failures(str(home)) or 0


def _wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_gap_then_recovery_loses_nothing(machine_home, broker, as_gateway):
    gap = _fast_gap(sleep=lambda s: time.sleep(10))  # retrier idle: the next write drains
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    c.execute("INSERT", *_params(1))
    broker.stop()
    assert c.execute("INSERT", *_params(2)) == 1
    assert c.execute("INSERT", *_params(3)) == 1
    assert len(gap) == 2
    broker.start()
    c.execute("INSERT", *_params(4))
    assert [r["skill_name"] for r in broker.rows] == ["row-1", "row-2", "row-3", "row-4"]
    assert _tally(machine_home) == 0
    held = json.loads(broker.rows[1]["metadata"])
    assert held["k"] == "v" and held["buffered_at"].endswith("Z")
    assert "buffered_at" not in json.loads(broker.rows[3]["metadata"])


def test_retrier_flushes_a_quiet_seat(machine_home, broker, as_gateway):
    gap = _fast_gap()
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    c.execute("INSERT", *_params(1))
    broker.start()
    assert _wait_until(lambda: len(broker.rows) == 1)
    assert len(gap) == 0
    assert _tally(machine_home) == 0


def test_not_the_gateway_still_fails_and_counts(machine_home, broker, monkeypatch):
    monkeypatch.setenv("SMD_GATEWAY_PID", str(os.getpid() + 1))
    gap = _fast_gap()
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    with pytest.raises(AuditWriteError, match="Connection refused"):
        c.execute("INSERT", *_params(1))
    assert len(gap) == 0
    assert _tally(machine_home) == 1


def test_unset_gateway_pid_is_closed(machine_home, broker, monkeypatch):
    monkeypatch.delenv("SMD_GATEWAY_PID", raising=False)
    c = BrokerAuditClient(
        socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=_fast_gap()
    )
    broker.stop()
    with pytest.raises(AuditWriteError):
        c.execute("INSERT", *_params(1))
    assert _tally(machine_home) == 1


def test_default_client_is_unchanged_fail_closed(machine_home, broker, as_gateway):
    """The cost breaker and the gates must still see the failure on the spot."""
    gap = _fast_gap()
    c = BrokerAuditClient(socket_path=broker.path, gap_buffer=gap)
    broker.stop()
    with pytest.raises(AuditWriteError, match="audit broker socket error"):
        c.execute("INSERT", *_params(1))
    assert len(gap) == 0
    assert _tally(machine_home) == 1


def test_missing_socket_file_is_also_held(machine_home, broker, as_gateway):
    gap = _fast_gap(sleep=lambda s: time.sleep(10))
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    os.unlink(broker.path)
    c.execute("INSERT", *_params(1))
    assert len(gap) == 1
    assert _tally(machine_home) == 0


def test_overflow_drops_oldest_and_counts_once(machine_home, broker, as_gateway):
    gap = _fast_gap(max_rows=2, sleep=lambda s: time.sleep(10))
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    for n in (1, 2, 3):
        c.execute("INSERT", *_params(n))
    assert _tally(machine_home) == 1
    broker.start()
    c.execute("INSERT", *_params(4))
    assert [r["skill_name"] for r in broker.rows] == ["row-2", "row-3", "row-4"]


def test_gap_past_deadline_counts_what_was_held(machine_home, broker, as_gateway):
    gap = _fast_gap(deadline_seconds=0.05)
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    c.execute("INSERT", *_params(1))
    c.execute("INSERT", *_params(2))
    assert _wait_until(lambda: len(gap) == 0)
    assert _tally(machine_home) == 2


def test_exit_with_rows_pending_counts_them(machine_home, broker, as_gateway):
    gap = _fast_gap(sleep=lambda s: time.sleep(10))
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    c.execute("INSERT", *_params(1))
    gap.drain_at_exit()
    assert len(gap) == 0
    assert _tally(machine_home) == 1


def test_ambiguous_send_during_flush_is_counted_not_resent(machine_home, broker, as_gateway):
    """Sent but no reply: it may have landed, so it is never sent twice."""
    gap = _fast_gap(sleep=lambda s: time.sleep(10))
    c = BrokerAuditClient(
        socket_path=broker.path, timeout=0.2, buffer_on_unreachable=True, gap_buffer=gap
    )
    broker.stop()
    c.execute("INSERT", *_params(1))
    broker.reply = False
    broker.start()
    with pytest.raises(AuditWriteError):
        c.execute("INSERT", *_params(2))  # row-1 flushed (no reply), then row-2 itself
    assert [r["skill_name"] for r in broker.rows] == ["row-1", "row-2"]
    assert len(gap) == 0
    assert _tally(machine_home) == 2


def test_non_json_metadata_is_kept_verbatim(machine_home, broker, as_gateway):
    gap = _fast_gap(sleep=lambda s: time.sleep(10))
    c = BrokerAuditClient(socket_path=broker.path, buffer_on_unreachable=True, gap_buffer=gap)
    broker.stop()
    c.execute("INSERT", *_params(1, metadata="not json"))
    c.execute("INSERT", *_params(2, metadata=None))
    broker.start()
    c.execute("INSERT", *_params(3))
    m1 = json.loads(broker.rows[0]["metadata"])
    m2 = json.loads(broker.rows[1]["metadata"])
    assert m1["original"] == "not json" and "buffered_at" in m1
    assert set(m2) == {"buffered_at"}


def test_factory_passes_the_flag_through(monkeypatch, broker):
    monkeypatch.setenv(audit_client.SOCKET_ENV, broker.path)
    assert audit_client.audit_client_from_env("x")._buffer_on_unreachable is False
    assert (
        audit_client.audit_client_from_env("x", buffer_on_unreachable=True)._buffer_on_unreachable
        is True
    )
