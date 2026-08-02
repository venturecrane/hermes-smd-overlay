"""Tests for ``spec_applier.preferences`` — root-owned per-person install.

The refusals matter most (fail-static, whole cycle): one broken object must
freeze the whole preference install loudly while the previously installed tree
keeps serving — and a list FAULT must never read as an emptied prefix, while a
SUCCESSFUL empty list must prune (the removed person's file is gone).
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest

from shared.person_prefs import PREFS_MANIFEST_NAME, PREFS_SUBDIR, person_slug
from spec_applier.applier import SpecApplyError, SpecApplyOutcome
from spec_applier.preferences import (
    apply_preferences,
    list_pref_keys,
    person_pref_key,
    previous_person_pref_key,
)

BUCKET = "smd-customer-config"
SLUG = "pilot-smokeball"


class FakeS3:
    """list_objects_v2 + get_object over an in-memory key space."""

    def __init__(self, objects: dict[str, bytes], *, list_fails: bool = False):
        self.objects = dict(objects)
        self.list_fails = list_fails

    def list_objects_v2(self, *, Bucket, Prefix, **_kw):  # noqa: N803 — boto3 kwargs
        if self.list_fails:
            raise RuntimeError("R2 unavailable")
        contents = [
            {"Key": key, "ETag": hashlib.md5(data).hexdigest()}  # noqa: S324 — ETag shape
            for key, data in sorted(self.objects.items())
            if key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, *, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.objects[Key])}


def _pref_bytes(person: str, body: str = "Bullet points. Short emails.", **over) -> bytes:
    doc = {
        "schema_version": 1,
        "customer": SLUG,
        "person": person,
        "person_slug": person_slug(person),
        "body": body,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "updated_at": "2026-08-02T00:00:00.000Z",
    }
    doc.update(over)
    return json.dumps(doc, sort_keys=True).encode()


def _seed(*people: str) -> dict[str, bytes]:
    return {person_pref_key(SLUG, person_slug(p)): _pref_bytes(p) for p in people}


def _apply(s3, tmp_path):
    return apply_preferences(s3_client=s3, bucket=BUCKET, slug=SLUG, spec_dir=tmp_path)


# ---------------------------------------------------------------------------
# Happy path — verbatim install, root-computed manifest
# ---------------------------------------------------------------------------


def test_installs_each_person_verbatim_and_commits_the_manifest(tmp_path):
    s3 = FakeS3(_seed("chris@firm.com", "sarah@firm.com"))
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.APPLIED
    slug = person_slug("chris@firm.com")
    installed = tmp_path / PREFS_SUBDIR / f"{slug}.json"
    raw = s3.objects[person_pref_key(SLUG, slug)]
    # Verbatim: the installed file IS the vault object.
    assert installed.read_bytes() == raw
    manifest = json.loads((tmp_path / PREFS_MANIFEST_NAME).read_text())
    entry = manifest["preferences"][slug]
    assert entry["person"] == "chris@firm.com"
    # Root-computed over the bytes written, which is the converge signal.
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(manifest["preferences"]) == 2


def test_second_apply_with_identical_objects_is_unchanged(tmp_path):
    s3 = FakeS3(_seed("chris@firm.com"))
    assert _apply(s3, tmp_path).outcome is SpecApplyOutcome.APPLIED
    assert _apply(s3, tmp_path).outcome is SpecApplyOutcome.UNCHANGED


def test_previous_json_recovery_copies_are_never_installed(tmp_path):
    objects = _seed("chris@firm.com")
    objects[previous_person_pref_key(SLUG, person_slug("chris@firm.com"))] = b"old"
    s3 = FakeS3(objects)
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.APPLIED
    assert len(result.installed) == 1


# ---------------------------------------------------------------------------
# Refusals — whole-cycle fail-static
# ---------------------------------------------------------------------------


def _corrupt_cases():
    good = "sarah@firm.com"
    slug = person_slug(good)
    return [
        ("slug-mismatch", person_pref_key(SLUG, slug), _pref_bytes(good, person_slug="other")),
        (
            "body-hash-mismatch",
            person_pref_key(SLUG, slug),
            _pref_bytes(good, sha256="0" * 64),
        ),
        ("not-json", person_pref_key(SLUG, slug), b"{torn"),
        (
            "domain-subject",
            person_pref_key(SLUG, slug),
            _pref_bytes(good).replace(b"sarah@firm.com", b"@firm.com"),
        ),
    ]


@pytest.mark.parametrize("label,key,data", _corrupt_cases())
def test_one_broken_object_refuses_the_whole_cycle_and_prior_tree_stands(
    tmp_path, label, key, data
):
    # Cycle 1: a good install for chris.
    s3 = FakeS3(_seed("chris@firm.com"))
    assert _apply(s3, tmp_path).outcome is SpecApplyOutcome.APPLIED
    before_manifest = (tmp_path / PREFS_MANIFEST_NAME).read_bytes()
    # Cycle 2: sarah's object is broken — NOTHING changes, chris keeps serving.
    s3.objects[key] = data
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.REJECTED, label
    assert result.reasons
    assert (tmp_path / PREFS_MANIFEST_NAME).read_bytes() == before_manifest
    chris = tmp_path / PREFS_SUBDIR / f"{person_slug('chris@firm.com')}.json"
    assert chris.exists()


def test_key_that_does_not_match_the_derived_slug_is_refused(tmp_path):
    # A well-formed object filed under someone else's key: location and content
    # disagree about whose preferences these are.
    s3 = FakeS3({person_pref_key(SLUG, "wrong-place"): _pref_bytes("sarah@firm.com")})
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.REJECTED
    assert any("does not match the derived slug" in r for r in result.reasons)


# ---------------------------------------------------------------------------
# Removal + faults
# ---------------------------------------------------------------------------


def test_removed_person_is_pruned_on_the_next_cycle(tmp_path):
    s3 = FakeS3(_seed("chris@firm.com", "sarah@firm.com"))
    _apply(s3, tmp_path)
    del s3.objects[person_pref_key(SLUG, person_slug("sarah@firm.com"))]
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.APPLIED
    assert result.pruned == (f"{PREFS_SUBDIR}/{person_slug('sarah@firm.com')}.json",)
    manifest = json.loads((tmp_path / PREFS_MANIFEST_NAME).read_text())
    assert person_slug("sarah@firm.com") not in manifest["preferences"]


def test_successful_empty_list_prunes_everything(tmp_path):
    s3 = FakeS3(_seed("chris@firm.com"))
    _apply(s3, tmp_path)
    s3.objects.clear()
    result = _apply(s3, tmp_path)
    assert result.outcome is SpecApplyOutcome.APPLIED
    assert result.pruned
    assert json.loads((tmp_path / PREFS_MANIFEST_NAME).read_text())["preferences"] == {}


def test_a_list_fault_raises_rather_than_reading_as_an_empty_prefix(tmp_path):
    """An R2 outage must never look like 'everyone deleted their preferences' —
    the fault propagates and the caller skips the tick."""
    s3 = FakeS3(_seed("chris@firm.com"))
    _apply(s3, tmp_path)
    s3.list_fails = True
    with pytest.raises(SpecApplyError):
        list_pref_keys(s3, BUCKET, SLUG)
    with pytest.raises(SpecApplyError):
        _apply(s3, tmp_path)
    # Prior tree untouched.
    assert (tmp_path / PREFS_SUBDIR / f"{person_slug('chris@firm.com')}.json").exists()
