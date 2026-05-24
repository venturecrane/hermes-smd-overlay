"""Tests for the ``hermes-smd-trust`` plugin.

Covers:
  - The plugin package imports successfully.
  - ``register(ctx)`` is callable.
  - After registration, both ``pre_tool_call`` (trust-ceiling enforcement)
    and ``transform_tool_result`` (Composio per-connection isolation guard)
    appear in the fake context's hook registry.
"""

from __future__ import annotations

from tests.conftest import load_plugin


def test_trust_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-trust must attach to pre_tool_call and transform_tool_result."""
    mod = load_plugin("hermes-smd-trust")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "pre_tool_call" in fake_ctx.registered
    assert "transform_tool_result" in fake_ctx.registered
