"""Tests for the ``hermes-smd-audit`` plugin.

Covers:
  - The plugin package imports successfully.
  - ``register(ctx)`` is callable.
  - After registration, both ``post_tool_call`` and ``post_llm_call``
    appear in the fake context's hook registry.
"""

from __future__ import annotations

from tests.conftest import load_plugin


def test_audit_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-audit must attach to post_tool_call and post_llm_call."""
    mod = load_plugin("hermes-smd-audit")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "post_tool_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered
