"""The audit-write-failure tally: it counts, it crosses processes, it can fail.

ss-console #2498. Every one of these asserts a property the heartbeat depends
on, and each has a falsifier: revert the change under test and the named
assertion fails, not some downstream one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shared import audit_failure_counter as counter
from shared.audit_client import AuditWriteError

ROOT = Path(__file__).parent.parent


@pytest.fixture
def machine_home(tmp_path, monkeypatch):
    """A HERMES_HOME whose ``.smd`` exists, as it does on a booted Machine."""
    (tmp_path / ".smd").mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# The three states, and why the middle one is not the same as the first
# ---------------------------------------------------------------------------


def test_no_smd_dir_reports_unknown_not_zero(tmp_path, monkeypatch):
    """A seat that cannot answer must not answer 'healthy'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert counter.read_audit_write_failures() is None


def test_booted_seat_with_no_failures_reports_a_real_zero(machine_home):
    """0 is the value that lets a recovered seat stop alerting. Absence cannot
    do that job, which is why this is not None."""
    assert counter.read_audit_write_failures() == 0


def test_each_failure_adds_one(machine_home):
    for expected in (1, 2, 3):
        assert counter.record_audit_write_failure("broker unreachable") is True
        assert counter.read_audit_write_failures() == expected


def test_record_is_a_noop_without_a_tally_dir(tmp_path, monkeypatch):
    """Off-Machine — CI, a dev shell, a unit test that raises AuditWriteError —
    nothing is written and nothing is created. The directory is NEVER made
    here; importing the audit stack must not touch a developer's filesystem."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert counter.record_audit_write_failure("no home") is False
    assert not (tmp_path / ".smd").exists()


def test_record_never_raises_when_the_path_is_unusable(machine_home):
    """A broken counter must not turn a degraded audit write into a crash."""
    # A directory where the tally file belongs: every open() for write fails.
    counter.tally_path().mkdir()
    assert counter.record_audit_write_failure("unwritable") is False
    assert counter.read_audit_write_failures() is None


# ---------------------------------------------------------------------------
# The property the design rests on
# ---------------------------------------------------------------------------


def test_concurrent_writers_do_not_lose_counts(machine_home):
    """The reason this is a byte tally and not a JSON counter.

    A broker outage produces a BURST across the agent process, the gate, and
    every cron pre_run child at once. A read-modify-write counter loses
    increments under exactly that burst — the case the field exists for. Four
    real processes, twenty-five bumps each, and the count must be exactly 100.
    """
    bumper = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from shared.audit_failure_counter import record_audit_write_failure\n"
        "for _ in range(25): record_audit_write_failure('burst')\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    procs = [
        subprocess.Popen([sys.executable, "-c", bumper], env=env, cwd=str(ROOT)) for _ in range(4)
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    assert counter.read_audit_write_failures() == 100


def test_the_tally_survives_a_new_process(machine_home):
    """The whole point: hooks run in the agent process, the heartbeat reads in
    the gate's. A process variable cannot cross that boundary; this must."""
    counter.record_audit_write_failure("written here")
    reader = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
        "from shared.audit_failure_counter import read_audit_write_failures\n"
        "print(read_audit_write_failures())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", reader],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"


# ---------------------------------------------------------------------------
# The wiring: raising the canonical error is what counts a lost row
# ---------------------------------------------------------------------------


def test_raising_audit_write_error_tallies_the_lost_row(machine_home):
    """The choke point. Counting lives in AuditWriteError's constructor so a
    future writer cannot add a swallow site that forgets to count — which is
    the failure #2498 exists to close."""
    before = counter.read_audit_write_failures()
    with pytest.raises(AuditWriteError):
        raise AuditWriteError("broker refused append")
    assert counter.read_audit_write_failures() == before + 1


def test_a_successful_audit_write_tallies_nothing(machine_home):
    """The falsifier for the test above: if the tally counted writes rather
    than failures, or counted unconditionally, this would move."""
    before = counter.read_audit_write_failures()
    assert before == 0
    assert counter.read_audit_write_failures() == 0


# ---------------------------------------------------------------------------
# The seat has more than one HERMES_HOME (pilot-smokeball, 2026-09-26)
# ---------------------------------------------------------------------------


def _profile_tally(home: Path, profile: str, count: int) -> Path:
    smd = home / "profiles" / profile / ".smd"
    smd.mkdir(parents=True, mode=0o700)
    tally = smd / "audit_write_failures.tally"
    tally.write_bytes(b"x" * count)
    return tally


def test_the_gateways_profile_tally_is_counted(machine_home):
    """The gateway runs under ``-p operator`` and tallies into its profile home.
    Reading only the seat home reported 13 while the gateway's file held 238.
    Falsifier: drop the profile glob and this reads 13."""
    (machine_home / ".smd" / "audit_write_failures.tally").write_bytes(b"x" * 13)
    _profile_tally(machine_home, "operator", 238)
    assert counter.read_audit_write_failures() == 251


def test_a_reader_inside_a_profile_home_still_answers_for_the_seat(machine_home, monkeypatch):
    (machine_home / ".smd" / "audit_write_failures.tally").write_bytes(b"x" * 2)
    _profile_tally(machine_home, "operator", 5)
    monkeypatch.setenv("HERMES_HOME", str(machine_home / "profiles" / "operator"))
    assert counter.read_audit_write_failures() == 7


def test_profile_losses_count_when_the_seat_home_has_none(machine_home):
    _profile_tally(machine_home, "operator", 4)
    assert counter.read_audit_write_failures() == 4


def test_a_profile_tally_without_a_seat_smd_is_still_unknown(tmp_path, monkeypatch):
    """No seat-level ``.smd`` keeps its meaning: this seat cannot answer."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _profile_tally(tmp_path, "operator", 4)
    assert counter.read_audit_write_failures() is None


def test_a_broken_profile_tally_is_skipped_not_fatal(machine_home):
    (machine_home / ".smd" / "audit_write_failures.tally").write_bytes(b"x" * 3)
    bad = machine_home / "profiles" / "operator" / ".smd" / "audit_write_failures.tally"
    bad.mkdir(parents=True)
    _profile_tally(machine_home, "other", 2)
    assert counter.read_audit_write_failures() == 5
