"""Authored-spec control self-check (ss-console #2234).

The alarm half. ``shared.spec_gate`` already notices a broken control at the
send site and writes an audit row, but that is a record, not an alert: it fires
only when something happens to send, and it lands in a per-seat SQLite file
nobody watches. On ``pilot-smokeball`` it accumulated for six days while the
firm's mail quietly stopped. This check runs on the heartbeat instead, so a
broken control is reported whether or not the seat is busy.

The tests below pin the three-way outcome that makes the alert trustworthy:

* a real empty map (checked, nothing broken) — the state that RESOLVES an alert;
* a real broken map (declared, affirmatively not installed);
* ``ok=False`` (cannot read the config or the manifest), which must never be
  reported as a missing spec. "The firm never installed it" and "we could not
  look" produce identical emptiness and want opposite responses.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared import spec_control_check, spec_manifest


def _declare(monkeypatch, declaration):
    class FakeConfig:
        output_classes = declaration

        @classmethod
        def from_volume(cls):
            return cls()

    monkeypatch.setattr(spec_control_check, "CustomerConfig", FakeConfig)


def _tree(tmp_path, specs, *, manifest=None):
    if manifest is not None:
        (tmp_path / "manifest.json").write_text(manifest)
        return tmp_path
    entries = {}
    for rel, (output_class, prop) in specs.items():
        body = f"authored {output_class} {prop}\n"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        entries[rel] = {
            "class": output_class,
            "property": prop,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "bytes": len(body),
        }
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "customer": "s", "specs": entries})
    )
    return tmp_path


def test_a_seat_that_declares_nothing_is_healthy_and_empty(monkeypatch, tmp_path):
    """Most seats. An empty map is a REAL state — checked, nothing to report —
    and distinct from ok=False, which means we could not look."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {})
    result = spec_control_check.check()
    assert result.ok is True
    assert result.entries == {}


def test_declared_and_installed_reports_installed_true(monkeypatch, tmp_path):
    """The healthy declared case — and the state that must RESOLVE an open
    alert, which is why the entry ships rather than being omitted."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV,
        str(_tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})),
    )
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})
    result = spec_control_check.check()
    assert result.ok is True
    assert result.entries == {"staff.voice": {"declared": True, "installed": True}}


def test_declared_and_missing_reports_installed_false(monkeypatch, tmp_path):
    """The pilot-smokeball state on 2026-08-10."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})
    result = spec_control_check.check()
    assert result.ok is True
    assert result.entries == {"staff.voice": {"declared": True, "installed": False}}


def test_properties_are_keyed_separately(monkeypatch, tmp_path):
    """A seat can have staff.voice installed and staff.format missing.
    Resolving one must not clear the alert on the other, so the key is per
    (class, property) — never per class."""
    monkeypatch.setenv(
        spec_manifest.SPEC_DIR_ENV,
        str(_tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})),
    )
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "expected"}})
    result = spec_control_check.check()
    assert result.entries == {
        "staff.format": {"declared": True, "installed": False},
        "staff.voice": {"declared": True, "installed": True},
    }


def test_a_spec_whose_bytes_no_longer_verify_counts_as_not_installed(monkeypatch, tmp_path):
    """A tampered spec is not a working control. The gate refuses on it; the
    alert must not report it as healthy."""
    tree = _tree(tmp_path, {"classes/staff/voice.md": ("staff", "voice")})
    (tree / "classes/staff/voice.md").write_text("rewritten\n")
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tree))
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})
    assert spec_control_check.check().entries == {
        "staff.voice": {"declared": True, "installed": False}
    }


def test_none_declarations_are_not_gaps(monkeypatch, tmp_path):
    """``none`` is an authored choice that no spec is expected — the opposite of
    a broken control, and it must not page anyone."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(monkeypatch, {"staff": {"voice_spec": "none", "format_spec": "none"}})
    result = spec_control_check.check()
    assert result.ok is True
    assert result.entries == {}


def test_an_unreadable_manifest_pages_rather_than_claiming_nothing_is_installed(
    monkeypatch, tmp_path
):
    """The load-bearing one. A declaring seat that cannot read its manifest must
    report ok=False, NOT a map full of installed:false — the latter would blame
    the firm for our own blindness, and would resolve the moment we regained
    sight rather than when a spec actually landed."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {}, manifest="{not json")))
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})
    result = spec_control_check.check()
    assert result.ok is False
    assert result.entries is None


def test_an_unresolvable_spec_dir_pages_too(monkeypatch):
    monkeypatch.delenv(spec_manifest.SPEC_DIR_ENV, raising=False)
    _declare(monkeypatch, {"staff": {"voice_spec": "expected", "format_spec": "none"}})
    result = spec_control_check.check()
    assert result.ok is False
    assert result.entries is None


def test_an_unreadable_config_pages(monkeypatch, tmp_path):
    """Unconfirmed is not "declares nothing"."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))

    class Exploding:
        @classmethod
        def from_volume(cls):
            raise RuntimeError("volume unreadable")

    monkeypatch.setattr(spec_control_check, "CustomerConfig", Exploding)
    result = spec_control_check.check()
    assert result.ok is False
    assert result.entries is None


def test_a_malformed_class_block_is_dropped_not_guessed(monkeypatch, tmp_path):
    """One bad block does not discredit its siblings — the same
    drop-the-entry-keep-the-neighbours rule the connector map follows."""
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(_tree(tmp_path, {})))
    _declare(
        monkeypatch,
        {"staff": {"voice_spec": "expected"}, "outbound_client": "not-a-mapping"},
    )
    result = spec_control_check.check()
    assert result.ok is True
    assert result.entries == {"staff.voice": {"declared": True, "installed": False}}


@pytest.mark.parametrize("boom", [KeyError("x"), RuntimeError("x")])
def test_an_internal_fault_pages_rather_than_going_dark(monkeypatch, boom):
    """A check that crashes must report itself broken, never return healthy."""

    def explode(*_a, **_k):
        raise boom

    monkeypatch.setattr(spec_control_check, "_declared_properties", explode)
    result = spec_control_check.check()
    assert result.ok is False
    assert result.entries is None
