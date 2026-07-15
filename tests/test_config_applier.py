"""Unit tests for ``config_applier.applier`` — orchestration with injected I/O.

Every side effect is faked: a ``FakeS3`` get_object, a ``FakeAudit`` capturing
the row, and ``tmp_path`` for the volume. No network, no broker socket, no real
R2. Covers pull (success + faults), atomic_write (atomicity + faults), validate
re-use, and the full apply() decision matrix (applied / rejected-on-validation /
rejected-on-floor / rejected-or-deferred on rebuild-class paths).
"""

import io

import pytest
import yaml

from config_applier import applier
from config_applier.applier import (
    ApplyOutcome,
    ConfigApplyError,
    apply,
    atomic_write,
    config_key,
    pull_config,
    validate_bytes,
)
from shared.audit_contract import COLUMNS

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeS3:
    """Minimal boto3-style S3 client. Serves one object or raises on missing."""

    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self._objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803 - boto3 kwarg names
        self.calls.append((Bucket, Key))
        try:
            data = self._objects[(Bucket, Key)]
        except KeyError as exc:
            raise RuntimeError(f"NoSuchKey: {Bucket}/{Key}") from exc
        return {"Body": io.BytesIO(data)}


class FakeAudit:
    """Captures ``execute(sql, *params)`` calls. Optionally raises to model a
    broker-down audit path."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.rows: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params):
        if self.fail:
            raise RuntimeError("broker unreachable")
        self.rows.append((sql, params))
        return 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_doc(*, vertical: str = "law-firm", external_send: str | None = None) -> dict:
    # ADR 0056: exposure is authored per persona; external_send drives the
    # persona's entitlements.exposure (replacing the retired scope.action_ceilings).
    exposure: dict = {"internal_write": "autonomous"}
    if external_send is not None:
        exposure["external_send"] = external_send
    return {
        "schema_version": 1,
        "customer_id": "acme",
        "customer_name": "Acme Corp",
        "vertical": vertical,
        "fly_region": "iad",
        "model": "claude-opus-4-7",
        "hermes_ref": "v2026.5.16-smd.0",
        "personas": [
            {
                "slug": "marcus",
                "status": "active",
                "name": "Marcus",
                "entitlements": {"exposure": exposure},
                "skills": [
                    {
                        "name": "inbox-triage",
                        "enabled": True,
                        "initiation": {"manual": True, "scheduled": False, "webhook": False},
                    }
                ],
            }
        ],
        "connectors": {"Email": {"adapter": "gmail", "backend": "mcp:gmail", "enabled": True}},
        "scope": {"email_folders_visible": ["Inbox"]},
        "memory": {
            "d1_namespace": "acme",
            "r2_vault_path": "vaults/acme/",
            "vectorize_index": "hermes-acme-vault",
        },
    }


def _valid_yaml(*, vertical: str = "law-firm", external_send: str | None = None) -> str:
    return yaml.safe_dump(
        _valid_doc(vertical=vertical, external_send=external_send), sort_keys=False
    )


VALID = _valid_yaml().encode()
BUCKET = "smd-operator"
SLUG = "acme"
KEY = ("smd-operator", "vaults/acme/customer.yaml")


# ---------------------------------------------------------------------------
# config_key
# ---------------------------------------------------------------------------


def test_config_key_is_vault_scoped():
    assert config_key("acme") == "vaults/acme/customer.yaml"


def test_config_key_strips_and_rejects_blank():
    assert config_key("  acme ") == "vaults/acme/customer.yaml"
    with pytest.raises(ConfigApplyError, match="slug is required"):
        config_key("")
    with pytest.raises(ConfigApplyError):
        config_key(None)


# ---------------------------------------------------------------------------
# pull_config
# ---------------------------------------------------------------------------


def test_pull_config_returns_bytes():
    s3 = FakeS3({KEY: VALID})
    assert pull_config(s3, BUCKET, SLUG) == VALID
    assert s3.calls == [KEY]


def test_pull_config_missing_object_raises_config_apply_error():
    s3 = FakeS3({})
    with pytest.raises(ConfigApplyError, match="could not read"):
        pull_config(s3, BUCKET, SLUG)


def test_pull_config_str_body_is_encoded():
    class StrBodyS3:
        def get_object(self, *, Bucket, Key):  # noqa: N803
            return {"Body": io.StringIO("hello: world")}

    assert pull_config(StrBodyS3(), BUCKET, SLUG) == b"hello: world"


def test_pull_config_no_body_raises():
    class NoBodyS3:
        def get_object(self, *, Bucket, Key):  # noqa: N803
            return {}

    with pytest.raises(ConfigApplyError, match="no Body"):
        pull_config(NoBodyS3(), BUCKET, SLUG)


# ---------------------------------------------------------------------------
# validate_bytes (re-uses the parity validator)
# ---------------------------------------------------------------------------


def test_validate_bytes_accepts_valid():
    assert validate_bytes(VALID) == []


def test_validate_bytes_rejects_unknown_vertical():
    errors = validate_bytes(_valid_yaml(vertical="snake-charming").encode())
    assert any("vertical must be one of" in e for e in errors)


def test_validate_bytes_rejects_non_utf8():
    errors = validate_bytes(b"\xff\xfe not utf8")
    assert any("not valid UTF-8" in e for e in errors)


def test_validate_bytes_runs_secret_scan():
    # A literal-looking secret should trip the parity validator's raw scan.
    leaky = VALID.decode() + "\n  api_key: sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF1234567890abcd\n"
    errors = validate_bytes(leaky.encode())
    assert errors, "secret-bearing config should not validate clean"


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "customer.yaml"
    atomic_write(target, b"payload")
    assert target.read_bytes() == b"payload"


def test_atomic_write_overwrites_atomically(tmp_path):
    target = tmp_path / "customer.yaml"
    target.write_bytes(b"old")
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    # No leftover temp files in the directory.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "customer.yaml"]
    assert leftovers == []


def test_atomic_write_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "deeper" / "customer.yaml"
    atomic_write(target, b"x")
    assert target.read_bytes() == b"x"


def test_atomic_write_rejects_non_bytes(tmp_path):
    with pytest.raises(ConfigApplyError, match="must be bytes"):
        atomic_write(tmp_path / "x", "a string")  # type: ignore[arg-type]


def test_atomic_write_preserves_target_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "customer.yaml"
    target.write_bytes(b"original")

    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(applier.os, "replace", boom)
    with pytest.raises(ConfigApplyError, match="failed"):
        atomic_write(target, b"replacement")
    # The original is intact; the half-written temp is cleaned up.
    assert target.read_bytes() == b"original"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "customer.yaml"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# apply — happy path
# ---------------------------------------------------------------------------


def test_apply_writes_validates_and_audits(tmp_path):
    volume = tmp_path / "customer.yaml"
    s3 = FakeS3({KEY: VALID})
    audit = FakeAudit()

    result = apply(
        s3_client=s3,
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=audit,
        prev_epoch=4,
    )

    assert result.outcome is ApplyOutcome.APPLIED
    assert result.applied is True
    assert result.epoch == 5
    assert result.audited is True
    # Volume now holds the pulled bytes verbatim.
    assert volume.read_bytes() == VALID
    # One CONFIG_WRITE row emitted, with the correct action_type + epoch.
    assert len(audit.rows) == 1
    _sql, params = audit.rows[0]
    assert len(params) == len(COLUMNS)
    assert params[COLUMNS.index("action_type")] == "CONFIG_WRITE"
    metadata = params[COLUMNS.index("metadata")]
    assert '"epoch":5' in metadata
    assert '"customer":"acme"' in metadata
    # output_digest column carries the new-config digest (provenance, not content).
    assert params[COLUMNS.index("output_digest")] is not None


def test_apply_first_apply_onto_empty_volume_stamps_epoch_one(tmp_path):
    volume = tmp_path / "customer.yaml"
    result = apply(
        s3_client=FakeS3({KEY: VALID}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
        prev_epoch=None,
    )
    assert result.outcome is ApplyOutcome.APPLIED
    assert result.epoch == 1
    assert volume.read_bytes() == VALID


def test_apply_initial_seed_writes_rebuild_class_fields(tmp_path):
    # No config on the volume yet (bootstrap seed): the full document — including
    # rebuild-class vertical/model/memory — is written. The non-live-writable
    # gate governs the LIVE replace path, not the initial seed.
    volume = tmp_path / "customer.yaml"
    assert not volume.exists()
    result = apply(
        s3_client=FakeS3({KEY: VALID}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.APPLIED, result.reasons
    assert volume.read_bytes() == VALID


def test_apply_succeeds_even_if_audit_fails(tmp_path):
    volume = tmp_path / "customer.yaml"
    result = apply(
        s3_client=FakeS3({KEY: VALID}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(fail=True),
    )
    # The write is the load-bearing action; a failed audit row does not undo it.
    assert result.outcome is ApplyOutcome.APPLIED
    assert result.audited is False
    assert volume.read_bytes() == VALID


# ---------------------------------------------------------------------------
# apply — rejection paths (no write)
# ---------------------------------------------------------------------------


def test_apply_rejects_invalid_config_without_writing(tmp_path):
    volume = tmp_path / "customer.yaml"
    volume.write_bytes(b"schema_version: 1\ncustomer_id: acme\n")  # pre-existing good-ish file
    before = volume.read_bytes()

    bad = _valid_yaml(vertical="snake-charming").encode()
    audit = FakeAudit()
    result = apply(
        s3_client=FakeS3({KEY: bad}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=audit,
    )
    assert result.outcome is ApplyOutcome.REJECTED
    assert any("vertical must be one of" in r for r in result.reasons)
    # Volume untouched, no audit row.
    assert volume.read_bytes() == before
    assert audit.rows == []


def test_apply_rejects_floor_widening(tmp_path, monkeypatch):
    # Machinery coverage: a synthetic external_send floor is injected for the
    # law-firm slug (the validator's accepted vertical). In production law-firm
    # declares NO floor — the external-send-draft-floor was removed 2026-07
    # (ADR 0035) — but the apply path must still reject widening past any
    # floor a future vertical declares. vertical_floors() reads the shared map
    # at call time, so the injection reaches the live apply path.
    from shared import action_classes

    monkeypatch.setitem(
        action_classes.VERTICAL_FLOORS, "law-firm", {"external_send": "draft_for_review"}
    )
    # Current volume: external_send at the injected floor (draft_for_review).
    volume = tmp_path / "customer.yaml"
    before = _valid_yaml(external_send="draft_for_review").encode()
    volume.write_bytes(before)
    # New config tries to widen external_send to autonomous past the floor.
    new = _valid_yaml(external_send="autonomous").encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.REJECTED
    assert any("vertical floor" in r for r in result.reasons)
    # Volume keeps the floor-preserving config — the widening was not written.
    assert volume.read_bytes() == before


def test_apply_rejects_rebuild_class_path_on_live_path(tmp_path):
    # Current volume differs from new only in the model (rebuild-class).
    volume = tmp_path / "customer.yaml"
    volume.write_bytes(VALID)
    new_doc = _valid_doc()
    new_doc["model"] = "claude-opus-4-8"
    new = yaml.safe_dump(new_doc, sort_keys=False).encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.REJECTED
    assert any("rebuild-class" in r and "model" in r for r in result.reasons)
    assert "model" in str(result.reasons)
    # Untouched.
    assert volume.read_bytes() == VALID


def test_apply_defers_rebuild_class_when_allowed(tmp_path):
    volume = tmp_path / "customer.yaml"
    volume.write_bytes(VALID)
    new_doc = _valid_doc()
    new_doc["model"] = "claude-opus-4-8"
    new = yaml.safe_dump(new_doc, sort_keys=False).encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
        allow_deferred_paths=True,
    )
    assert result.outcome is ApplyOutcome.DEFERRED
    assert "model" in result.changed
    # Deferred = no write.
    assert volume.read_bytes() == VALID


def test_apply_applies_live_writable_ceiling_change(tmp_path):
    # ADR 0056: persona exposure is live-writable. Tightening internal_write
    # (no vertical floor) from autonomous → draft_for_review should APPLY.
    volume = tmp_path / "customer.yaml"
    current_doc = _valid_doc()
    current_doc["personas"][0]["entitlements"]["exposure"]["internal_write"] = "autonomous"
    volume.write_bytes(yaml.safe_dump(current_doc, sort_keys=False).encode())
    new_doc = _valid_doc()
    new_doc["personas"][0]["entitlements"]["exposure"]["internal_write"] = "draft_for_review"
    new = yaml.safe_dump(new_doc, sort_keys=False).encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.APPLIED, result.reasons
    assert "personas.0.entitlements.exposure.internal_write" in result.changed
    assert volume.read_bytes() == new


def test_apply_applies_per_skill_settings_change(tmp_path):
    # ss #1931: per-skill settings are the engagement's authored dials — a
    # chase-cadence flip must apply live (before this, a settings edit held the
    # WHOLE diff, found live 2026-07-14).
    volume = tmp_path / "customer.yaml"
    current_doc = _valid_doc()
    current_doc["personas"][0]["skills"][0]["settings"] = {
        "chase_cadence_days": 5,
        "escalate_after_attempts": 3,
    }
    volume.write_bytes(yaml.safe_dump(current_doc, sort_keys=False).encode())
    new_doc = _valid_doc()
    new_doc["personas"][0]["skills"][0]["settings"] = {
        "chase_cadence_days": 9,
        "escalate_after_attempts": 3,
    }
    new = yaml.safe_dump(new_doc, sort_keys=False).encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.APPLIED, result.reasons
    assert "personas.0.skills.0.settings.chase_cadence_days" in result.changed
    assert volume.read_bytes() == new


def test_apply_held_diff_reason_is_accurate_and_names_atomicity(tmp_path):
    # ss #1931: a held diff must (a) label an unlisted path as an allow-list
    # matter, NOT "rebuild-class", and (b) say out loud that the whole diff is
    # held, including live-writable changes bundled with it.
    volume = tmp_path / "customer.yaml"
    volume.write_bytes(VALID)
    new_doc = _valid_doc()
    new_doc["personas"][0]["name"] = "Marcus II"  # unlisted (not never-list)
    new_doc["personas"][0]["skills"][0]["enabled"] = False  # live-writable sibling
    new = yaml.safe_dump(new_doc, sort_keys=False).encode()

    result = apply(
        s3_client=FakeS3({KEY: new}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    assert result.outcome is ApplyOutcome.REJECTED
    joined = " ".join(result.reasons)
    assert "not on the live-writable allow-list" in joined
    assert "personas.0.name" in joined
    assert "rebuild-class" not in joined  # unlisted ≠ rebuild-class
    assert "whole diff is held" in joined
    assert volume.read_bytes() == VALID  # the enabled flip did NOT partially apply


# ---------------------------------------------------------------------------
# apply — unrecoverable faults propagate as ConfigApplyError
# ---------------------------------------------------------------------------


def test_apply_propagates_pull_fault(tmp_path):
    with pytest.raises(ConfigApplyError, match="could not read"):
        apply(
            s3_client=FakeS3({}),  # object missing
            bucket=BUCKET,
            slug=SLUG,
            volume_path=tmp_path / "customer.yaml",
            audit_client=FakeAudit(),
        )


def test_apply_no_op_when_config_unchanged(tmp_path):
    volume = tmp_path / "customer.yaml"
    volume.write_bytes(VALID)
    result = apply(
        s3_client=FakeS3({KEY: VALID}),
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        audit_client=FakeAudit(),
    )
    # Identical config: no changed paths, still a clean APPLIED (idempotent
    # re-write is harmless and the epoch advances).
    assert result.outcome is ApplyOutcome.APPLIED
    assert result.changed == ()
