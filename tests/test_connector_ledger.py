"""Tests for the per-server connector call-outcome ledger (shared/connector_ledger.py)."""

from __future__ import annotations

import json

import pytest

from shared import connector_ledger as cl


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", str(tmp_path / "ledger.json"))
    return tmp_path / "ledger.json"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_failure_starts_a_run_and_sets_first_error_ts(_ledger_in_tmp):
    assert cl.record_call(
        "smokeball", ok=False, error_message="x -> HTTP 401: y", conn_class=True, now=100.0
    )
    entry = _read(_ledger_in_tmp)["servers"]["smokeball"]
    assert entry["consecutive_failures"] == 1
    assert entry["first_error_ts"] == 100.0
    assert entry["last_error_ts"] == 100.0
    assert entry["last_conn_error_ts"] == 100.0
    assert entry["last_error_message"] == "x -> HTTP 401: y"


def test_consecutive_failures_increment_but_first_error_ts_is_stable(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, now=100.0)
    cl.record_call("smokeball", ok=False, now=160.0)
    cl.record_call("smokeball", ok=False, now=220.0)
    entry = _read(_ledger_in_tmp)["servers"]["smokeball"]
    assert entry["consecutive_failures"] == 3
    assert entry["first_error_ts"] == 100.0  # run start, not the latest error
    assert entry["last_error_ts"] == 220.0


def test_non_conn_failure_does_not_touch_last_conn_error_ts(_ledger_in_tmp):
    cl.record_call(
        "smokeball", ok=False, error_message="HTTP 404-ish business", conn_class=False, now=100.0
    )
    entry = _read(_ledger_in_tmp)["servers"]["smokeball"]
    assert "last_conn_error_ts" not in entry


def test_success_resets_run_but_keeps_the_key(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, error_message="boom", conn_class=True, now=100.0)
    cl.record_call("smokeball", ok=True, now=200.0)
    servers = _read(_ledger_in_tmp)["servers"]
    assert "smokeball" in servers  # resolve-on-proven-success needs the key
    entry = servers["smokeball"]
    assert entry["consecutive_failures"] == 0
    assert entry["last_ok_ts"] == 200.0
    assert "first_error_ts" not in entry
    assert "last_conn_error_ts" not in entry
    # Historical fields survive for the admin staleness display.
    assert entry["last_error_ts"] == 100.0
    assert entry["last_error_message"] == "boom"


def test_new_run_after_success_gets_fresh_first_error_ts(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, now=100.0)
    cl.record_call("smokeball", ok=True, now=200.0)
    cl.record_call("smokeball", ok=False, now=300.0)
    entry = _read(_ledger_in_tmp)["servers"]["smokeball"]
    assert entry["consecutive_failures"] == 1
    assert entry["first_error_ts"] == 300.0


def test_error_message_truncated_at_write(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, error_message="x" * 1000, now=100.0)
    entry = _read(_ledger_in_tmp)["servers"]["smokeball"]
    assert len(entry["last_error_message"]) == cl.MAX_ERROR_CHARS


def test_servers_are_independent(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, now=100.0)
    cl.record_call("agentmail", ok=True, now=100.0)
    servers = _read(_ledger_in_tmp)["servers"]
    assert servers["smokeball"]["consecutive_failures"] == 1
    assert servers["agentmail"]["consecutive_failures"] == 0


def test_corrupt_ledger_is_replaced_not_fatal(_ledger_in_tmp):
    _ledger_in_tmp.write_text("{not json", encoding="utf-8")
    assert cl.record_call("smokeball", ok=True, now=100.0)
    assert _read(_ledger_in_tmp)["servers"]["smokeball"]["consecutive_failures"] == 0


def test_mark_mapping_broken_preserves_servers(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, now=100.0)
    assert cl.mark_mapping_broken()
    doc = _read(_ledger_in_tmp)
    assert doc["mapping_ok"] is False
    assert "smokeball" in doc["servers"]


def test_eviction_sheds_never_errored_healthy_entries_first(_ledger_in_tmp):
    # Fill the cap with healthy never-errored entries...
    for i in range(cl.MAX_SERVERS):
        cl.record_call(f"healthy-{i}", ok=True, now=float(i))
    # ...then record a failing server and a recently-recovered server.
    cl.record_call("failing", ok=False, error_message="down", conn_class=True, now=1000.0)
    cl.record_call("recovered", ok=False, now=1100.0)
    cl.record_call("recovered", ok=True, now=1200.0)
    servers = _read(_ledger_in_tmp)["servers"]
    assert len(servers) == cl.MAX_SERVERS
    # The alerting-relevant entries survive; healthy cruft was shed.
    assert "failing" in servers  # an open alert depends on this key
    assert "recovered" in servers  # an open alert resolves via this key
    # Two healthy never-errored entries were shed to make room (which two is
    # tie-order and irrelevant — only the alerting entries are load-bearing).
    healthy_kept = [name for name in servers if name.startswith("healthy-")]
    assert len(healthy_kept) == cl.MAX_SERVERS - 2


def test_rejects_empty_server_name(_ledger_in_tmp):
    assert cl.record_call("", ok=True) is False
    assert not _ledger_in_tmp.exists()
