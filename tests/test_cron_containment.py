"""Sentinel helpers for the durable cron containment lever (ss-console#2276)."""

from __future__ import annotations

from shared.cron_containment import containment_active, containment_reason, sentinel_path


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
