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
import os
from typing import Any

from shared.audit_client import BrokerAuditClient, audit_client_from_env
from shared.d1_client import D1Client
from shared.secrets import require

from . import (  # noqa: F401 — surface for tests
    emit,
    immutability,
    integrity,
    schemas,
    skill_capture,
)
from .emit import (
    AuditLogWriter,
    detect_skill_manage_creation,
    emit_agent_skill_created_event,
    emit_llm_event,
    emit_subagent_stop_event,
    emit_tool_event,
)
from .skill_capture import (
    R2Config,
    capture_skill_body,
    load_r2_config_from_env,
    reconcile_pending_bodies,
)

logger = logging.getLogger(__name__)


# Module-level writer holder. Populated by ``register()`` from the env-bound
# D1Client. Stays ``None`` if the registration failed; the hook callbacks
# log a warning and return when the writer is absent so the agent keeps
# running through a misconfigured Machine.
_WRITER: AuditLogWriter | None = None
_CUSTOMER_SLUG: str | None = None
# ADR 0022 Stream 2: per-customer D1 client for the agent_skills_inventory
# writes, plus the R2 config for the skill-bodies bucket. Both populated by
# register(); the hook no-ops cleanly when either is None.
_SKILL_D1_CLIENT: D1Client | None = None
_R2_CONFIG: R2Config | None = None


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
            # ADR 0022 Stream 2 — write-ahead body capture. Reads SKILL.md
            # from the per-profile skills directory and persists to per-
            # customer R2 with the write-ahead pattern. No-op when the
            # D1 client or R2 config isn't wired (logged once at register).
            _maybe_capture_skill_body(
                customer_slug=_CUSTOMER_SLUG,
                session_id=session_id,
                created_slug=created_slug,
                args=args,
            )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: AGENT_SKILL_CREATED emission failed (session=%s err=%s)",
            session_id,
            exc,
        )


def _maybe_capture_skill_body(
    *,
    customer_slug: str,
    session_id: str,
    created_slug: str,
    args: dict | None,
) -> None:
    """Invoke skill_capture.capture_skill_body when the inventory client
    and R2 config are wired. Wrapped in its own try/except per the
    AGENTS.md exception-safety rule."""
    if _SKILL_D1_CLIENT is None:
        # Logged once at register(); skip silently per-call.
        return
    # Persona slug: the agent runs under one profile per Machine (ADR 0011
    # v1: length-1 personas[]). Hermes exposes the active profile via
    # HERMES_ACTIVE_PROFILE; fall back to the SMD_ACTIVE_PERSONA secret
    # if present, then to the env-set HERMES_HOME shape default.
    persona_slug = os.getenv("HERMES_ACTIVE_PROFILE") or os.getenv("SMD_ACTIVE_PERSONA") or ""
    if not persona_slug:
        # The Curator's args may carry it explicitly for some flows.
        if isinstance(args, dict):
            candidate = args.get("persona_slug") or args.get("persona")
            if isinstance(candidate, str) and candidate.strip():
                persona_slug = candidate.strip()
    if not persona_slug:
        logger.debug(
            "skill_capture: persona_slug unresolved (skill=%s); skipping body capture",
            created_slug,
        )
        return

    hermes_home = os.getenv("HERMES_HOME") or "/opt/data"
    try:
        result = capture_skill_body(
            _SKILL_D1_CLIENT,
            _R2_CONFIG,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=created_slug,
            source_turn_id=session_id,
            hermes_home=hermes_home,
        )
        logger.info(
            "skill_capture: persona=%s skill=%s status=%s reason=%s",
            persona_slug,
            created_slug,
            result.r2_status,
            result.reason,
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "skill_capture: unhandled exception (persona=%s skill=%s err=%s)",
            persona_slug,
            created_slug,
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
    """Plugin entry point. Wires the three hooks plus ADR 0022 Stream 2.

    Resolves the D1 binding and customer slug from env at registration time
    (failing loud here is correct — the Machine cannot ship audit rows
    without these secrets). If registration fails, the plugin still
    registers the hook callbacks (so Hermes accepts the plugin) but they
    no-op at debug level.

    ADR 0022 Stream 2 — separately resolves the R2 skill-bodies config
    (R2_ENDPOINT_URL + R2_SKILL_BODIES_* env). When absent, the body
    capture path no-ops cleanly and the boot reconciler is skipped.
    Audit emission continues unchanged on that misconfiguration.
    """
    global _WRITER, _CUSTOMER_SLUG, _SKILL_D1_CLIENT, _R2_CONFIG

    try:
        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        _CUSTOMER_SLUG = secrets_map["SMD_CUSTOMER_SLUG"]
        # Single selection point for the audit transport: a BrokerAuditClient
        # when SMD_AUDIT_BROKER_SOCKET is set (the ledger file is broker-owned,
        # OP-P1-4), else a direct D1Client on the audit binding (legacy/test).
        client = audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
        _broker_mode = isinstance(client, BrokerAuditClient)
        _WRITER = AuditLogWriter(client)
        # The Machine's bootstrap does not apply the per-customer migrations, so
        # ensure the audit_log table exists before the first write (ss-console
        # #1285). In broker mode the broker owns the ledger file and its schema
        # (the agent uid cannot write it), so skip — calling CREATE TABLE through
        # the broker client (read-only file) would fail. Idempotent otherwise; a
        # failure must not crash plugin load.
        if not _broker_mode:
            try:
                _WRITER.ensure_schema()
            except Exception as exc:  # noqa: BLE001 — never crash Hermes plugin load
                logger.error(
                    "hermes-smd-audit: ensure_schema failed; audit writes will fail: %s", exc
                )
        # ADR 0022 Stream 2 — skill inventory is MUTABLE agent state
        # (skill_capture INSERTs a 'pending' row then UPDATEs it to
        # 'persisted'/'failed'). It must live in a hermes-WRITABLE file. When
        # the audit ledger is broker-owned (OP-P1-4 hardening), the ledger
        # file is read-only to the agent uid, so the skills table is split
        # onto its own binding ``SMD_D1_AGENT_STATE_BINDING``. Falls back to
        # the audit binding when that env is unset (direct mode / existing
        # tests) so today's single-file behavior is unchanged.
        state_binding = (
            os.environ.get("SMD_D1_AGENT_STATE_BINDING") or secrets_map["SMD_D1_AUDIT_BINDING"]
        )
        _SKILL_D1_CLIENT = D1Client(
            binding_name=state_binding,
            customer_slug=_CUSTOMER_SLUG,
        )
        # The Machine bootstrap does not run the per-customer migrations, so
        # the agent_skills_inventory table may not exist — and a freshly-split
        # agent-state file definitely won't have it. Create it idempotently on
        # the hermes-owned binding (mirrors the audit_log ensure_schema above).
        try:
            for ddl in schemas.AUDIT_PLUGIN_DDLS:
                _SKILL_D1_CLIENT.execute(ddl)
        except Exception as exc:  # noqa: BLE001 — never crash Hermes plugin load
            logger.error(
                "hermes-smd-audit: agent-state ensure_schema failed; skill capture will fail: %s",
                exc,
            )
        logger.info(
            "hermes-smd-audit registered (customer=%s audit_binding=%s state_binding=%s)",
            _CUSTOMER_SLUG,
            secrets_map["SMD_D1_AUDIT_BINDING"],
            state_binding,
        )
    except KeyError as exc:
        # Per AGENTS.md hard rule #4, the plugin manifest declares its
        # ``requires_env`` so Hermes should not load us with missing env.
        # If it does, we still register the callbacks (no-op) so the
        # dispatcher's contract holds.
        _WRITER = None
        _CUSTOMER_SLUG = None
        _SKILL_D1_CLIENT = None
        logger.warning("hermes-smd-audit: env not configured, hooks will no-op: %s", exc)

    # ADR 0022 Stream 2 — R2 skill-bodies config is optional from this
    # plugin's perspective: when missing, the capture path INSERTs the
    # D1 row in 'pending' and the boot reconciler retries when env later
    # appears. We don't fail registration on missing R2 env so misconfig
    # never blocks the audit pipeline.
    _R2_CONFIG = load_r2_config_from_env()
    if _R2_CONFIG is None:
        logger.warning(
            "hermes-smd-audit: R2 skill-bodies env not configured (R2_ENDPOINT_URL + "
            "R2_SKILL_BODIES_{BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY}); body capture "
            "writes pending D1 rows only, reconciler skipped on this boot."
        )
    else:
        logger.info(
            "hermes-smd-audit: R2 skill-bodies bucket configured (bucket=%s)",
            _R2_CONFIG.bucket,
        )
        # Run the boot reconciler synchronously at registration. It is
        # bounded (max_iterations=500 by default) and reads from the
        # same D1 client; this is a one-pass best-effort retry.
        if _SKILL_D1_CLIENT is not None and _CUSTOMER_SLUG is not None:
            try:
                summary = reconcile_pending_bodies(
                    _SKILL_D1_CLIENT,
                    _R2_CONFIG,
                    hermes_home=os.getenv("HERMES_HOME") or "/opt/data",
                    customer_slug=_CUSTOMER_SLUG,
                )
                logger.info(
                    "hermes-smd-audit reconciler: scanned=%d persisted=%d failed=%d skipped_missing_body=%d",
                    summary.scanned,
                    summary.persisted,
                    summary.failed,
                    summary.skipped_missing_body,
                )
            except Exception as exc:  # noqa: BLE001 — reconciler is best-effort
                logger.warning("hermes-smd-audit reconciler failed: %s", exc)

    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("subagent_stop", on_subagent_stop)
