"""Tests for the audit-wiring health surface (#64).

The failure class: audit emission dies silently while enforcement keeps
running — a compliance ledger going dark with no signal. Three properties
are locked here:

1. The boot sentinel round-trips and is staleness-checked by pid (a dead
   plugin can't sentinel its own non-execution; a previous boot's file must
   degrade, never masquerade as current state).
2. The config snapshot surfaces ``audit.writer_wired`` (and degrades honestly
   when the sentinel is absent).
3. ``NoAuditWarner`` warns continuously (rate-limited), not once at init.
"""

from __future__ import annotations

import logging
import os

from shared import audit_status as ast
from shared import config_snapshot as cs

# --------------------------------------------------------------------------- #
# write/read round-trip
# --------------------------------------------------------------------------- #


def test_status_round_trip(tmp_path) -> None:
    assert ast.write_audit_status(
        wired=True, transport="broker", reason=None, hermes_home=str(tmp_path)
    )
    status = ast.read_audit_status(str(tmp_path))
    assert status is not None
    assert status["schema"] == ast.SCHEMA
    assert status["wired"] is True
    assert status["transport"] == "broker"
    assert status["reason"] is None
    assert status["pid"] == os.getpid()


def test_write_overwrites_previous_outcome(tmp_path) -> None:
    # register() writes "in progress" first, then the outcome — last write wins.
    ast.write_audit_status(
        wired=False, transport=None, reason="registration in progress", hermes_home=str(tmp_path)
    )
    ast.write_audit_status(wired=True, transport="direct", reason=None, hermes_home=str(tmp_path))
    status = ast.read_audit_status(str(tmp_path))
    assert status and status["wired"] is True and status["transport"] == "direct"


def test_write_failure_is_swallowed(tmp_path) -> None:
    # An unwritable home must never raise out of plugin registration.
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the dir should go", encoding="utf-8")
    assert (
        ast.write_audit_status(
            wired=True, transport="broker", reason=None, hermes_home=str(blocker)
        )
        is False
    )


def test_read_absent_or_garbage_returns_none(tmp_path) -> None:
    assert ast.read_audit_status(str(tmp_path)) is None
    smd = tmp_path / ".smd"
    smd.mkdir()
    (smd / "audit_status.json").write_text("{ not json", encoding="utf-8")
    assert ast.read_audit_status(str(tmp_path)) is None
    (smd / "audit_status.json").write_text('{"schema": "wrong/9", "wired": true}', encoding="utf-8")
    assert ast.read_audit_status(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# evaluate_status — staleness semantics
# --------------------------------------------------------------------------- #


def test_evaluate_absent_sentinel_degrades_to_unknown() -> None:
    fact, degraded = ast.evaluate_status(None)
    assert fact == {"writer_wired": None, "transport": None, "reason": None}
    assert degraded and degraded[0]["field"] == "audit.writer_wired"


def test_evaluate_live_writer_pid_is_current_boot_fact() -> None:
    # Staleness key is WRITER-PID LIVENESS, not equality with a discovered
    # "agent pid" — Hermes children inherit SMD_CUSTOMER_SLUG, so discovery
    # can land on a child of the gateway (live-verified on customer-zero
    # 2026-06-12: sentinel pid 927, discovered pid a sibling).
    status = {"schema": ast.SCHEMA, "wired": True, "transport": "broker", "reason": None, "pid": 7}
    fact, degraded = ast.evaluate_status(status, pid_alive=lambda pid: pid == 7)
    assert fact["writer_wired"] is True
    assert fact["transport"] == "broker"
    assert degraded == []


def test_evaluate_dead_writer_pid_degrades_as_previous_boot() -> None:
    status = {"schema": ast.SCHEMA, "wired": True, "transport": "broker", "reason": None, "pid": 7}
    fact, degraded = ast.evaluate_status(status, pid_alive=lambda pid: False)
    # The value is still reported (it's a real file), but flagged stale —
    # a wired:true from a previous boot must not read as "currently wired".
    assert fact["writer_wired"] is True
    assert degraded and "previous boot" in degraded[0]["reason"]


def test_evaluate_unusable_sentinel_pid_degrades() -> None:
    for bogus in (None, -1, "927"):
        status = {
            "schema": ast.SCHEMA,
            "wired": False,
            "transport": None,
            "reason": "x",
            "pid": bogus,
        }
        fact, degraded = ast.evaluate_status(status, pid_alive=lambda pid: True)
        assert fact["writer_wired"] is False
        assert degraded and "staleness" in degraded[0]["reason"]


def test_evaluate_own_process_pid_is_alive_by_default() -> None:
    # The default pid_alive uses /proc; our own pid must read as alive.
    import os

    status = {
        "schema": ast.SCHEMA,
        "wired": True,
        "transport": "direct",
        "reason": None,
        "pid": os.getpid(),
    }
    fact, degraded = ast.evaluate_status(status)
    if ast._pid_alive(os.getpid()):  # /proc present (Linux/CI)
        assert degraded == []
    else:  # macOS dev machines have no /proc — degrades honestly
        assert degraded and "previous boot" in degraded[0]["reason"]
    assert fact["writer_wired"] is True


def test_evaluate_non_bool_wired_reports_unknown() -> None:
    status = {"schema": ast.SCHEMA, "wired": "yes", "pid": 7}
    fact, _ = ast.evaluate_status(status, pid_alive=lambda pid: True)
    assert fact["writer_wired"] is None


# --------------------------------------------------------------------------- #
# config snapshot integration
# --------------------------------------------------------------------------- #


def test_build_snapshot_carries_audit_fact() -> None:
    snap = cs.build_snapshot(
        allowlist=["FOO"],
        agent_env={"FOO": False},
        overlay_ref={"value": "abc", "source": "direct_url"},
        profiles=[],
        extra_degraded=[],
        audit={"writer_wired": True, "transport": "broker", "reason": None},
    )
    assert snap["audit"] == {"writer_wired": True, "transport": "broker", "reason": None}
    assert not any(d["field"] == "audit.writer_wired" for d in snap["degraded"])


def test_build_snapshot_without_audit_degrades() -> None:
    snap = cs.build_snapshot(
        allowlist=["FOO"],
        agent_env={"FOO": False},
        overlay_ref={"value": "abc", "source": "direct_url"},
        profiles=[],
        extra_degraded=[],
    )
    assert snap["audit"] == {"writer_wired": None, "transport": None, "reason": None}
    assert any(d["field"] == "audit.writer_wired" for d in snap["degraded"])


# --------------------------------------------------------------------------- #
# NoAuditWarner — rate-limited continuous signal
# --------------------------------------------------------------------------- #


def test_no_audit_warner_rate_limits(caplog) -> None:
    log = logging.getLogger("test.no_audit")
    warner = ast.NoAuditWarner(interval_seconds=3600.0)
    with caplog.at_level(logging.DEBUG, logger="test.no_audit"):
        assert warner.warn(log, "first") is True
        assert warner.warn(log, "second") is False  # suppressed within interval
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1 and "NO-AUDIT MODE" in warnings[0].getMessage()
    assert len(debugs) == 1  # the suppressed call still leaves a debug trace


def test_no_audit_warner_fires_again_after_interval(caplog) -> None:
    log = logging.getLogger("test.no_audit.interval")
    warner = ast.NoAuditWarner(interval_seconds=0.0)
    with caplog.at_level(logging.WARNING, logger="test.no_audit.interval"):
        assert warner.warn(log, "first") is True
        assert warner.warn(log, "second") is True  # zero interval → fires again
    assert sum(1 for r in caplog.records if r.levelno == logging.WARNING) == 2
