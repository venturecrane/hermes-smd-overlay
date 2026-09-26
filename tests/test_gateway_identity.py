"""The non-gateway tool wall (pilot-smokeball 2026-09-21) and the staff spec path.

A second Hermes runtime on a broker-audited seat ran tools while the broker
refused every audit row it wrote. ``shared.gateway_identity`` decides whether
this process may run tools: the broker's word first, the environment (closed)
when the broker cannot say.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from shared import gateway_identity as gi
from tests.conftest import load_plugin


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    gi._reset_for_tests()
    monkeypatch.delenv(gi.SOCKET_ENV, raising=False)
    monkeypatch.delenv(gi.GATEWAY_PID_ENV, raising=False)
    yield
    gi._reset_for_tests()


def _broker(tmp_path: Path, reply: dict | None) -> str:
    """A one-shot Unix-socket broker answering ``health`` with ``reply``.
    ``None`` closes without answering (a broker that cannot say)."""
    # pytest's tmp_path overflows AF_UNIX's ~104-byte limit on macOS.
    path = os.path.join(tempfile.mkdtemp(prefix="gw", dir="/tmp"), "b.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)

    def _serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(65_536)
                if reply is not None:
                    conn.sendall(json.dumps(reply).encode() + b"\n")

    threading.Thread(target=_serve, daemon=True).start()
    return path


def test_no_broker_socket_means_no_wall():
    assert gi.wall_block() is None


def test_broker_says_not_gateway_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True, "caller_is_gateway": False}))
    block = gi.wall_block()
    assert block == {"action": "block", "message": gi.BLOCK_MESSAGE}


def test_broker_says_gateway_passes_even_without_the_env_pid(tmp_path, monkeypatch):
    """The broker is ground truth; the environment is only the fallback."""
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True, "caller_is_gateway": True}))
    assert gi.wall_block() is None


def test_old_broker_without_the_field_falls_back_to_a_matching_env_pid(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True}))
    monkeypatch.setenv(gi.GATEWAY_PID_ENV, str(os.getpid()))
    assert gi.wall_block() is None


def test_unreachable_broker_and_unset_pid_fails_closed(tmp_path, monkeypatch):
    """A bare ``fly ssh`` shell: the socket var is fly.toml [env], the PID var is
    entrypoint-only. Falsifier: treat unset as 'no wall' and this passes through."""
    monkeypatch.setenv(gi.SOCKET_ENV, str(tmp_path / "nothing-listens.sock"))
    assert gi.wall_block() is not None


def test_unreachable_broker_and_a_different_pid_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SOCKET_ENV, str(tmp_path / "nothing-listens.sock"))
    monkeypatch.setenv(gi.GATEWAY_PID_ENV, str(os.getpid() + 1))
    assert gi.wall_block() is not None


def test_garbage_pid_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, None))
    monkeypatch.setenv(gi.GATEWAY_PID_ENV, "not-a-pid")
    assert gi.wall_block() is not None


def test_a_misidentified_gateway_is_walled_and_raises_an_alarm(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True, "caller_is_gateway": False}))
    monkeypatch.setenv(gi.GATEWAY_PID_ENV, str(os.getpid()))
    with caplog.at_level(logging.ERROR, logger="shared.gateway_identity"):
        assert gi.wall_block() is not None
    assert any("does not recognise" in r.getMessage() for r in caplog.records)


def test_a_definitive_answer_is_cached_per_process(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True, "caller_is_gateway": True}))
    assert gi.is_gateway() is True
    monkeypatch.setenv(gi.SOCKET_ENV, str(tmp_path / "gone.sock"))
    assert gi.is_gateway() is True


def test_a_failed_health_probe_tallies_no_lost_audit_row(tmp_path, monkeypatch):
    """Reusing BrokerAuditClient._request would raise AuditWriteError, whose
    constructor counts a lost row. A probe that fails lost nothing."""
    from shared import audit_failure_counter as counter

    (tmp_path / ".smd").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(gi.SOCKET_ENV, str(tmp_path / "nothing-listens.sock"))
    gi.wall_block()
    assert counter.read_audit_write_failures() == 0


def test_the_trust_plugin_applies_the_wall_before_any_tool(tmp_path, monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    monkeypatch.setenv(gi.SOCKET_ENV, _broker(tmp_path, {"ok": True, "caller_is_gateway": False}))
    result = trust.on_pre_tool_call(tool_name="read_file", args={"path": "/tmp/x"}, session_id="s")
    assert result == {"action": "block", "message": gi.BLOCK_MESSAGE}


def test_staff_spec_not_read_refusal_names_the_path(tmp_path, monkeypatch):
    """The spec gate's own message (staff class), not only the voice gate's."""
    from shared import spec_gate, spec_manifest

    body = "Write like Christa.\n"
    rel = "classes/staff/voice.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(body)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "x",
                "source_digest": "d",
                "specs": {
                    rel: {
                        "class": "staff",
                        "property": "voice",
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "bytes": len(body),
                    }
                },
            }
        )
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    message = spec_gate._draft_message("staff", "spec_not_read")
    assert f"read_file at {tmp_path / rel}" in message
