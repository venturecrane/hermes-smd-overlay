"""Unit tests for ADR 0047 cron materialization.

The store factory is faked, so these run without Hermes installed (CI has no
cron.jobs). materialize_cron registers each persona's cron into a store scoped
to that persona's profile home — store_for(slug) returns the persona's store.
"""

from __future__ import annotations

import itertools

import pytest

from bootstrap.cron_materialize import (
    CronMaterializeError,
    managed_name,
    materialize_cron,
)


class FakeCronStore:
    """In-memory stand-in for Hermes' cron.jobs store (one persona's home)."""

    def __init__(self, jobs: list[dict] | None = None) -> None:
        self.jobs: list[dict] = list(jobs or [])
        self._ids = (f"job-{n}" for n in itertools.count(1))
        self.creates: list[dict] = []
        self.removed: list[str] = []

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return list(self.jobs)

    def create_job(self, **kwargs) -> dict:
        job = {"id": next(self._ids), **kwargs}
        self.jobs.append(job)
        self.creates.append(kwargs)
        return job

    def remove_job(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        self.removed.append(job_id)
        return len(self.jobs) < before


class FakeFactory:
    """store_for(slug) — lazily creates one FakeCronStore per persona slug, and
    records which slugs were asked for (proving per-profile-home scoping)."""

    def __init__(self, preset: dict[str, FakeCronStore] | None = None) -> None:
        self.stores: dict[str, FakeCronStore] = dict(preset or {})
        self.asked: list[str] = []

    def __call__(self, slug: str) -> FakeCronStore:
        self.asked.append(slug)
        return self.stores.setdefault(slug, FakeCronStore())


def _customer(cron_entries: list[dict], slug: str = "crane") -> dict:
    return {"personas": [{"slug": slug, "cron": cron_entries}]}


def test_registers_one_job_per_authored_entry() -> None:
    f = FakeFactory()
    registered = materialize_cron(
        _customer([{"skill": "inbox-triage", "schedule": "0 7-19 * * *", "wake_policy": "always"}]),
        f,
    )
    assert registered == [managed_name("crane", "inbox-triage")]
    create = f.stores["crane"].creates[0]
    assert create["schedule"] == "0 7-19 * * *"
    assert create["skills"] == ["inbox-triage"]
    assert create["name"] == "op-managed:crane:inbox-triage"
    assert create["no_agent"] is False


def test_registers_into_each_personas_own_store() -> None:
    """The profile-home fix: per persona, store_for(slug) is asked, and each
    persona's job lands in THAT persona's store — never a shared/data home."""
    customer = {
        "personas": [
            {"slug": "crane", "cron": [{"skill": "inbox-triage", "schedule": "0 7 * * *"}]},
            {"slug": "scribe", "cron": [{"skill": "digest", "schedule": "0 18 * * *"}]},
        ]
    }
    f = FakeFactory()
    materialize_cron(customer, f)
    assert set(f.asked) == {"crane", "scribe"}
    assert [c["name"] for c in f.stores["crane"].creates] == ["op-managed:crane:inbox-triage"]
    assert [c["name"] for c in f.stores["scribe"].creates] == ["op-managed:scribe:digest"]


def test_idempotent_reprovision_keeps_one_job() -> None:
    """Materialize twice (same factory/store) → still exactly one job."""
    entries = [{"skill": "inbox-triage", "schedule": "0 7-19 * * *", "wake_policy": "always"}]
    f = FakeFactory()
    materialize_cron(_customer(entries), f)
    materialize_cron(_customer(entries), f)
    managed = [j for j in f.stores["crane"].jobs if str(j["name"]).startswith("op-managed:")]
    assert len(managed) == 1, "managed cron job duplicated across reprovisions"


def test_changed_schedule_replaces_not_duplicates() -> None:
    f = FakeFactory()
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), f)
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 9 * * *"}]), f)
    managed = [j for j in f.stores["crane"].jobs if str(j["name"]).startswith("op-managed:")]
    assert len(managed) == 1
    assert managed[0]["schedule"] == "0 9 * * *"


def test_dropping_one_of_several_entries_is_cleaned() -> None:
    """Dropping ONE of a persona's cron entries (persona still authors cron)
    removes the dropped job and keeps the rest. (Dropping ALL of a persona's cron
    is also reconciled now — see test_dropping_all_cron_removes_orphaned_job.)"""
    f = FakeFactory()
    materialize_cron(
        {
            "personas": [
                {
                    "slug": "crane",
                    "cron": [
                        {"skill": "inbox-triage", "schedule": "0 7 * * *"},
                        {"skill": "digest", "schedule": "0 18 * * *"},
                    ],
                }
            ]
        },
        f,
    )
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), f)
    managed = [
        j["name"] for j in f.stores["crane"].jobs if str(j["name"]).startswith("op-managed:")
    ]
    assert managed == ["op-managed:crane:inbox-triage"]


def test_leaves_unmanaged_jobs_untouched() -> None:
    """A user/agent-created job (no managed prefix) is never removed."""
    store = FakeCronStore(jobs=[{"id": "user-1", "name": "my own job", "schedule": "* * * * *"}])
    f = FakeFactory(preset={"crane": store})
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), f)
    assert any(j["id"] == "user-1" for j in store.jobs)
    assert "user-1" not in store.removed


def test_default_wake_policy_is_always() -> None:
    f = FakeFactory()
    materialize_cron(
        _customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), f
    )  # no wake_policy
    assert f.stores["crane"].creates[0]["no_agent"] is False


def test_unsupported_wake_policy_fails_closed() -> None:
    f = FakeFactory()
    with pytest.raises(CronMaterializeError, match="wake_policy"):
        materialize_cron(
            _customer([{"skill": "watch", "schedule": "* * * * *", "wake_policy": "manual_poll"}]),
            f,
        )
    assert f.asked == [], "must not touch any store when an entry is unmaterializable"


# --- pre_run_decides (ADR 0047 phase 2) -------------------------------------


class _RecordingStager:
    """Fake script stager: records calls, returns a ``<skill>/<base>`` ref."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    def __call__(self, persona_slug: str, skill: str, pre_run: str) -> str:
        self.calls.append((persona_slug, skill, pre_run))
        if self.fail:
            raise FileNotFoundError(f"no such script: {pre_run}")
        return f"{skill}/{pre_run}"


def test_pre_run_decides_registers_job_with_staged_script() -> None:
    f = FakeFactory()
    stager = _RecordingStager()
    registered = materialize_cron(
        _customer(
            [
                {
                    "skill": "deadline-miss-escalator",
                    "schedule": "0 8 * * *",
                    "pre_run": "pre_run.py",
                    "wake_policy": "pre_run_decides",
                }
            ]
        ),
        f,
        stager,
    )
    assert registered == [managed_name("crane", "deadline-miss-escalator")]
    assert stager.calls == [("crane", "deadline-miss-escalator", "pre_run.py")]
    create = f.stores["crane"].creates[0]
    # The pre-run script gates the wake; the agent still runs the skill when woken.
    assert create["script"] == "deadline-miss-escalator/pre_run.py"
    assert create["no_agent"] is False
    assert create["skills"] == ["deadline-miss-escalator"]
    assert create["schedule"] == "0 8 * * *"


def test_always_entry_registers_no_script() -> None:
    f = FakeFactory()
    materialize_cron(
        _customer([{"skill": "inbox-triage", "schedule": "0 7 * * *", "wake_policy": "always"}]),
        f,
        _RecordingStager(),
    )
    assert "script" not in f.stores["crane"].creates[0]


def test_pre_run_decides_without_pre_run_fails_closed() -> None:
    f = FakeFactory()
    with pytest.raises(CronMaterializeError, match="requires a 'pre_run' script"):
        materialize_cron(
            _customer(
                [{"skill": "watch", "schedule": "0 8 * * *", "wake_policy": "pre_run_decides"}]
            ),
            f,
            _RecordingStager(),
        )
    assert f.asked == [], "must not touch any store when a pre_run script is missing"


def test_pre_run_decides_without_stager_fails_closed() -> None:
    f = FakeFactory()
    with pytest.raises(CronMaterializeError, match="script stager"):
        materialize_cron(
            _customer(
                [
                    {
                        "skill": "watch",
                        "schedule": "0 8 * * *",
                        "pre_run": "pre_run.py",
                        "wake_policy": "pre_run_decides",
                    }
                ]
            ),
            f,  # no stager passed
        )
    assert f.asked == [], "must not touch any store when no stager is available"


def test_stage_failure_aborts_before_store_mutation() -> None:
    """A pre-run script that cannot be staged fails closed with the store
    untouched — no managed job removed, none created."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:old", "schedule": "0 1 * * *"}]
    )
    f = FakeFactory(preset={"crane": store})
    with pytest.raises(CronMaterializeError, match="could not stage pre_run"):
        materialize_cron(
            _customer(
                [
                    {
                        "skill": "deadline-miss-escalator",
                        "schedule": "0 8 * * *",
                        "pre_run": "pre_run.py",
                        "wake_policy": "pre_run_decides",
                    }
                ]
            ),
            f,
            _RecordingStager(fail=True),
        )
    assert store.removed == []
    assert store.creates == []


def test_missing_skill_or_schedule_fails_closed() -> None:
    f = FakeFactory()
    with pytest.raises(CronMaterializeError, match="missing skill/schedule"):
        materialize_cron(_customer([{"skill": "inbox-triage"}]), f)  # no schedule
    assert f.asked == []


def test_bad_entry_raises_before_mutating_store() -> None:
    """Fail-closed must abort BEFORE any store is touched, so a partial bad batch
    never half-registers."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:old", "schedule": "0 1 * * *"}]
    )
    f = FakeFactory(preset={"crane": store})
    with pytest.raises(CronMaterializeError):
        materialize_cron(
            _customer([{"skill": "watch", "schedule": "* * * * *", "wake_policy": "bogus"}]),
            f,
        )
    assert store.removed == []


# --- drop-ALL reconciliation: the orphan-firing bug -------------------------


def test_dropping_all_cron_removes_orphaned_job() -> None:
    """Dropping ALL of a persona's cron: with the persona in ``reconcile_slugs``,
    its previously-registered managed job is removed. This is the live defect —
    a cron commented out of customer.yaml whose job kept firing across reboots."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:health-monitor", "schedule": "*/30 * * * *"}]
    )
    f = FakeFactory(preset={"crane": store})
    registered = materialize_cron(_customer([]), f, reconcile_slugs=["crane"])
    assert registered == []
    assert [j for j in store.jobs if str(j["name"]).startswith("op-managed:")] == ([]), (
        "orphaned managed job must be removed when all cron is dropped"
    )
    assert "m-1" in store.removed


def test_dropping_all_cron_is_idempotent() -> None:
    """Reconciling a now-cron-less persona twice stays empty (no error, no churn)."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:health-monitor", "schedule": "*/30 * * * *"}]
    )
    f = FakeFactory(preset={"crane": store})
    materialize_cron(_customer([]), f, reconcile_slugs=["crane"])
    materialize_cron(_customer([]), f, reconcile_slugs=["crane"])
    assert [j for j in store.jobs if str(j["name"]).startswith("op-managed:")] == []


def test_mixed_persona_keeps_one_drops_another() -> None:
    """One persona keeps its cron; another dropped all. Both reconciled: the
    keeper gets its authored job, the dropper's orphan is removed."""
    keeper = FakeCronStore()
    dropper = FakeCronStore(
        jobs=[
            {
                "id": "m-9",
                "name": "op-managed:avery:deadline-miss-escalator",
                "schedule": "0 8 * * *",
            }
        ]
    )
    f = FakeFactory(preset={"crane": keeper, "avery": dropper})
    customer = {
        "personas": [
            {"slug": "crane", "cron": [{"skill": "inbox-triage", "schedule": "0 7 * * *"}]},
            {"slug": "avery", "cron": []},
        ]
    }
    registered = materialize_cron(customer, f, reconcile_slugs=["crane", "avery"])
    assert registered == [managed_name("crane", "inbox-triage")]
    assert [c["name"] for c in keeper.creates] == ["op-managed:crane:inbox-triage"]
    assert [j for j in dropper.jobs if str(j["name"]).startswith("op-managed:")] == []
    assert "m-9" in dropper.removed


def test_reconcile_slugs_none_preserves_legacy_no_removal() -> None:
    """Back-compat: with ``reconcile_slugs=None`` (the default) a persona that
    authors no cron is NOT visited — the legacy behavior existing callers rely on.
    translate.py always passes the full persona-slug set, so live boots reconcile;
    this guards the default for any other caller/test."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:health-monitor", "schedule": "*/30 * * * *"}]
    )
    f = FakeFactory(preset={"crane": store})
    materialize_cron(_customer([]), f)  # no reconcile_slugs
    assert f.asked == [], "no authored cron + no reconcile_slugs ⇒ no store touched"
    assert store.removed == []
    assert any(j["id"] == "m-1" for j in store.jobs)


def test_store_acquire_failure_aborts_before_any_mutation() -> None:
    """Fail-closed across the WHOLE reconcile set: if acquiring/listing one
    reconciled persona's store fails, NO store is mutated (no half-applied boot)."""
    good = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:inbox-triage", "schedule": "0 7 * * *"}]
    )

    class _FailingFactory:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def __call__(self, slug: str):
            self.asked.append(slug)
            if slug == "bad":
                raise RuntimeError("cannot open cron store")
            return good

    f = _FailingFactory()
    with pytest.raises(CronMaterializeError, match="could not acquire/list cron store"):
        materialize_cron(
            _customer([{"skill": "inbox-triage", "schedule": "0 9 * * *"}]),
            f,
            reconcile_slugs=["crane", "bad"],
        )
    assert good.removed == [], "no removal may happen if any store acquisition fails"
    assert good.creates == [], "no creation may happen if any store acquisition fails"
