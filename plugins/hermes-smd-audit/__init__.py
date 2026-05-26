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
from .emit import (
    AuditLogWriter,
    detect_skill_manage_creation,
    emit_agent_skill_created_event,
    emit_llm_event,
    emit_subagent_stop_event,
    emit_tool_event,
)

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

    tool_name = kwargs.get("tool_name", "") or ""
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else None
    session_id = kwargs.get("session_id", "") or ""
    tool_call_id = kwargs.get("tool_call_id", "") or ""

    try:
        emit_tool_event(
            writer,
            customer=_CUSTOMER_SLUG,
            tool_name=tool_name,
            args=args,
            result=kwargs.get("result"),
            task_id=kwargs.get("task_id", "") or "",
            session_id=session_id,
            tool_call_id=tool_call_id,
            duration_ms=kwargs.get("duration_ms"),
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: post_tool_call emission failed (tool=%s session=%s err=%s)",
            tool_name,
            session_id,
            exc,
        )

    # ADR 0017 §40 — when `skill_manage` is invoked to create a new skill,
    # emit AGENT_SKILL_CREATED in addition to TOOL_CALL_COMPLETED. This is
    # the mirror-don't-gate observation surface; the Curator's flow is not
    # intercepted.
    try:
        created_slug = detect_skill_manage_creation(tool_name=tool_name, args=args)
        if created_slug is not None:
            emit_agent_skill_created_event(
                writer,
                customer=_CUSTOMER_SLUG,
                session_id=session_id,
                skill_name_created=created_slug,
                skill_manage_args=args,
                tool_call_id=tool_call_id,
            )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: AGENT_SKILL_CREATED emission failed (session=%s err=%s)",
            session_id,
            exc,
        )


def on_subagent_stop(**kwargs: Any) -> None:
    """Write one SUBAGENT_STOPPED audit row per delegated child agent.

    ADR 0021 Stream C: every ``delegate_task`` parent expects one audit row
    per child so the assembly-time schema contract has a visible trail
    (mirror-don't-gate per ADR 0016). The hook fires after each delegated
    subagent's run terminates, regardless of return status.

    Expected kwargs (per Hermes subagent_stop hook contract):
        session_id, parent_session_id, child_role, child_status,
        duration_ms, task_id (optional), skill_name (optional)

    Exception-safe: any failure is logged and swallowed.
    """
    writer = _writer()
    if writer is None or _CUSTOMER_SLUG is None:
        logger.debug("hermes-smd-audit: subagent_stop skipped (writer unconfigured)")
        return

    try:
        emit_subagent_stop_event(
            writer,
            customer=_CUSTOMER_SLUG,
            session_id=kwargs.get("session_id", "") or "",
            parent_session_id=kwargs.get("parent_session_id"),
            child_role=kwargs.get("child_role", "") or "",
            child_status=kwargs.get("child_status", "") or "",
            duration_ms=kwargs.get("duration_ms"),
            task_id=kwargs.get("task_id", "") or "",
            skill_name=kwargs.get("skill_name"),
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: subagent_stop emission failed (child_role=%s session=%s err=%s)",
            kwargs.get("child_role"),
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
    ctx.register_hook("subagent_stop", on_subagent_stop)
