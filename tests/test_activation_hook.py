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
import inspect
import logging
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
    def __init__(
        self,
        hook_names: set[str],
        *,
        async_hook: str | None = None,
    ) -> None:
        # Mirror PluginManager._hooks: name -> [callbacks].
        self._hooks = {h: [lambda **k: None] for h in hook_names}
        if async_hook is not None:

            async def async_callback(**kwargs):
                return None

            assert inspect.iscoroutinefunction(async_callback)
            self._hooks[async_hook] = [async_callback]


def _install_fake_plugins(
    monkeypatch,
    *,
    hooks: set[str],
    invoke_results: list,
    block_message: str | None = _BLOCK[0]["message"],
    async_hook: str | None = None,
    discover_raises: Exception | None = None,
    invoke_raises: Exception | None = None,
    workspace_tools: set[str] | None = None,
    config: dict | None = None,
) -> dict:
    """Inject a fake ``hermes_cli.plugins`` exposing the three fns the handler
    imports. Returns a dict recording the ``force`` flag and invoke calls.

    ``config`` is what the fake ``hermes_cli.config.load_config`` returns for the
    webhook read-surface check (step 5). It defaults to a config with no webhook
    platform, so that check skips and these tests stay about governance."""
    calls: dict = {"force": None, "invoke": [], "block": []}
    parent = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.plugins")
    mgr = _FakeManager(hooks, async_hook=async_hook)

    def discover_plugins(force: bool = False) -> None:
        calls["force"] = force
        if discover_raises is not None:
            raise discover_raises

    def get_plugin_manager():
        return mgr

    def get_pre_tool_call_block_message(tool_name: str, args: dict, **kwargs):
        calls["block"].append((tool_name, args, kwargs))
        if invoke_raises is not None:
            raise invoke_raises
        return block_message

    def invoke_hook(hook_name: str, **kwargs):
        calls["invoke"].append((hook_name, kwargs))
        if invoke_raises is not None:
            raise invoke_raises
        return invoke_results

    mod.discover_plugins = discover_plugins  # type: ignore[attr-defined]
    mod.get_pre_tool_call_block_message = get_pre_tool_call_block_message  # type: ignore[attr-defined]
    mod.get_plugin_manager = get_plugin_manager  # type: ignore[attr-defined]
    mod.invoke_hook = invoke_hook  # type: ignore[attr-defined]
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: config if config is not None else {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", mod)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    tools_parent = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")

    class _Registry:
        @staticmethod
        def get_all_tool_names():
            return workspace_tools or {
                "workspace_gmail_search",
                "workspace_gmail_get",
                "workspace_gmail_create_draft",
                "workspace_gmail_modify",
                "workspace_gmail_archive",
                "workspace_calendar_list",
                "workspace_calendar_get",
                "workspace_calendar_create_draft",
                "workspace_calendar_update_draft",
                "workspace_drive_list",
                "workspace_drive_get",
                "workspace_drive_export",
                "workspace_docs_create",
                "workspace_docs_get",
                "workspace_docs_append",
                "workspace_sheets_create",
                "workspace_sheets_get_values",
                "workspace_sheets_update_values",
            }

    registry_mod.registry = _Registry()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools", tools_parent)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
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
    # ... and drove the production block-message interpreter with the banned probe.
    assert calls["block"], "self-check did not drive the pre-execution trust path"
    tool_name, args, kwargs = calls["block"][0]
    assert tool_name == "email_send"
    assert args == {}
    assert kwargs["session_id"] == "smd-activation-selfcheck"


def test_selfcheck_probes_use_the_shared_session_id(monkeypatch, no_real_exit):
    """ss-console #2122. The probe's session id is a CONTRACT: the interactive
    cost meter recognizes the boot dispatch by this exact value and declines to
    price it. A second copy of the literal here would drift, and the drift looks
    like the bug it fixes — an INVARIANT_VIOLATION row on every boot."""
    from shared.selfcheck import SELFCHECK_SESSION_ID, is_selfcheck_session

    calls = _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))

    _, _, block_kwargs = calls["block"][0]
    assert block_kwargs["session_id"] == SELFCHECK_SESSION_ID
    assert block_kwargs["tool_call_id"] == SELFCHECK_SESSION_ID
    audit_dispatch = [c for c in calls["invoke"] if c[0] == "post_llm_call"]
    assert audit_dispatch, "the audit self-check did not drive post_llm_call"
    assert is_selfcheck_session(audit_dispatch[0][1]["session_id"])


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
    _install_fake_plugins(
        monkeypatch,
        hooks=_ALL_HOOKS,
        invoke_results=[],
        block_message=None,
    )
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_fails_closed_when_workspace_tools_are_missing(monkeypatch, no_real_exit):
    _install_fake_plugins(
        monkeypatch,
        hooks=_ALL_HOOKS,
        invoke_results=_BLOCK,
        workspace_tools={"workspace_gmail_search"},
    )
    handler = _load_handler()
    with pytest.raises(_Exit):
        asyncio.run(handler.handle("gateway:startup", {}))


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


@pytest.mark.parametrize("async_hook", ["pre_tool_call", "pre_gateway_dispatch"])
def test_fails_closed_when_any_registered_callback_is_async(
    monkeypatch,
    no_real_exit,
    async_hook,
):
    calls = _install_fake_plugins(
        monkeypatch,
        hooks=_ALL_HOOKS,
        invoke_results=_BLOCK,
        async_hook=async_hook,
    )
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1
    assert calls["block"] == []
    assert calls["invoke"] == []


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
    # Both self-checks ran: trust interpreter then audit dispatch.
    names = [c[0] for c in calls["invoke"]]
    assert calls["block"]
    assert "post_llm_call" in names


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


# --- cost-breaker boot self-check (ADR 0062 §6, ss-console #1701) -------------
# The negative-fire probe that earns sticky_stop_cost_cap its enforced status:
# every boot proves the breaker actually trips + refuses, in a throwaway db.


def test_run_boot_probe_passes_against_the_real_breaker():
    # The real negative-fire probe: trips HARD_STOP in a throwaway db and
    # confirms the guard refuses. Returns (True, "").
    import shared.cost_breaker as cb

    ok, reason = asyncio.run(cb.run_boot_probe())
    assert ok is True, reason


def test_cost_breaker_self_check_fails_closed_when_probe_reports_inert(monkeypatch, no_real_exit):
    # An inert breaker (probe returns ok=False) must _die (fail-closed) — an
    # operator whose spend cap cannot fire must not serve.
    import shared.cost_breaker as cb

    async def _inert():
        return False, "ladder did not trip HARD_STOP (level=OK)"

    monkeypatch.setattr(cb, "run_boot_probe", _inert)
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler._cost_breaker_self_check())
    assert ei.value.code == 1


def test_full_handle_runs_cost_breaker_check(monkeypatch, no_real_exit):
    # The governed-path boot runs ALL self-checks including the breaker; if the
    # breaker were inert the whole boot fails closed.
    _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    import shared.cost_breaker as cb

    async def _inert():
        return False, "inert"

    monkeypatch.setattr(cb, "run_boot_probe", _inert)
    handler = _load_handler()
    with pytest.raises(_Exit):
        asyncio.run(handler.handle("gateway:startup", {}))


# ---------------------------------------------------------------------------
# Webhook read-surface assertion (ss-console#2145). Step 5 of the boot gate:
# read_file reaches webhook turns only when the config half and the runtime
# half BOTH shipped, and config-half-only is silent at runtime — so the gate
# reads the RESOLVED surface and refuses to serve when it disagrees.
# ---------------------------------------------------------------------------

_WEBHOOK_CONFIG = {"platforms": {"webhook": {"enabled": True}}}


def _fake_surface(monkeypatch, *, offers_read_file: bool):
    """Point the handler's read-surface contract at a known answer, so these
    tests are about the gate's WIRING; the contract's own resolution is covered
    against faithful Hermes fakes in tests/test_webhook_read_surface.py."""
    import shared.webhook_read_surface as wrs

    def _assert(_config):
        if not offers_read_file:
            raise wrs.WebhookReadSurfaceError("webhook tool surface is missing ['read_file']")

    monkeypatch.setattr(wrs, "assert_read_file_on_webhook", _assert)


def test_boot_fails_closed_when_webhook_turns_cannot_read_files(monkeypatch, no_real_exit):
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=False)
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_boot_passes_when_webhook_turns_can_read_files(monkeypatch, no_real_exit):
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=True)
    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))


def test_check_skipped_on_a_seat_without_the_webhook_platform(monkeypatch, no_real_exit):
    # A seat that serves no webhook has no webhook surface to be wrong about,
    # and must not be refused a boot over a platform it never serves. The
    # broken-surface fake would _die if the check ran.
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config={"platforms": {}}
    )
    _fake_surface(monkeypatch, offers_read_file=False)
    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))


def test_check_fails_closed_when_the_config_cannot_be_loaded(monkeypatch, no_real_exit):
    # Unverifiable is not the same as fine.
    _install_fake_plugins(monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK)
    import sys as _sys

    def _boom():
        raise OSError("config.yaml unreadable")

    _sys.modules["hermes_cli.config"].load_config = _boom
    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_config_check_prefers_the_gateway_loader(monkeypatch):
    """The turn path reads gateway.run._load_gateway_config (which overlays
    managed scope), not hermes_cli.config.load_config. Reading the wrong dict
    would let the gate be right about a config no turn uses."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config={"from": "hermes_cli"}
    )
    gateway_parent = types.ModuleType("gateway")
    run_mod = types.ModuleType("gateway.run")
    run_mod._load_gateway_config = lambda: {"from": "gateway"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway", gateway_parent)
    monkeypatch.setitem(sys.modules, "gateway.run", run_mod)

    handler = _load_handler()
    assert handler._gateway_config() == {"from": "gateway"}


def test_config_check_falls_back_when_the_gateway_loader_moves(monkeypatch):
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config={"from": "hermes_cli"}
    )
    monkeypatch.delitem(sys.modules, "gateway.run", raising=False)
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    handler = _load_handler()
    assert handler._gateway_config() == {"from": "hermes_cli"}


# ---------------------------------------------------------------------------
# Webhook EXPECTED-TOOLS tier (ss-console#2222). Step 5b over the SAME resolved
# surface with the OPPOSITE posture: log CRITICAL, write the sentinel the gate's
# heartbeat reads, and KEEP BOOTING.
#
# The two tiers answer different orders of harm. Without ``read_file`` the spec
# read-mark can never be set and every voice-gated delivery refuses — the seat
# cannot do its job on its only channel, so being visibly down is better than
# serving. A missing ``operator_seat_facts`` means ONE class of answer (an ask
# about the seat) is improvised instead of grounded. That is bad; refusing to
# serve the paid client over it is worse. Every test below exists to keep the two
# apart, because collapsing them in either direction is a real design mistake:
# this tier was specified as fatal and the critique reversed it.
# ---------------------------------------------------------------------------


def _fake_expected(monkeypatch, *, missing: bool, raises: bool = False):
    """Point the handler's expected-tools contract at a known answer. The
    contract's own resolution is covered against faithful Hermes fakes in
    tests/test_webhook_read_surface.py."""
    import shared.webhook_read_surface as wrs

    def _report(_config):
        if raises:
            raise RuntimeError("surface resolution exploded")
        return {"operator_seat_facts": {"expected": True, "offered": not missing}}

    monkeypatch.setattr(wrs, "expected_tool_report", _report)


def _sentinel_reader(monkeypatch, tmp_path):
    """Redirect the boot sentinel into tmp and hand back a reader for it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def _read():
        import shared.webhook_read_surface as wrs

        return wrs.read_webhook_surface_status(str(tmp_path))

    return _read


def test_a_missing_expected_tool_does_not_stop_the_boot(monkeypatch, no_real_exit, tmp_path):
    """THE tier distinction as an executable claim. Falsifier: route this through
    ``_die`` like the read_file check, and this test fails."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=True)
    _fake_expected(monkeypatch, missing=True)
    read = _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))  # no _Exit

    status = read()
    assert status["ok"] is True, "the CHECK ran; it is the TOOL that is missing"
    assert status["tools"]["operator_seat_facts"]["offered"] is False


def test_a_missing_expected_tool_logs_critical(monkeypatch, no_real_exit, tmp_path, caplog):
    """Non-fatal must not mean quiet — the whole point of the tier is that the
    failure is visible somewhere other than a crash. Falsifier: downgrade the log
    to INFO and a degraded surface becomes indistinguishable from a healthy one."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=True)
    _fake_expected(monkeypatch, missing=True)
    _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    with caplog.at_level(logging.CRITICAL):
        asyncio.run(handler.handle("gateway:startup", {}))
    critical = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert critical, "a degraded warn tier must log CRITICAL"
    text = " ".join(r.getMessage() for r in critical)
    assert "operator_seat_facts" in text
    assert "CONTINUES" in text


def test_a_healthy_expected_tier_writes_a_green_sentinel(monkeypatch, no_real_exit, tmp_path):
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=True)
    _fake_expected(monkeypatch, missing=False)
    read = _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))
    status = read()
    assert status["ok"] is True
    assert status["tools"]["operator_seat_facts"]["offered"] is True


def test_an_unresolvable_surface_is_reported_as_our_blindness(monkeypatch, no_real_exit, tmp_path):
    """``ok=False, tools=None``: the check itself could not run. Never an empty
    map, which the console would read as "checked, everything offered"."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=True)
    _fake_expected(monkeypatch, missing=False, raises=True)
    read = _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))
    status = read()
    assert status["ok"] is False
    assert status["tools"] is None


def test_the_fatal_read_file_tier_still_dies_regardless_of_the_warn_tier(
    monkeypatch, no_real_exit, tmp_path
):
    """The fatal tier is UNCHANGED. A posture change that quietly relaxed the
    read_file assertion would trade a loud failure for a silent one — exactly
    what #2145's boot gate exists to prevent."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config=_WEBHOOK_CONFIG
    )
    _fake_surface(monkeypatch, offers_read_file=False)
    _fake_expected(monkeypatch, missing=False)  # warn tier perfectly healthy
    _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    with pytest.raises(_Exit) as ei:
        asyncio.run(handler.handle("gateway:startup", {}))
    assert ei.value.code == 1


def test_expected_tier_skipped_on_a_seat_without_the_webhook_platform(
    monkeypatch, no_real_exit, tmp_path
):
    """No sentinel is written at all, so the heartbeat HOLDS rather than
    reporting a green for a surface this seat does not have."""
    _install_fake_plugins(
        monkeypatch, hooks=_ALL_HOOKS, invoke_results=_BLOCK, config={"platforms": {}}
    )
    _fake_surface(monkeypatch, offers_read_file=False)
    _fake_expected(monkeypatch, missing=True)
    read = _sentinel_reader(monkeypatch, tmp_path)

    handler = _load_handler()
    asyncio.run(handler.handle("gateway:startup", {}))
    assert read() is None
