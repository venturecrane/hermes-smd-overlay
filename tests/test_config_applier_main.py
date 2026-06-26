"""Unit tests for ``config_applier.__main__`` — the root poll loop.

Drives :class:`PollLoop.run_once` with a fake S3 (``head_object`` + ``get_object``
with an ETag that changes when the served object changes), a fake audit client,
and a real ``tmp_path`` volume + epoch file. Covers: unchanged object → no apply;
changed object → apply + epoch persisted + ETag cached; rejected object → cached,
no rewrite; R2/write fault → not cached, retried; boot-seed of a current config;
and the atomic_write owner/mode preservation backstop.
"""

import io
import os
import stat

import yaml

from config_applier.__main__ import PollLoop, read_epoch, write_epoch
from config_applier.applier import ApplyOutcome, atomic_write

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeS3:
    """Serves one config object with an ETag that the test mutates via
    ``set_object``. ``head_object`` returns the ETag; ``get_object`` returns the
    body. ``missing=True`` makes both raise (object absent)."""

    def __init__(self, body: bytes | None = None, etag: str = "etag-1"):
        self._body = body
        self._etag = etag
        self.head_calls = 0
        self.get_calls = 0
        self.fail_head = False

    def set_object(self, body: bytes, etag: str) -> None:
        self._body = body
        self._etag = etag

    def head_object(self, *, Bucket, Key):  # noqa: N803
        self.head_calls += 1
        if self.fail_head or self._body is None:
            raise RuntimeError("NoSuchKey")
        return {"ETag": self._etag}

    def get_object(self, *, Bucket, Key):  # noqa: N803
        self.get_calls += 1
        if self._body is None:
            raise RuntimeError("NoSuchKey")
        return {"Body": io.BytesIO(self._body)}


class FakeAudit:
    def __init__(self):
        self.rows = []

    def execute(self, sql, *params):
        self.rows.append((sql, params))
        return 1


# ---------------------------------------------------------------------------
# Config fixtures (valid + a rebuild-class variant)
# ---------------------------------------------------------------------------


def _doc(*, model: str = "claude-opus-4-7", internal_write: str | None = None) -> dict:
    # ADR 0056: a live-writable ceiling change is a persona exposure edit.
    # internal_write carries no vertical floor, so it is the clean knob for the
    # tighten/widen live-apply tests.
    exposure: dict = {}
    if internal_write is not None:
        exposure["internal_write"] = internal_write
    return {
        "schema_version": 1,
        "customer_id": "acme",
        "customer_name": "Acme Corp",
        "vertical": "law-firm",
        "fly_region": "iad",
        "model": model,
        "hermes_ref": "v2026.5.16-smd.0",
        "personas": [
            {
                "slug": "marcus",
                "status": "active",
                "name": "Marcus",
                "entitlements": {"exposure": exposure},
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


def _yaml(**kw) -> bytes:
    return yaml.safe_dump(_doc(**kw), sort_keys=False).encode()


SLUG = "acme"
BUCKET = "smd-operator"


def _loop(tmp_path, s3, audit, *, volume_body: bytes | None = None) -> PollLoop:
    volume = tmp_path / "customer.yaml"
    if volume_body is not None:
        volume.write_bytes(volume_body)
    return PollLoop(
        s3_client=s3,
        bucket=BUCKET,
        slug=SLUG,
        volume_path=volume,
        epoch_file=tmp_path / ".config-epoch",
        audit_client=audit,
        poll_seconds=1,
    )


# ---------------------------------------------------------------------------
# epoch file
# ---------------------------------------------------------------------------


def test_read_epoch_absent_is_none(tmp_path):
    assert read_epoch(tmp_path / "missing") is None


def test_write_then_read_epoch_roundtrips(tmp_path):
    f = tmp_path / ".config-epoch"
    write_epoch(f, 7)
    assert read_epoch(f) == 7


def test_read_epoch_garbled_is_none(tmp_path):
    f = tmp_path / ".config-epoch"
    f.write_text("not-an-int")
    assert read_epoch(f) is None


# ---------------------------------------------------------------------------
# run_once — change detection
# ---------------------------------------------------------------------------


def test_run_once_fresh_machine_applies_initial_seed(tmp_path):
    # No volume file: fresh Machine. First tick pulls + applies the seed.
    s3 = FakeS3(_yaml(), etag="e1")
    audit = FakeAudit()
    loop = _loop(tmp_path, s3, audit)  # no volume_body
    outcome = loop.run_once()
    assert outcome is ApplyOutcome.APPLIED
    assert loop.volume_path.read_bytes() == _yaml()
    assert read_epoch(loop.epoch_file) == 1
    assert loop._last_etag == "e1"


def test_run_once_unchanged_after_boot_does_not_apply(tmp_path):
    # Volume already holds the current config (booted from it). Seed records the
    # current ETag; the same ETag on the next tick is a no-op.
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    audit = FakeAudit()
    loop = _loop(tmp_path, s3, audit, volume_body=body)
    assert loop.run_once() is None  # seeded, unchanged
    assert s3.get_calls == 0  # never pulled
    assert audit.rows == []


def test_run_once_changed_etag_applies_and_persists_epoch(tmp_path):
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    audit = FakeAudit()
    loop = _loop(tmp_path, s3, audit, volume_body=body)
    # First tick: seeds (records e1), unchanged → None.
    assert loop.run_once() is None
    # Push a live-writable change (tighten the ceiling) with a NEW etag.
    s3.set_object(_yaml(internal_write="draft_for_review"), etag="e2")
    outcome = loop.run_once()
    assert outcome is ApplyOutcome.APPLIED
    assert loop._last_etag == "e2"
    assert read_epoch(loop.epoch_file) == 1  # first apply stamps 1
    assert b"draft_for_review" in loop.volume_path.read_bytes()
    assert len(audit.rows) == 1  # one CONFIG_WRITE


def test_run_once_epoch_increments_across_two_applies(tmp_path):
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    loop = _loop(tmp_path, s3, FakeAudit(), volume_body=body)
    loop.run_once()  # seed
    s3.set_object(_yaml(internal_write="draft_for_review"), etag="e2")
    loop.run_once()
    assert read_epoch(loop.epoch_file) == 1
    s3.set_object(_yaml(internal_write="refused"), etag="e3")
    loop.run_once()
    assert read_epoch(loop.epoch_file) == 2


# ---------------------------------------------------------------------------
# run_once — rejection / deferral / fault
# ---------------------------------------------------------------------------


def test_run_once_rebuild_class_defers_and_caches(tmp_path):
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    audit = FakeAudit()
    loop = _loop(tmp_path, s3, audit, volume_body=body)
    loop.run_once()  # seed
    # Push a rebuild-class change (model) — allow_deferred_paths=True → DEFERRED.
    s3.set_object(_yaml(model="claude-opus-4-8"), etag="e2")
    outcome = loop.run_once()
    assert outcome is ApplyOutcome.DEFERRED
    # Running config unchanged, ETag cached so we don't re-evaluate the same obj.
    assert loop.volume_path.read_bytes() == body
    assert loop._last_etag == "e2"
    assert audit.rows == []  # deferred = no write = no CONFIG_WRITE
    # Next tick on the same object: no re-evaluation.
    assert loop.run_once() is None


def test_run_once_invalid_config_rejected_and_cached(tmp_path):
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    loop = _loop(tmp_path, s3, FakeAudit(), volume_body=body)
    loop.run_once()  # seed
    s3.set_object(yaml.safe_dump(_doc() | {"vertical": "snake-charming"}).encode(), etag="e2")
    outcome = loop.run_once()
    assert outcome is ApplyOutcome.REJECTED
    assert loop.volume_path.read_bytes() == body
    assert loop._last_etag == "e2"  # cached so we don't churn on the bad object


def test_run_once_head_fault_skips_tick_no_cache_change(tmp_path):
    body = _yaml()
    s3 = FakeS3(body, etag="e1")
    loop = _loop(tmp_path, s3, FakeAudit(), volume_body=body)
    loop.run_once()  # seed → _last_etag == e1
    s3.fail_head = True
    assert loop.run_once() is None
    assert loop._last_etag == "e1"  # unchanged; will retry next tick


# ---------------------------------------------------------------------------
# atomic_write — owner/mode preservation (the on-box critical fix)
# ---------------------------------------------------------------------------


def test_atomic_write_preserves_mode_of_existing_target(tmp_path):
    target = tmp_path / "customer.yaml"
    target.write_bytes(b"old")
    os.chmod(target, 0o644)
    atomic_write(target, b"new")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o644, f"expected 0o644 preserved, got {oct(mode)}"
    assert target.read_bytes() == b"new"


def test_atomic_write_preserves_non_default_mode(tmp_path):
    # A distinctly non-mkstemp mode (0600 is mkstemp's default, so use 0640) must
    # survive the replace — proves we restore the TARGET's mode, not the temp's.
    target = tmp_path / "customer.yaml"
    target.write_bytes(b"old")
    os.chmod(target, 0o640)
    atomic_write(target, b"new")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_new_target_has_no_preservation(tmp_path):
    # Brand-new file (initial seed): nothing to preserve, write still succeeds.
    target = tmp_path / "fresh.yaml"
    assert not target.exists()
    atomic_write(target, b"seed")
    assert target.read_bytes() == b"seed"


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------


def test_pollloop_run_once_never_raises_on_config_level_faults(tmp_path):
    # A missing R2 object (HEAD raises) is swallowed — the loop must not die.
    s3 = FakeS3(body=None)  # object absent → head raises
    loop = _loop(tmp_path, s3, FakeAudit())
    assert loop.run_once() is None
