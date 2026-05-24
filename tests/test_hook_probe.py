"""Tests for the ``hermes-smd-hook-probe`` smoke plugin.

Covers:
  - The plugin package imports successfully.
  - ``register(ctx)`` is callable.
  - After registration, ALL SIX hooks the overlay depends on appear in
    the fake context's hook registry. The probe exists specifically to
    smoke-test Hermes' hook surface at rebase time, so it must attach
    to every hook the rest of the overlay uses.
"""

from __future__ import annotations

from tests.conftest import load_plugin


def test_hook_probe_registers_all_six_hooks(fake_ctx) -> None:
    """hermes-smd-hook-probe must attach to all six overlay-relevant hooks."""
    mod = load_plugin("hermes-smd-hook-probe")
    assert callable(mod.register)

    mod.register(fake_ctx)

    expected_hooks = {
        "pre_tool_call",
        "post_tool_call",
        "pre_llm_call",
        "post_llm_call",
        "transform_tool_result",
        "on_session_end",
    }
    for hook in expected_hooks:
        assert hook in fake_ctx.registered, f"hook {hook!r} not registered"
