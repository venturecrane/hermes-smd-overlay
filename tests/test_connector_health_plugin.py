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
