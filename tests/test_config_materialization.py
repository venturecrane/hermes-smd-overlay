"""Materialization guard — authored customer.yaml blocks must reach a runtime target.

Primary purpose: regression guard for the cron drop. ``personas[].cron`` was a
validated-but-never-materialized block — the validator accepted it, but nothing
wired it into the runtime, so the skill never ran on a schedule. These tests
fail if that silent drop returns, and they pin the idempotency + ownership
safety of :mod:`bootstrap.cron_sync`.

Secondary purpose: :data:`REGISTRY` is the living classification map — every
authored block is MATERIALIZED (reaches a concrete runtime target), ELSEWHERE
(consumed outside translate, e.g. portal RBAC / env vars), or WAIVED (authored,
no consumer yet). Adding a block to the schema without classifying it here is
the drift this map exists to surface.

Scope note: this file runs without the real Hermes runtime by stubbing
``cron.jobs`` (the create/load/save boundary is Hermes' contract, proven by
reading its source; what we test here is OUR sync logic). True schema-level
anti-drift — failing CI when a NEW ss-console schema block appears unclassified —
belongs with the schema, in ss-console, and is a follow-up. Registration is NOT
function: that a job lands in jobs.json does not prove the skill triages — only
the runtime deploy proves that (see operator/IMPLEMENTATION.md verification).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

from bootstrap.cron_sync import CronSyncError, sync_cron_jobs

# ---------------------------------------------------------------------------
# Classification map (the living anti-drift documentation)
# ---------------------------------------------------------------------------

MATERIALIZED = "MATERIALIZED"
ELSEWHERE = "ELSEWHERE"
WAIVED = "WAIVED"

#: block path -> (category, concrete target / consumer / waiver reason)
REGISTRY: dict[str, tuple[str, str]] = {
    # --- MATERIALIZED: translate.py writes a concrete runtime artifact ---
    "personas[].skills[].trust_ceiling": (MATERIALIZED, "profile config.yaml: skills[].trust_ceiling"),
    "personas[].tone": (MATERIALIZED, "profile config.yaml: persona.tone + SOUL.md"),
    "personas[].bundles": (MATERIALIZED, "profile skill-bundles/<slug>.yaml"),
    "connectors{mcp:*}": (MATERIALIZED, "profile config.yaml: mcp_servers"),
    "webhook_triggers": (MATERIALIZED, "profile config.yaml: platforms.webhook"),
    "telegram": (MATERIALIZED, "profile config.yaml: telegram"),
    "scope": (MATERIALIZED, "profile config.yaml: scope"),
    "escalation": (MATERIALIZED, "profile config.yaml: escalation"),
    "voice_library": (MATERIALIZED, "profile config.yaml: voice_library"),
    "memory": (MATERIALIZED, "profile config.yaml: memory"),
    # --- MATERIALIZED elsewhere than translate: the cron store (THIS fix) ---
    "personas[].cron": (MATERIALIZED, "Hermes cron jobs.json via bootstrap.cron_sync"),
    # --- ELSEWHERE: consumed outside the translate/cron-sync bootstrap path ---
    "google_auth": (ELSEWHERE, "Fly secret + broker env (entrypoint.sh), not config.yaml"),
    "authority": (ELSEWHERE, "portal RBAC (ADR 0041)"),
    "credential_custody_default": (ELSEWHERE, "portal RBAC (ADR 0042)"),
    "voice_cohorts": (ELSEWHERE, "voice runtime"),
    # --- WAIVED: authored, no runtime consumer yet (each its own follow-up) ---
    "personas[].skills[].action_ceilings": (WAIVED, "per-action-class ceiling not yet enforced"),
    "personas[].voice_overrides": (WAIVED, "no consumer yet"),
    "personas[].escalation_overrides": (WAIVED, "no consumer yet"),
    "personas[].channel_bindings": (WAIVED, "no consumer yet"),
    "personas[].skills[].cost_estimate": (WAIVED, "informational only"),
    "personas[].skills[].scope": (WAIVED, "not yet materialized"),
    "addons": (WAIVED, "ADR 0022 Stream 3 not yet materialized"),
    "practice_areas": (WAIVED, "informational only"),
}


def test_registry_entries_are_well_formed():
    for block, (category, target) in REGISTRY.items():
        assert category in (MATERIALIZED, ELSEWHERE, WAIVED), f"{block}: bad category {category!r}"
        assert target, f"{block}: missing target/reason"


def test_cron_block_is_classified_materialized():
    # The regression's anchor: cron is an authored block and MUST be materialized.
    category, target = REGISTRY["personas[].cron"]
    assert category == MATERIALIZED
    assert "jobs.json" in target


# ---------------------------------------------------------------------------
# Hermes cron stub — mimics cron.jobs' load/save/create against a real file
# ---------------------------------------------------------------------------


@pytest.fixture
def cron_stub(monkeypatch):
    """Inject a fake ``cron.jobs`` whose CRUD reads/writes the module's JOBS_FILE.

    :func:`bootstrap.cron_sync._load_hermes_cron` retargets ``JOBS_FILE`` to
    ``<hermes_home>/cron/jobs.json``, so the stub persists exactly where the
    test then reads. The stub mirrors the subset of the real job record that the
    sync logic inspects (name, skills, deliver, schedule.expr).
    """
    mod = types.ModuleType("cron.jobs")
    mod.CRON_DIR = None
    mod.JOBS_FILE = None
    mod.OUTPUT_DIR = None
    counter = {"n": 0}

    def load_jobs():
        path = mod.JOBS_FILE
        if path is None or not Path(path).exists():
            return []
        return json.loads(Path(path).read_text()).get("jobs", [])

    def save_jobs(jobs):
        path = Path(mod.JOBS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"jobs": jobs}, indent=2))

    def create_job(prompt, schedule, name=None, skills=None, deliver=None, no_agent=False, **_kw):
        counter["n"] += 1
        job = {
            "id": f"stub{counter['n']:08d}",
            "name": name,
            "prompt": prompt,
            "skills": list(skills or []),
            "skill": (list(skills or []) or [None])[0],
            "schedule": {"kind": "cron", "expr": schedule, "display": schedule},
            "schedule_display": schedule,
            "deliver": deliver,
            "no_agent": no_agent,
            "next_run_at": "2026-01-01T00:00:00+00:00",
        }
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
        return job

    mod.load_jobs = load_jobs
    mod.save_jobs = save_jobs
    mod.create_job = create_job

    parent = types.ModuleType("cron")
    parent.__path__ = []  # mark as package so import_module("cron.jobs") resolves
    monkeypatch.setitem(sys.modules, "cron", parent)
    monkeypatch.setitem(sys.modules, "cron.jobs", mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_customer(tmp_path: Path, customer: dict) -> tuple[str, str]:
    """Write a customer.yaml and return (customer_yaml_path, hermes_home)."""
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(yaml.safe_dump(customer, sort_keys=False))
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    return str(yaml_path), str(hermes_home)


def _customer(
    *,
    cron: list[dict] | None = None,
    telegram: dict | None = None,
    customer_id: str = "smd",
    persona_slug: str = "crane",
) -> dict:
    persona: dict = {"slug": persona_slug, "name": "Crane"}
    if cron is not None:
        persona["cron"] = cron
    doc: dict = {"customer_id": customer_id, "personas": [persona]}
    if telegram is not None:
        doc["telegram"] = telegram
    return doc


def _jobs(hermes_home: str) -> list[dict]:
    path = Path(hermes_home) / "cron" / "jobs.json"
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("jobs", [])


# ---------------------------------------------------------------------------
# Cron-sync behaviour
# ---------------------------------------------------------------------------


def test_cron_sync_registers_job(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path,
        _customer(
            cron=[{"skill": "inbox-triage", "schedule": "0 7-19 * * *", "wake_policy": "always"}],
            telegram={"enabled": True, "allow_from": ["7367659986"]},
        ),
    )
    created = sync_cron_jobs(yaml_path, home)

    jobs = _jobs(home)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == "smd-mat-smd-crane-inbox-triage"
    assert job["skills"] == ["inbox-triage"]
    assert job["schedule"]["expr"] == "0 7-19 * * *"
    assert job["deliver"] == "telegram:7367659986"
    assert created == ["smd-mat-smd-crane-inbox-triage"]


def test_cron_sync_is_idempotent(tmp_path, cron_stub):
    cfg = _customer(
        cron=[{"skill": "inbox-triage", "schedule": "0 7-19 * * *"}],
        telegram={"enabled": True, "allow_from": ["7367659986"]},
    )
    yaml_path, home = _write_customer(tmp_path, cfg)

    sync_cron_jobs(yaml_path, home)
    second = sync_cron_jobs(yaml_path, home)  # re-run on every boot must converge

    assert _jobs(home), "job should still exist"
    assert len(_jobs(home)) == 1, "second sync must not duplicate"
    assert second == [], "nothing created/replaced on an unchanged re-run"


def test_cron_sync_preserves_foreign_jobs(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path, _customer(cron=[{"skill": "inbox-triage", "schedule": "0 7-19 * * *"}])
    )
    # An agent-authored / user job (no smd-mat- prefix) must survive sync.
    cron_dir = Path(home) / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc", "name": "agent-made-this", "skills": ["other"]}]})
    )

    sync_cron_jobs(yaml_path, home)

    names = {j["name"] for j in _jobs(home)}
    assert "agent-made-this" in names
    assert "smd-mat-smd-crane-inbox-triage" in names


def test_cron_sync_removes_unauthored_owned_job(tmp_path, cron_stub):
    # An smd-mat- job no longer in customer.yaml is removed (converge, not accrete).
    yaml_path, home = _write_customer(tmp_path, _customer(cron=[]))
    cron_dir = Path(home) / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "old", "name": "smd-mat-smd-crane-stale-skill", "skills": ["stale-skill"]}]})
    )

    sync_cron_jobs(yaml_path, home)

    assert _jobs(home) == []


def test_cron_sync_replaces_changed_schedule(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path, _customer(cron=[{"skill": "inbox-triage", "schedule": "0 7-19 * * *"}])
    )
    sync_cron_jobs(yaml_path, home)

    # Author a new schedule in place; re-sync should replace (still exactly one job).
    Path(yaml_path).write_text(
        yaml.safe_dump(_customer(cron=[{"skill": "inbox-triage", "schedule": "0 9 * * *"}]), sort_keys=False)
    )
    changed = sync_cron_jobs(yaml_path, home)

    jobs = _jobs(home)
    assert len(jobs) == 1
    assert jobs[0]["schedule"]["expr"] == "0 9 * * *"
    assert changed == ["smd-mat-smd-crane-inbox-triage"]


def test_cron_sync_rejects_pre_run_decides(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path,
        _customer(cron=[{"skill": "x", "schedule": "0 9 * * *", "wake_policy": "pre_run_decides"}]),
    )
    with pytest.raises(CronSyncError, match="wake_policy"):
        sync_cron_jobs(yaml_path, home)


def test_cron_sync_rejects_duplicate_skill(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path,
        _customer(
            cron=[
                {"skill": "inbox-triage", "schedule": "0 9 * * *"},
                {"skill": "inbox-triage", "schedule": "0 17 * * *"},
            ]
        ),
    )
    with pytest.raises(CronSyncError, match="duplicate"):
        sync_cron_jobs(yaml_path, home)


def test_cron_sync_rejects_missing_fields(tmp_path, cron_stub):
    yaml_path, home = _write_customer(tmp_path, _customer(cron=[{"schedule": "0 9 * * *"}]))
    with pytest.raises(CronSyncError, match="skill"):
        sync_cron_jobs(yaml_path, home)


# ---------------------------------------------------------------------------
# Deliver resolution
# ---------------------------------------------------------------------------


def test_deliver_authored_wins(tmp_path, cron_stub):
    yaml_path, home = _write_customer(
        tmp_path,
        _customer(
            cron=[{"skill": "inbox-triage", "schedule": "0 9 * * *", "deliver": "origin"}],
            telegram={"enabled": True, "allow_from": ["7367659986"]},
        ),
    )
    sync_cron_jobs(yaml_path, home)
    assert _jobs(home)[0]["deliver"] == "origin"


def test_deliver_falls_back_to_local_without_single_telegram(tmp_path, cron_stub):
    # No authored deliver, telegram disabled → local (and the module WARNs).
    yaml_path, home = _write_customer(
        tmp_path, _customer(cron=[{"skill": "inbox-triage", "schedule": "0 9 * * *"}])
    )
    sync_cron_jobs(yaml_path, home)
    assert _jobs(home)[0]["deliver"] == "local"


def test_deliver_local_when_multiple_telegram_users(tmp_path, cron_stub):
    # Ambiguous target (more than one allowed user) → don't guess; local.
    yaml_path, home = _write_customer(
        tmp_path,
        _customer(
            cron=[{"skill": "inbox-triage", "schedule": "0 9 * * *"}],
            telegram={"enabled": True, "allow_from": ["111", "222"]},
        ),
    )
    sync_cron_jobs(yaml_path, home)
    assert _jobs(home)[0]["deliver"] == "local"
