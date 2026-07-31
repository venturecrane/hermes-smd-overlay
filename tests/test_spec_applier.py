"""Unit tests for ``spec_applier`` — the root-owned authored-spec install.

Every side effect is faked: a ``FakeS3`` get_object/head_object and ``tmp_path``
for the install target. No network, no real volume, no root.

The tests that matter most are the refusals. A loader that installs the happy
path is easy; what this package exists to guarantee is that a document whose
integrity claim fails installs NOTHING and leaves the previous tree standing,
because "refuse the update" and "remove the spec" are different outcomes and
only the first is ever correct here.
"""

from __future__ import annotations

import hashlib
import io
import json
import os

import pytest

from spec_applier.applier import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    SpecApplyError,
    SpecApplyOutcome,
    SpecObjectMissing,
    apply,
    parse_and_verify,
    pull_specs,
    spec_object_key,
)

# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


class FakeS3:
    """Minimal boto3-style client serving one keyed object."""

    def __init__(self, objects: dict[tuple[str, str], bytes]):
        self._objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803 — boto3 kwargs
        self.calls.append((Bucket, Key))
        try:
            return {"Body": io.BytesIO(self._objects[(Bucket, Key)])}
        except KeyError as exc:
            raise _NoSuchKey(f"{Bucket}/{Key}") from exc

    def head_object(self, *, Bucket: str, Key: str):  # noqa: N803
        data = self._objects.get((Bucket, Key))
        if data is None:
            raise _NoSuchKey(f"{Bucket}/{Key}")
        return {"ETag": hashlib.md5(data).hexdigest()}  # noqa: S324 — ETag shape only


class _NoSuchKey(Exception):
    """Shaped like botocore's ClientError for a missing key."""

    def __init__(self, msg: str):
        super().__init__(msg)
        self.response = {"Error": {"Code": "NoSuchKey"}}


def _doc(bodies: dict[str, dict[str, str]], *, version: int = SCHEMA_VERSION) -> bytes:
    """Build a source document, computing each declared sha256 correctly."""
    classes: dict[str, dict] = {}
    for slug, props in bodies.items():
        classes[slug] = {
            prop: {
                "body": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for prop, text in props.items()
        }
    return json.dumps({"schema_version": version, "classes": classes}).encode()


SLUG = "ashton-price"
BUCKET = "smd-customer-config"
KEY = (BUCKET, f"vaults/{SLUG}/output-classes.json")


# ---------------------------------------------------------------------------
# Key derivation + pull
# ---------------------------------------------------------------------------


def test_spec_object_key_scopes_to_the_customer_vault():
    assert spec_object_key(SLUG) == f"vaults/{SLUG}/output-classes.json"


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_spec_object_key_refuses_a_blank_slug(bad):
    """A blank slug would address the vault ROOT, so it is refused outright."""
    with pytest.raises(SpecApplyError):
        spec_object_key(bad)


def test_pull_raises_spec_object_missing_when_the_vault_has_none():
    """The ORDINARY state of a seat whose customer authored nothing — it must be
    distinguishable from a genuine R2 fault, because one is a non-event and the
    other deserves a retry."""
    with pytest.raises(SpecObjectMissing):
        pull_specs(FakeS3({}), BUCKET, SLUG)


def test_pull_wraps_a_real_fault_as_spec_apply_error():
    class Broken:
        def get_object(self, **_kw):
            raise RuntimeError("connection reset")

    with pytest.raises(SpecApplyError) as exc:
        pull_specs(Broken(), BUCKET, SLUG)
    assert not isinstance(exc.value, SpecObjectMissing)


# ---------------------------------------------------------------------------
# Parse + hash verification
# ---------------------------------------------------------------------------


def test_parse_accepts_a_well_formed_document():
    specs, errors = parse_and_verify(_doc({"staff": {"voice": "Write plainly.\n"}}))
    assert errors == []
    assert [(s.output_class, s.prop, s.rel_path) for s in specs] == [
        ("staff", "voice", "classes/staff/voice.md")
    ]


def test_parse_refuses_a_body_whose_declared_hash_disagrees():
    raw = json.loads(_doc({"staff": {"voice": "Write plainly.\n"}}))
    raw["classes"]["staff"]["voice"]["sha256"] = "0" * 64
    specs, errors = parse_and_verify(json.dumps(raw).encode())
    assert specs == []
    assert any("does not match" in e for e in errors)


def test_one_bad_property_refuses_the_whole_document():
    """No partial adoption: a document half of which failed integrity is one
    whose author and whose bytes disagree, and nothing in it is more trustworthy
    than the part that failed."""
    raw = json.loads(_doc({"staff": {"voice": "ok\n"}, "outbound_client": {"voice": "also ok\n"}}))
    raw["classes"]["outbound_client"]["voice"]["sha256"] = "f" * 64
    specs, errors = parse_and_verify(json.dumps(raw).encode())
    assert specs == []
    assert errors


def test_parse_refuses_an_unknown_schema_version():
    _, errors = parse_and_verify(_doc({"staff": {"voice": "x\n"}}, version=99))
    assert any("schema_version" in e for e in errors)


@pytest.mark.parametrize("slug", ["../escape", "a/b", "UPPER", "x" * 65])
def test_parse_refuses_an_unsafe_class_slug(slug):
    """Strict rather than sanitizing — a slug carrying `/` or `..` is an escape
    attempt or a bug, and a quietly-rewritten path hides both."""
    raw = json.loads(_doc({"staff": {"voice": "x\n"}}))
    raw["classes"][slug] = raw["classes"].pop("staff")
    _, errors = parse_and_verify(json.dumps(raw).encode())
    assert any(slug in e for e in errors)


def test_parse_refuses_non_json_and_non_utf8():
    assert parse_and_verify(b"not json at all")[1]
    assert parse_and_verify(b"\xff\xfe\x00")[1]


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def test_apply_installs_bodies_and_a_root_computed_manifest(tmp_path):
    doc = _doc({"staff": {"voice": "Say the number.\n", "format": "Bullets.\n"}})
    result = apply(s3_client=FakeS3({KEY: doc}), bucket=BUCKET, slug=SLUG, spec_dir=tmp_path)

    assert result.outcome is SpecApplyOutcome.APPLIED
    assert (tmp_path / "classes/staff/voice.md").read_text() == "Say the number.\n"
    assert (tmp_path / "classes/staff/format.md").read_text() == "Bullets.\n"

    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert manifest["customer"] == SLUG
    entry = manifest["specs"]["classes/staff/voice.md"]
    assert entry["class"] == "staff"
    assert entry["property"] == "voice"
    # Computed over the bytes ON DISK, not copied from the document. A manifest
    # that echoed the source's claim would verify the source against itself.
    assert (
        entry["sha256"]
        == hashlib.sha256((tmp_path / "classes/staff/voice.md").read_bytes()).hexdigest()
    )


def test_installed_files_are_group_and_world_readable_not_writable(tmp_path):
    """The asymmetry IS the security property: the agent must read these and
    must never write them."""
    apply(
        s3_client=FakeS3({KEY: _doc({"staff": {"voice": "x\n"}})}),
        bucket=BUCKET,
        slug=SLUG,
        spec_dir=tmp_path,
    )
    mode = (tmp_path / "classes/staff/voice.md").stat().st_mode & 0o7777
    assert mode == 0o644
    assert (tmp_path / "classes").stat().st_mode & 0o7777 == 0o755


@pytest.mark.parametrize("umask", [0o022, 0o027, 0o077])
def test_every_directory_is_traversable_whatever_the_umask(tmp_path, umask):
    """The assertion above was right and passed for the wrong reason.

    ``Path.mkdir(parents=True)`` creates INTERMEDIATE directories with
    ``0o777 & ~umask`` and does not apply the caller's mode to them. The applier
    hardened only the leaf, so ``classes/`` inherited the process umask. Under
    the test runner's 0o022 that is 0o755 and the existing assertion passed —
    it was measuring the runner's umask, not the code.

    On a live seat the applier runs as ROOT, whose umask in the customer image
    is 0o027, so ``classes/`` landed at 0o750: no world execute, agent gets
    Permission denied, and the spec below it is unreachable however correct its
    own mode is. Observed on hermes-smd-staging 2026-07-31
    (``vfy_01KYWVR8PBBEP85W3F5SSNC9FD``) — manifest correct, digest matching,
    applier logging APPLIED, feature dead.

    Parametrising the umask is the whole point: a fixed-umask test cannot
    distinguish "the code sets this" from "the environment happened to".
    """
    previous = os.umask(umask)
    try:
        apply(
            s3_client=FakeS3({KEY: _doc({"staff": {"voice": "x\n"}})}),
            bucket=BUCKET,
            slug=SLUG,
            spec_dir=tmp_path / "specs",
        )
    finally:
        os.umask(previous)

    root = tmp_path / "specs"
    for path in (root, root / "classes", root / "classes/staff"):
        mode = path.stat().st_mode & 0o7777
        assert mode == 0o755, f"{path} is {mode:o}; the agent cannot traverse it"
    # The file itself must be readable and NOT writable — the asymmetry is the
    # security property, and it must not be sacrificed to fix traversal.
    assert (root / "classes/staff/voice.md").stat().st_mode & 0o7777 == 0o644


def test_a_rejected_document_leaves_the_previous_tree_standing(tmp_path):
    """Fail-static. The seat keeps serving the spec it was serving; a bad publish
    costs it its update, never its correctness."""
    good = _doc({"staff": {"voice": "original\n"}})
    apply(s3_client=FakeS3({KEY: good}), bucket=BUCKET, slug=SLUG, spec_dir=tmp_path)

    corrupt = json.loads(_doc({"staff": {"voice": "replacement\n"}}))
    corrupt["classes"]["staff"]["voice"]["sha256"] = "0" * 64
    result = apply(
        s3_client=FakeS3({KEY: json.dumps(corrupt).encode()}),
        bucket=BUCKET,
        slug=SLUG,
        spec_dir=tmp_path,
    )

    assert result.outcome is SpecApplyOutcome.REJECTED
    assert (tmp_path / "classes/staff/voice.md").read_text() == "original\n"
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert "classes/staff/voice.md" in manifest["specs"]


def test_reapplying_identical_bytes_is_unchanged(tmp_path):
    doc = _doc({"staff": {"voice": "x\n"}})
    s3 = FakeS3({KEY: doc})
    assert (
        apply(s3_client=s3, bucket=BUCKET, slug=SLUG, spec_dir=tmp_path).outcome
        is SpecApplyOutcome.APPLIED
    )
    assert (
        apply(s3_client=s3, bucket=BUCKET, slug=SLUG, spec_dir=tmp_path).outcome
        is SpecApplyOutcome.UNCHANGED
    )


def test_a_class_dropped_from_the_document_is_pruned(tmp_path):
    apply(
        s3_client=FakeS3({KEY: _doc({"staff": {"voice": "a\n"}, "record": {"format": "b\n"}})}),
        bucket=BUCKET,
        slug=SLUG,
        spec_dir=tmp_path,
    )
    assert (tmp_path / "classes/record/format.md").exists()

    result = apply(
        s3_client=FakeS3({KEY: _doc({"staff": {"voice": "a\n"}})}),
        bucket=BUCKET,
        slug=SLUG,
        spec_dir=tmp_path,
    )
    assert "classes/record/format.md" in result.pruned
    assert not (tmp_path / "classes/record/format.md").exists()
    assert (tmp_path / "classes/staff/voice.md").exists()


def test_oversize_body_is_refused(tmp_path):
    from spec_applier.applier import MAX_SPEC_BYTES

    _, errors = parse_and_verify(_doc({"staff": {"voice": "x" * (MAX_SPEC_BYTES + 1)}}))
    assert any("ceiling" in e for e in errors)
