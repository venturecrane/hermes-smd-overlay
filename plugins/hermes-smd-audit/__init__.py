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
from shared.audit_status import NoAuditWarner, write_audit_status
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
# ADR 0062 §4 / #1701: the interactive-turn cost meter feeds the shared
# sticky_stop ladder from post_llm_call. Breaker built lazily on first turn
# (needs the audit client + customer.yaml); _COST_BREAKER_INIT guards the
# one-time build so a construction failure alarms once, not every turn.
_COST_BREAKER: Any = None
_COST_BREAKER_INIT: bool = False

# #64: when the writer never wired, every hook is a silent no-op — say so at
# WARNING on a rate limit (not just once at init) so a dark ledger is visible
# in the logs for as long as it stays dark.
_NO_AUDIT_WARNER = NoAuditWarner()


def _writer() -> AuditLogWriter | None:
    """Read the module-level writer. Returns ``None`` if registration failed."""
    return _WRITER


def _meter_loop_arms(kwargs: dict, breaker: Any = None) -> None:
    """Feed the sticky-stop ladder's runaway-loop arms from one tool outcome.

    WHY THIS EXISTS. ADR 0062's ladder has four arms. Only the cost arm was
    ever fed — ``record_cost_cents`` from the interactive meter and the job
    segment loop. ``record_tool_failure`` and ``record_refusal`` were fully
    implemented, thresholded, audited and tested, and appeared in no caller in
    either repo. An Operator that spent too much stopped; an Operator stuck in
    a loop failing the same call, or refusing every call, did not. The logic
    was complete and simply never told anything had happened.

    THE SIGNAL. ``post_tool_call`` fires after dispatch regardless of outcome
    and carries ``status`` ("ok" | "error" | "blocked") plus ``error_type``
    (None | "tool_error" | "plugin_block"). Both are pin-verified at the
    firing site in docs/hook-surface.md rather than published upstream, so
    they are re-checked at every Hermes rebase.

    POSITIVE-ONLY DETECTION, and the direction matters. Every branch requires
    an explicitly recognised ``status``; an absent or unfamiliar value records
    NOTHING. If the envelope changes, the arms go quiet and the seat behaves
    exactly as it did before this function existed — the old, unbraked
    behaviour. The alternative (treating "no status" as failure) would let an
    upstream rename manufacture a HARD_STOP on a healthy client seat, which is
    a worse outcome than the gap being closed.

    Success is recorded for the same reason it is required on the wrapper: the
    ladder counts CONSECUTIVE failures, so feeding failures without successes
    would make every long-lived seat eventually stop for no reason. Both come
    off the same ``status`` field, so they can never be half-wired.
    """
    try:
        status = kwargs.get("status")
        if status not in ("ok", "error", "blocked"):
            return  # unrecognised or absent envelope: record nothing, brake unchanged
        breaker = breaker if breaker is not None else _cost_breaker()
        if breaker is None:
            return  # not armed (no slug, or construction failed); already logged
        tool_name = kwargs.get("tool_name")
        tool_name = tool_name if isinstance(tool_name, str) and tool_name else None

        if status == "ok":
            breaker.record_tool_success()
            return
        if status == "error":
            state = breaker.record_tool_failure(tool_name)
        else:
            # "blocked" is our own policy layer refusing, which is exactly what
            # the refusal-cascade arm counts. A block from anything other than
            # a plugin is not a refusal, so it is left alone rather than
            # folded into either ladder.
            if kwargs.get("error_type") != "plugin_block":
                return
            state = breaker.record_refusal(tool_name)

        if state is not None and getattr(state, "level", None) is not None:
            level = getattr(state.level, "value", state.level)
            if level != "OK":
                logger.warning(
                    "hermes-smd-audit: sticky-stop ladder at %s after %s (tool=%s)",
                    level,
                    status,
                    tool_name,
                )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-audit: loop-arm metering failed: %s", exc)


async def run_loop_arm_boot_probe() -> tuple[bool, str]:
    """Negative-fire probe for the runaway-loop arms. Returns ``(ok, reason)``.

    WHY A SECOND BOOT PROBE. ``cost_breaker.run_boot_probe`` proves the LADDER
    halts. It cannot prove the loop arms are FED, because it drives the state
    machine directly and the cost arm was already wired. The gap this closes is
    the one that let ``record_tool_failure`` sit implemented, thresholded and
    audited with no caller for months: every test passed, and nothing on a seat
    ever called it.

    So this probe drives the REAL hook handler -- ``_meter_loop_arms``, the same
    function ``on_post_tool_call`` calls -- with the REAL envelope Hermes emits,
    against a THROWAWAY ladder that never touches
    ``/opt/data/smd/sticky_stop.db``. What it asserts is not "the ladder can
    climb" but "a tool failure arriving at the hook moves it".

    Three assertions, because two of them can pass while the control is useless:

      1. A ``status="error"`` envelope trips the throwaway ladder to HARD_STOP.
      2. A ``status="ok"`` envelope RESETS the streak. An arm that only climbs
         would stop every healthy long-lived seat, so a probe that skipped this
         would bless the worst regression this code can have.
      3. An UNRECOGNISED envelope moves nothing. If detection ever stopped being
         positive-only, an upstream rename would start manufacturing stops on
         live seats -- and that failure is invisible to assertions 1 and 2.

    Runs the sync handler in an executor: the activation handler owns the
    gateway's event loop, and ``CostBreaker`` bridges to async via
    ``asyncio.run``, which raises inside a running loop. Threading it is what
    lets the probe exercise the real handler rather than a re-implementation of
    it -- the re-implementation is exactly what would not have caught the
    original bug.
    """
    import asyncio
    import os
    import sqlite3
    import tempfile
    from dataclasses import replace

    from shared.cost_breaker import _CREATE_TABLE_SQL, CostBreaker, _NoAuditSink
    from shared.sticky_stop import (
        DEFAULT_THRESHOLDS,
        SqliteStickyStopStore,
        StickyStopLevel,
        StickyStopMachine,
    )

    fd, tmp = tempfile.mkstemp(prefix="smd-looparm-probe-", suffix=".db")
    os.close(fd)
    os.unlink(tmp)
    conn = None
    try:
        conn = sqlite3.connect(tmp, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
        # One failure IS the hard stop, so the probe needs a single envelope and
        # cannot pass by accident on a ladder that merely counts.
        thresholds = replace(
            DEFAULT_THRESHOLDS,
            tool_failure_warn=1,
            tool_failure_soft_stop=1,
            tool_failure_hard_stop=1,
        )
        breaker = CostBreaker(
            customer="_probe",
            persona="_probe",
            machine=StickyStopMachine(
                store=SqliteStickyStopStore(conn),
                audit_writer=_NoAuditSink(),
                thresholds=thresholds,
            ),
        )
        loop = asyncio.get_running_loop()

        async def feed(envelope: dict) -> None:
            await loop.run_in_executor(None, _meter_loop_arms, envelope, breaker)

        def level() -> str:
            row = conn.execute(
                "SELECT level FROM sticky_stop_state WHERE customer='_probe'"
            ).fetchone()
            return row[0] if row else StickyStopLevel.OK.value

        def streak():
            row = conn.execute(
                "SELECT consecutive_tool_failures FROM sticky_stop_state WHERE customer='_probe'"
            ).fetchone()
            return row[0] if row else None

        # 1. a failure envelope must trip it
        await feed({"status": "error", "tool_name": "_probe_tool"})
        if level() != StickyStopLevel.HARD_STOP.value:
            return (
                False,
                f"a status=error envelope did not trip the tool-failure arm "
                f"(level={level()}); the hook is not feeding record_tool_failure",
            )

        # 2. a success envelope must reset the streak
        await feed({"status": "ok", "tool_name": "_probe_tool"})
        if streak() != 0:
            return (
                False,
                f"a status=ok envelope did not reset the failure streak "
                f"(consecutive_tool_failures={streak()}); every long-lived seat "
                "would eventually stop for no reason",
            )

        # 3. an unrecognised envelope must move nothing
        before = streak()
        await feed({"status": "no_such_status", "tool_name": "_probe_tool"})
        if streak() != before:
            return (
                False,
                "an unrecognised status moved the ladder; detection is no longer "
                "positive-only, so an upstream envelope rename could stop a live seat",
            )

        return True, "loop arms fed: error trips, ok resets, unknown is inert"
    except Exception as exc:  # noqa: BLE001
        return False, f"loop-arm probe raised: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass


def on_post_tool_call(**kwargs: Any) -> None:
    """Write one TOOL_CALL_COMPLETED audit row per tool invocation.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms

    Exception-safe: any failure (D1 unreachable, schema drift, banned tool
    classification raised unexpectedly) is logged and swallowed. The
    Hermes dispatcher's own try/except is a backstop, not the primary
    guard.
    """
    # Feed the runaway-loop arms BEFORE the writer check below. A dark ledger
    # must not also disarm the brake: the audit writer and the sticky-stop
    # ladder are different failure domains, and "D1 is unreachable" is not a
    # reason to let a looping agent keep going.
    _meter_loop_arms(kwargs)

    writer = _writer()
    if writer is None or _CUSTOMER_SLUG is None:
        # Registration failed; nothing to do. Rate-limited WARNING (#64) so a
        # dark ledger stays visible without spamming every tool call.
        _NO_AUDIT_WARNER.warn(logger, "post_tool_call skipped (writer unconfigured)")
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
        _NO_AUDIT_WARNER.warn(logger, "subagent_stop skipped (writer unconfigured)")
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


def _cost_breaker() -> Any:
    """Lazily build the Machine-wide cost breaker for interactive metering.
    Built once; a failure logs loudly and leaves it None (interactive turns
    then go unmetered — the same unbounded posture as before #1701, never a
    silent regression, and the meter-fail path is not this: this is breaker
    CONSTRUCTION, which if it fails means no ladder at all)."""
    global _COST_BREAKER, _COST_BREAKER_INIT
    if _COST_BREAKER_INIT:
        return _COST_BREAKER
    _COST_BREAKER_INIT = True
    try:
        from shared.cost_breaker import build_breaker
        from shared.customer_config import CustomerConfig

        slug = (
            _CUSTOMER_SLUG or os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG")
        )
        if not slug:
            logger.error("interactive cost meter: no customer slug; breaker NOT armed")
            return None
        config = None
        try:
            config = CustomerConfig.from_volume()
        except Exception as exc:  # noqa: BLE001 — defaults still protect
            logger.warning("interactive cost meter: customer.yaml unreadable; defaults: %s", exc)
        _COST_BREAKER = build_breaker(
            customer=slug,
            persona="_machine",
            audit_client=audit_client_from_env(customer_slug=slug),
            config=config,
        )
        logger.info("interactive cost meter: breaker armed (customer=%s)", slug)
    except Exception as exc:  # noqa: BLE001
        logger.error("interactive cost meter: breaker construction failed; NOT armed: %s", exc)
        _COST_BREAKER = None
    return _COST_BREAKER


def on_post_llm_call(**kwargs: Any) -> None:
    """Write one LLM_TURN_COMPLETED audit row per completed turn.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, assistant_response, conversation_history,
        model, platform

    Exception-safe: any failure is logged and swallowed.
    """
    writer = _writer()
    if writer is None or _CUSTOMER_SLUG is None:
        _NO_AUDIT_WARNER.warn(logger, "post_llm_call skipped (writer unconfigured)")
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

    # ADR 0062 §4 / #1701: meter this interactive turn into the cost breaker.
    # Separate try/except so a metering fault never affects audit emission (and
    # vice versa). Never raises — the meter alarms and keeps going on failure.
    try:
        from shared.interactive_cost_meter import meter_interactive_turn

        meter_interactive_turn(
            model=kwargs.get("model", "") or "",
            conversation_history=kwargs.get("conversation_history"),
            assistant_response=kwargs.get("assistant_response", "") or "",
            session_id=kwargs.get("session_id", "") or "",
            breaker=_cost_breaker(),
            audit_client=audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
            if _CUSTOMER_SLUG
            else None,
        )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning(
            "hermes-smd-audit: interactive cost meter failed (session=%s err=%s)",
            kwargs.get("session_id"),
            exc,
        )


#: What a routine-change row names as its actor. NOT a person and not the
#: agent: the seat sees only the materialized customer.yaml, so the truthful
#: actor is the artifact that carried the change. Inventing a named person the
#: Machine cannot verify would be exactly the fabrication class the venture
#: bans. The role is `operator` because SMD owns that artifact — the firm asks,
#: SMD authors, the seat records what it was handed.
_ROUTINE_ACTOR = "customer.yaml"


def _drain_routine_changes_to_ledger(writer) -> None:
    """Write one row per routine that crossed the scheduled line at this boot.

    ss-console #2498. ``bootstrap/cron_materialize.py`` spooled these to the
    volume; this process is the one the broker lets write the ledger, so it
    turns them into rows. Best-effort by construction — a failed write is
    already counted by ``shared.audit_failure_counter`` and must never keep the
    gateway from registering.
    """
    if writer is None:
        return
    try:
        from shared.routine_change_spool import drain_routine_changes

        changes = drain_routine_changes()
    except Exception as exc:  # noqa: BLE001 — never crash Hermes plugin load
        logger.warning("hermes-smd-audit: routine-change spool unreadable: %s", exc)
        return
    for change in changes:
        enabled = bool(change.get("enabled"))
        try:
            writer.write(
                schemas.AuditEvent(
                    action_type="ROUTINE_ENABLED" if enabled else "ROUTINE_DISABLED",
                    actor=_ROUTINE_ACTOR,
                    actor_role=schemas.ActorRole.OPERATOR,
                    skill_name=str(change.get("skill") or "") or None,
                    metadata={
                        "persona": str(change.get("persona_slug") or ""),
                        "schedule": change.get("schedule"),
                        "source": "customer.yaml cron reconcile at boot",
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — never crash Hermes plugin load
            logger.warning(
                "hermes-smd-audit: routine-change row failed (skill=%s enabled=%s): %s",
                change.get("skill"),
                enabled,
                exc,
            )
    if changes:
        logger.info("hermes-smd-audit: recorded %d routine change(s) from boot", len(changes))


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

    # #64: boot-scoped wiring sentinel for the config-seam snapshot. Written
    # FIRST as un-wired so a mid-registration crash leaves an honest
    # wired:false for this pid (a handler can't sentinel its own
    # non-execution); overwritten below with the real outcome.
    write_audit_status(wired=False, transport=None, reason="registration in progress")

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
        write_audit_status(
            wired=True,
            transport="broker" if _broker_mode else "direct",
            reason=None,
        )
        _drain_routine_changes_to_ledger(_WRITER)
    except KeyError as exc:
        # Per AGENTS.md hard rule #4, the plugin manifest declares its
        # ``requires_env`` so Hermes should not load us with missing env.
        # If it does, we still register the callbacks (no-op) so the
        # dispatcher's contract holds.
        _WRITER = None
        _CUSTOMER_SLUG = None
        _SKILL_D1_CLIENT = None
        logger.warning("hermes-smd-audit: env not configured, hooks will no-op: %s", exc)
        # KeyError from shared.secrets.require names the missing VAR, never a
        # value — safe to record as the un-wired reason.
        write_audit_status(wired=False, transport=None, reason=f"env not configured: {exc}")

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
