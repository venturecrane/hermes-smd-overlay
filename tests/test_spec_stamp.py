"""Tests for the per-profile authored-spec POINTER stamp (ss ADR 0083, #2084).

The delivery half. ``_install_persona_skills`` copytrees each skill body into the
profile on EVERY boot, which restores the catalog's unstamped SKILL.md — so the
stamp has to be re-applied every boot, and therefore has to replace rather than
append. A stamp that appended would grow one copy per boot; on a Machine that
restarts weekly that is a slow, silent context-flood nobody would attribute to a
stamp. The non-stacking assertion below is the load-bearing one.

The other assertion that matters is what the stamp does NOT contain: the spec
prose. SKILL.md is frozen at boot; the spec tree is refreshed under a running
Machine by the root poller. An embedded prose copy would drift from the file it
claims to reproduce, and a confidently-served stale spec is worse than no read
because nothing about it looks wrong.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from bootstrap import translate
from shared import spec_manifest

BODY = "Lead with the answer. Decompose every number.\n"


@pytest.fixture
def installed_specs(tmp_path, monkeypatch):
    spec_dir = tmp_path / "specs"
    rel = "classes/staff/voice.md"
    (spec_dir / rel).parent.mkdir(parents=True)
    (spec_dir / rel).write_text(BODY)
    (spec_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "ashton-price",
                "source_digest": "abc",
                "specs": {
                    rel: {
                        "class": "staff",
                        "property": "voice",
                        "sha256": hashlib.sha256(BODY.encode()).hexdigest(),
                        "bytes": len(BODY),
                    }
                },
            }
        )
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))
    return spec_dir


def test_block_is_empty_when_nothing_is_installed(tmp_path, monkeypatch):
    """A seat with no specs produces a byte-identical SKILL.md — the
    _write_if_changed idempotency contract."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
    assert translate._spec_pointer_block() == ""


def test_block_carries_the_pointer_and_hash(installed_specs):
    block = translate._spec_pointer_block()
    assert "`staff`" in block
    assert "voice" in block
    assert str(installed_specs / "classes/staff/voice.md") in block
    assert hashlib.sha256(BODY.encode()).hexdigest()[:16] in block


def test_block_never_carries_the_spec_prose(installed_specs):
    """The pointer, never the prose. A boot-frozen prose stamp against a
    hot-synced spec would disagree, silently."""
    assert "Decompose every number" not in translate._spec_pointer_block()


def test_block_states_the_precedence_rule(installed_specs):
    """The discipline outranks the voice. A spec shapes how something is said;
    it never licenses saying something the record does not support."""
    block = translate._spec_pointer_block()
    assert "drafting discipline outranks the voice" in block
    assert "court register" in block


def test_stamp_is_idempotent_and_never_stacks(tmp_path, installed_specs):
    """The boot loop's real shape: copytree restores the unstamped file, the
    stamp is re-applied. Ten boots must leave exactly one block."""
    skill_md = tmp_path / "SKILL.md"
    original = "# email-reply\n\nDraft a reply.\n"
    block = translate._spec_pointer_block()

    for _ in range(10):
        skill_md.write_text(original)  # what copytree does every boot
        translate._stamp_skill_md(skill_md, block)

    text = skill_md.read_text()
    assert text.count(translate._SPEC_STAMP_BEGIN) == 1
    assert text.count(translate._SPEC_STAMP_END) == 1
    assert text.startswith(original.rstrip("\n"))


def test_restamping_an_already_stamped_file_does_not_stack(tmp_path, installed_specs):
    """The path where copytree did NOT restore the file (an unchanged catalog):
    the existing region is excised before the fresh block is appended."""
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# s\n")
    block = translate._spec_pointer_block()
    translate._stamp_skill_md(skill_md, block)
    first = skill_md.read_text()
    assert translate._stamp_skill_md(skill_md, block) is False
    assert skill_md.read_text() == first


def test_an_empty_block_excises_a_previous_stamp(tmp_path, installed_specs):
    """A seat whose specs were removed loses its pointers rather than keeping a
    stamp naming files that are gone."""
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# s\n")
    translate._stamp_skill_md(skill_md, translate._spec_pointer_block())
    assert translate._SPEC_STAMP_BEGIN in skill_md.read_text()

    translate._stamp_skill_md(skill_md, "")
    assert translate._SPEC_STAMP_BEGIN not in skill_md.read_text()
    assert skill_md.read_text().startswith("# s")


def test_strip_removes_multiple_accumulated_regions():
    """A file that somehow grew two stamps must come out with none, not one
    fewer."""
    text = (
        "# s\n\n"
        f"{translate._SPEC_STAMP_BEGIN}\na\n{translate._SPEC_STAMP_END}\n"
        f"{translate._SPEC_STAMP_BEGIN}\nb\n{translate._SPEC_STAMP_END}\n"
    )
    out = translate._strip_stamp(text)
    assert translate._SPEC_STAMP_BEGIN not in out
    assert "a\n" not in out and "b\n" not in out


def test_strip_truncates_an_unterminated_stamp():
    """A half-written stamp has no closing marker for the next boot to find, so
    keeping it would make the region unremovable."""
    text = f"# s\n\n{translate._SPEC_STAMP_BEGIN}\npartial"
    assert translate._SPEC_STAMP_BEGIN not in translate._strip_stamp(text)


def test_stamp_survives_a_missing_skill_md(tmp_path, installed_specs):
    assert translate._stamp_skill_md(tmp_path / "absent.md", "block") is False
