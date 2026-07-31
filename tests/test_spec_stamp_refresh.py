"""The pointer stays current on a RUNNING Machine (ss ADR 0083, plan step 0.4).

WHAT THIS EXISTS TO PREVENT. A client's FIRST authored spec did not reach the
model until the Machine rebooted. The root poller installs a newly-authored spec
within seconds and writes the root-owned manifest — but the pointer that tells
the model the spec exists was rendered only at boot, by ``translate``, and the
renderer emits nothing when no specs are installed. So a Machine that booted
with none carried NO pointer at all.

That is the difference between the product's promise — type it, and from then on
it comes out that way — and "type it, then reboot". It was invisible in the one
proof taken by hand, because that run rebuilt the Machine between uploading the
spec and testing it, and so measured *works after a restart* while reading as
*works*.

The refresh runs as hermes in the agent's own process, never in the root
applier: the profile tree is hermes-owned, and a root re-stamp leaves
root-owned files the next boot cannot overwrite — the 2026-07-16 outage, where a
root-written cron store left the scheduler unable to read its own jobs for eight
days while every health signal stayed green.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared import spec_manifest, spec_stamp

BODY = "Lead with the answer. Decompose every number.\n"
OTHER = "Open with the bottom line. Close with one next action.\n"


def _write_manifest(spec_dir, body: str) -> None:
    rel = "classes/staff/voice.md"
    (spec_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (spec_dir / rel).write_text(body)
    (spec_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "smd-staging",
                "source_digest": hashlib.sha256(body.encode()).hexdigest(),
                "specs": {
                    rel: {
                        "class": "staff",
                        "property": "voice",
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                        "bytes": len(body),
                    }
                },
            }
        )
    )


@pytest.fixture
def profiles(tmp_path):
    """A profile tree shaped like a booted Machine's, with an UNSTAMPED skill."""
    skill = tmp_path / "profiles" / "crane" / "skills" / "inbox-triage"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# inbox-triage\n\nDo the thing.\n")
    spec_stamp._reset_for_tests()
    return tmp_path / "profiles"


def test_a_spec_installed_after_boot_reaches_the_skill_without_a_reboot(
    profiles, tmp_path, monkeypatch
):
    """THE REGRESSION. This is the whole defect, in one assertion.

    The skill starts unstamped — a Machine that booted with no specs. A spec is
    then installed under it, exactly as the running poller does. Without the
    refresh, the model is never told, and the promise silently becomes
    'effective next reboot'.
    """
    skill_md = profiles / "crane" / "skills" / "inbox-triage" / "SKILL.md"
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))

    assert spec_stamp.SPEC_STAMP_BEGIN not in skill_md.read_text()

    _write_manifest(spec_dir, BODY)
    changed = spec_stamp.refresh_profile_stamps(profiles)

    assert changed == 1
    text = skill_md.read_text()
    assert spec_stamp.SPEC_STAMP_BEGIN in text
    assert "`staff`" in text
    assert "classes/staff/voice.md" in text
    # The pointer names the file and its root-recorded digest — never the prose.
    assert "Decompose every number" not in text


def test_refresh_is_free_when_nothing_moved(profiles, tmp_path, monkeypatch):
    """Runs on every turn, so the common path must cost one manifest read."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))
    _write_manifest(spec_dir, BODY)

    assert spec_stamp.refresh_profile_stamps(profiles) == 1
    assert spec_stamp.refresh_profile_stamps(profiles) == 0
    assert spec_stamp.refresh_profile_stamps(profiles) == 0


def test_a_replaced_body_re_renders_the_pointer(profiles, tmp_path, monkeypatch):
    """The stamp carries the digest, so a swapped body must re-render.

    A same-path body swap is the ordinary shape of a client EDITING their spec.
    A fingerprint keyed only on paths would call that unchanged and leave a
    stamp asserting a hash the file no longer has.
    """
    skill_md = profiles / "crane" / "skills" / "inbox-triage" / "SKILL.md"
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))

    _write_manifest(spec_dir, BODY)
    spec_stamp.refresh_profile_stamps(profiles)
    first = skill_md.read_text()

    _write_manifest(spec_dir, OTHER)
    assert spec_stamp.refresh_profile_stamps(profiles) == 1
    assert skill_md.read_text() != first
    assert hashlib.sha256(OTHER.encode()).hexdigest()[:16] in skill_md.read_text()


def test_removing_every_spec_excises_the_pointer(profiles, tmp_path, monkeypatch):
    """A seat whose specs went away must lose its pointers, not keep a table of
    files that are gone — a pointer to a missing file is worse than none."""
    skill_md = profiles / "crane" / "skills" / "inbox-triage" / "SKILL.md"
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))

    _write_manifest(spec_dir, BODY)
    spec_stamp.refresh_profile_stamps(profiles)
    assert spec_stamp.SPEC_STAMP_BEGIN in skill_md.read_text()

    (spec_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "customer": "smd-staging", "specs": {}})
    )
    spec_stamp.refresh_profile_stamps(profiles)
    assert spec_stamp.SPEC_STAMP_BEGIN not in skill_md.read_text()


def test_refresh_never_stacks_across_repeated_manifest_changes(profiles, tmp_path, monkeypatch):
    """Non-stacking is load-bearing under the poller as it is under boot.

    The poller can fire many times between reboots. A stamp that appended
    rather than replaced would grow one copy per portal edit — a slow context
    flood nobody would attribute to a stamp.
    """
    skill_md = profiles / "crane" / "skills" / "inbox-triage" / "SKILL.md"
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(spec_dir))

    for body in (BODY, OTHER, BODY, OTHER):
        _write_manifest(spec_dir, body)
        spec_stamp.refresh_profile_stamps(profiles)

    assert skill_md.read_text().count(spec_stamp.SPEC_STAMP_BEGIN) == 1
    assert skill_md.read_text().count(spec_stamp.SPEC_STAMP_END) == 1


def test_a_missing_profiles_root_is_survivable(tmp_path, monkeypatch):
    """Delivery is best-effort; the send-site gate is the guarantee. A wrong or
    absent path yields zero refreshed files, never a raised turn."""
    spec_stamp._reset_for_tests()
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path / "specs"))
    assert spec_stamp.refresh_profile_stamps(tmp_path / "nope") == 0
