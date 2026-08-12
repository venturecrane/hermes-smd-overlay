"""Sentinel helpers for the durable cron containment lever (ss-console#2276)."""

from __future__ import annotations

from pathlib import Path

from shared.cron_containment import (
    containment_active,
    containment_reason,
    containment_state,
    sentinel_path,
)


def test_sentinel_path_prefers_arg_then_env_then_default(monkeypatch):
    assert str(sentinel_path("/x")) == "/x/CRON_CONTAINMENT"
    monkeypatch.setenv("HERMES_HOME", "/y")
    assert str(sentinel_path()) == "/y/CRON_CONTAINMENT"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert str(sentinel_path()) == "/opt/data/CRON_CONTAINMENT"


def test_active_and_reason_roundtrip(tmp_path):
    assert containment_active(str(tmp_path)) is False
    assert containment_reason(str(tmp_path)) == ""
    f = tmp_path / "CRON_CONTAINMENT"
    f.write_text("ss#2258: fabricated sends, seat contained\nsecond line ignored\n")
    assert containment_active(str(tmp_path)) is True
    assert containment_reason(str(tmp_path)) == "ss#2258: fabricated sends, seat contained"


def test_empty_sentinel_is_active_with_blank_reason(tmp_path):
    (tmp_path / "CRON_CONTAINMENT").write_text("")
    assert containment_active(str(tmp_path)) is True
    assert containment_reason(str(tmp_path)) == ""


def test_directory_named_like_sentinel_is_not_active(tmp_path):
    (tmp_path / "CRON_CONTAINMENT").mkdir()
    assert containment_active(str(tmp_path)) is False


# ---------------------------------------------------------------------------
# tri-state read (ss-console#2291)
# ---------------------------------------------------------------------------


def test_state_is_a_real_bool_when_the_volume_is_readable(tmp_path):
    assert containment_state(str(tmp_path)) is False
    (tmp_path / "CRON_CONTAINMENT").write_text("contained\n")
    assert containment_state(str(tmp_path)) is True


def test_state_is_unknown_when_the_sentinel_cannot_be_read(tmp_path, monkeypatch):
    """A permission error is not evidence of absence. Reporting it as False
    would publish a contained seat as a normal one (ss-console#2276)."""

    def _denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", _denied)
    assert containment_state(str(tmp_path)) is None


def test_state_is_unknown_when_the_home_is_absent(tmp_path):
    assert containment_state(str(tmp_path / "not-mounted")) is None


def test_active_collapses_unknown_to_false_for_bootstrap(tmp_path, monkeypatch):
    """Bootstrap's fail-open contract is deliberate and unchanged: it must
    keep getting a bool, and must not treat 'could not tell' as contained."""

    def _denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", _denied)
    assert containment_active(str(tmp_path)) is False
    assert containment_active(str(tmp_path / "not-mounted")) is False
