"""shared/cron_attribution.py — session id → routine identity (ss-console #2122).

The resolver is an enrichment on the audit path: every test that proves a
resolution also has a sibling proving the corresponding NON-resolution (the
false control), because a resolver that answers for sessions it should not
recognize would fabricate attribution — worse than the NULL it replaces.
"""

from __future__ import annotations

import json

import pytest

from shared.cron_attribution import (
    _CACHE,
    RoutineIdentity,
    parse_cron_session,
    resolve_routine,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


def _write_store(home, persona: str, jobs: list[dict]) -> None:
    d = home / "profiles" / persona / "cron"
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")


JOB = {
    "id": "a726fd5efd24",
    "name": "op-managed:operator:deadline-miss-escalator",
    "skills": ["deadline-miss-escalator"],
}


# ---------------------------------------------------------------- parse


def test_parse_extracts_job_id():
    assert parse_cron_session("cron_a726fd5efd24_20260802_070001") == "a726fd5efd24"


def test_parse_job_id_containing_underscore():
    assert parse_cron_session("cron_ab_cd_20260802_070001") == "ab_cd"


@pytest.mark.parametrize(
    "session_id",
    [
        "",  # empty
        "interactive-0192",  # not cron
        "cron_a726fd5efd24",  # no timestamp
        "cron_a726fd5efd24_2026_0802",  # malformed timestamp
        "CRON_a726fd5efd24_20260802_070001",  # wrong case — scheduler emits lowercase
        None,  # not a string
    ],
)
def test_parse_rejects_non_cron_shapes(session_id):
    assert parse_cron_session(session_id) is None


# ---------------------------------------------------------------- resolve


def test_resolves_managed_job_to_persona_and_skill(tmp_path):
    _write_store(tmp_path, "operator", [JOB])
    identity = resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path))
    assert identity == RoutineIdentity(
        job_id="a726fd5efd24",
        job_name="op-managed:operator:deadline-miss-escalator",
        persona="operator",
        skill="deadline-miss-escalator",
    )


def test_unknown_job_id_resolves_to_none(tmp_path):
    """FALSE CONTROL: a perfectly cron-shaped session whose id is not in any
    store must yield None — never a guess from an unrelated job."""
    _write_store(tmp_path, "operator", [JOB])
    assert resolve_routine("cron_ffffffffffff_20260802_070001", hermes_home=str(tmp_path)) is None


def test_non_cron_session_resolves_to_none(tmp_path):
    _write_store(tmp_path, "operator", [JOB])
    assert resolve_routine("some-interactive-session", hermes_home=str(tmp_path)) is None


def test_unmanaged_job_falls_back_to_declared_skill(tmp_path):
    _write_store(
        tmp_path,
        "operator",
        [{"id": "beef00000001", "name": "agent-authored nightly", "skills": ["lien-check"]}],
    )
    identity = resolve_routine("cron_beef00000001_20260802_070001", hermes_home=str(tmp_path))
    assert identity is not None
    assert identity.persona is None
    assert identity.skill == "lien-check"
    assert identity.job_name == "agent-authored nightly"


def test_root_store_is_consulted(tmp_path):
    d = tmp_path / "cron"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text(json.dumps([JOB]), encoding="utf-8")
    identity = resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path))
    assert identity is not None and identity.skill == "deadline-miss-escalator"


def test_missing_home_resolves_to_none(tmp_path):
    assert (
        resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path / "absent"))
        is None
    )


def test_malformed_store_resolves_to_none_without_raising(tmp_path):
    d = tmp_path / "profiles" / "operator" / "cron"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text("{not json", encoding="utf-8")
    assert resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path)) is None


def test_cache_invalidates_on_mtime_change(tmp_path):
    """Rotation mid-process: after re-materialization mints a new id, the NEW
    id resolves and the OLD id stops resolving (no stale-cache attribution)."""
    import os

    _write_store(tmp_path, "operator", [JOB])
    assert resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path))

    rotated = dict(JOB, id="0123456789ab")
    _write_store(tmp_path, "operator", [rotated])
    # Force a distinct mtime even on coarse-mtime filesystems.
    p = tmp_path / "profiles" / "operator" / "cron" / "jobs.json"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert resolve_routine("cron_a726fd5efd24_20260802_070001", hermes_home=str(tmp_path)) is None
    fresh = resolve_routine("cron_0123456789ab_20260802_070001", hermes_home=str(tmp_path))
    assert fresh is not None and fresh.job_id == "0123456789ab"


# ------------------------------------------------- emission integration


def _load_audit_plugin():
    """Same sequencing as tests/test_audit_emit.py::load_plugin — the parent
    module must be registered before exec so `from . import emit` works."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).parent.parent
    init_path = root / "plugins" / "hermes-smd-audit" / "__init__.py"
    spec = importlib.util.spec_from_file_location("plugin_hermes_smd_audit", init_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["plugin_hermes_smd_audit"] = module
    spec.loader.exec_module(module)
    return module


def test_emit_tool_event_stamps_attribution_on_real_sqlite(tmp_path):
    """End-to-end: a cron session id + a live jobs.json → the written row's
    skill_name COLUMN carries the routine's skill and metadata carries the
    stable routine name + the job id it resolved from."""
    from shared.d1_client import D1Client

    _write_store(tmp_path, "operator", [JOB])

    mod = _load_audit_plugin()
    db = str(tmp_path / "audit.db")
    client = D1Client(binding_name=db, customer_slug="acme")
    writer = mod.emit.AuditLogWriter(client)
    writer.ensure_schema()

    ulid = mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_list_messages",
        args=None,
        result=None,
        task_id="",
        session_id="cron_a726fd5efd24_20260802_070001",
        tool_call_id="tc-1",
        duration_ms=12,
        hermes_home_for_attribution=str(tmp_path),
    )
    assert ulid is not None

    row = client.query("SELECT skill_name, metadata FROM audit_log WHERE id = ?", ulid)[0]
    assert row["skill_name"] == "deadline-miss-escalator"
    meta = json.loads(row["metadata"])
    assert meta["routine"] == "op-managed:operator:deadline-miss-escalator"
    assert meta["cron_job_id"] == "a726fd5efd24"
    assert meta["skill"] == "deadline-miss-escalator"


def test_emit_tool_event_non_cron_session_leaves_skill_null(tmp_path):
    """FALSE CONTROL for the integration: an interactive session with the same
    stores present writes skill_name NULL and no routine metadata."""
    from shared.d1_client import D1Client

    _write_store(tmp_path, "operator", [JOB])

    mod = _load_audit_plugin()
    db = str(tmp_path / "audit.db")
    client = D1Client(binding_name=db, customer_slug="acme")
    writer = mod.emit.AuditLogWriter(client)
    writer.ensure_schema()

    ulid = mod.emit.emit_tool_event(
        writer,
        customer="acme",
        tool_name="email_list_messages",
        args=None,
        result=None,
        task_id="",
        session_id="interactive-0192",
        tool_call_id="tc-2",
        duration_ms=12,
        hermes_home_for_attribution=str(tmp_path),
    )
    assert ulid is not None

    row = client.query("SELECT skill_name, metadata FROM audit_log WHERE id = ?", ulid)[0]
    assert row["skill_name"] is None
    meta = json.loads(row["metadata"])
    assert "routine" not in meta
    assert "cron_job_id" not in meta
