"""Tests for the agent-side connector-health plugin (post_tool_call handler).

The tool→server mapping lives in Hermes (``tools.mcp_tool``), which is not
installed in the overlay test env — a fake module is injected into
``sys.modules``. This doubles as the pin-bump tripwire's complement: the
IMPORT-FAILED path must flag the ledger so the console pages the dark
window rather than the alert class dying silently.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[1] / "plugins" / "hermes-smd-connector-health" / "__init__.py"
)
_spec = importlib.util.spec_from_file_location("hermes_smd_connector_health", _PLUGIN_PATH)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)


@pytest.fixture(autouse=True)
def _ledger_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", str(tmp_path / "ledger.json"))
    # Reset the module's one-shot latches between tests.
    plugin._MAPPING_BROKEN = False
    plugin._UNMAPPED_WARNED.clear()
    plugin._SURFACE_SWEPT = False
    return tmp_path / "ledger.json"


@pytest.fixture
def fake_mapping(monkeypatch):
    """Install a fake tools.mcp_tool with a server-name mapping."""
    tools_pkg = types.ModuleType("tools")
    mcp_tool = types.ModuleType("tools.mcp_tool")
    mcp_tool._mcp_tool_server_names = {
        "mcp_smokeball_get_matter": "smokeball",
        "mcp_msgraph_mail_list_messages": "msgraph_mail",
    }
    tools_pkg.mcp_tool = mcp_tool
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_tool)
    return mcp_tool


def _servers(path):
    return json.loads(path.read_text(encoding="utf-8"))["servers"]


def test_error_on_mapped_tool_is_counted_with_conn_class(fake_mapping, _ledger_in_tmp):
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="error",
        error_type="tool_error",
        error_message="Smokeball GET /matters -> HTTP 401: (empty body)",
    )
    entry = _servers(_ledger_in_tmp)["smokeball"]
    assert entry["consecutive_failures"] == 1
    assert "last_conn_error_ts" in entry


def test_business_error_counts_without_conn_evidence(fake_mapping, _ledger_in_tmp):
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="error",
        error_type="tool_error",
        error_message="Smokeball GET /matters/x -> HTTP 404: matter not found",
    )
    entry = _servers(_ledger_in_tmp)["smokeball"]
    assert entry["consecutive_failures"] == 1
    assert "last_conn_error_ts" not in entry


def test_ok_resets_the_run(fake_mapping, _ledger_in_tmp):
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="error",
        error_type="tool_error",
        error_message="boom",
    )
    plugin.on_post_tool_call(tool_name="mcp_smokeball_get_matter", status="ok")
    entry = _servers(_ledger_in_tmp)["smokeball"]
    assert entry["consecutive_failures"] == 0


def test_plugin_block_is_policy_not_outage(fake_mapping, _ledger_in_tmp):
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="error",
        error_type="plugin_block",
        error_message="blocked by trust",
    )
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="blocked",
        error_type="plugin_block",
    )
    assert not _ledger_in_tmp.exists()


def test_non_mcp_tools_are_ignored(fake_mapping, _ledger_in_tmp):
    plugin.on_post_tool_call(tool_name="execute_code", status="error", error_type="tool_error")
    assert not _ledger_in_tmp.exists()


def test_unmapped_mcp_tool_is_not_counted_no_prefix_parse(fake_mapping, _ledger_in_tmp):
    # NO prefix-parse fallback by design: a misparse (msgraph vs msgraph_mail
    # is ambiguous) would mint a phantom-key alert with no path to RECOVERED.
    plugin.on_post_tool_call(
        tool_name="mcp_brandnew_server_tool",
        status="error",
        error_type="tool_error",
        error_message="boom",
    )
    assert not _ledger_in_tmp.exists()


def test_mapping_import_failure_flags_ledger_for_paging(_ledger_in_tmp, monkeypatch):
    # No fake module installed and hermes absent → import fails → the ledger
    # is flagged mapping_ok=False so connector_check reports check-not-ok and
    # the console PAGES the dark window.
    monkeypatch.delitem(sys.modules, "tools.mcp_tool", raising=False)
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        status="error",
        error_type="tool_error",
        error_message="boom",
    )
    doc = json.loads(_ledger_in_tmp.read_text(encoding="utf-8"))
    assert doc["mapping_ok"] is False

    from shared.connector_check import check

    assert check().ok is False


def test_handler_never_raises(fake_mapping, _ledger_in_tmp, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(plugin, "record_call", boom)
    # Must swallow — health capture never breaks the agent turn.
    plugin.on_post_tool_call(tool_name="mcp_smokeball_get_matter", status="ok")


# ---------------------------------------------------------------------------
# Tool-surface sweep (ss-console#2845)
#
# The sweep is the instrument that was missing when two AgentMail READ verbs
# sat unclassified — and therefore REFUSED on every call — while a static pin
# in tests/test_tool_classification_completeness.py asserted the surface was
# fully decided. These tests exist so the sweep cannot become the same kind of
# check: one proves it NAMES drift, one proves it reports a clean surface out
# loud rather than silently, and one proves silence means it never ran. If the
# clean case said nothing, "swept and clean" and "never swept" would be
# identical from outside, which is the entire defect class.
# ---------------------------------------------------------------------------


def _sweep_with(monkeypatch, mapping):
    """Run one sweep against ``mapping`` through the real code path."""
    tools_pkg = types.ModuleType("tools")
    mcp_tool = types.ModuleType("tools.mcp_tool")
    mcp_tool._mcp_tool_server_names = mapping
    tools_pkg.mcp_tool = mcp_tool
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_tool)
    plugin._resolve_server(next(iter(mapping), "mcp_nothing_at_all"))


def _sweep_lines(caplog):
    return [r for r in caplog.records if "TOOL SURFACE SWEEP" in r.getMessage()]


def test_sweep_names_an_unclassified_registered_tool(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _sweep_with(
        monkeypatch,
        {
            "mcp_agentmail_get_thread": "agentmail",
            "mcp_agentmail_definitely_not_classified": "agentmail",
        },
    )
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, f"expected one drift ERROR, got {[r.getMessage() for r in errors]}"
    text = errors[0].getMessage()
    assert "mcp_agentmail_definitely_not_classified" in text
    assert "1 unclassified" in text
    # The classified sibling must NOT be named, or the sweep is only reciting
    # the surface back at us.
    assert "mcp_agentmail_get_thread" not in text


def test_sweep_reports_a_clean_surface_out_loud_and_without_erroring(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _sweep_with(monkeypatch, {"mcp_agentmail_get_thread": "agentmail"})
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    clean = _sweep_lines(caplog)
    assert len(clean) == 1, "a clean sweep must still say so — silence is the failure mode"
    assert "0 unclassified" in clean[0].getMessage()


def test_sweep_canonicalizes_the_wire_form_before_judging(monkeypatch, caplog):
    """v0.19 spells tools ``mcp__server__tool``; the policy map is single-underscore.

    Without canonicalization every tool on a v0.19 seat reads as unclassified,
    which is the 2026-08-20 incident inverted into a false alarm.
    """
    caplog.set_level(logging.DEBUG)
    _sweep_with(monkeypatch, {"mcp__agentmail__get_thread": "agentmail"})
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_sweep_does_not_latch_on_an_empty_mapping(monkeypatch, caplog):
    """MCP servers register after plugin load; an empty dict means 'too early'."""
    caplog.set_level(logging.DEBUG)
    _sweep_with(monkeypatch, {})
    assert _sweep_lines(caplog) == []
    assert plugin._SURFACE_SWEPT is False, "an empty mapping must not burn the one shot"


def test_sweep_runs_once_per_process(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    mapping = {"mcp_agentmail_still_not_classified": "agentmail"}
    _sweep_with(monkeypatch, mapping)
    _sweep_with(monkeypatch, mapping)
    assert len(_sweep_lines(caplog)) == 1


@pytest.mark.parametrize("tool", ["mcp_agentmail_get_message", "mcp_agentmail_search_inboxes"])
def test_the_two_verbs_this_shipped_with_are_decided_everywhere(tool: str) -> None:
    """Regression pin for the specific drift that prompted the sweep.

    Found on a live seat 2026-09-18: both were offered by AgentMail, absent
    from every policy table, and therefore REFUSED on every call. A READ tool
    has to land on THREE tables, and landing on one of them is the half-wired
    state the sibling completeness tests exist to catch.
    """
    from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
    from shared.provenance import TENANT_SOURCE_READ_TOOLS

    assert TOOL_ACTION_CLASS_MAP.get(tool) is ActionClass.READ
    assert tool in TENANT_SOURCE_READ_TOOLS
