"""Tests for plugins/hermes-smd-jobs (B1 agent-facing durable-job tools).

The broker/ledger logic is covered on the console side and the client in
test_job_ledger_client.py; here we prove the four tool handlers marshal intent
correctly and that registration is well-formed, with the broker client faked.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_plugin


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create(self, row):
        self.calls.append(("create", row))
        return "JOB1"

    def read(self, job_id):
        self.calls.append(("read", job_id))
        return {
            "id": job_id,
            "status": "running",
            "spent_cents": 10,
            "budget_cents": 500,
            "result_ref": None,
            "error": None,
            "attempts": 1,
        }

    def cancel(self, job_id):
        self.calls.append(("cancel", job_id))
        return True

    def idem_begin(self, job_id, step_key, lease_epoch):
        self.calls.append(("idem_begin", job_id, step_key, lease_epoch))
        return "proceed"


@pytest.fixture
def jobs(monkeypatch):
    plugin = load_plugin("hermes-smd-jobs")
    fake = _FakeClient()
    monkeypatch.setattr(plugin, "BrokerJobClient", lambda *a, **k: fake)
    return plugin, fake


def test_start_background_job_records_intent(jobs, monkeypatch):
    plugin, fake = jobs
    monkeypatch.setenv("CUSTOMER_SLUG", "demo-law")
    monkeypatch.setenv("HERMES_MODEL", "claude-sonnet-4-6")
    out = json.loads(
        plugin._start_background_job({"brief": "review docs", "deliver_to": "telegram:1"})
    )
    assert out["job_id"] == "JOB1"
    assert out["status"] == "queued"
    action, row = fake.calls[-1]
    assert action == "create"
    assert row["customer_slug"] == "demo-law"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["brief"] == "review docs"
    assert row["deliver_to"] == "telegram:1"
    assert row["brief_digest"].startswith("sha256:")
    assert row["budget_cents"] >= 1


def test_start_requires_brief(jobs):
    plugin, _ = jobs
    with pytest.raises(ValueError):
        plugin._start_background_job({"brief": "   "})


def test_job_status_projects_fields(jobs):
    plugin, _ = jobs
    out = json.loads(plugin._job_status({"job_id": "JOB1"}))
    assert out["status"] == "running"
    assert out["budget_cents"] == 500
    assert out["id"] == "JOB1"


def test_job_cancel(jobs):
    plugin, _ = jobs
    out = json.loads(plugin._job_cancel({"job_id": "JOB1"}))
    assert out["cancel_requested"] is True


def test_sideeffect_noop_outside_job(jobs, monkeypatch):
    plugin, _ = jobs
    monkeypatch.delenv("HERMES_JOB_ID", raising=False)
    out = json.loads(plugin._job_record_sideeffect({"step_key": "send:x"}))
    assert out == {"decision": "proceed", "journaled": False}


def test_sideeffect_journaled_inside_job(jobs, monkeypatch):
    plugin, fake = jobs
    monkeypatch.setenv("HERMES_JOB_ID", "JOB1")
    monkeypatch.setenv("HERMES_JOB_LEASE_EPOCH", "2")
    out = json.loads(plugin._job_record_sideeffect({"step_key": "send:x"}))
    assert out == {"decision": "proceed", "journaled": True}
    assert ("idem_begin", "JOB1", "send:x", 2) in fake.calls


def test_register_registers_four_jobs_tools(jobs):
    plugin, _ = jobs
    registered: list[dict] = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    plugin.register(Ctx())
    names = {r["name"] for r in registered}
    assert names == {"start_background_job", "job_status", "job_cancel", "job_record_sideeffect"}
    assert all(r["toolset"] == "jobs" for r in registered)
    assert all(r["requires_env"] == ["SMD_WORKSPACE_BROKER_SOCKET"] for r in registered)
