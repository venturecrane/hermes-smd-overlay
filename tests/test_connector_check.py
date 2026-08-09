"""Tests for the connector-health self-check reader (shared/connector_check.py)."""

from __future__ import annotations

import json

import pytest

from shared import connector_ledger as cl
from shared.connector_check import check


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", str(tmp_path / "ledger.json"))
    return tmp_path / "ledger.json"


def test_missing_ledger_with_dir_present_is_legit_empty(_ledger_in_tmp):
    # Fresh boot / no MCP call yet, boot-created dir present: check ok, empty
    # map — the console holds any open alert (absence never resolves) but
    # nothing pages.
    result = check(now=1000.0)
    assert result.ok is True
    assert result.servers == {}


def test_missing_ledger_DIR_is_check_not_ok(tmp_path, monkeypatch):
    # 2026-07-25 smd-staging live finding: dir never boot-created → every
    # record_call silently failed → a real 401 outage read legit-empty green.
    # A missing DIR means the writer cannot possibly record: page, not hold.
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", str(tmp_path / "no-such-dir" / "ledger.json"))
    result = check(now=1000.0)
    assert result.ok is False
    assert result.servers is None


def test_corrupt_ledger_is_check_not_ok(_ledger_in_tmp):
    _ledger_in_tmp.write_text("{definitely not json", encoding="utf-8")
    result = check(now=1000.0)
    assert result.ok is False
    assert result.servers is None  # never emit a map you cannot trust


def test_wrong_shape_is_check_not_ok(_ledger_in_tmp):
    _ledger_in_tmp.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert check(now=1000.0).ok is False


def test_mapping_broken_is_check_not_ok(_ledger_in_tmp):
    # Pin bump moved _mcp_tool_server_names: nothing is being counted, so
    # the whole class must PAGE (connector_check_error), not go dark.
    cl.record_call("smokeball", ok=True, now=100.0)
    cl.mark_mapping_broken()
    result = check(now=1000.0)
    assert result.ok is False
    assert result.servers is None


def test_failure_run_entry_shape_with_writer_side_ages(_ledger_in_tmp):
    cl.record_call("smokeball", ok=True, now=100.0)
    cl.record_call(
        "smokeball", ok=False, error_message="GET /m -> HTTP 401: x", conn_class=True, now=700.0
    )
    cl.record_call(
        "smokeball", ok=False, error_message="GET /m -> HTTP 401: x", conn_class=True, now=760.0
    )
    result = check(now=1060.0)
    assert result.ok is True
    entry = result.servers["smokeball"]
    assert entry["consecutive_failures"] == 2
    assert entry["run_age_seconds"] == 360  # 1060 - 700, stamped writer-side
    assert entry["conn_evidence"] is True
    assert entry["last_ok_age_seconds"] == 960
    assert entry["last_error_age_seconds"] == 300
    assert entry["last_error_message"] == "GET /m -> HTTP 401: x"


def test_business_only_run_has_no_conn_evidence(_ledger_in_tmp):
    cl.record_call(
        "smokeball", ok=False, error_message="HTTP business", conn_class=False, now=700.0
    )
    entry = check(now=1000.0).servers["smokeball"]
    assert entry["conn_evidence"] is False


def test_conn_evidence_from_previous_run_does_not_carry(_ledger_in_tmp):
    # Run 1 had conn evidence; a success ended it; run 2 is business-only.
    cl.record_call("smokeball", ok=False, error_message="x", conn_class=True, now=100.0)
    cl.record_call("smokeball", ok=True, now=200.0)
    cl.record_call("smokeball", ok=False, error_message="business", conn_class=False, now=300.0)
    entry = check(now=400.0).servers["smokeball"]
    assert entry["consecutive_failures"] == 1
    assert entry["conn_evidence"] is False  # last_conn_error_ts was cleared


def test_healthy_entry_has_no_run_fields(_ledger_in_tmp):
    cl.record_call("agentmail", ok=True, now=100.0)
    entry = check(now=400.0).servers["agentmail"]
    assert entry["consecutive_failures"] == 0
    assert "run_age_seconds" not in entry
    assert "conn_evidence" not in entry
    assert entry["last_ok_age_seconds"] == 300


def test_malformed_entry_is_dropped_siblings_kept(_ledger_in_tmp):
    cl.record_call("agentmail", ok=True, now=100.0)
    doc = json.loads(_ledger_in_tmp.read_text(encoding="utf-8"))
    doc["servers"]["junk"] = {"consecutive_failures": "seven"}
    doc["servers"]["junk2"] = "not a dict"
    _ledger_in_tmp.write_text(json.dumps(doc), encoding="utf-8")
    result = check(now=400.0)
    assert result.ok is True
    assert set(result.servers) == {"agentmail"}


def test_failure_run_with_unparseable_start_is_dropped(_ledger_in_tmp):
    # An age-gated condition cannot be evaluated without a run start; drop
    # (hold) rather than guess.
    cl.record_call("smokeball", ok=False, now=100.0)
    doc = json.loads(_ledger_in_tmp.read_text(encoding="utf-8"))
    doc["servers"]["smokeball"]["first_error_ts"] = "not-a-number"
    _ledger_in_tmp.write_text(json.dumps(doc), encoding="utf-8")
    assert check(now=400.0).servers == {}


def test_clock_regression_clamps_age_to_zero(_ledger_in_tmp):
    cl.record_call("smokeball", ok=False, now=1000.0)
    entry = check(now=900.0).servers["smokeball"]  # reader clock behind writer
    assert entry["run_age_seconds"] == 0


# ---------------------------------------------------------------------------
# token_ages (ss#2148) — durable-credential age for pre-expiry alerting
# ---------------------------------------------------------------------------


def test_token_ages_reports_file_age(tmp_path, monkeypatch):
    import os

    from shared.connector_check import token_ages

    token = tmp_path / "refresh_token"
    token.write_text("not-a-real-value")
    os.utime(token, (1000.0, 1000.0))
    monkeypatch.setenv("SMOKEBALL_REFRESH_TOKEN_FILE", str(token))
    ages = token_ages(now=1600.0)
    assert ages == {"smokeball": 600}


def test_token_ages_missing_file_reports_nothing(tmp_path, monkeypatch):
    # Absence is "nothing to report" (the console holds), never zero — a zero
    # would read as freshly-rotated on a seat that has never connected.
    from shared.connector_check import token_ages

    monkeypatch.setenv("SMOKEBALL_REFRESH_TOKEN_FILE", str(tmp_path / "absent"))
    assert token_ages(now=1600.0) == {}


def test_token_ages_clock_regression_clamps_to_zero(tmp_path, monkeypatch):
    import os

    from shared.connector_check import token_ages

    token = tmp_path / "refresh_token"
    token.write_text("x")
    os.utime(token, (2000.0, 2000.0))
    monkeypatch.setenv("SMOKEBALL_REFRESH_TOKEN_FILE", str(token))
    assert token_ages(now=1000.0) == {"smokeball": 0}


def test_token_ages_never_synthesizes_health_entries(tmp_path, monkeypatch):
    # The whole reason this is a separate field: a token file's existence must
    # not create a servers-map entry (a fabricated consecutive_failures=0
    # would falsely RESOLVE an open connector_down alert).
    from shared.connector_check import token_ages

    token = tmp_path / "refresh_token"
    token.write_text("x")
    monkeypatch.setenv("SMOKEBALL_REFRESH_TOKEN_FILE", str(token))
    result = check(now=1000.0)
    assert result.servers == {}  # no ledger writes → no health entries
    assert "smokeball" in token_ages(now=1000.0)
