"""Tests for the gateway:startup activation gate (hooks/smd-overlay-activation).

The handler is the AUTHORITATIVE live boot gate (ss-console#1285): it force-loads
the overlay into the gateway's live PluginManager singleton and drives a REAL
pre_tool_call dispatch self-check, failing closed (``os._exit(1)``) if the overlay
does not govern the live turn-path. The pre-gateway safety-substrate invariant
cannot assert this (wrong process); this handler can, because it runs in the
gateway process.

These tests exercise that orchestration against a faithful fake of Hermes'
``hermes_cli.plugins`` module (the handler imports ``discover_plugins`` /
``get_plugin_manager`` / ``invoke_hook`` lazily inside ``handle``, so the fake is
injected into ``sys.modules`` before the call). ``os._exit`` is patched to raise a
sentinel so the fail-closed path is observable instead of killing the test process.
The four outcomes that matter: pass when governed; fail-closed when hooks are absent;
fail-closed when present-but-incomplete; fail-closed when trust does not fire.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_HANDLER = (
    Path(__file__).resolve().parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
)

# The full hook surface the handler requires in the live singleton (mirrors the
# handler's _REQUIRED_HOOKS — kept in sync deliberately so a drift fails a test).
_ALL_HOOKS = {
    "pre_tool_call",
    "post_tool_call",
    "post_llm_call",
    "pre_llm_call",
    "subagent_stop",
    "on_session_end",
    "pre_gateway_dispatch",
}
# What the trust ceiling returns for the banned probe tool.
_BLOCK = [{"action": "block", "message": "Refused: email_send is permanently banned"}]


class _Exit(Exception):
    """Raised in place of os._exit so a fail-closed exit is observable in-test."""

    def __init__(self, code: int) -> None:
        super().__init__(f"os._exit({code})")
        self.code = code


def _load_handler() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("smd_activation_handler", _HANDLER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeManager:
    def __init__(self, hook_names: set[str]) -> None:
        # Mirror PluginManager._hooks: name -> [callbacks].
        self._hooks = {h: [lambda **k: None] for h in hook_names}


def _install_fake_plugins(
    monkeypatch,
    *,
    hooks: set[str],
    invoke_results: list,
    discover_raises: Exception | None = None,
    invoke_raises: Exception | None = None,
) -> dict:
    """Inject a fake ``hermes_cli.plugins`` exposing the three fns the handler
    imports. Returns a dict recording the ``force`` flag and invoke calls."""
    calls: dict = {"force": None, "invoke": []}
    parent = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.plugins")
    mgr = _FakeManager(hooks)

    def discover_plugins(force: bool = False) -> None:
        calls["force"] = force
        if discover_raises is not None:
            raise discover_raises

    def get_plugin_manager():
        return mgr

    def invoke_hook(hook_name: str, **kwargs):
        calls["invoke"].append((hook_name, kwargs))
        if invoke_raises is not None:
            raise invoke_raises
        return invoke_results

    mod.discover_plugins = discover_plugins  # type: ignore[attr-defined]
    mod.get_plugin_manager = get_plugin_manager  # type: ignore[attr-defined]
    mod.invoke_hook = invoke_hook  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", mod)
    return calls


@pytest.fixture
def no_real_exit(monkeypatch):
    def _fake_exit(code: int):
        raise _Exit(code)

    monkeypatch.setattr(os, "_exit", _fake_exit)


def test_passes_when_governed(monkeypatch, no_real_exit):
    calls = _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    handler = _load_handler()
    # No _Exit raised => the gate passed.
    asyncio.run(handler.handle("gateway:startup", {}))
    # It force-discovered (not a no-op discover) ...
    assert calls["force"] is True
    # ... and drove the REAL pre_tool_call dispatch with the banned probe tool.
    assert calls["invoke"], "self-check did not drive a dispatch"
    name, kwargs = calls["invoke"][0]
    assert name == "pre_tool_call"
    assert kwargs["tool_name"] == "email_send"


def test_fails_closed_when_no_hooks_registered(monkeypatch, no_real_exit):
    # The exact production failure: overlay registered nothing into the live singleton.
    _install_fake_plugins(monkeypatch, hooks=set(), invoke_results=_BLOCK)
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_fails_closed_when_hooks_incomplete(monkeypatch, no_real_exit):
    # pre_tool_call present (trust would block) but audit/voice/etc. missing —
    # an incompletely-governed operator must not serve.
    calls = _install_fake_plugins(monkeypatch, hooks={"pre_tool_call"}, invoke_results=_BLOCK)
    handler = _load_handler()
    with pytest.raises(_Exit):
        asyncio.run(handler.handle("gateway:startup", {}))
    # It dies at the hook-completeness check, BEFORE the dispatch self-check.
    assert calls["invoke"] == []


def test_fails_closed_when_trust_not_enforcing(monkeypatch, no_real_exit):
    # All hooks present, but the trust gate did NOT block the banned tool — a
    # registered-but-inert pre_tool_call is exactly the silent failure to catch.
    _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=[None])
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_fails_closed_when_discover_raises(monkeypatch, no_real_exit):
    _install_fake_plugins(
        monkeypatch,
        hooks=_ALL_HOOKS,
        invoke_results=_BLOCK,
        discover_raises=RuntimeError("discover boom"),
    )
    handler = _load_handler()
    with pytest.raises(_Exit):
        asyncio.run(handler.handle("gateway:startup", {}))


def test_fails_closed_when_dispatch_raises(monkeypatch, no_real_exit):
    _install_fake_plugins(
        monkeypatch,
        hooks=_ALL_HOOKS,
        invoke_results=_BLOCK,
        invoke_raises=RuntimeError("dispatch boom"),
    )
    handler = _load_handler()
    with pytest.raises(_Exit):
        asyncio.run(handler.handle("gateway:startup", {}))


def test_fails_closed_when_hermes_plugins_unimportable(monkeypatch, no_real_exit):
    # Ensure the lazy import genuinely fails (no real hermes_cli on the path, and
    # any fake removed) — the handler must fail closed, not pass silently.
    monkeypatch.delitem(sys.modules, "hermes_cli.plugins", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)

    real_import = __import__

    def _blocked_import(name, *a, **k):
        if name == "hermes_cli.plugins" or name.startswith("hermes_cli"):
            raise ImportError("no hermes_cli in this environment")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _blocked_import)
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def _audit_env(monkeypatch, handler, *, writes: bool):
    """Wire the handler's audit-DB helpers to an in-memory counter and make the
    fake post_llm_call dispatch increment it iff ``writes`` (simulating the real
    audit hook writing a row). Returns the counter list."""
    count = [0]
    monkeypatch.setattr(handler, "_audit_db_path", lambda: "/fake/audit.db")
    monkeypatch.setattr(handler, "_audit_row_count", lambda _p: count[0])
    return count


def test_passes_when_governed_and_auditing(monkeypatch, no_real_exit):
    calls = _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    handler = _load_handler()
    count = _audit_env(monkeypatch, handler, writes=True)
    # Make the post_llm_call dispatch "write" a row, like the real audit hook.
    import sys as _sys

    plugins_mod = _sys.modules["hermes_cli.plugins"]
    _orig = plugins_mod.invoke_hook

    def _counting_invoke(hook_name, **kwargs):
        if hook_name == "post_llm_call":
            count[0] += 1
        return _orig(hook_name, **kwargs)

    plugins_mod.invoke_hook = _counting_invoke
    asyncio.run(handler.handle("gateway:startup", {}))  # no _Exit => passed
    # Both self-checks ran: trust (pre_tool_call) then audit (post_llm_call).
    names = [c[0] for c in calls["invoke"]]
    assert "pre_tool_call" in names and "post_llm_call" in names


def test_fails_closed_when_audit_not_writing(monkeypatch, no_real_exit):
    # Hooks present, trust fires (blocks email_send), BUT the post_llm_call dispatch
    # writes no audit row — the exact ss-console#1285 Q2 failure, now fail-closed.
    _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    handler = _load_handler()
    _audit_env(monkeypatch, handler, writes=False)  # counter never increments
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_audit_check_skipped_when_no_binding(monkeypatch, no_real_exit):
    # No resolvable audit DB (binding unset) -> audit self-check degrades to skipped,
    # the handler still passes on trust governance alone (no false fail-closed).
    _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    handler = _load_handler()
    monkeypatch.setattr(handler, "_audit_db_path", lambda: None)
    asyncio.run(handler.handle("gateway:startup", {}))  # no _Exit => passed
