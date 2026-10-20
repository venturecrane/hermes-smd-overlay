"""Why a row was lost, recorded beside the count (2026-10-20).

The tally said captain-mark lost about two rows after each release and could
not say why: the reason was a rate-limited log line in a file nobody may read.
These pin the reason record: a closed vocabulary, no content, summed across the
seat and profile homes like the tally, and one line per counted row.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from shared import audit_failure_counter as counter
from shared.audit_client import AuditWriteError, BrokerAuditClient
from shared.audit_contract import INSERT_SQL, agent_event_params


@pytest.fixture
def machine_home(tmp_path, monkeypatch):
    (tmp_path / ".smd").mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def sock_dir():
    # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path is not.
    d = Path(tempfile.mkdtemp(prefix="smdlr", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_no_smd_dir_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert counter.read_audit_write_failure_reasons() is None


def test_a_booted_seat_with_no_losses_reports_an_empty_histogram(machine_home):
    assert counter.read_audit_write_failure_reasons() == {}


def test_each_loss_records_its_class_and_action_type(machine_home):
    counter.record_audit_write_failure("x", reason="gap_exit", action_type="TOOL_CALL_COMPLETED")
    counter.record_audit_write_failure("y", reason="gap_exit", action_type="TOOL_CALL_COMPLETED")
    counter.record_audit_write_failure(
        "z", reason="refused_not_gateway", action_type="CONFIG_WRITE"
    )
    assert counter.read_audit_write_failure_reasons() == {
        "gap_exit:TOOL_CALL_COMPLETED": 2,
        "refused_not_gateway:CONFIG_WRITE": 1,
    }
    assert counter.read_audit_write_failures() == 3


def test_free_text_can_never_reach_the_file(machine_home):
    """The context string is logged, never written; an unknown class and a
    malformed action_type collapse to constants."""
    counter.record_audit_write_failure(
        "to jane@example.com about matter 2024-001",
        reason="jane@example.com",
        action_type="matter 2024-001",
    )
    raw = (machine_home / ".smd" / "audit_write_failures.reasons").read_text()
    assert raw == "unclassified\n"


def test_profile_reasons_are_summed_with_the_seat(machine_home):
    profile = machine_home / "profiles" / "operator"
    (profile / ".smd").mkdir(parents=True)
    counter.record_audit_write_failure("a", reason="gap_exit", hermes_home=str(profile))
    counter.record_audit_write_failure("b", reason="gap_exit")
    assert counter.read_audit_write_failure_reasons() == {"gap_exit": 2}
    assert counter.read_audit_write_failure_reasons(hermes_home=str(profile)) == {"gap_exit": 2}


def test_damaged_lines_are_skipped_not_invented(machine_home):
    path = machine_home / ".smd" / "audit_write_failures.reasons"
    path.write_text("gap_exit\nnot a key\nwhatever:lower\nbroker_io:CONFIG_WRITE\n")
    assert counter.read_audit_write_failure_reasons() == {
        "gap_exit": 1,
        "broker_io:CONFIG_WRITE": 1,
    }


def test_a_large_file_is_read_from_its_end(machine_home, monkeypatch):
    monkeypatch.setattr(counter, "_REASONS_READ_CAP_BYTES", 64)
    path = machine_home / ".smd" / "audit_write_failures.reasons"
    path.write_text("gap_full\n" * 100 + "gap_exit\n" * 3)
    hist = counter.read_audit_write_failure_reasons()
    assert hist is not None
    assert hist["gap_exit"] == 3
    assert sum(hist.values()) < 103


def _params(action_type: str) -> list:
    return agent_event_params(action_type=action_type, metadata={"k": "v"})


def _one_shot_broker(path: Path, reply: dict) -> threading.Thread:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.recv(65_536)
            conn.sendall(json.dumps(reply).encode() + b"\n")
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_a_not_gateway_refusal_is_classified(machine_home, sock_dir):
    sock = sock_dir / "b.sock"
    t = _one_shot_broker(
        sock, {"ok": False, "error": "request did not originate from the gateway process"}
    )
    client = BrokerAuditClient(socket_path=str(sock))
    with pytest.raises(AuditWriteError):
        client.execute(INSERT_SQL, *_params("CONFIG_WRITE"))
    t.join(timeout=5)
    assert counter.read_audit_write_failure_reasons() == {"refused_not_gateway:CONFIG_WRITE": 1}


def test_any_other_refusal_is_classified_apart(machine_home, sock_dir):
    sock = sock_dir / "b.sock"
    t = _one_shot_broker(sock, {"ok": False, "error": "audit_append: unknown column(s)"})
    client = BrokerAuditClient(socket_path=str(sock))
    with pytest.raises(AuditWriteError):
        client.execute(INSERT_SQL, *_params("CONFIG_WRITE"))
    t.join(timeout=5)
    assert counter.read_audit_write_failure_reasons() == {"broker_refused:CONFIG_WRITE": 1}


def test_an_unreachable_broker_on_a_non_holding_writer_is_classified(machine_home, sock_dir):
    client = BrokerAuditClient(socket_path=str(sock_dir / "absent.sock"))
    with pytest.raises(AuditWriteError):
        client.execute(INSERT_SQL, *_params("WEBHOOK_ROUTED"))
    assert counter.read_audit_write_failure_reasons() == {"broker_unreachable:WEBHOOK_ROUTED": 1}


def test_the_heartbeat_carries_the_histogram_and_an_empty_one():
    from shared import heartbeat as hb

    base = {
        "heartbeat_ts": "2026-10-20T00:00:00Z",
        "last_audit_ts": None,
        "last_skill_ts": None,
        "uptime_seconds": 1,
        "version": "v",
    }
    assert "audit_write_failure_reasons" not in hb.build_payload(**base)
    empty = hb.build_payload(**base, audit_write_failure_reasons={})
    assert empty["audit_write_failure_reasons"] == {}
    some = hb.build_payload(**base, audit_write_failure_reasons={"gap_exit": 2})
    assert some["audit_write_failure_reasons"] == {"gap_exit": 2}


def _no_chown(monkeypatch):
    monkeypatch.setattr(
        counter.os, "fchown", lambda *a: (_ for _ in ()).throw(AssertionError("fchown"))
    )
    monkeypatch.setattr(
        counter.os, "chown", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chown"))
    )


def test_a_root_writer_never_follows_a_link_the_agent_planted(machine_home, monkeypatch, tmp_path):
    """Review 2026-10-20 (BLOCK): .smd is agent-owned. A root append there
    followed a planted symlink, and the fchown after it handed the agent the
    target's ownership. Root now writes only into its own directory, never
    through a link, and chowns nothing."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"secret\n")
    before = victim.stat()
    for name in ("audit_write_failures.tally", "audit_write_failures.reasons"):
        (machine_home / ".smd" / name).symlink_to(victim)
    monkeypatch.setattr(counter, "_is_root", lambda: True)
    _no_chown(monkeypatch)
    assert counter.record_audit_write_failure(
        "x", reason="refused_not_gateway", action_type="CONFIG_WRITE"
    )
    after = victim.stat()
    assert victim.read_bytes() == b"secret\n"
    assert (after.st_uid, after.st_gid, after.st_mode) == (
        before.st_uid,
        before.st_gid,
        before.st_mode,
    )
    root = machine_home / ".smd-root"
    assert (root / "audit_write_failures.tally").read_bytes() == b"\x01"
    assert (root / "audit_write_failures.reasons").read_text() == (
        "refused_not_gateway:CONFIG_WRITE\n"
    )
    # A planted link is not a count: the agent-dir tally reads as "cannot answer".
    assert counter.read_audit_write_failures() is None
    for name in ("audit_write_failures.tally", "audit_write_failures.reasons"):
        (machine_home / ".smd" / name).unlink()
    assert counter.read_audit_write_failures() == 1
    assert counter.read_audit_write_failure_reasons() == {"refused_not_gateway:CONFIG_WRITE": 1}


def test_a_root_writer_refuses_a_root_dir_the_agent_replaced_with_a_link(
    machine_home, monkeypatch, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (machine_home / ".smd-root").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(counter, "_is_root", lambda: True)
    _no_chown(monkeypatch)
    assert counter.record_audit_write_failure("x", reason="gap_exit") is False
    assert list(elsewhere.iterdir()) == []


def test_a_root_writer_refuses_a_root_dir_others_can_write(machine_home, monkeypatch):
    (machine_home / ".smd-root").mkdir(mode=0o770)
    os.chmod(machine_home / ".smd-root", 0o770)  # group-writable: refused
    monkeypatch.setattr(counter, "_is_root", lambda: True)
    assert counter.record_audit_write_failure("x", reason="gap_exit") is False


def test_root_files_stay_readable_under_the_entrypoint_umask(machine_home, monkeypatch):
    """entrypoint.sh runs the root pollers under umask 027; 0640 root files
    would leave the agent-uid heartbeat unable to read root's reasons."""
    monkeypatch.setattr(counter, "_is_root", lambda: True)
    old = os.umask(0o027)
    try:
        assert counter.record_audit_write_failure("x", reason="gap_exit")
    finally:
        os.umask(old)
    for name in ("audit_write_failures.tally", "audit_write_failures.reasons"):
        assert (machine_home / ".smd-root" / name).stat().st_mode & 0o777 == 0o644


def test_a_reader_never_follows_a_link_or_reads_a_device(machine_home):
    (machine_home / ".smd" / "audit_write_failures.reasons").symlink_to("/dev/zero")
    assert counter.read_audit_write_failure_reasons() == {}


def test_a_planted_fifo_never_blocks_the_reader(machine_home):
    """A FIFO named like the reasons file would block open() forever with no
    writer; the heartbeat must not hang on it."""
    import threading as _t

    os.mkfifo(machine_home / ".smd" / "audit_write_failures.reasons")
    result: list = []
    reader = _t.Thread(
        target=lambda: result.append(counter.read_audit_write_failure_reasons()), daemon=True
    )
    reader.start()
    reader.join(timeout=3)
    assert not reader.is_alive(), "the reader blocked on a FIFO"
    assert result == [{}]


def test_a_non_root_writer_keeps_using_the_agent_dir(machine_home):
    assert counter.record_audit_write_failure("x", reason="gap_exit") is True
    assert (machine_home / ".smd" / "audit_write_failures.tally").read_bytes() == b"\x01"
    assert not (machine_home / ".smd-root").exists()
