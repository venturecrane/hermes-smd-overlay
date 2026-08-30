"""Contract test: overlay-registered tools must reach the model with a real
parameter schema.

Regression guard for the empty-parameter-schema bug (2026-06-20): every overlay
plugin registered its tools with a *bare* JSON-schema object
(``{"type": "object", "properties": {...}}``) while Hermes core reads the
parameters from ``schema["parameters"]["properties"]`` (``model_tools.py:560``)
and ``get_definitions`` emits ``{"type": "function", "function": {**entry.schema,
"name": ...}}`` without spreading the description. The result: all 19 overlay
tools were advertised to the model with empty parameters — the worker could see
the names but could not pass ``query`` / ``message_id`` / ``mailbox``.

:func:`shared.tool_registration.register_wrapped_tool` is the single chokepoint
that wraps every overlay schema into the OpenAI function shape. These tests pin
that contract two ways:

* **Layer 1 (always runs):** load each plugin with a fake ctx and assert every
  registered tool's schema is function-shaped (non-empty ``parameters.properties``
  + a description), independent of Hermes being installed.
* **Layer 2 (skips when Hermes absent):** run a wrapped registration through the
  *real* ``tools.registry`` and assert the emitted model-facing function dict
  carries non-empty ``parameters.properties``. This pins the actual core contract,
  not a self-consistent model of it. It is expected to skip in overlay CI (Hermes
  lives on the Machine image); there the staging ``tools[]`` pull is the gate.
"""

from __future__ import annotations

import pytest

from shared.tool_registration import register_wrapped_tool

from .conftest import load_plugin


class _RecordingCtx:
    """Captures register_tool/register_hook like Hermes' PluginContext."""

    def __init__(self) -> None:
        self.hooks: dict = {}
        self.tools: dict = {}

    def register_hook(self, name, callback) -> None:
        self.hooks.setdefault(name, []).append(callback)

    def register_tool(self, **kwargs) -> None:
        self.tools[kwargs["name"]] = kwargs


# Plugins whose register() routes through register_wrapped_tool, and the tool
# count each must surface (guards against a plugin silently dropping tools).
_PLUGINS = {
    "hermes-smd-workspace": 18,
    "hermes-smd-peer-memory": 1,
    "hermes-smd-jobs": None,  # count not pinned (preventive); just must be > 0
    "hermes-smd-medchron": 3,  # ss-console #2614: submit / status / allowance
}


def _registered_tools(plugin_name: str) -> dict:
    mod = load_plugin(plugin_name)
    ctx = _RecordingCtx()
    mod.register(ctx)
    return ctx.tools


# ---------------------------------------------------------------------------
# Layer 1 — function-shape contract, no Hermes required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plugin_name,expected_count", _PLUGINS.items())
def test_every_registered_tool_is_function_shaped(plugin_name, expected_count):
    tools = _registered_tools(plugin_name)
    assert tools, f"{plugin_name} registered no tools"
    if expected_count is not None:
        assert len(tools) == expected_count, (
            f"{plugin_name} registered {len(tools)} tools, expected {expected_count}"
        )

    for name, kwargs in tools.items():
        schema = kwargs["schema"]
        # Must be the OpenAI function shape Hermes serializes from.
        assert "parameters" in schema, f"{name}: schema has no top-level 'parameters'"
        params = schema["parameters"]
        assert isinstance(params, dict), f"{name}: parameters is not a dict"
        props = params.get("properties")
        assert props, f"{name}: parameters.properties is empty"
        # Description must live inside the schema (get_definitions does not
        # spread entry.description into the model-facing function dict).
        assert schema.get("description"), f"{name}: schema carries no description"
        # Every required field must be a declared property.
        for req in params.get("required", []):
            assert req in props, f"{name}: required '{req}' missing from properties"


def test_workspace_gmail_tools_expose_their_params():
    tools = _registered_tools("hermes-smd-workspace")
    search = tools["workspace_gmail_search"]["schema"]["parameters"]["properties"]
    assert "query" in search and "mailbox" in search
    get = tools["workspace_gmail_get"]["schema"]["parameters"]["properties"]
    assert "message_id" in get and "mailbox" in get


# ---------------------------------------------------------------------------
# Layer 2 — real Hermes registry contract (skips when Hermes is not installed)
# ---------------------------------------------------------------------------


def test_real_registry_emits_non_empty_parameters():
    registry_mod = pytest.importorskip(
        "tools.registry",
        reason="Hermes core not importable in overlay CI; staging tools[] pull is the gate",
    )
    registry = registry_mod.registry

    probe_name = "smd_schema_contract_probe"
    bare_schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}, "mailbox": {"type": "string"}},
        "required": ["q"],
    }

    class _RealCtx:
        def register_tool(self, **kwargs):
            registry.register(**kwargs)

    try:
        register_wrapped_tool(
            _RealCtx(),
            name=probe_name,
            toolset="smd_contract_probe",
            schema=bare_schema,
            handler=lambda args, **_: "{}",
            description="probe tool for the schema contract test",
        )
        defs = registry.get_definitions({probe_name})
        assert defs, "real registry returned no definition for the probe tool"
        fn = defs[0]["function"]
        # This is exactly what was empty before the fix.
        assert fn["parameters"]["properties"].get("q"), (
            "real registry emitted empty parameters.properties — wrap did not reach the model path"
        )
        assert fn.get("description"), "real registry emitted no description"
    finally:
        deregister = getattr(registry, "deregister", None)
        if callable(deregister):
            deregister(probe_name)


def test_real_registry_dispatches_the_payload_positionally():
    """The dispatch contract itself, pinned where it can actually be falsified.

    ``tests/test_tool_handler_dispatch_contract.py`` asserts every overlay handler
    accepts ``handler(args, **kwargs)``, but it derives that shape from a Sentry
    stack frame — it is a model of Hermes, not an observation of it. If Hermes
    ever changed dispatch to, say, ``handler(**args)``, that guard would stay
    green while every tool broke.

    This closes the loop wherever Hermes IS importable: register a probe through
    the same chokepoint the plugins use, dispatch it, and assert the tool
    arguments arrived as a single positional dict. It skips in overlay CI (Hermes
    core is not installed there) and runs on a seat and in staging, which is the
    only place the premise is falsifiable at all.
    """
    registry_mod = pytest.importorskip(
        "tools.registry",
        reason="Hermes core not importable in overlay CI; staging tools[] pull is the gate",
    )
    registry = registry_mod.registry

    probe_name = "smd_dispatch_contract_probe"
    seen: list[tuple[tuple, dict]] = []

    class _RealCtx:
        def register_tool(self, **kwargs):
            registry.register(**kwargs)

    try:
        register_wrapped_tool(
            _RealCtx(),
            name=probe_name,
            toolset="smd_contract_probe",
            schema={"type": "object", "properties": {"q": {"type": "string"}}},
            handler=lambda *a, **k: seen.append((a, k)) or "{}",
            description="probe tool for the dispatch contract test",
        )
        registry.dispatch(probe_name, {"q": "hello"})

        assert seen, "the real registry never invoked the probe handler"
        positional, _keyword = seen[0]
        assert positional, (
            "Hermes dispatched with NO positional argument. The overlay's handler "
            "signatures and tests/test_tool_handler_dispatch_contract.py both assume "
            "handler(args, **kwargs); if that changed, they are now wrong."
        )
        assert positional[0] == {"q": "hello"}, (
            f"expected the tool arguments as the first positional dict, got {positional[0]!r}"
        )
    finally:
        deregister = getattr(registry, "deregister", None)
        if callable(deregister):
            deregister(probe_name)
