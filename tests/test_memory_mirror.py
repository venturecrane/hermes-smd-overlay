"""Tests for the ``hermes-smd-memory-mirror`` plugin.

Covers:
  - The plugin package imports successfully.
  - ``register(ctx)`` is callable.
  - After registration, ``on_session_end`` (Honcho conclusion mirror
    trigger) appears in the fake context's hook registry.
"""

from __future__ import annotations

from tests.conftest import load_plugin


def test_memory_mirror_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-memory-mirror must attach to on_session_end."""
    mod = load_plugin("hermes-smd-memory-mirror")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "on_session_end" in fake_ctx.registered
