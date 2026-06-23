"""Author-built MCP connector platform — overlay-side governance proof (ADR 0053).

These tests pin the two trust facts the overlay owns for the synthetic reference
connector: the registry wiring (absolute venv command + auth_model) and the
classification (echo/record decided under their RUNTIME-prefixed names; surprise
REFUSED). The completeness gate in test_tool_classification_completeness.py
already forces every registered connector to have a classified surface and pins
the reference surface for drift defense; this file adds the positive/negative
classification assertions that are the platform's whole point.

The manifest<=map cross-check (the connector's manifest.toml lives in ss-console)
is NOT here — it cannot be, this repo does not have the manifest. It is enforced
where both artifacts are present: at boot-smoke on a live Machine, and in
ss-console CI by fetching the pinned overlay map (verify-overlay-pairs.py
pattern). See ADR 0053.
"""

from __future__ import annotations

from bootstrap.mcp_registry import MCP_CONNECTOR_REGISTRY, McpConnectorSpec
from shared.action_classes import ActionClass, classify_tool


def test_mcp_connector_spec_has_auth_model_field() -> None:
    # Additive field; existing vendor entries default to None.
    spec = McpConnectorSpec(name="x")
    assert spec.auth_model is None


def test_reference_registry_entry_wiring() -> None:
    spec = MCP_CONNECTOR_REGISTRY["reference"]
    assert spec.auth_model == "static"
    assert spec.transport == "stdio"
    # Launched by the ABSOLUTE path to its own venv console-script — no PATH
    # lookup, no Hermes-venv coupling.
    assert spec.command == "/opt/connectors/_reference/.venv/bin/reference-mcp"
    assert ("REFERENCE_API_KEY", "REFERENCE_API_KEY") in spec.env_secrets


def test_reference_classified_tools_decided_under_runtime_names() -> None:
    echo = classify_tool("mcp_reference_echo")
    assert echo.action_class is ActionClass.READ
    assert echo.unmapped is False

    record = classify_tool("mcp_reference_record")
    assert record.action_class is ActionClass.INTERNAL_WRITE
    assert record.unmapped is False


def test_reference_surprise_is_fail_closed_refused() -> None:
    # The fixture's whole point: a live tool that nothing classifies must fall to
    # the fail-closed terminal class and never execute — even though it really is
    # exposed by the running connector.
    verdict = classify_tool("mcp_reference_surprise")
    assert verdict.action_class is ActionClass.REFUSED
    assert verdict.unmapped is True
