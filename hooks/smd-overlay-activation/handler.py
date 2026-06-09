"""Gateway-startup activation gate for the SMD overlay (ss-console#1285).

THE BUG THIS CLOSES (World 1, confirmed at runtime). The overlay's agent-plugins
(audit, trust, voice, memory-mirror, webhook-router) register their hooks into the
PluginManager *singleton* that the agent fire sites dispatch from
(``model_tools``/``run_agent`` -> ``invoke_hook`` -> ``get_plugin_manager()._hooks``).
But Hermes discovers plugins idempotently: ``discover_and_load`` early-returns once
``_discovered`` is set (hermes_cli/plugins.py), and on the live gateway that first
discovery ran at import time — BEFORE the overlay was present+enabled on the volume —
so the gateway's singleton ended up WITHOUT the overlay. Every registered hook was
inert on live turns: a proven real tool call wrote zero ``audit_log`` rows and the
trust ceiling never fired (ss-console#1285 Q2 confirmation).

WHY A gateway:startup HOOK, AND NOT A HERMES-CORE PATCH. The fix must run IN the
gateway process, after the overlay is present+enabled, to repopulate THAT process's
singleton — and it must be overlay-side only, because a carried patch against the
pinned Hermes ref is exactly the fork-maintenance trap ADR 0024 (pin-only) / ADR 0015
(plugin-only overlay) closed. Hermes' own HookRegistry (gateway/hooks.py) loads
``async def handle`` handlers from ``$HERMES_HOME/hooks/`` and emits ``gateway:startup``
inside the startup coroutine (gateway/run.py:3730), in the gateway's event loop,
before steady-state turns. So this handler — installed on the volume by bootstrap.sh,
NOT a core file — is the overlay using Hermes' public API:

  1. ``discover_plugins(force=True)`` (plugins.py:1363): ``force`` CLEARS the singleton's
     ``_hooks`` then reloads from disk (discover_and_load force branch), so the
     now-present+enabled overlay registers into the LIVE singleton. force=True is
     Hermes' own supported "make plugin changes visible in a long-lived session" path,
     and it is idempotent — re-running yields the same registration, no double-load.
  2. A LIVE self-check: drive ``invoke_hook("pre_tool_call", tool_name="email_send", ...)``
     — the EXACT production dispatch fn + singleton a real turn uses — and require the
     trust ceiling to return a block directive. This asserts the property that actually
     matters: a hook FIRES through the live turn-path singleton, not merely that
     ``register()`` ran (the proxy that let the pre-gateway invariant stay green while
     the gateway was inert). The probe is a permanently-banned tool, so it is
     deterministic and READ-ONLY — it does not mutate ``audit_log``.
  3. FAIL-CLOSED: HookRegistry swallows handler exceptions (gateway/hooks.py:19), so a
     failed self-check cannot rely on raising — the handler calls ``os._exit(1)``.
     ``gateway:startup`` fires before steady-state turns, so exiting prevents the gateway
     from ever serving ungoverned. Better visibly down (crash-loop; Fly restarts) than
     silently ungoverned.

This handler IS the authoritative live boot gate. The pre-gateway safety-substrate
invariant (operator/safety-substrate) runs in a DIFFERENT process and can only assert
the gate is WIRED (handler installed) + the registration LOGIC is sound — it
structurally cannot assert the live-turn property. The two are complementary.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("hermes_smd.overlay.activation")

# The hook types the overlay must have in the live singleton for the governance
# layer to be complete (union across the five functional plugins). Mirrors the
# pre-gateway invariant's _REQUIRED_HOOKS so the two gates assert the same surface.
_REQUIRED_HOOKS = {
    "pre_tool_call",  # trust ceiling gate — the safety-critical one
    "post_tool_call",  # audit + trust provenance
    "post_llm_call",  # audit + voice
    "pre_llm_call",  # voice
    "subagent_stop",  # audit
    "on_session_end",  # memory-mirror
    "pre_gateway_dispatch",  # webhook-router
}

# A permanently-banned tool (BANNED_TOOLS, ADR 0005 reviewer-as-sender). The trust
# ceiling must refuse it regardless of the customer's authored ceiling, so it is a
# deterministic "this MUST be gated" probe — and a READ-ONLY one (no audit write).
_BANNED_PROBE_TOOL = "email_send"


def _die(reason: str) -> None:
    """Fail closed: an operator that cannot prove it governs live turns must not
    serve. Log CRITICAL with the SPECIFIC reason (so the failure is diagnosable —
    the in-process visibility the live gateway has lacked), flush, then terminate
    the gateway process. HookRegistry would swallow a ``raise``, so exit directly."""
    logger.critical(
        "SMD OVERLAY ACTIVATION FAILED — refusing to serve an ungoverned operator. %s",
        reason,
    )
    # Best-effort flush so the reason reaches the logs before the hard exit.
    for h in list(logging.getLogger().handlers):
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(1)


async def handle(event_type: str, context: dict | None = None) -> None:
    """Fire at ``gateway:startup``: force-load the overlay into the live singleton,
    then prove it governs the live turn-path or fail closed."""
    try:
        from hermes_cli.plugins import (
            discover_plugins,
            get_plugin_manager,
            invoke_hook,
        )
    except Exception as e:  # noqa: BLE001
        _die(
            f"cannot import hermes_cli.plugins ({type(e).__name__}: {e}) — "
            "governance cannot be verified"
        )
        return

    # 1. Force the live gateway singleton to (re)load plugins from disk, now that
    #    bootstrap has placed + enabled the overlay on the volume. force=True clears
    #    _hooks first, so this is idempotent (no double-registration on re-runs).
    try:
        discover_plugins(force=True)
    except Exception as e:  # noqa: BLE001
        _die(f"discover_plugins(force=True) raised: {type(e).__name__}: {e}")
        return

    mgr = get_plugin_manager()
    present = set(getattr(mgr, "_hooks", {}) or {})
    missing = _REQUIRED_HOOKS - present
    if missing:
        _die(
            "overlay hooks absent from the live gateway singleton after force-discover: "
            f"missing {sorted(missing)} (present: {sorted(present)}) — the overlay is "
            "installed/enabled but did not register into the live process"
        )
        return

    # 2. LIVE self-check: drive the REAL dispatch fn + singleton with a banned tool
    #    and require the trust ceiling to FIRE a block directive. This is the property
    #    that matters — a hook fires through the live turn-path — not just that
    #    register() ran. Read-only: a banned tool is gated before any audit write.
    slug = os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or ""
    try:
        results: list[Any] = invoke_hook(
            "pre_tool_call",
            tool_name=_BANNED_PROBE_TOOL,
            args={},
            session_id="smd-activation-selfcheck",
            tool_call_id="smd-activation-selfcheck",
            customer_slug=slug,
        )
    except Exception as e:  # noqa: BLE001
        _die(f"invoke_hook(pre_tool_call) self-check raised: {type(e).__name__}: {e}")
        return

    blocked = any(isinstance(r, dict) and r.get("action") == "block" for r in (results or []))
    if not blocked:
        _die(
            f"TRUST NOT ENFORCING on the live gateway: banned tool {_BANNED_PROBE_TOOL!r} "
            f"was not gated by any pre_tool_call hook in the live singleton (got {results!r})"
        )
        return

    logger.info(
        "SMD overlay ACTIVE on the live gateway: %d hook type(s) in the live singleton, "
        "trust gate fired on banned %r — self-check passed, operator is governed.",
        len(present),
        _BANNED_PROBE_TOOL,
    )
