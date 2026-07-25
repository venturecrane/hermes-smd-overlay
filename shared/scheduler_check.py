"""Scheduler self-check for the heartbeat emitter (ss work-liveness fix).

Why this exists: on 2026-07-16 a root-run probe left a seat's
``profiles/operator/cron/jobs.json`` root-owned 0600. The hermes-uid gateway
scheduler could not read its own job DB and NOTHING fired for 8 days while
every monitoring surface stayed green — the gate process (which hosts the
heartbeat emitter) was alive, and process-liveness was the only pulse anyone
evaluated. This module gives the heartbeat a work-liveness pulse: every tick
answers "can the scheduler read its jobs, and is anything overdue?" so the
console-side alerter can page a human in minutes instead of never.

Semantics are matched to the pinned NousResearch/hermes-agent cron store
(``cron/jobs.py`` at v2026.7.1@7c1a0295), read at source before this was
written:

* ``next_run_at`` advances PREEMPTIVELY before execution (``advance_next_run``)
  and again at completion (``mark_job_run``) — so a long-running job never
  looks overdue, and no in-flight guard is needed. A dead or locked-out
  scheduler is exactly the thing that leaves ``next_run_at`` in the past.
* Job ``state`` vocabulary is ``scheduled | paused | error | completed``
  (there is no "running" state). ``state == "error"`` means a RECURRING job
  could not compute its next fire (e.g. croniter missing) — hermes leaves it
  enabled with ``next_run_at = None`` precisely so it is not silently
  disabled; treating it as healthy here would re-silence it, so any
  error-state job fails the check.
* Naive-timezone ``next_run_at`` values are a known-real bug class
  (ss-console#1691): computing overdue from one could manufacture hours of
  phantom lateness, so naive or unparseable stamps are skipped with a WARNING
  and never page.

Enumeration is a filesystem scan of ``$HERMES_HOME/profiles/*/cron/jobs.json``.
Deliberately NOT ``path.exists()``: under a root-0700 directory ``exists()``
swallows ``PermissionError`` and returns False, which would make the exact
incident state above read as "legitimately empty, green". We ``read_text()``
and classify the exception: ``FileNotFoundError`` is a legitimate empty state
(a profile with no cron), anything else (``PermissionError``, other
``OSError``, ``JSONDecodeError``) fails the check.

The authored-vs-materialized comparison reads the root-owned, world-readable
live ``customer.yaml`` (the keystone copy): if personas author cron entries
but zero jobs are materialized, something ate the job store — that must not
read as the smd-style "deliberately no cron" green. A yaml read/parse failure
skips only this comparison (WARNING), never fails the whole check: the
store-level checks above still stand, and the yaml being unreadable by the
gate is an entrypoint-ownership bug that breaks much louder things first.

Everything here is read-only and fail-soft by construction; the emitter wraps
the call and applies a consecutive-failure debounce before reporting a
checker crash as ``scheduler_ok=0`` (report-late beats report-never — an
omitted field would recreate the "monitoring green while broken" class).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("hermes-smd-scheduler-check")

DEFAULT_HERMES_HOME = "/opt/data"
DEFAULT_CUSTOMER_YAML_PATH = "/var/lib/smd-config/customer.yaml"

# Post-restart window during which overdue math is suppressed (the volume's
# persisted next_run_at values for non-managed jobs can be legitimately stale
# until the scheduler's first ticks). ok/job_count still report — the window
# only mutes the overdue number, and the console holds (never resolves) on an
# absent field, so a restart mid-incident cannot emit a false all-clear.
BOOT_SUPPRESS_SECONDS = 900


@dataclass(frozen=True)
class SchedulerCheck:
    """One tick's verdict. ``max_overdue_seconds`` is 0 when the store was
    readable and nothing is overdue (a real measurement — the console resolves
    from it), and None ONLY when it could not be measured: the boot window
    suppressed the math, the profiles dir was unreadable, or pre-materialize
    boot — the payload omits the
    field either way and the console alerter holds rather than resolves."""

    ok: bool
    job_count: int
    max_overdue_seconds: int | None


def _job_overdue_seconds(job: object, now_utc: datetime, source: str) -> tuple[float | None, bool]:
    """(overdue_seconds, job_is_error) for one job record.

    Returns ``(None, False)`` for every skip case; ``(None, True)`` for an
    error-state job (fails the seat check); ``(seconds, False)`` when the job
    is past its next fire.
    """
    if not isinstance(job, dict):
        # A malformed row means the store itself is suspect.
        return None, True
    if job.get("enabled") is False:
        return None, False
    state = job.get("state")
    if state == "error":
        # Recurring job that could not compute a next fire (hermes leaves it
        # enabled + next_run_at None so it is not silently disabled).
        logger.warning("scheduler-check: job in state=error in %s: %r", source, job.get("name"))
        return None, True
    if state in ("paused", "completed"):
        return None, False
    nra = job.get("next_run_at")
    if not isinstance(nra, str) or not nra:
        return None, False
    try:
        dt = datetime.fromisoformat(nra)
    except ValueError:
        logger.warning(
            "scheduler-check: unparseable next_run_at %r in %s (%r)", nra, source, job.get("name")
        )
        return None, False
    if dt.tzinfo is None:
        # ss-console#1691 class: a naive stamp under a shifted HERMES_TIMEZONE
        # could read as hours overdue. Never page from one.
        logger.warning(
            "scheduler-check: naive next_run_at %r in %s (%r) — skipped",
            nra,
            source,
            job.get("name"),
        )
        return None, False
    overdue = (now_utc - dt).total_seconds()
    return (overdue if overdue > 0 else None), False


def _authored_cron_count(customer_yaml_path: str) -> int | None:
    """Count authored ``personas[].cron[]`` entries in the live customer.yaml.

    ``None`` means "could not determine" (missing file, parse failure) — the
    caller skips the authored-vs-materialized comparison in that case.
    """
    try:
        import yaml

        with open(customer_yaml_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 — comparison is best-effort
        logger.warning(
            "scheduler-check: customer.yaml unreadable (%s) — authored check skipped", exc
        )
        return None
    personas = data.get("personas")
    if not isinstance(personas, list):
        return 0
    count = 0
    for persona in personas:
        if isinstance(persona, dict):
            cron = persona.get("cron")
            if isinstance(cron, list):
                count += len(cron)
    return count


def check(
    hermes_home: str | None = None,
    *,
    customer_yaml_path: str | None = None,
    uptime_seconds: int | None = None,
    now_utc: datetime | None = None,
) -> SchedulerCheck:
    """Run the scheduler self-check. Read-only; raises nothing by design
    intent, but the emitter still wraps the call (belt and suspenders)."""
    home = hermes_home or os.environ.get("HERMES_HOME") or DEFAULT_HERMES_HOME
    yaml_path = customer_yaml_path or os.environ.get(
        "SMD_CUSTOMER_YAML_PATH", DEFAULT_CUSTOMER_YAML_PATH
    )
    now = now_utc or datetime.now(timezone.utc)

    ok = True
    job_count = 0
    # 0.0, not None: "no job is overdue" is a REAL measurement that must reach
    # the wire — the console resolves an open work_overdue alert only from a
    # reported number (NULL holds). Found live 2026-07-25: pilot-smokeball's
    # work_overdue opened during a reprovision window, the job then fired, and
    # the steady-state None held the alert open forever (no RECOVERED). None
    # now means exactly one thing: the boot window suppressed the math.
    max_overdue: float = 0.0

    profiles_dir = Path(home) / "profiles"
    try:
        entries = sorted(p for p in profiles_dir.iterdir() if p.is_dir())
    except FileNotFoundError:
        # Pre-materialize boot: no profiles yet is a legitimate empty state.
        return SchedulerCheck(ok=True, job_count=0, max_overdue_seconds=None)
    except OSError as exc:
        logger.warning("scheduler-check: profiles dir unreadable: %s", exc)
        return SchedulerCheck(ok=False, job_count=0, max_overdue_seconds=None)

    for profile in entries:
        jobs_path = profile / "cron" / "jobs.json"
        source = f"{profile.name}/cron/jobs.json"
        try:
            text = jobs_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue  # profile with no cron — legitimate
        except OSError as exc:
            # PermissionError lands here: the incident state (root-owned file
            # the hermes-uid scheduler cannot read). NEVER "legit-empty".
            logger.warning("scheduler-check: %s unreadable: %s", source, exc)
            ok = False
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("scheduler-check: %s unparseable: %s", source, exc)
            ok = False
            continue
        jobs = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            logger.warning("scheduler-check: %s has no jobs list", source)
            ok = False
            continue
        job_count += len(jobs)
        for job in jobs:
            overdue, job_error = _job_overdue_seconds(job, now, source)
            if job_error:
                ok = False
            if overdue is not None and overdue > max_overdue:
                max_overdue = overdue

    authored = _authored_cron_count(yaml_path)
    if authored is not None and authored > 0 and job_count == 0:
        # Authored schedules with zero materialized jobs: something ate the
        # store. Must not read as the smd-style deliberate-zero green.
        logger.warning(
            "scheduler-check: %d authored cron entries but 0 materialized jobs", authored
        )
        ok = False

    if uptime_seconds is not None and uptime_seconds < BOOT_SUPPRESS_SECONDS:
        # The one remaining None: post-restart staleness window (a just-booted
        # store can look overdue while the scheduler catches up). The console
        # holds — neither opens nor resolves — until the window passes.
        return SchedulerCheck(ok=ok, job_count=job_count, max_overdue_seconds=None)

    return SchedulerCheck(ok=ok, job_count=job_count, max_overdue_seconds=int(max_overdue))


__all__ = [
    "BOOT_SUPPRESS_SECONDS",
    "DEFAULT_CUSTOMER_YAML_PATH",
    "DEFAULT_HERMES_HOME",
    "SchedulerCheck",
    "check",
]
