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

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

MANAGED_PREFIX = "op-managed"
WAKE_ALWAYS = "always"
WAKE_PRE_RUN_DECIDES = "pre_run_decides"
_SUPPORTED_WAKE_POLICIES = frozenset({WAKE_ALWAYS, WAKE_PRE_RUN_DECIDES})


class CronMaterializeError(RuntimeError):
    """An authored cron entry could not be materialized (fail-closed)."""


@dataclass(frozen=True)
class RoutineChange:
    """One routine crossing the scheduled / not-scheduled line (ss-console #2498).

    THE GAP THIS CLOSES. Turning a routine on or off is the single most
    consequential thing that happens to a seat between hand-off and steady
    state, and until now it happened entirely OUTSIDE the ledger: it is a
    customer.yaml edit, materialized here at boot, leaving no row. A Named
    Administrator reading the record of a silent week could not tell a seat
    whose routines were deliberately off (ashton-price since #2332) from one
    whose routines were broken. #2498 S6.

    ``enabled`` False = the routine was scheduled and is no longer.

    Emitted on the DELTA only, never on the reconcile. This module removes and
    recreates every managed job on EVERY boot, so a row per created job would
    say "enabled" about eleven unchanged routines each time the Machine
    restarts — noise that would bury the one line that mattered and would make
    the ledger claim a change no one made.
    """

    persona_slug: str
    skill: str
    enabled: bool
    schedule: str | None  # the authored cron expression when enabling; None when disabling


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


def _scheduled_initiation_skills(persona: dict[str, Any]) -> set[str]:
    """Names of the persona's ENABLED skills that grant ``initiation.scheduled``
    (ADR 0056). A cron entry may only target one of these."""
    out: set[str] = set()
    for skill in persona.get("skills") or []:
        if not isinstance(skill, dict) or not skill.get("enabled"):
            continue
        name = skill.get("name")
        initiation = skill.get("initiation")
        if (
            isinstance(name, str)
            and name
            and isinstance(initiation, dict)
            and initiation.get("scheduled") is True
        ):
            out.add(name)
    return out


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
        scheduled_skills = _scheduled_initiation_skills(persona)
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
            # ADR 0056: a scheduled (cron) firing requires the skill to grant
            # initiation.scheduled. The validator already gates this at authoring
            # / re-validation time; this is the defense-in-depth runtime gate so a
            # cron job is never registered for a skill that did not grant the
            # scheduled initiation path — fail-closed, never silently registered.
            if skill not in scheduled_skills:
                raise CronMaterializeError(
                    f"persona {pslug!r} skill {skill!r}: cron entry references a skill "
                    "that does not grant initiation.scheduled (ADR 0056). Author "
                    "initiation.scheduled: true on the skill or drop the cron entry."
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
    reconcile_slugs: Iterable[str] | None = None,
    containment: bool = False,
    on_routine_change: Callable[[RoutineChange], None] | None = None,
) -> list[str]:
    """Reconcile Hermes cron jobs from customer.yaml into each persona's profile
    home, converging every reconciled persona's store to EXACTLY its authored
    cron set — **including the empty set**. Dropping ALL of a persona's cron now
    removes its last managed job rather than orphaning it.

    ``store_for(persona_slug)`` returns a CronStore scoped to that persona's
    Hermes home, so jobs land where ``hermes -p <slug>`` reads them.

    ``stage_script_for(persona_slug, skill, pre_run_basename) -> script_ref``
    stages a ``pre_run_decides`` entry's pre-run script into the persona
    profile's ``scripts/`` dir (Hermes' scheduler refuses scripts outside
    ``$HERMES_HOME/scripts/``) and returns the ref to register. It is injected so
    this module stays unit-testable without a filesystem; the real implementation
    lives in ``translate.py``. Required only when a ``pre_run_decides`` entry is
    present — a NULL stager with such an entry fails loud.

    Reconcile set = personas that author cron (``by_persona``) ∪ ``reconcile_slugs``.
    Callers pass ``reconcile_slugs`` = every persona that could hold a managed job
    (in practice, every persona in customer.yaml) so a persona that dropped ALL
    its cron is still visited and its orphaned managed job removed. A provided
    ``reconcile_slugs`` is expected to be a SUPERSET of ``by_persona`` keys; the
    union exists only so the ``None`` default preserves the legacy
    "reconcile authored personas only" behavior for existing callers/tests.

    Idempotent and fail-closed: BOTH validation (bad/unsupported entry, an
    unstageable pre-run script) AND store acquisition/listing happen in a first
    pass, before ANY store mutation — so a failure (bad entry, missing script, or
    an unreadable store) leaves every store untouched. The second pass removes
    each reconciled persona's managed jobs, then recreates only the authored set.
    Returns the managed names now registered.

    ``containment=True`` (the ss-console#2276 sentinel,
    :mod:`shared.cron_containment`) converges every reconciled persona to the
    EMPTY set regardless of what customer.yaml authors: the desired set is
    forced to nothing, existing managed jobs are removed, and nothing is
    created. Authored entries are deliberately not validated in this mode —
    containment must converge to zero even when customer.yaml is the thing
    that is broken."""
    # raises before any mutation on bad input (skipped under containment: the
    # desired set is empty by decree, and a malformed customer.yaml must not
    # keep a contained seat from converging to zero jobs)
    by_persona = {} if containment else _desired_by_persona(customer)

    reconcile = sorted(set(by_persona) | {s for s in (reconcile_slugs or []) if s})

    # ---- Pass A: validate + stage + acquire every store. NO store mutation. ----
    # Pre-stage every pre-run script first, so a missing/unstageable script aborts
    # with the store untouched. Keyed by (persona_slug, managed_name) → ref.
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

    # Acquire each reconciled persona's store and snapshot its managed jobs. Any
    # store-acquisition/list failure raises HERE, before the first remove/create,
    # so an unreadable store can never leave a half-applied reconcile across the
    # broadened set.
    stores: dict[str, CronStore] = {}
    managed_to_remove: dict[str, list[str]] = {}
    managed_before: dict[str, set[str]] = {}
    for pslug in reconcile:
        try:
            store = store_for(pslug)
            existing = store.list_jobs(include_disabled=True)
        except Exception as exc:  # noqa: BLE001 — fail-closed before any mutation
            raise CronMaterializeError(
                f"persona {pslug!r}: could not acquire/list cron store: {exc}"
            ) from exc
        stores[pslug] = store
        managed_to_remove[pslug] = [job["id"] for job in existing if _is_managed(job)]
        managed_before[pslug] = {str(job.get("name", "")) for job in existing if _is_managed(job)}

    # ---- Pass B: mutate. Remove managed jobs, then create the authored set. ----
    registered: list[str] = []
    for pslug in reconcile:
        store = stores[pslug]
        for job_id in managed_to_remove[pslug]:
            store.remove_job(job_id)
        for job in by_persona.get(pslug, []):
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
        _report_routine_changes(
            pslug,
            before=managed_before.get(pslug, set()),
            desired=by_persona.get(pslug, []),
            on_routine_change=on_routine_change,
        )
    return registered


def _report_routine_changes(
    persona_slug: str,
    *,
    before: set[str],
    desired: list[_DesiredJob],
    on_routine_change: Callable[[RoutineChange], None] | None,
) -> None:
    """Report only routines that CROSSED the scheduled line for this persona.

    Reported after the store mutation, so a change is only ever reported once
    the runtime already reflects it — the ordering the pause and entitlement
    controls use, and for their reason: a record of a change the seat did not
    make is worse than no record.

    A callback that raises is swallowed. Recording that a routine was turned on
    must never be what stops it from being turned on; bootstrap is the wrong
    place to fail on observability.
    """
    if on_routine_change is None:
        return
    schedules = {job.name: job.schedule for job in desired}
    skills = {job.name: job.skill for job in desired}
    after = set(schedules)
    for name in sorted(after - before):
        _emit_routine_change(
            on_routine_change,
            RoutineChange(persona_slug, skills[name], True, schedules[name]),
        )
    for name in sorted(before - after):
        # A removed job's skill is recoverable from its managed name
        # (op-managed:<persona>:<skill>) — the store row is already gone.
        _emit_routine_change(
            on_routine_change,
            RoutineChange(persona_slug, name.split(":", 2)[-1], False, None),
        )


def _emit_routine_change(
    on_routine_change: Callable[[RoutineChange], None], change: RoutineChange
) -> None:
    try:
        on_routine_change(change)
    except Exception:  # noqa: BLE001 — observability never gates materialization
        logger.warning(
            "cron_materialize: routine-change report failed for %s/%s (enabled=%s); "
            "the job change itself stands",
            change.persona_slug,
            change.skill,
            change.enabled,
        )
