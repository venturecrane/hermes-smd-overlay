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
