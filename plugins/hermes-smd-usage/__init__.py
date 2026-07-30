"""hermes-smd-usage — per-person token attribution (ss-console #2070 O4).

The Operator's cost plane reports what a SEAT spent: one Anthropic workspace
per customer, nightly, day-grained. That was enough while a seat was mostly
scheduled routines. It stops being enough the moment a firm's people hold
sustained conversations with the Operator instead of with claude.ai — the
question becomes *whose* usage, and whether the flat retainer still clears
COGS with everyone talking to it.

This plugin answers it at the only place the answer exists: the agent process,
per API request. It subscribes ``post_api_request`` (verified against the
pinned Hermes ref — ``agent/conversation_loop.py:4104-4137`` passes
``session_id``, ``platform``, ``model``, and a normalized ``usage`` dict;
verify ``vfy_01KYT51JXBHNSSE99PCCXAA2M3``) and folds each request into a
(day, person, model) row on the seat's agent-state db. The console reads it
live over ``runtime_read`` kind ``usage_export``.

**Attribution.** A turn opened by an inbound email carries a recorded
:class:`~shared.inbound.InboundOrigin` for its session (deterministic since
overlay #195), so its tokens attribute to that sender. Everything else — cron,
skills, delegated sub-agents, MCP — attributes to ``system:<platform>``. That
includes delegated work, which can be the *expensive* work: the admin surface
labels the system share accordingly rather than implying it was nobody's.

**Never a gate.** Metering observes; it must not be able to fail a turn. The
hook swallows everything, and a missing db or a malformed usage payload is a
no-op, not an error.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared import inbound

from . import usage_store

logger = logging.getLogger(__name__)

# The agent-state db (peer preferences, skills inventory live here too). Same
# binding-resolution shape as shared.audit_client: a value starting with "/" is
# the path itself, otherwise it names the env var holding it.
_AGENT_STATE_BINDING_ENV = "SMD_D1_AGENT_STATE_BINDING"
_AUDIT_BINDING_ENV = "SMD_D1_AUDIT_BINDING"

_STORE: usage_store.UsageStore | None = None


def _resolve_db_path() -> str | None:
    """Agent-state db path, falling back to the audit binding's directory mate."""
    for env_name in (_AGENT_STATE_BINDING_ENV, _AUDIT_BINDING_ENV):
        raw = os.environ.get(env_name)
        if not raw:
            continue
        path = raw if raw.startswith("/") else os.environ.get(raw)
        if path:
            return path
    return None


def _attribution(session_id: str, platform: str) -> tuple[str, str]:
    """(attributed_to, attribution_source) for this request.

    An inbound-opened session attributes to its verified sender; anything else
    is ``system:<platform>`` — honest about not knowing rather than guessing a
    person onto scheduled or delegated work.
    """
    try:
        origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id)
    except Exception:  # noqa: BLE001 — attribution never raises into the hook
        origin = None
    if origin is not None and origin.sender_address:
        return origin.sender_address.strip().lower(), "inbound_origin"
    return f"system:{platform or 'unknown'}", "fallback"


def on_post_api_request(**kwargs: Any) -> None:
    """Fold one API request's token usage into the per-person meter."""
    if _STORE is None:
        return
    try:
        usage = kwargs.get("usage")
        if not isinstance(usage, dict):
            return
        session_id = kwargs.get("session_id")
        session_id = session_id if isinstance(session_id, str) else ""
        platform = kwargs.get("platform")
        platform = platform if isinstance(platform, str) else ""
        model = kwargs.get("model")
        model = model if isinstance(model, str) else ""

        attributed_to, source = _attribution(session_id, platform)
        _STORE.record(
            attributed_to=attributed_to,
            attribution_source=source,
            model=model,
            usage=usage,
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-usage: post_api_request handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_api_request``.

    The hook is registered unconditionally so the plugin set stays uniform
    across seats; it no-ops when no agent-state binding is configured (a seat
    without one simply has no meter, never a broken turn).
    """
    global _STORE

    path = _resolve_db_path()
    if not path:
        _STORE = None
        logger.info(
            "hermes-smd-usage: no agent-state binding (%s/%s); per-person metering disabled",
            _AGENT_STATE_BINDING_ENV,
            _AUDIT_BINDING_ENV,
        )
    else:
        try:
            _STORE = usage_store.UsageStore(path)
            logger.info("hermes-smd-usage registered (meter=%s)", path)
        except Exception as exc:  # noqa: BLE001
            _STORE = None
            logger.warning("hermes-smd-usage: meter unavailable (%s); metering disabled", exc)

    ctx.register_hook("post_api_request", on_post_api_request)
