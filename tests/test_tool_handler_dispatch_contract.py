"""Every registered tool handler must be callable the way Hermes dispatches it.

Hermes' tool registry calls a handler as ``entry.handler(args, **kwargs)`` — the
tool arguments arrive as a dict in the FIRST POSITIONAL slot, and Hermes adds
keyword arguments of its own (``task_id`` / ``user_task``; see the docstrings at
``plugins/hermes-smd-corrections/__init__.py`` and
``plugins/hermes-smd-establishment/__init__.py``, which document what Hermes
withholds). A handler that cannot accept that shape raises ``TypeError`` inside
``registry.dispatch`` and the tool silently does nothing.

That is not hypothetical. ``smd_send_message`` shipped with ``def
_smd_send_message(**kwargs)`` — no positional parameter at all — and every
invocation raised (Sentry SMD-OPERATOR-1B, first seen 2026-08-13, tenant
pilot-smokeball, release ec3fb713). It was the seat's ONLY send tool, so
autonomous sends were dead from the day the tool landed. Its own unit tests
passed throughout, because they called it the way the handler was written
(``_smd_send_message(to=..., subject=...)``) rather than the way Hermes calls it.
The test encoded the defect.

WHAT THIS ASSERTS, AND WHY IT IS SHAPED THIS WAY.

The check is STRUCTURAL — a positional parameter exists, and ``**kwargs`` exists
— rather than a ``signature.bind()`` against a guessed kwarg list. Binding
against ``task_id``/``user_task`` would pin today's kwargs and go quietly green
if Hermes ever changed which extras it passes, while still failing to notice a
handler that lost its positional slot. The structural pair is the invariant that
actually matters: Hermes passes a positional dict, and it passes extras the
handler must tolerate.

WHAT IT DOES NOT CATCH. A handler can satisfy this and still be wrong — ``def
h(args, **kwargs)`` binds identically whether the body reads ``args`` or
``kwargs``, and reading ``kwargs`` would produce an EMPTY payload while still
reporting success. That is the sibling bug that shipped alongside this one; only
a behavioural assertion catches it (see the body assertions in
``tests/test_msgraph_transports.py``). This guard covers the crash class only,
and says so rather than implying more.

ANTI-VACUUM. Three measures, because a guard that cannot fail has measured
nothing:

* the handler is resolved from keyword OR positional registration and a missing
  one is a hard failure, never a skip;
* a floor on the number of tools collected, so a plugin that registers nothing
  cannot make the assertion vacuously true;
* ``smd_send_message`` is pinned by name — the tool this guard was written for
  must always be among those checked.

The fan-out deliberately calls each sub-plugin's ``register()`` directly rather
than going through ``load_and_register_subplugins``, whose per-plugin
``except Exception`` (``__init__.py``) would swallow a load failure and empty the
population this test reasons about.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

# Reuse the umbrella's canonical discovery + fan-out so this test exercises the
# same load path the gateway uses, rather than introducing a second loader.
from tests.test_overlay_fanout import RecordingCtx, _load_umbrella

_REPO = Path(__file__).resolve().parent.parent

#: Registered-tool floor. 32 tools are registered today, measured by running
#: ``_registered_tools()`` (workspace 18, establishment 3, escalation 2, jobs 4,
#: and one each from trust, peer-memory, initiation, corrections, drafting).
#: Pinned a little below that so adding a tool does not churn the test, while a
#: plugin that stops registering entirely still trips it. Raise it if the real
#: count moves up materially.
_MIN_REGISTERED_TOOLS = 30

#: The tool this guard exists for. Pinned by name so a refactor that stops
#: registering it turns this test red instead of silently shrinking its scope.
_PINNED_TOOL = "smd_send_message"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Sub-plugins resolve their per-customer namespace + audit binding from env
    at register time. Set them so ``register()`` runs its full path rather than a
    degraded branch that would register no tools at all."""
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", str(tmp_path / "audit.db"))
    return tmp_path


def _resolve_handler(name: str, args: tuple, kwargs: dict) -> Any:
    """The handler from a recorded ``register_tool`` call.

    Every call site today routes through ``shared.tool_registration`` and passes
    keywords, so ``kwargs["handler"]`` is the normal path. A direct positional
    ``ctx.register_tool(name, toolset, schema, handler)`` would land in ``args``
    instead; read it there rather than returning None, because a silent skip is
    exactly how this class of defect stays invisible.
    """
    if "handler" in kwargs:
        return kwargs["handler"]
    if len(args) > 3:
        return args[3]
    raise AssertionError(
        f"{name}: could not locate the handler in its register_tool call "
        f"(args={len(args)} positional, kwargs={sorted(kwargs)}). "
        "This guard must never skip a tool — teach it the new call shape."
    )


def _registered_tools() -> dict[str, Any]:
    """``{tool_name: handler}`` across every declared sub-plugin.

    One ctx per plugin so a failure names the plugin that caused it.
    """
    mod = _load_umbrella()
    tools: dict[str, Any] = {}
    for rel in mod.declared_subplugins():
        plugin_dir = _REPO / rel
        module = mod._load_subplugin_module(plugin_dir)
        register_fn = getattr(module, "register", None)
        if register_fn is None:
            continue
        ctx = RecordingCtx()
        register_fn(ctx)
        for args, kwargs in ctx.tools:
            name = kwargs.get("name") or (args[0] if args else None)
            assert name, f"{plugin_dir.name}: register_tool call with no resolvable tool name"
            tools[name] = _resolve_handler(name, args, kwargs)
    return tools


def test_every_registered_handler_accepts_the_dispatch_shape(env):
    """A positional argument slot and ``**kwargs``, on every registered tool."""
    tools = _registered_tools()

    violations = []
    for name, handler in sorted(tools.items()):
        assert callable(handler), f"{name}: registered handler is not callable ({handler!r})"
        params = list(inspect.signature(handler).parameters.values())
        positional = [
            p
            for p in params
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        var_keyword = [p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD]
        if not positional:
            violations.append(
                f"{name}: takes no positional argument, but Hermes dispatches "
                f"handler(args, **kwargs) — signature is {inspect.signature(handler)}"
            )
        elif not var_keyword:
            violations.append(
                f"{name}: has no **kwargs, so it cannot tolerate the keyword "
                f"arguments Hermes adds — signature is {inspect.signature(handler)}"
            )

    assert not violations, "tool handlers that Hermes cannot call:\n  " + "\n  ".join(violations)


def test_the_guard_sees_a_real_population(env):
    """Anti-vacuum: the assertion above must be running against actual tools."""
    tools = _registered_tools()
    assert len(tools) >= _MIN_REGISTERED_TOOLS, (
        f"only {len(tools)} tools collected (floor {_MIN_REGISTERED_TOOLS}) — a sub-plugin "
        "probably failed to register, which would make the dispatch-shape assertion "
        f"vacuously true. Collected: {sorted(tools)}"
    )
    assert _PINNED_TOOL in tools, (
        f"{_PINNED_TOOL} is not among the registered tools this guard checks. "
        "It is the tool the guard was written for; if it moved, follow it."
    )
