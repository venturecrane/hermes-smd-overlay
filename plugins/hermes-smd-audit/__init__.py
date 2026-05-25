"""hermes-smd-audit — per-tool and per-LLM-call audit emission to per-customer D1.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):

- ``post_tool_call`` (model_tools.py:826-836) — one D1 row per tool invocation
  with ``duration_ms``; banned tool names produce an ``INVARIANT_VIOLATION``
  refusal row (defense-in-depth, the trust plugin should have caught the
  invocation pre-call).
- ``post_llm_call`` (run_agent.py:15901-15910) — one D1 row per completed
  turn. Interrupted turns do NOT fire this hook; cross-correlation by
  ``session_id`` against the on_session_end memory-mirror hook captures them.

Hook callbacks are exception-safe. The Hermes dispatcher wraps each
callback in its own try/except, but a noisy callback creates log spam.
Real emission work is wrapped here; failures land at ``logger.warning``
and never re-raise.

Audit rows are written through ``shared.d1_client.D1Client`` against the
per-customer D1 binding named by ``SMD_D1_AUDIT_BINDING``. The binding is
runtime-asserted against ``SMD_CUSTOMER_SLUG`` on every call (the D1Client
contract). Secret values never appear in log output; only the action_type,
actor, and skill_name are logged on failure.
"""

import logging
from typing import Any

from shared.d1_client import D1Client
from shared.secrets import require

from . import emit, immutability, integrity, schemas  # noqa: F401 — surface for tests
from .emit import AuditLogWriter, emit_llm_event, emit_tool_event

logger = logging.getLogger(__name__)


# Module-level writer holder. Populated by ``register()`` from the env-bound
# D1Client. Stays ``None`` if the registration failed; the hook callbacks
# log a warning and return when the writer is absent so the agent keeps
# running through a misconfigured Machine.
_WRITER: AuditLogWriter | None = None
_CUSTOMER_SLUG: str | None = None


def _writer() -> AuditLogWriter | None:
    """Read the module-level writer. Returns ``None`` if registration failed."""
    return _WRITER


def on_post_tool_call(**kwargs: Any) -> None:
    """Write one TOOL_CALL_COMPLETED audit row per tool invocation.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms

    Exception-safe: any failure (D1 unreachable, schema drift, banned tool
    classification raised unexpectedly) is logged and swallowed. The
    Hermes dispatcher's own try/except is a backstop, not the primary
    guard.
    """
    writer = _writer()
    if writer is None or _CUSTOMER_SLUG is None:
        # Registration failed; nothing to do. Log once at debug so we
        # don't spam every tool call when the audit plugin is disabled.
        logger.debug("hermes-smd-audit: post_tool_call skipped (writer unconfigured)")
        return

    try:
        emit_tool_event(
            writer,
            customer=_CUSTOMER_SLUG,
            tool_name=kwargs.get("tool_name", ""),
            args=kwargs.get("args") if isinstance(kwargs.get("args"), dict) else None,
            result=kwargs.get("result"),
            task_id=kwargs.get("task_id", "") or "",
            session_id=kwargs.get("session_id", "") or "",
            tool_call_id=kwargs.get("tool_call_id", "") or "",
            duration_ms=kwargs.get("duration_ms"),
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: post_tool_call emission failed (tool=%s session=%s err=%s)",
            kwargs.get("tool_name"),
            kwargs.get("session_id"),
            exc,
        )


def on_post_llm_call(**kwargs: Any) -> None:
    """Write one LLM_TURN_COMPLETED audit row per completed turn.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, assistant_response, conversation_history,
        model, platform

    Exception-safe: any failure is logged and swallowed.
    """
    writer = _writer()
    if writer is None or _CUSTOMER_SLUG is None:
        logger.debug("hermes-smd-audit: post_llm_call skipped (writer unconfigured)")
        return

    try:
        emit_llm_event(
            writer,
            customer=_CUSTOMER_SLUG,
            session_id=kwargs.get("session_id", "") or "",
            user_message=kwargs.get("user_message", "") or "",
            assistant_response=kwargs.get("assistant_response", "") or "",
            model=kwargs.get("model", "") or "",
            platform=kwargs.get("platform", "") or "",
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: post_llm_call emission failed (session=%s err=%s)",
            kwargs.get("session_id"),
            exc,
        )


def register(ctx) -> None:
    """Plugin entry point. Wires both hooks.

    Resolves the D1 binding and customer slug from env at registration time
    (failing loud here is correct — the Machine cannot ship audit rows
    without these secrets). If registration fails, the plugin still
    registers the hook callbacks (so Hermes accepts the plugin) but they
    no-op at debug level.
    """
    global _WRITER, _CUSTOMER_SLUG

    try:
        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        _CUSTOMER_SLUG = secrets_map["SMD_CUSTOMER_SLUG"]
        client = D1Client(
            binding_name=secrets_map["SMD_D1_AUDIT_BINDING"],
            customer_slug=_CUSTOMER_SLUG,
        )
        _WRITER = AuditLogWriter(client)
        logger.info(
            "hermes-smd-audit registered (customer=%s binding=%s)",
            _CUSTOMER_SLUG,
            secrets_map["SMD_D1_AUDIT_BINDING"],
        )
    except KeyError as exc:
        # Per AGENTS.md hard rule #4, the plugin manifest declares its
        # ``requires_env`` so Hermes should not load us with missing env.
        # If it does, we still register the callbacks (no-op) so the
        # dispatcher's contract holds.
        _WRITER = None
        _CUSTOMER_SLUG = None
        logger.warning("hermes-smd-audit: env not configured, hooks will no-op: %s", exc)

    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
