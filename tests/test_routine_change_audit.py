"""Turning a routine on or off leaves a row that names who did it (#2498).

Three seams, tested where each one lives:

  1. ``cron_materialize`` reports the DELTA — routines that crossed the
     scheduled line — and never the reconcile, which recreates every managed
     job on every boot.
  2. ``routine_change_spool`` carries the report from bootstrap (which cannot
     write the ledger) to the gateway (which can), exactly once.
  3. The audit plugin turns each drained event into a ROUTINE_ENABLED /
     ROUTINE_DISABLED row with an actor.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from bootstrap.cron_materialize import RoutineChange, materialize_cron
from shared.routine_change_spool import append_routine_change, drain_routine_changes, spool_path

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# 1. The delta
# ---------------------------------------------------------------------------


class _Store:
    """Minimal CronStore: a name-keyed dict of jobs."""

    def __init__(self, existing: list[str] | None = None) -> None:
        self.jobs = [{"id": f"id-{n}", "name": n} for n in (existing or [])]

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return list(self.jobs)

    def create_job(self, **kwargs) -> dict:
        job = {"id": f"id-{kwargs['name']}", **kwargs}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True


def _customer(entries: list[dict]) -> dict:
    skills = [
        {"name": e["skill"], "enabled": True, "initiation": {"scheduled": True}} for e in entries
    ]
    return {"personas": [{"slug": "crane", "skills": skills, "cron": entries}]}


def _entry(skill: str, schedule: str = "0 7 * * *") -> dict:
    return {"skill": skill, "schedule": schedule, "wake_policy": "always"}


def _run(customer: dict, store: _Store) -> list[RoutineChange]:
    seen: list[RoutineChange] = []
    materialize_cron(
        customer,
        lambda _slug: store,
        reconcile_slugs=["crane"],
        on_routine_change=seen.append,
    )
    return seen


def test_newly_authored_routine_reports_enabled() -> None:
    seen = _run(_customer([_entry("inbox-triage")]), _Store())
    assert [(c.skill, c.enabled, c.schedule) for c in seen] == [("inbox-triage", True, "0 7 * * *")]


def test_dropped_routine_reports_disabled() -> None:
    """The ashton-price shape: the authored list empties (#2332) and the
    reconciler removes the job. That is the row the firm needs to see."""
    seen = _run(_customer([]), _Store(["op-managed:crane:medical-records-chaser"]))
    assert [(c.skill, c.enabled) for c in seen] == [("medical-records-chaser", False)]


def test_an_unchanged_routine_reports_nothing_across_a_reboot() -> None:
    """THE property that makes the row worth reading. materialize_cron removes
    and recreates every managed job on every boot; a row per created job would
    claim a change nobody made, every restart, burying the one that mattered."""
    customer = _customer([_entry("inbox-triage")])
    store = _Store()
    assert len(_run(customer, store)) == 1  # first boot: genuinely enabled
    assert _run(customer, store) == []  # second boot: nothing changed
    assert _run(customer, store) == []  # third boot: still nothing


def test_containment_reports_every_scheduled_routine_as_disabled() -> None:
    """ss#2276 containment converges the seat to zero managed jobs. That IS a
    disable, and hiding it would recreate the exact confusion #2498 closes."""
    store = _Store(["op-managed:crane:inbox-triage"])
    seen: list[RoutineChange] = []
    materialize_cron(
        _customer([_entry("inbox-triage")]),
        lambda _slug: store,
        reconcile_slugs=["crane"],
        containment=True,
        on_routine_change=seen.append,
    )
    assert [(c.skill, c.enabled) for c in seen] == [("inbox-triage", False)]


def test_a_raising_callback_does_not_stop_materialization() -> None:
    """Recording that a routine was turned on must never be what stops it from
    being turned on."""
    store = _Store()

    def boom(_change: RoutineChange) -> None:
        raise RuntimeError("ledger on fire")

    registered = materialize_cron(
        _customer([_entry("inbox-triage")]),
        lambda _slug: store,
        reconcile_slugs=["crane"],
        on_routine_change=boom,
    )
    assert registered == ["op-managed:crane:inbox-triage"]
    assert [j["name"] for j in store.jobs] == ["op-managed:crane:inbox-triage"]


def test_no_callback_is_the_pre_2498_behavior() -> None:
    store = _Store()
    assert materialize_cron(
        _customer([_entry("inbox-triage")]), lambda _slug: store, reconcile_slugs=["crane"]
    ) == ["op-managed:crane:inbox-triage"]


# ---------------------------------------------------------------------------
# 2. The spool
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_spool_round_trips_in_order(home) -> None:
    append_routine_change(
        persona_slug="crane", skill="medical-records-chaser", enabled=True, schedule="9 8 * * 2"
    )
    append_routine_change(
        persona_slug="crane", skill="lien-ledger-tracker", enabled=False, schedule=None
    )
    events = drain_routine_changes()
    assert [(e["skill"], e["enabled"], e["schedule"]) for e in events] == [
        ("medical-records-chaser", True, "9 8 * * 2"),
        ("lien-ledger-tracker", False, None),
    ]


def test_draining_twice_yields_nothing_the_second_time(home) -> None:
    """A row emitted twice is a lie about how many times the firm changed its
    mind. The spool is renamed aside before it is parsed, so a second
    registration in the same boot finds nothing."""
    append_routine_change(
        persona_slug="crane", skill="inbox-triage", enabled=True, schedule="@daily"
    )
    assert len(drain_routine_changes()) == 1
    assert drain_routine_changes() == []
    assert not spool_path().exists()


def test_an_empty_spool_is_the_ordinary_case(home) -> None:
    assert drain_routine_changes() == []


def test_junk_lines_are_discarded_not_replayed(home) -> None:
    spool_path().parent.mkdir(parents=True, exist_ok=True)
    spool_path().write_text(
        'not json\n{"schema":"wrong/9"}\n'
        '{"schema":"smd.routine_change/1","skill":"inbox-triage","enabled":true,'
        '"persona_slug":"crane","schedule":"@daily"}\n',
        encoding="utf-8",
    )
    events = drain_routine_changes()
    assert [e["skill"] for e in events] == ["inbox-triage"]
    assert not spool_path().exists()


def test_the_bootstrap_spooler_pins_the_volume_home_not_the_persona_home(tmp_path) -> None:
    """``_real_cron_store_for`` sets the process-global HERMES_HOME to each
    PERSONA PROFILE home while it reconciles. A spooler that read the env would
    drop events under ``<volume>/profiles/<slug>/.smd`` while the plugin drains
    ``<volume>/.smd`` — written nowhere, and nothing would say so."""
    from bootstrap.translate import _routine_change_spooler

    volume = tmp_path / "volume"
    volume.mkdir()
    spooler = _routine_change_spooler(volume)
    # Simulate the per-persona mutation the real store factory performs.
    import os

    os.environ["HERMES_HOME"] = str(volume / "profiles" / "crane")
    try:
        spooler(RoutineChange("crane", "inbox-triage", True, "@daily"))
    finally:
        os.environ.pop("HERMES_HOME", None)
    assert (volume / ".smd" / "routine_changes.jsonl").is_file()
    assert not (volume / "profiles" / "crane" / ".smd").exists()


# ---------------------------------------------------------------------------
# 3. The row
# ---------------------------------------------------------------------------


def _audit_plugin():
    name = "hermes_smd_audit_for_routine_test"
    if name in sys.modules:
        return sys.modules[name]
    init = ROOT / "plugins" / "hermes-smd-audit" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[str(init.parent)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _RecordingWriter:
    def __init__(self, fail: bool = False) -> None:
        self.events: list = []
        self._fail = fail

    def write(self, event):
        self.events.append(event)
        if self._fail:
            raise RuntimeError("ledger unreachable")
        return "01ULID"


def test_drained_changes_become_rows_with_an_actor(home) -> None:
    plugin = _audit_plugin()
    append_routine_change(
        persona_slug="crane", skill="medical-records-chaser", enabled=True, schedule="9 8 * * 2"
    )
    append_routine_change(
        persona_slug="crane", skill="lien-ledger-tracker", enabled=False, schedule=None
    )
    writer = _RecordingWriter()
    plugin._drain_routine_changes_to_ledger(writer)

    assert [e.action_type for e in writer.events] == ["ROUTINE_ENABLED", "ROUTINE_DISABLED"]
    assert [e.skill_name for e in writer.events] == [
        "medical-records-chaser",
        "lien-ledger-tracker",
    ]
    for event in writer.events:
        # An actor is required: "a routine was turned off" with nobody attached
        # is the half-answer the issue calls out.
        assert event.actor
        assert event.actor_role is not None
    assert writer.events[0].metadata["schedule"] == "9 8 * * 2"


def test_a_failed_row_does_not_stop_the_next_one_or_registration(home) -> None:
    plugin = _audit_plugin()
    append_routine_change(persona_slug="crane", skill="a", enabled=True, schedule="@daily")
    append_routine_change(persona_slug="crane", skill="b", enabled=False, schedule=None)
    writer = _RecordingWriter(fail=True)
    plugin._drain_routine_changes_to_ledger(writer)  # must not raise
    assert [e.skill_name for e in writer.events] == ["a", "b"]


def test_no_writer_means_no_attempt(home) -> None:
    plugin = _audit_plugin()
    append_routine_change(persona_slug="crane", skill="a", enabled=True, schedule="@daily")
    plugin._drain_routine_changes_to_ledger(None)  # must not raise
