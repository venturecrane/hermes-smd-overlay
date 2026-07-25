"""Tests for shared/scheduler_check.py (ss work-liveness fix).

The load-bearing cases are named for the incidents they close:

* ``test_permission_denied_is_never_legit_empty`` — the 2026-07-16/24
  incident state (root-owned jobs.json the hermes scheduler cannot read)
  must fail the check, never read as an empty store.
* ``test_error_state_job_fails_the_check`` — hermes marks a recurring job
  ``state=error`` when it cannot compute a next fire (croniter class);
  skipping it would re-silence the schedule hermes deliberately refused to
  silently disable.
* ``test_authored_but_not_materialized_fails`` — authored cron with zero
  materialized jobs must not read as the smd-style deliberate-zero green.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from shared.scheduler_check import BOOT_SUPPRESS_SECONDS, SchedulerCheck, check

NOW = datetime(2026, 7, 24, 20, 0, 0, tzinfo=timezone.utc)


def _write_jobs(profile_dir, jobs, name="jobs.json"):
    cron = profile_dir / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    path = cron / name
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def _job(**overrides):
    base = {
        "id": "j1",
        "name": "op-managed:operator:deadline-miss-escalator",
        "enabled": True,
        "state": "scheduled",
        "next_run_at": (NOW + timedelta(hours=1)).isoformat(),
        "last_run_at": (NOW - timedelta(days=1)).isoformat(),
    }
    base.update(overrides)
    return base


def _home(tmp_path):
    (tmp_path / "profiles").mkdir()
    return str(tmp_path)


def _no_yaml(tmp_path):
    """A customer.yaml path that does not exist (authored check skipped)."""
    return str(tmp_path / "absent-customer.yaml")


def test_missing_profiles_dir_is_legit_empty(tmp_path):
    result = check(str(tmp_path / "nope"), customer_yaml_path=_no_yaml(tmp_path))
    assert result == SchedulerCheck(ok=True, job_count=0, max_overdue_seconds=None)


def test_profile_without_jobs_file_is_legit_empty(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "profiles" / "operator").mkdir()
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is True
    assert result.job_count == 0


def test_permission_denied_is_never_legit_empty(tmp_path):
    """THE incident state: a jobs.json the checking uid cannot read."""
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    path = _write_jobs(profile, [_job()])
    path.chmod(0o000)
    try:
        result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    finally:
        path.chmod(0o600)
    assert result.ok is False


def test_permission_denied_on_cron_dir_is_never_legit_empty(tmp_path):
    """Variant: the cron DIRECTORY is unreadable (root-0700 shape). A
    path.exists() implementation would swallow this as False/legit-empty —
    the read_text() classification must fail the check instead."""
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job()])
    (profile / "cron").chmod(0o000)
    try:
        result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    finally:
        (profile / "cron").chmod(0o700)
    assert result.ok is False


def test_unparseable_json_fails(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    (profile / "cron").mkdir()
    (profile / "cron" / "jobs.json").write_text("{not json", encoding="utf-8")
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is False


def test_healthy_future_jobs_green(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job(), _job(id="j2", name="op-managed:operator:digest")])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result == SchedulerCheck(ok=True, job_count=2, max_overdue_seconds=0)


def test_overdue_job_reports_seconds(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job(next_run_at=(NOW - timedelta(seconds=1234)).isoformat())])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is True
    assert result.max_overdue_seconds == 1234


def test_max_overdue_across_profiles(tmp_path):
    home = _home(tmp_path)
    for name, overdue in (("operator", 100), ("intake", 5000)):
        profile = tmp_path / "profiles" / name
        profile.mkdir()
        _write_jobs(profile, [_job(next_run_at=(NOW - timedelta(seconds=overdue)).isoformat())])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.max_overdue_seconds == 5000
    assert result.job_count == 2


def test_disabled_paused_completed_jobs_skipped(tmp_path):
    """Deliberately-idle jobs never page, even with a past next_run_at."""
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    past = (NOW - timedelta(days=3)).isoformat()
    _write_jobs(
        profile,
        [
            _job(enabled=False, next_run_at=past),
            _job(id="j2", state="paused", next_run_at=past),
            _job(id="j3", state="completed", enabled=False, next_run_at=past),
        ],
    )
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is True
    assert (
        result.max_overdue_seconds == 0
    )  # 0 = measured "nothing overdue" (resolves); None only when unmeasurable
    assert result.job_count == 3  # counted (materialization signal), not aged


def test_error_state_job_fails_the_check(tmp_path):
    """hermes leaves a recurring job enabled with state=error + next_run_at
    None when it cannot compute the next fire — the schedule has quietly
    gone off, which is exactly a work-liveness failure."""
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job(state="error", next_run_at=None)])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is False


def test_naive_and_unparseable_next_run_at_skipped(tmp_path):
    """ss-console#1691 class: naive stamps could read hours overdue under a
    shifted timezone — they must never page (skip + WARNING)."""
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(
        profile,
        [
            _job(next_run_at="2026-07-20T07:00:00"),  # naive, days "overdue"
            _job(id="j2", next_run_at="not-a-timestamp"),
        ],
    )
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is True
    assert (
        result.max_overdue_seconds == 0
    )  # 0 = measured "nothing overdue" (resolves); None only when unmeasurable


def test_malformed_job_row_fails_the_check(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, ["not-a-dict"])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is False


def test_boot_suppression_mutes_overdue_only(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job(next_run_at=(NOW - timedelta(hours=2)).isoformat())])
    result = check(
        home,
        customer_yaml_path=_no_yaml(tmp_path),
        now_utc=NOW,
        uptime_seconds=BOOT_SUPPRESS_SECONDS - 1,
    )
    assert result.ok is True
    assert result.job_count == 1
    assert result.max_overdue_seconds is None  # muted by the boot window
    steady = check(
        home,
        customer_yaml_path=_no_yaml(tmp_path),
        now_utc=NOW,
        uptime_seconds=BOOT_SUPPRESS_SECONDS + 1,
    )
    assert steady.max_overdue_seconds == 7200


def test_authored_but_not_materialized_fails(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "profiles" / "operator").mkdir()  # profile exists, no cron store
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(
        "personas:\n"
        "  - slug: operator\n"
        "    cron:\n"
        "      - skill: deadline-miss-escalator\n"
        "        schedule: '0 7 * * *'\n",
        encoding="utf-8",
    )
    result = check(home, customer_yaml_path=str(yaml_path), now_utc=NOW)
    assert result.ok is False


def test_authored_zero_stays_green_at_zero_jobs(tmp_path):
    """The smd seat: all cron deliberately unauthored — zero jobs is green."""
    home = _home(tmp_path)
    (tmp_path / "profiles" / "crane").mkdir()
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text("personas:\n  - slug: crane\n", encoding="utf-8")
    result = check(home, customer_yaml_path=str(yaml_path), now_utc=NOW)
    assert result.ok is True
    assert result.job_count == 0


def test_unreadable_yaml_skips_authored_check_only(tmp_path):
    home = _home(tmp_path)
    profile = tmp_path / "profiles" / "operator"
    profile.mkdir()
    _write_jobs(profile, [_job()])
    result = check(home, customer_yaml_path=_no_yaml(tmp_path), now_utc=NOW)
    assert result.ok is True  # store checks stand; authored comparison skipped
