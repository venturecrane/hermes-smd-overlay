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

Scope:
  - ``wake_policy: always`` → a normal skill-bearing agent job (the agent wakes
    and runs the skill each fire).
  - ``wake_policy: pre_run_decides`` (ADR 0047 phase 2, the ADR 0021 Stream B
    watcher policy) → a skill-bearing job carrying a **pre-run script** that
    decides, per tick, whether to wake the agent. The script emits the Hermes
    wake gate (``{"wakeAgent": false}`` on its last stdout line skips the LLM
    entirely — the zero-token quiet path, with the skill's own
    ``suppressed_wake`` audit row as the heartbeat); anything else wakes the
    agent with the script's stdout injected as context. The pre-run script lives
    in the skill body, but Hermes' scheduler only runs scripts that resolve
    inside ``$HERMES_HOME/scripts/`` (a path-traversal guard), so this module
    **stages** the script into the persona profile's ``scripts/`` dir via an
    injected ``stage_script_for`` callable and registers the resolved ref. An
    entry that names an unsupported ``wake_policy`` — or a ``pre_run_decides``
    entry with no ``pre_run`` script — fails loud rather than mis-wiring it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

MANAGED_PREFIX = "op-managed"
WAKE_ALWAYS = "always"
WAKE_PRE_RUN_DECIDES = "pre_run_decides"
_SUPPORTED_WAKE_POLICIES = frozenset({WAKE_ALWAYS, WAKE_PRE_RUN_DECIDES})


class CronMaterializeError(RuntimeError):
    """An authored cron entry could not be materialized (fail-closed)."""


@dataclass(frozen=True)
class _DesiredJob:
    """One authored cron entry resolved to its materialization inputs."""

    name: str  # managed name (op-managed:<persona>:<skill>)
    schedule: str
    skill: str
    pre_run: str | None  # pre-run script basename when pre_run_decides, else None


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


def _desired_by_persona(customer: dict[str, Any]) -> dict[str, list[_DesiredJob]]:
    """Return {persona_slug: [_DesiredJob, ...]} for every authored cron entry,
    grouped by persona.

    Grouped by persona because each persona's cron must be registered in THAT
    persona's Hermes profile home (``<root>/profiles/<slug>``) — the home the
    gateway reads when it runs ``hermes -p <slug> gateway run``. Registering in
    the bare data home (the original bug) left the job somewhere the gateway's
    ticker never looks, so it never fired.

    Raises CronMaterializeError on a malformed entry, an unsupported
    wake_policy, or a ``pre_run_decides`` entry with no ``pre_run`` script —
    never silently drops one.
    """
    by_persona: dict[str, list[_DesiredJob]] = {}
    for persona in customer.get("personas") or []:
        pslug = str(persona.get("slug") or persona.get("name") or "").strip()
        entries: list[_DesiredJob] = []
        for entry in persona.get("cron") or []:
            if not isinstance(entry, dict):
                raise CronMaterializeError(
                    f"persona {pslug!r}: cron entry is not a mapping: {entry!r}"
                )
            skill = str(entry.get("skill") or "").strip()
            schedule = str(entry.get("schedule") or "").strip()
            wake = str(entry.get("wake_policy") or WAKE_ALWAYS).strip()
            pre_run = str(entry.get("pre_run") or "").strip()
            if not skill or not schedule:
                raise CronMaterializeError(
                    f"persona {pslug!r}: cron entry missing skill/schedule: {entry!r}"
                )
            if wake not in _SUPPORTED_WAKE_POLICIES:
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {skill!r}: wake_policy {wake!r} is not "
                    f"materializable (supported: {', '.join(sorted(_SUPPORTED_WAKE_POLICIES))}). "
                    "Aborting bootstrap rather than silently dropping the schedule."
                )
            if wake == WAKE_PRE_RUN_DECIDES and not pre_run:
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {skill!r}: wake_policy 'pre_run_decides' "
                    "requires a 'pre_run' script (the per-tick wake gate). "
                    "Aborting bootstrap rather than waking the agent every tick."
                )
            if not pslug:
                raise CronMaterializeError(f"cron entry has no resolvable persona slug: {entry!r}")
            entries.append(
                _DesiredJob(
                    name=managed_name(pslug, skill),
                    schedule=schedule,
                    skill=skill,
                    pre_run=pre_run if wake == WAKE_PRE_RUN_DECIDES else None,
                )
            )
        if entries:
            by_persona[pslug] = entries
    return by_persona


def materialize_cron(
    customer: dict[str, Any],
    store_for: Callable[[str], CronStore],
    stage_script_for: Callable[[str, str, str], str] | None = None,
) -> list[str]:
    """Reconcile Hermes cron jobs from customer.yaml into each persona's profile
    home. ``store_for(persona_slug)`` returns a CronStore scoped to that
    persona's Hermes home, so jobs land where ``hermes -p <slug>`` reads them.

    ``stage_script_for(persona_slug, skill, pre_run_basename) -> script_ref``
    stages a ``pre_run_decides`` entry's pre-run script into the persona
    profile's ``scripts/`` dir (Hermes' scheduler refuses scripts outside
    ``$HERMES_HOME/scripts/``) and returns the ref to register. It is injected so
    this module stays unit-testable without a filesystem; the real implementation
    lives in ``translate.py``. Required only when a ``pre_run_decides`` entry is
    present — a NULL stager with such an entry fails loud.

    Idempotent per persona: for every persona that currently authors ≥1 cron
    entry, removes that persona's managed jobs and recreates the authored set —
    so adding, changing, or dropping ONE of a persona's entries reconciles
    cleanly. Returns the managed names now registered. Fail-closed: a bad or
    unsupported entry, or a pre-run script that cannot be staged, raises BEFORE
    any store mutation (validate and stage everything first).

    Known limitation (acceptable for Phase 1, where cron is being ADDED): a
    persona that drops ALL its cron entries is no longer reconciled here, so its
    last managed job is not auto-removed on the next reprovision. Removing every
    cron from a persona therefore needs a manual ``hermes -p <slug> cron remove``
    (or re-authoring then removing). The common add/change/drop-one paths are
    handled."""
    by_persona = _desired_by_persona(customer)  # raises before any mutation on bad input

    # Pre-stage every pre-run script BEFORE touching any cron store, so the
    # "validate (and stage) all before any mutation" fail-closed property holds:
    # a missing/unstageable script aborts with the store untouched. Keyed by
    # (persona_slug, managed_name) → resolved script ref.
    staged_refs: dict[tuple[str, str], str] = {}
    for pslug, desired in by_persona.items():
        for job in desired:
            if job.pre_run is None:
                continue
            if stage_script_for is None:
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {job.skill!r}: wake_policy "
                    "'pre_run_decides' needs a script stager but none was provided."
                )
            try:
                staged_refs[(pslug, job.name)] = stage_script_for(pslug, job.skill, job.pre_run)
            except Exception as exc:  # noqa: BLE001 — any staging failure is fail-closed
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {job.skill!r}: could not stage pre_run "
                    f"script {job.pre_run!r}: {exc}"
                ) from exc

    registered: list[str] = []
    for pslug, desired in by_persona.items():
        store = store_for(pslug)
        for job in store.list_jobs(include_disabled=True):
            if _is_managed(job):
                store.remove_job(job["id"])
        for job in desired:
            kwargs: dict[str, Any] = {
                "prompt": "",
                "schedule": job.schedule,
                "name": job.name,
                "skills": [job.skill],
                "no_agent": False,
            }
            ref = staged_refs.get((pslug, job.name))
            if ref is not None:
                # no_agent stays False: when the gate wakes the agent it runs the
                # skill, with the script's stdout injected as context. The script
                # only gates the wake — it is not the whole job.
                kwargs["script"] = ref
            store.create_job(**kwargs)
            registered.append(job.name)
    return registered
