"""Tests for the hermes-smd-audit skill_capture module — ADR 0022 Stream 2.

Covers:
  * Pure helpers: compute_content_hash, make_r2_key.
  * read_skill_body: file present / missing / unreadable.
  * load_r2_config_from_env: full env / any-missing → None.
  * capture_skill_body: happy path, R2 missing, R2 fails, body missing,
    D1 INSERT fails.
  * reconcile_pending_bodies: persisted skipped, pending recovered, hash
    mismatch failed, body missing failed.
  * Schema DDLs are stable and idempotent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the audit plugin package; see test_audit_emit for rationale."""
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        init_path,
        submodule_search_locations=[str(init_path.parent)],
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def audit_mod():
    return load_plugin("hermes-smd-audit")


@pytest.fixture
def skill_capture(audit_mod):
    return audit_mod.skill_capture


# ---------------------------------------------------------------------------
# Fake D1Client — supports execute() + query() for the SELECT path
# ---------------------------------------------------------------------------


class FakeD1Client:
    """In-memory recorder. Stores INSERTed rows and supports query() for the
    pending/failed scan. Mirrors the subset of D1Client used by skill_capture.
    """

    def __init__(
        self,
        *,
        raise_on_execute: Exception | None = None,
        rows: list[tuple] | None = None,
    ) -> None:
        self.executes: list[tuple[str, tuple]] = []
        self.queries: list[tuple[str, tuple]] = []
        self._raise = raise_on_execute
        self._rows = rows or []

    def execute(self, sql: str, *params) -> int:
        if self._raise is not None:
            raise self._raise
        self.executes.append((sql, tuple(params)))
        return 1

    def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        for p in params_list:
            self.executes.append((sql, tuple(p)))
        return len(params_list)

    def query(self, sql: str, *params) -> list[tuple]:
        self.queries.append((sql, tuple(params)))
        return list(self._rows)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_compute_content_hash_is_stable_sha256(skill_capture):
    assert skill_capture.compute_content_hash(b"hello") == hashlib.sha256(b"hello").hexdigest()
    assert len(skill_capture.compute_content_hash(b"")) == 64


def test_make_r2_key_shape(skill_capture):
    key = skill_capture.make_r2_key(
        persona_slug="marcus",
        skill_name="demand-letter-draft",
        content_hash="a" * 64,
    )
    assert key == "skills/marcus/demand-letter-draft/" + ("a" * 64) + ".md"


# ---------------------------------------------------------------------------
# read_skill_body
# ---------------------------------------------------------------------------


def test_read_skill_body_returns_body_and_hash(tmp_path, skill_capture):
    profiles_dir = tmp_path / "profiles" / "marcus" / "skills" / "demand-letter-draft"
    profiles_dir.mkdir(parents=True)
    body_bytes = b"---\nname: demand-letter-draft\n---\nbody contents"
    (profiles_dir / "SKILL.md").write_bytes(body_bytes)

    result = skill_capture.read_skill_body(
        hermes_home=str(tmp_path),
        persona_slug="marcus",
        skill_name="demand-letter-draft",
    )
    assert result is not None
    assert result.body_bytes == body_bytes
    assert result.content_hash == hashlib.sha256(body_bytes).hexdigest()


def test_read_skill_body_returns_none_when_missing(tmp_path, skill_capture):
    result = skill_capture.read_skill_body(
        hermes_home=str(tmp_path), persona_slug="marcus", skill_name="ghost-skill"
    )
    assert result is None


# ---------------------------------------------------------------------------
# load_r2_config_from_env
# ---------------------------------------------------------------------------


def test_load_r2_config_full_env(monkeypatch, skill_capture):
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_SKILL_BODIES_ACCESS_KEY_ID", "AKxxxxx")
    monkeypatch.setenv("R2_SKILL_BODIES_SECRET_ACCESS_KEY", "secretvalue")
    monkeypatch.setenv("R2_SKILL_BODIES_BUCKET", "ss-operator-smith-pi-firm-skills")

    config = skill_capture.load_r2_config_from_env()
    assert config is not None
    assert config.bucket == "ss-operator-smith-pi-firm-skills"
    assert config.endpoint_url == "https://example.r2.cloudflarestorage.com"


def test_load_r2_config_missing_any_returns_none(monkeypatch, skill_capture):
    monkeypatch.delenv("R2_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("R2_SKILL_BODIES_ACCESS_KEY_ID", "AK")
    monkeypatch.setenv("R2_SKILL_BODIES_SECRET_ACCESS_KEY", "S")
    monkeypatch.setenv("R2_SKILL_BODIES_BUCKET", "b")
    assert skill_capture.load_r2_config_from_env() is None


# ---------------------------------------------------------------------------
# capture_skill_body
# ---------------------------------------------------------------------------


def _seed_skill_body(tmp_path: Path, persona: str, skill: str, body: bytes) -> str:
    """Write a SKILL.md under tmp_path/profiles/<persona>/skills/<skill>/. Returns hash."""
    p = tmp_path / "profiles" / persona / "skills" / skill
    p.mkdir(parents=True)
    (p / "SKILL.md").write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def test_capture_happy_path_persists(tmp_path, skill_capture, monkeypatch):
    body = b"# SKILL.md body for demand-letter-draft\n"
    expected_hash = _seed_skill_body(tmp_path, "marcus", "demand-letter-draft", body)
    d1 = FakeD1Client()

    # Patch boto3 with a fake that records the PUT call.
    put_calls: list[dict] = []

    class _FakeS3Client:
        def put_object(self, **kwargs) -> dict:
            put_calls.append(kwargs)
            return {"ETag": '"fake-etag"'}

    class _FakeBoto3:
        def client(self, *args, **kwargs):
            return _FakeS3Client()

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3())
    # botocore.exceptions.ClientError is imported lazily inside put_skill_body;
    # provide a stub so the import succeeds.
    monkeypatch.setitem(
        sys.modules,
        "botocore.exceptions",
        type(
            "BCExc",
            (),
            {"ClientError": type("ClientError", (Exception,), {"response": {}})},
        )(),
    )

    r2 = skill_capture.R2Config(
        endpoint_url="https://example.r2.cloudflarestorage.com",
        access_key_id="AK",
        secret_access_key="S",
        bucket="ss-operator-smith-pi-firm-skills",
    )

    result = skill_capture.capture_skill_body(
        d1,
        r2,
        customer_slug="smith-pi-firm",
        persona_slug="marcus",
        skill_name="demand-letter-draft",
        source_turn_id="turn-001",
        hermes_home=str(tmp_path),
    )

    assert result.r2_status == "persisted"
    assert result.recorded is True
    assert result.r2_key == f"skills/marcus/demand-letter-draft/{expected_hash}.md"
    # Two D1 calls: INSERT pending, UPDATE persisted.
    assert len(d1.executes) == 2
    assert "INSERT INTO agent_skills_inventory" in d1.executes[0][0]
    assert "r2_status = 'persisted'" in d1.executes[1][0]
    # One R2 PUT with the right key, bucket, and body.
    assert len(put_calls) == 1
    assert put_calls[0]["Bucket"] == "ss-operator-smith-pi-firm-skills"
    assert put_calls[0]["Key"] == result.r2_key
    assert put_calls[0]["Body"] == body


def test_capture_skips_when_body_missing(tmp_path, skill_capture):
    d1 = FakeD1Client()
    r2 = skill_capture.R2Config(
        endpoint_url="https://x", access_key_id="a", secret_access_key="s", bucket="b"
    )
    result = skill_capture.capture_skill_body(
        d1,
        r2,
        customer_slug="smith-pi-firm",
        persona_slug="marcus",
        skill_name="ghost-skill",
        source_turn_id="turn-x",
        hermes_home=str(tmp_path),
    )
    assert result.recorded is False
    assert result.r2_status == "skipped"
    assert result.reason == "BodyMissingOnVolume"
    assert d1.executes == []  # No D1 row written for unrecoverable bodies.


def test_capture_pending_when_r2_config_missing(tmp_path, skill_capture):
    _seed_skill_body(tmp_path, "marcus", "demand-letter-draft", b"body")
    d1 = FakeD1Client()
    result = skill_capture.capture_skill_body(
        d1,
        None,  # R2 config absent
        customer_slug="smith-pi-firm",
        persona_slug="marcus",
        skill_name="demand-letter-draft",
        source_turn_id="turn-001",
        hermes_home=str(tmp_path),
    )
    assert result.recorded is True
    assert result.r2_status == "pending"
    assert result.reason == "R2EnvMissing"
    # Only the write-ahead INSERT — no UPDATE because no PUT attempted.
    assert len(d1.executes) == 1


def test_capture_marks_failed_on_r2_error(tmp_path, skill_capture, monkeypatch):
    body = b"body"
    _seed_skill_body(tmp_path, "marcus", "demand-letter-draft", body)
    d1 = FakeD1Client()

    # Provide a fake botocore.exceptions module with a ClientError that
    # carries a `response` dict — that's what skill_capture inspects.
    class _ClientError(Exception):
        def __init__(self, response: dict):
            super().__init__("synthetic")
            self.response = response

    monkeypatch.setitem(
        sys.modules, "botocore.exceptions", type("M", (), {"ClientError": _ClientError})()
    )

    class _FakeS3Client:
        def put_object(self, **kwargs):
            raise _ClientError({"Error": {"Code": "AccessDenied"}})

    class _FakeBoto3:
        def client(self, *args, **kwargs):
            return _FakeS3Client()

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3())

    r2 = skill_capture.R2Config(
        endpoint_url="https://x", access_key_id="a", secret_access_key="s", bucket="b"
    )
    result = skill_capture.capture_skill_body(
        d1,
        r2,
        customer_slug="smith-pi-firm",
        persona_slug="marcus",
        skill_name="demand-letter-draft",
        source_turn_id="turn-001",
        hermes_home=str(tmp_path),
    )

    assert result.r2_status == "failed"
    assert result.reason == "AccessDenied"
    # Two D1 calls: write-ahead INSERT + UPDATE failed.
    assert len(d1.executes) == 2
    assert "INSERT INTO agent_skills_inventory" in d1.executes[0][0]
    assert "r2_status = 'failed'" in d1.executes[1][0]
    # First param of the UPDATE failed SQL is the reason.
    assert d1.executes[1][1][0] == "AccessDenied"


def test_capture_skips_on_d1_insert_failure(tmp_path, skill_capture):
    _seed_skill_body(tmp_path, "marcus", "demand-letter-draft", b"body")
    d1 = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    r2 = skill_capture.R2Config(
        endpoint_url="https://x", access_key_id="a", secret_access_key="s", bucket="b"
    )
    result = skill_capture.capture_skill_body(
        d1,
        r2,
        customer_slug="smith-pi-firm",
        persona_slug="marcus",
        skill_name="demand-letter-draft",
        source_turn_id="turn-001",
        hermes_home=str(tmp_path),
    )
    assert result.recorded is False
    assert result.r2_status == "skipped"
    assert result.reason == "D1InsertFailed"


# ---------------------------------------------------------------------------
# reconcile_pending_bodies
# ---------------------------------------------------------------------------


def test_reconcile_marks_missing_body_as_failed(tmp_path, skill_capture):
    # Row exists in D1 but the file no longer exists on the volume.
    pending_row = (
        "smith-pi-firm",
        "marcus",
        "ghost-skill",
        "a" * 64,
        "skills/marcus/ghost-skill/" + ("a" * 64) + ".md",
    )
    d1 = FakeD1Client(rows=[pending_row])
    r2 = skill_capture.R2Config(
        endpoint_url="https://x", access_key_id="a", secret_access_key="s", bucket="b"
    )
    summary = skill_capture.reconcile_pending_bodies(
        d1, r2, hermes_home=str(tmp_path), customer_slug="smith-pi-firm"
    )
    assert summary.scanned == 1
    assert summary.skipped_missing_body == 1
    assert summary.persisted == 0
    # One UPDATE failed call recording BodyMissingOnVolume.
    assert any("r2_status = 'failed'" in sql for sql, _ in d1.executes)
    assert any("BodyMissingOnVolume" in params[0] for sql, params in d1.executes if params)


def test_reconcile_marks_hash_mismatch_as_failed(tmp_path, skill_capture):
    # Body file exists but its hash differs from the row's stored hash.
    _seed_skill_body(tmp_path, "marcus", "demand-letter-draft", b"actual body")
    bogus_hash = "b" * 64
    pending_row = (
        "smith-pi-firm",
        "marcus",
        "demand-letter-draft",
        bogus_hash,
        f"skills/marcus/demand-letter-draft/{bogus_hash}.md",
    )
    d1 = FakeD1Client(rows=[pending_row])
    r2 = skill_capture.R2Config(
        endpoint_url="https://x", access_key_id="a", secret_access_key="s", bucket="b"
    )
    summary = skill_capture.reconcile_pending_bodies(
        d1, r2, hermes_home=str(tmp_path), customer_slug="smith-pi-firm"
    )
    assert summary.failed == 1
    assert any(
        params and params[0] == "BodyHashMismatch"
        for sql, params in d1.executes
        if "r2_status = 'failed'" in sql
    )


def test_reconcile_skipped_when_r2_config_missing(tmp_path, skill_capture):
    d1 = FakeD1Client()
    summary = skill_capture.reconcile_pending_bodies(
        d1, None, hermes_home=str(tmp_path), customer_slug="smith-pi-firm"
    )
    assert summary == skill_capture.ReconcileSummary(0, 0, 0, 0)
    assert d1.queries == []  # No SELECT issued when R2 is unconfigured.


# ---------------------------------------------------------------------------
# Schema DDLs
# ---------------------------------------------------------------------------


def test_schemas_export_inventory_ddl(audit_mod):
    assert (
        "CREATE TABLE IF NOT EXISTS agent_skills_inventory"
        in audit_mod.schemas.AGENT_SKILLS_INVENTORY_DDL
    )
    assert "r2_status" in audit_mod.schemas.AGENT_SKILLS_INVENTORY_DDL
    assert "CHECK (r2_status IN" in audit_mod.schemas.AGENT_SKILLS_INVENTORY_DDL


def test_audit_plugin_ddls_tuple_contains_all_indexes(audit_mod):
    ddls = audit_mod.schemas.AUDIT_PLUGIN_DDLS
    assert len(ddls) == 5
    joined = "\n".join(ddls)
    assert "agent_skills_inventory_by_hash" in joined
    assert "agent_skills_inventory_r2_pending" in joined
    assert "agent_skills_inventory_active" in joined
    assert "agent_skills_inventory_by_persona" in joined


def test_ddls_idempotent(audit_mod):
    """Every DDL must use IF NOT EXISTS for safe boot-time re-application."""
    for ddl in audit_mod.schemas.AUDIT_PLUGIN_DDLS:
        assert "IF NOT EXISTS" in ddl
