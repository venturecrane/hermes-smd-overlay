"""Materialize ``customer.yaml`` ``personas[].cron[]`` into Hermes-native cron jobs.

ADR 0047. The authored cron block (customer-zero's hourly ``inbox-triage``) was
validated and then SILENTLY DROPPED at materialization — ``translate.py`` never
registered a job, so no scheduled turn ever fired ("validation passing ≠
materialized", the exact class that has bitten us). This module closes that: it
reconciles the authored cron entries into the Hermes-native cron store
(``cron.jobs`` — jobs persisted at ``$HERMES_HOME/cron/jobs.json``) at bootstrap,
before the gateway/scheduler starts.

Design:
  - **Declarative + idempotent.** Every job we own carries a managed-name prefix
    (``op-managed:<persona>:<skill>``). On each run we remove all managed jobs
    and recreate exactly the authored set — so the store always matches
    customer.yaml, and a job can never duplicate across restarts (the
    reprovision-twice → still-one-job property). Run history / job-id for a
    managed job resets on each boot; acceptable for forever-jobs whose schedule
    is absolute.
  - **Fail-closed and loud** (ADR 0047). A cron entry missing skill/schedule, or
    with a ``wake_policy`` this phase does not yet wire, RAISES — bootstrap
    aborts rather than silently dropping it.
  - **The store is injected** so this is unit-testable without Hermes installed
    (CI has no ``cron.jobs``); ``translate.py`` builds the real store lazily,
    only when cron entries actually exist.

Phase-1 scope: ``wake_policy: always`` → a normal skill-bearing agent job (the
agent wakes and runs the skill each fire). Polling/watcher policies
(``no_agent`` + pre-run script, per ADR 0021 Stream B) are a named follow-on;
authoring one today fails loud rather than mis-wiring it.
"""

from __future__ import annotations

from typing import Any, Protocol

MANAGED_PREFIX = "op-managed"
_SUPPORTED_WAKE_POLICIES = frozenset({"always"})


class CronMaterializeError(RuntimeError):
    """An authored cron entry could not be materialized (fail-closed)."""


class CronStore(Protocol):
    """The slice of Hermes' ``cron.jobs`` API this module uses."""

    def list_jobs(self, include_disabled: bool = False) -> list[dict[str, Any]]: ...

    def create_job(self, **kwargs: Any) -> dict[str, Any]: ...

    def remove_job(self, job_id: str) -> bool: ...


def managed_name(persona_slug: str, skill: str) -> str:
    """Deterministic name marking a job as owned by this materializer."""
    return f"{MANAGED_PREFIX}:{persona_slug}:{skill}"


def _is_managed(job: dict[str, Any]) -> bool:
    return str(job.get("name", "")).startswith(MANAGED_PREFIX + ":")


def _desired_jobs(customer: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (managed_name, schedule, skill) for every authored cron entry.

    Raises CronMaterializeError on a malformed entry or an unsupported
    wake_policy — never silently drops one.
    """
    desired: list[tuple[str, str, str]] = []
    for persona in customer.get("personas") or []:
        pslug = persona.get("slug") or persona.get("name") or "?"
        for entry in persona.get("cron") or []:
            if not isinstance(entry, dict):
                raise CronMaterializeError(
                    f"persona {pslug!r}: cron entry is not a mapping: {entry!r}"
                )
            skill = str(entry.get("skill") or "").strip()
            schedule = str(entry.get("schedule") or "").strip()
            wake = str(entry.get("wake_policy") or "always").strip()
            if not skill or not schedule:
                raise CronMaterializeError(
                    f"persona {pslug!r}: cron entry missing skill/schedule: {entry!r}"
                )
            if wake not in _SUPPORTED_WAKE_POLICIES:
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {skill!r}: wake_policy {wake!r} is not yet "
                    "materializable (only 'always' is wired — ADR 0047 phase 1). "
                    "Aborting bootstrap rather than silently dropping the schedule."
                )
            desired.append((managed_name(pslug, skill), schedule, skill))
    return desired


def materialize_cron(customer: dict[str, Any], store: CronStore) -> list[str]:
    """Reconcile Hermes cron jobs from customer.yaml. Returns the managed names
    now registered. Idempotent: removes all managed jobs, then creates exactly
    the authored set."""
    desired = _desired_jobs(customer)  # raises before any store mutation on bad input

    for job in store.list_jobs(include_disabled=True):
        if _is_managed(job):
            store.remove_job(job["id"])

    registered: list[str] = []
    for name, schedule, skill in desired:
        store.create_job(
            prompt="",
            schedule=schedule,
            name=name,
            skills=[skill],
            no_agent=False,
        )
        registered.append(name)
    return registered
