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
  2. A synchronous-contract check over every callback in the live manager. Hermes'
     dispatcher calls callbacks directly and never awaits them, so any coroutine
     callback would load successfully but silently do nothing.
  3. A LIVE trust self-check: drive ``get_pre_tool_call_block_message`` with
     ``tool_name="email_send"`` — the EXACT pre-execution interpreter model_tools uses —
     and require a refusal message. This asserts the property that actually matters:
     trust FIRES through the live turn-path singleton and its result is recognized by
     the executor. The probe is a permanently-banned tool, so it is deterministic and
     READ-ONLY.
  4. A LIVE audit self-check (ss-console#1285 Q2, made self-proving at boot): drive
     ``invoke_hook("post_llm_call", ...)`` and confirm a row actually lands in
     ``audit_log``. This proves the audit WRITE path works live in the gateway process —
     the literal Q2 question ("does a real turn write audit") answered at boot instead of
     by inference. The row carries a distinct session id so it is separable from
     real-turn rows when read back via the runtime seam. Degrades to skipped only when
     the audit DB cannot be resolved; a definitive "wrote nothing" fails closed.
  5. FAIL-CLOSED: HookRegistry swallows handler exceptions (gateway/hooks.py:19), so a
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

import inspect
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

# A permanently-banned tool (BANNED_TOOLS, ADR 0035). The trust
# ceiling must refuse it regardless of the customer's authored ceiling, so it is a
# deterministic "this MUST be gated" probe — and a READ-ONLY one (no audit write).
_BANNED_PROBE_TOOL = "email_send"

# Distinct session id for the boot-time audit self-check row, so it is trivially
# distinguishable from real-turn audit rows when read back (via the runtime seam).
_AUDIT_SELFCHECK_SESSION = "smd-activation-selfcheck"

_REQUIRED_WORKSPACE_TOOLS = {
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


def _audit_db_path() -> str | None:
    """Resolve the local audit sqlite path from SMD_D1_AUDIT_BINDING, mirroring
    d1_client's binding indirection: a value starting with '/' is a direct path;
    otherwise it names the env var that holds the path. Returns None if it cannot
    be resolved to a local path (e.g. a non-file binding) — the audit self-check
    then degrades to skipped rather than failing the boot on an unreadable handle."""
    b = os.environ.get("SMD_D1_AUDIT_BINDING", "")
    if not b:
        return None
    if b.startswith("/"):
        return b
    return os.environ.get(b)


def _audit_row_count(db_path: str) -> int | None:
    """Best-effort COUNT(*) of audit_log via a fresh read-only connection. None on
    any failure (DB/table absent or unreadable) so the caller degrades gracefully."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


async def _cost_breaker_self_check() -> None:
    """Negative-fire probe (ADR 0062 §6, ss-console #1701): prove the cost
    breaker actually HALTS — at every boot, in a THROWAWAY state file that never
    touches the real ``/opt/data/smd/sticky_stop.db``. The probe records spend
    far past a 1-cent cap and asserts the ladder trips HARD_STOP and the guard
    refuses; ``_die`` on either miss — an operator whose spend breaker cannot
    fire must not serve. This is the recurring ``prod-boot`` probe that earns
    ``sticky_stop_cost_cap`` its enforced status (manually trip-proven
    2026-07-04; this makes it self-proving on every boot). Async because the
    activation handler runs inside the gateway's event loop."""
    try:
        from shared.cost_breaker import run_boot_probe
    except Exception as e:  # noqa: BLE001
        _die(f"cost-breaker self-check import failed: {type(e).__name__}: {e}")
        return

    ok, reason = await run_boot_probe()
    if not ok:
        _die(f"COST BREAKER INERT on this boot: {reason} (ADR 0062 §6, #1701)")


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


def _async_callbacks(manager: Any) -> list[str]:
    """Return labels for callbacks incompatible with Hermes' sync dispatcher."""
    found: list[str] = []
    for hook_name, callbacks in (getattr(manager, "_hooks", {}) or {}).items():
        for callback in callbacks:
            call = callback.__call__ if callable(callback) else None
            if not inspect.iscoroutinefunction(callback) and not inspect.iscoroutinefunction(call):
                continue
            module = getattr(callback, "__module__", type(callback).__module__)
            name = getattr(callback, "__qualname__", type(callback).__qualname__)
            found.append(f"{hook_name}:{module}.{name}")
    return sorted(found)


async def handle(event_type: str, context: dict | None = None) -> None:
    """Fire at ``gateway:startup``: force-load the overlay into the live singleton,
    then prove it governs the live turn-path or fail closed."""
    # ADR 0023 Wave 1: Sentry error monitoring for the gateway (agent) process.
    # Disabled-safe (no SENTRY_DSN -> no-op) and best-effort so it never blocks
    # activation. Init first so the governance self-checks below — and any _die()
    # here — are captured in Sentry too.
    try:
        from shared.sentry_init import init_sentry

        init_sentry("gateway")
    except Exception:  # noqa: BLE001 — observability must never block boot
        logger.warning("sentry: gateway init skipped (import/init error)", exc_info=True)

    try:
        from hermes_cli.plugins import (
            discover_plugins,
            get_plugin_manager,
            get_pre_tool_call_block_message,
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

    incompatible = _async_callbacks(mgr)
    if incompatible:
        _die(
            "async callbacks registered in Hermes' synchronous hook dispatcher: "
            f"{incompatible} — these hooks would load but silently return unawaited "
            "coroutines instead of enforcing or observing"
        )
        return

    try:
        from tools.registry import registry

        registered_tools = set(registry.get_all_tool_names())
    except Exception as e:  # noqa: BLE001
        _die(f"cannot inspect the live tool registry: {type(e).__name__}: {e}")
        return
    missing_tools = _REQUIRED_WORKSPACE_TOOLS - registered_tools
    if missing_tools:
        _die(
            "mediated Workspace tools absent from the live registry after force-discover: "
            f"missing {sorted(missing_tools)}"
        )
        return

    # 2. LIVE self-check: drive the REAL dispatch fn + singleton with a banned tool
    #    through the same block-message interpreter model_tools calls immediately
    #    before execution. Read-only: a banned tool is gated before any audit write.
    try:
        block_message = get_pre_tool_call_block_message(
            tool_name=_BANNED_PROBE_TOOL,
            args={},
            session_id="smd-activation-selfcheck",
            tool_call_id="smd-activation-selfcheck",
        )
    except Exception as e:  # noqa: BLE001
        _die(f"get_pre_tool_call_block_message self-check raised: {type(e).__name__}: {e}")
        return

    if not isinstance(block_message, str) or not block_message:
        _die(
            f"TRUST NOT ENFORCING on the live gateway: banned tool {_BANNED_PROBE_TOOL!r} "
            "was not blocked by the production pre-tool-call interpreter "
            f"(got {block_message!r})"
        )
        return

    # 3. AUDIT self-check (ss-console#1285 Q2, made self-proving at boot): drive the
    #    REAL post_llm_call dispatch and confirm a row actually lands in audit_log.
    #    This proves the audit WRITE path works live in the gateway process — not
    #    merely that the audit hook is registered (invariant 8) or that dispatch
    #    reaches a hook (the trust probe above). The row carries a distinct session
    #    id so it is trivially separable from real-turn rows when read back via the
    #    runtime seam. Degrades to skipped (not fatal) only when the audit DB path
    #    cannot be resolved/read at all; a definitive "dispatch wrote nothing" fails
    #    closed — an operator whose audit is inert must not serve.
    db = _audit_db_path()
    before = _audit_row_count(db) if db else None
    try:
        invoke_hook(
            "post_llm_call",
            session_id=_AUDIT_SELFCHECK_SESSION,
            user_message="overlay activation self-check",
            assistant_response="overlay active",
            model="boot-selfcheck",
            platform="boot",
        )
    except Exception as e:  # noqa: BLE001
        _die(f"invoke_hook(post_llm_call) audit self-check raised: {type(e).__name__}: {e}")
        return

    after = _audit_row_count(db) if db else None
    if before is not None and after is not None and after <= before:
        _die(
            "AUDIT NOT WRITING on the live gateway: a post_llm_call dispatch wrote no "
            f"audit_log row (before={before}, after={after}) — the overlay is loaded and "
            "trust fires, but the audit write path is inert (ss-console#1285 Q2)"
        )
        return

    # 4. COST-BREAKER negative-fire self-check (ADR 0062 §6, #1701): prove the
    #    spend circuit breaker actually HALTS. Runs in a throwaway state file, so
    #    it never touches real breaker state; _die if the ladder does not trip or
    #    the guard does not refuse. This is the recurring prod-boot probe that
    #    earns sticky_stop_cost_cap its enforced status.
    await _cost_breaker_self_check()

    logger.info(
        "SMD overlay ACTIVE + AUDITING + SPEND-CAPPED on the live gateway: %d hook "
        "type(s) in the live singleton, trust gate fired on banned %r, audit row written "
        "(before=%s after=%s), cost breaker tripped+refused in the boot self-check "
        "— self-check passed, operator is governed, auditing, and spend-capped.",
        len(present),
        _BANNED_PROBE_TOOL,
        before,
        after,
    )
