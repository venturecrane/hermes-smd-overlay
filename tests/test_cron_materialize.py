"""Unit tests for ADR 0047 cron materialization.

The store is faked, so these run without Hermes installed (CI has no cron.jobs).
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
    """In-memory stand-in for Hermes' cron.jobs store."""

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


def _customer(cron_entries: list[dict], slug: str = "crane") -> dict:
    return {"personas": [{"slug": slug, "cron": cron_entries}]}


def test_registers_one_job_per_authored_entry() -> None:
    store = FakeCronStore()
    registered = materialize_cron(
        _customer([{"skill": "inbox-triage", "schedule": "0 7-19 * * *", "wake_policy": "always"}]),
        store,
    )
    assert registered == [managed_name("crane", "inbox-triage")]
    assert len(store.creates) == 1
    create = store.creates[0]
    assert create["schedule"] == "0 7-19 * * *"
    assert create["skills"] == ["inbox-triage"]
    assert create["name"] == "op-managed:crane:inbox-triage"
    assert create["no_agent"] is False


def test_idempotent_reprovision_keeps_one_job() -> None:
    """The critique's property: materialize twice → still exactly one job."""
    entries = [{"skill": "inbox-triage", "schedule": "0 7-19 * * *", "wake_policy": "always"}]
    store = FakeCronStore()
    materialize_cron(_customer(entries), store)
    materialize_cron(_customer(entries), store)
    managed = [j for j in store.jobs if str(j["name"]).startswith("op-managed:")]
    assert len(managed) == 1, "managed cron job duplicated across reprovisions"


def test_changed_schedule_replaces_not_duplicates() -> None:
    store = FakeCronStore()
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), store)
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 9 * * *"}]), store)
    managed = [j for j in store.jobs if str(j["name"]).startswith("op-managed:")]
    assert len(managed) == 1
    assert managed[0]["schedule"] == "0 9 * * *"


def test_removed_entry_is_deleted() -> None:
    store = FakeCronStore()
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), store)
    materialize_cron(_customer([]), store)  # cron removed from customer.yaml
    managed = [j for j in store.jobs if str(j["name"]).startswith("op-managed:")]
    assert managed == []


def test_leaves_unmanaged_jobs_untouched() -> None:
    """A user/agent-created job (no managed prefix) is never removed."""
    store = FakeCronStore(jobs=[{"id": "user-1", "name": "my own job", "schedule": "* * * * *"}])
    materialize_cron(_customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), store)
    assert any(j["id"] == "user-1" for j in store.jobs)
    assert "user-1" not in store.removed


def test_default_wake_policy_is_always() -> None:
    store = FakeCronStore()
    materialize_cron(
        _customer([{"skill": "inbox-triage", "schedule": "0 7 * * *"}]), store
    )  # no wake_policy
    assert store.creates[0]["no_agent"] is False


def test_unsupported_wake_policy_fails_closed() -> None:
    store = FakeCronStore()
    with pytest.raises(CronMaterializeError, match="wake_policy"):
        materialize_cron(
            _customer(
                [{"skill": "watch", "schedule": "* * * * *", "wake_policy": "pre_run_decides"}]
            ),
            store,
        )
    assert store.creates == [], "must not register anything when an entry is unmaterializable"


def test_missing_skill_or_schedule_fails_closed() -> None:
    store = FakeCronStore()
    with pytest.raises(CronMaterializeError, match="missing skill/schedule"):
        materialize_cron(_customer([{"skill": "inbox-triage"}]), store)  # no schedule
    assert store.creates == []


def test_bad_entry_raises_before_mutating_store() -> None:
    """Fail-closed must abort BEFORE any create/remove, so a partial bad batch
    never half-registers."""
    store = FakeCronStore(
        jobs=[{"id": "m-1", "name": "op-managed:crane:old", "schedule": "0 1 * * *"}]
    )
    with pytest.raises(CronMaterializeError):
        materialize_cron(
            _customer([{"skill": "watch", "schedule": "* * * * *", "wake_policy": "bogus"}]),
            store,
        )
    # the pre-existing managed job must NOT have been removed
    assert store.removed == []
