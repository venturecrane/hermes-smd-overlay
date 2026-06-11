"""Materialize authored cron schedules into Hermes' native cron store.

:mod:`bootstrap.translate` writes profile *files*
(``$HERMES_HOME/profiles/<slug>/``). This module mutates *live scheduler
state* (``$HERMES_HOME/cron/jobs.json``). The two are deliberately separate
bootstrap steps — ``bootstrap/cli.py`` calls :func:`sync_cron_jobs` AFTER
``translate_customer_yaml`` with its own logging and failure handling — so a
profile-file change can never silently de-sync the scheduler, and a cron-sync
failure never leaves a half-written profile.

Why this exists
---------------
``customer.yaml`` authors a per-persona ``cron[]`` block (ADR 0021 Stream B),
the validator accepts it, but nothing materialized it — the authored schedule
never reached the runtime, so the skill never ran on a schedule. This closes
that gap by registering a real Hermes cron job per authored entry.

Hermes cron facts this builds on (upstream ``cron/jobs.py``)
-----------------------------------------------------------
* Jobs live in ``$HERMES_HOME/cron/jobs.json`` and are created by
  :func:`cron.jobs.create_job` (parses the schedule, computes ``next_run_at``,
  appends, saves). The scheduler ticks ~60s and runs due jobs — no restart.
* Jobs are **GLOBAL to the Machine** — there is no per-persona field. For SMD's
  single-persona deployments this is fine; the job *name* encodes the persona
  for traceability. A cron run is profile-agnostic, but the repo skill body is
  in the global ``$HERMES_HOME/skills/`` catalog and the Workspace credential is
  broker-mediated (machine-global), so the skill still resolves and authenticates.
* Hermes injects the full ``SKILL.md`` into the run even with ``prompt=""``, so
  ``skills=[skill]`` drives the job.

Ownership + safety
------------------
Jobs we materialize carry the :data:`JOB_NAME_PREFIX` name prefix. Sync NEVER
touches a job without that prefix — agent-authored and user-created jobs are
off-limits. Re-runs converge (create missing, replace changed, remove
no-longer-authored) without accreting duplicates.

Only ``wake_policy: always`` is expressible. ``pre_run_decides`` needs an
upstream Hermes pre-run hook that does not exist; the ss-console validator
rejects it at author time and this module rejects it defensively.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Name prefix marking a job this materializer owns. Sync only ever creates,
#: replaces, or deletes jobs whose name starts with this — never anything else.
JOB_NAME_PREFIX = "smd-mat-"


class CronSyncError(Exception):
    """Raised when authored cron entries cannot be materialized."""


def _resolve_deliver(entry: dict[str, Any], customer: dict[str, Any]) -> str:
    """Resolve where a job's output goes.

    Order: authored ``cron[].deliver`` wins; else, when Telegram is enabled with
    exactly one allowed user, deliver to that chat (so the principal actually
    sees it); else ``local`` (output stays on the Machine — logged as a WARN
    because a human won't see it without pulling it off the box).
    """
    authored = entry.get("deliver")
    if authored:
        return str(authored)
    telegram = customer.get("telegram") or {}
    if telegram.get("enabled"):
        allow = [str(x).strip() for x in (telegram.get("allow_from") or []) if str(x).strip()]
        if len(allow) == 1:
            return f"telegram:{allow[0]}"
    return "local"


def _desired_jobs(customer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the desired job specs from ``personas[].cron``.

    Returns a mapping of job name → spec(name, skill, schedule, deliver).
    Raises :class:`CronSyncError` on a malformed entry, an unsupported
    ``wake_policy``, or a duplicate ``(persona, skill)`` (which would collide on
    the deterministic job name).
    """
    customer_id = str(customer.get("customer_id") or "customer")
    desired: dict[str, dict[str, Any]] = {}
    for persona in customer.get("personas") or []:
        slug = str(persona.get("slug") or "persona")
        seen: set[str] = set()
        for entry in persona.get("cron") or []:
            if not isinstance(entry, dict):
                raise CronSyncError(f"persona {slug!r}: cron entry must be a mapping (got {entry!r})")
            skill = entry.get("skill")
            schedule = entry.get("schedule")
            if not skill or not schedule:
                raise CronSyncError(
                    f"persona {slug!r}: cron entry needs both 'skill' and 'schedule' (got {entry!r})"
                )
            wake = entry.get("wake_policy", "always")
            if wake != "always":
                raise CronSyncError(
                    f"persona {slug!r} skill {skill!r}: wake_policy={wake!r} is not supported. "
                    f"Only 'always' is expressible — 'pre_run_decides' needs an upstream Hermes "
                    f"pre-run hook that does not exist."
                )
            if skill in seen:
                raise CronSyncError(
                    f"persona {slug!r}: duplicate cron entry for skill {skill!r} "
                    f"(both would materialize to the same job)."
                )
            seen.add(skill)
            name = f"{JOB_NAME_PREFIX}{customer_id}-{slug}-{skill}"
            desired[name] = {
                "name": name,
                "skill": str(skill),
                "schedule": str(schedule),
                "deliver": _resolve_deliver(entry, customer),
            }
            if desired[name]["deliver"] == "local":
                logger.warning(
                    "cron-sync: %s resolves deliver=local — output stays on the Machine; "
                    "the principal won't see it. Author cron[].deliver or enable a single-user "
                    "Telegram to route it.",
                    name,
                )
    return desired


def _job_matches(job: dict[str, Any], spec: dict[str, Any]) -> bool:
    """True when an existing job already encodes the desired spec (no churn)."""
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    if list(skills[:1]) != [spec["skill"]]:
        return False
    if str(job.get("deliver")) != spec["deliver"]:
        return False
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        current = schedule.get("expr") or schedule.get("display")
    else:
        current = None
    current = current or job.get("schedule_display")
    return current == spec["schedule"]


def _load_hermes_cron(hermes_home: Path):
    """Import Hermes' ``cron.jobs`` and point its storage at ``hermes_home``.

    ``cron.jobs`` resolves ``JOBS_FILE`` from ``HERMES_HOME`` at import time. We
    retarget the module's storage constants so this function honors the
    ``hermes_home`` argument deterministically regardless of import-time env (a
    no-op on the Machine, where they already match). This runs in the
    short-lived ``hermes-smd bootstrap`` process — separate from the long-lived
    gateway — so mutating the module's constants here is isolated and safe.
    """
    try:
        hermes_cron = importlib.import_module("cron.jobs")
    except ImportError as exc:  # pragma: no cover - real Hermes always present on the Machine
        raise CronSyncError(
            f"Hermes 'cron.jobs' module is not importable ({exc}); cannot register cron jobs."
        ) from exc
    cron_dir = hermes_home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    hermes_cron.CRON_DIR = cron_dir
    hermes_cron.JOBS_FILE = cron_dir / "jobs.json"
    hermes_cron.OUTPUT_DIR = cron_dir / "output"
    return hermes_cron


def sync_cron_jobs(customer_yaml_path: str, hermes_home: str) -> list[str]:
    """Materialize ``personas[].cron`` into Hermes cron jobs, idempotently.

    Reads the authored ``customer.yaml``, computes the desired set of
    materializer-owned jobs, and reconciles ``jobs.json`` to match: create
    missing, replace changed, remove no-longer-authored — touching ONLY jobs
    under :data:`JOB_NAME_PREFIX`. Unchanged jobs are left as-is (no churn, so a
    plain reboot is a no-op).

    Args:
        customer_yaml_path: Path to the authored ``customer.yaml``.
        hermes_home: Hermes home whose ``cron/jobs.json`` is reconciled.

    Returns:
        Names of jobs created or replaced this run (empty when already in sync).

    Raises:
        CronSyncError: On a malformed/unsupported cron entry or if Hermes' cron
            module is unavailable. Callers (the bootstrap CLI) log and continue
            so a cron problem never crashloops the Machine.
    """
    yaml_path = Path(customer_yaml_path)
    with yaml_path.open() as handle:
        customer = yaml.safe_load(handle) or {}

    desired = _desired_jobs(customer)
    hermes_cron = _load_hermes_cron(Path(hermes_home))
    existing = hermes_cron.load_jobs()
    existing_owned = {
        str(j.get("name")): j
        for j in existing
        if str(j.get("name") or "").startswith(JOB_NAME_PREFIX)
    }

    to_create: list[dict[str, Any]] = []
    to_delete: set[str] = set()
    for name, spec in desired.items():
        current = existing_owned.get(name)
        if current is None:
            to_create.append(spec)
        elif not _job_matches(current, spec):
            to_delete.add(name)  # stale version → replace
            to_create.append(spec)
        # else: already in sync — leave untouched
    removed = [name for name in existing_owned if name not in desired]
    to_delete.update(removed)

    if to_delete:
        kept = [j for j in existing if str(j.get("name")) not in to_delete]
        hermes_cron.save_jobs(kept)

    for spec in to_create:
        hermes_cron.create_job(
            prompt="",  # Hermes injects the full SKILL.md; the skill drives the run
            schedule=spec["schedule"],
            name=spec["name"],
            skills=[spec["skill"]],
            deliver=spec["deliver"],
            no_agent=False,
        )
        logger.info(
            "cron-sync: registered %s (skill=%s schedule=%s deliver=%s)",
            spec["name"],
            spec["skill"],
            spec["schedule"],
            spec["deliver"],
        )

    logger.info(
        "cron-sync: desired=%d created/replaced=%d removed=%d unchanged=%d",
        len(desired),
        len(to_create),
        len(removed),
        len(desired) - len(to_create),
    )
    return [spec["name"] for spec in to_create]
