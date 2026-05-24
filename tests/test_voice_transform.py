"""Tests for the ``hermes-smd-voice`` plugin.

Covers:
  - The plugin package imports successfully.
  - ``register(ctx)`` is callable.
  - After registration, both ``pre_llm_call`` (voice-sample injection)
    and ``post_llm_call`` (per-turn voice telemetry) appear in the fake
    context's hook registry.
"""

from __future__ import annotations

from tests.conftest import load_plugin


def test_voice_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-voice must attach to pre_llm_call and post_llm_call."""
    mod = load_plugin("hermes-smd-voice")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "pre_llm_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered
