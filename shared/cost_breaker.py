"""Cost circuit breaker glue — ADR 0062, ss-console #1661.

Binds the vendored sticky_stop state machine (``shared/sticky_stop.py``) to
this Machine's runtime: Machine-local SQLite persistence on the Fly volume,
audit emission through the broker-owned ledger, and thresholds authored in
``customer.yaml safety.sticky_stop`` (platform defaults apply when
unauthored — an integrity control per ADR 0035, not a client entitlement).

Two consumers:

* The durable-job segment loop (``shared/job_segment.py``) — the one seam
  where exact per-segment cents are in hand (Hermes-native
  ``agent.usage_pricing``). It records spend after every segment and asserts
  the breaker before firing the next one; a HARD_STOP dead-letters the job
  to ``needs_review`` (the existing budget-exhaustion pattern).
* The webhook gate (``webhook_gate.py``) — reads the level (read-only) and
  refuses to wake the agent while HARD_STOP is pinned, so a job-path trip
  or Captain stop parks inbound work too.

The interactive turn seam has no token counts (the Hermes ``post_llm_call``
hook passes ``model`` but no usage), so exact interactive enforcement is
blocked on an upstream usage kwarg — named in ADR 0062, tracked separately.
The gate's inbound daily wake cap bounds that path in the meantime.

Persistence path: ``/opt/data/smd/sticky_stop.db`` (hermes-writable; the
root-owned gate opens it read-only). Overridable via
``SMD_STICKY_STOP_DB_PATH`` for tests — declared in contracts/consumes.yaml.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any

from shared.audit_contract import INSERT_SQL, build_audit_params
from shared.ids import iso_utc, ulid
from shared.sticky_stop import (
    DEFAULT_THRESHOLDS,
    SqliteStickyStopStore,
    StickyStopAuditRecord,
    StickyStopError,
    StickyStopLevel,
    StickyStopMachine,
    StickyStopState,
    StickyStopThresholds,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/opt/data/smd/sticky_stop.db"

# Mirrors ss-console operator/migrations/0004_sticky_stop_state.sql. The
# Machine bootstrap does not apply per-customer migrations (same posture as
# the audit plugin's ensure_schema, ss-console #1285), so the glue creates
# the table idempotently on first open.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sticky_stop_state (
  customer                          TEXT NOT NULL,
  persona                           TEXT NOT NULL,
  level                             TEXT NOT NULL DEFAULT 'OK',
  updated_at                        TEXT NOT NULL,
  reason                            TEXT,
  condition                         TEXT,
  consecutive_tool_failures         INTEGER NOT NULL DEFAULT 0,
  tool_failure_window_started_at    TEXT,
  refusal_count                     INTEGER NOT NULL DEFAULT 0,
  refusal_window_started_at         TEXT,
  cost_cents_today                  INTEGER NOT NULL DEFAULT 0,
  cost_date                         TEXT,
  PRIMARY KEY (customer, persona)
)
"""


def db_path() -> str:
    """Resolve the breaker state file path (env override for tests)."""
    return os.environ.get("SMD_STICKY_STOP_DB_PATH") or DEFAULT_DB_PATH


class AuditLedgerSink:
    """Adapts StickyStopAuditRecord onto the Machine's audit ledger.

    Writes through the same transport the audit plugin uses
    (``shared.audit_client.audit_client_from_env`` — broker-owned ledger in
    production, direct file binding in legacy/test). Write failures raise:
    a transition the substrate cannot record is a transition that did not
    safely happen (issue #891 invariant); the state-machine callers treat
    that as the trip standing.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def write(self, record: StickyStopAuditRecord) -> None:
        params = build_audit_params(
            row_id=ulid(),
            ts=iso_utc(),
            action_type=record.action_type,
            actor=record.actor,
            actor_role=record.actor_role,
            skill_name=record.skill_name,
            metadata=record.metadata,
        )
        self._client.execute(INSERT_SQL, *params)


def thresholds_from_config(config: Any) -> StickyStopThresholds:
    """Build thresholds from a CustomerConfig, defaulting when unauthored.

    Only ``cost_cap_daily_cents`` is customer-authorable in v1 (ADR 0062 §5);
    the ladder percentages (warn 80 / soft 100 / hard 200) are platform
    semantics. A malformed value fails toward the platform default rather
    than fail-open.
    """
    authored: int | None = None
    try:
        block = config.sticky_stop if config is not None else {}
        raw = block.get("cost_cap_daily_cents")
        if raw is not None:
            authored = int(raw)
            if authored <= 0:
                raise ValueError("cost_cap_daily_cents must be positive")
    except Exception as exc:  # noqa: BLE001 — default, never fail-open
        logger.warning(
            "cost_breaker: invalid safety.sticky_stop.cost_cap_daily_cents; "
            "using platform default %s: %s",
            DEFAULT_THRESHOLDS.cost_daily_cents,
            exc,
        )
        authored = None
    if authored is None:
        return DEFAULT_THRESHOLDS
    from dataclasses import replace

    return replace(DEFAULT_THRESHOLDS, cost_daily_cents=authored)


class CostBreaker:
    """Per-process handle on the breaker: sync facade over the async machine.

    The job worker and segment loop are synchronous; sticky_stop is async.
    Each call runs on a private event loop. SQLite serializes writers via
    the connection's busy timeout; state is one row per (customer, persona)
    and the write rate is per-segment, so contention is negligible.
    """

    def __init__(
        self,
        *,
        customer: str,
        persona: str,
        machine: StickyStopMachine,
    ) -> None:
        self._customer = customer
        self._persona = persona
        self._machine = machine
        self._lock = threading.Lock()

    def _run(self, coro):
        with self._lock:
            return asyncio.run(coro)

    def record_cost_cents(self, amount_cents: int) -> StickyStopState:
        """Record segment spend; returns the (possibly transitioned) state."""
        return self._run(
            self._machine.record_cost_cents(
                customer=self._customer,
                persona=self._persona,
                amount_cents=amount_cents,
            )
        )

    def record_tool_failure(self, skill_name: str | None = None) -> StickyStopState:
        """Record one failed tool call. Climbs the consecutive-failure ladder."""
        return self._run(
            self._machine.record_tool_failure(
                customer=self._customer,
                persona=self._persona,
                skill_name=skill_name,
            )
        )

    def record_tool_success(self) -> StickyStopState:
        """Record one successful tool call; resets the consecutive-failure streak.

        NOT optional, and not symmetry for its own sake. The ladder counts
        CONSECUTIVE failures, so a caller that records failures and never
        successes turns every seat into a slow march toward HARD_STOP —
        the streak would only ever climb, and an Operator that failed eight
        calls across eight healthy days would stop as if it had looped. The
        failure and success arms must be fed from the same signal or neither
        should be fed at all.
        """
        return self._run(
            self._machine.record_tool_success(
                customer=self._customer,
                persona=self._persona,
            )
        )

    def record_refusal(self, skill_name: str | None = None) -> StickyStopState:
        """Record one trust-ceiling refusal. Climbs the refusal-cascade ladder."""
        return self._run(
            self._machine.record_refusal(
                customer=self._customer,
                persona=self._persona,
                skill_name=skill_name,
            )
        )

    def assert_allowed(self) -> StickyStopState:
        """Raise StickyStopError when HARD_STOP is pinned; else return state."""
        return self._run(
            self._machine.assert_allowed(
                customer=self._customer,
                persona=self._persona,
            )
        )


def build_breaker(
    *,
    customer: str,
    persona: str,
    audit_client: Any,
    config: Any = None,
    path: str | None = None,
) -> CostBreaker:
    """Construct the production breaker for this Machine.

    Opens (creating parent dir + table if needed) the Machine-local state
    file, wires the audit-ledger sink, and applies authored-or-default
    thresholds. Callers pass the audit client they already hold (the job
    plugin constructs one via ``audit_client_from_env``).
    """
    resolved = Path(path or db_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    machine = StickyStopMachine(
        store=SqliteStickyStopStore(conn),
        audit_writer=AuditLedgerSink(audit_client),
        thresholds=thresholds_from_config(config),
    )
    return CostBreaker(customer=customer, persona=persona, machine=machine)


class _NoAuditSink:
    """A sink that records nothing. Used for the gate-driven clear: the
    audit-ledger broker PID-gates appends to the gateway process (OP-P1-4),
    so the webhook-gate process CANNOT write the Machine ledger. That is by
    design — the STOP is a runtime self-protection event on the Machine
    ledger (emitted from the agent process), but the RESUME is a Captain
    governance action, audited control-plane-side where the Captain is
    authenticated (the console's admin clear endpoint). The state reset is
    the recovery; the Machine ledger does not need the resume row."""

    async def write(self, record: Any) -> None:  # noqa: D401
        return None


def clear_hard_stops(
    *,
    captain_id: str,
    reason: str,
    audit_client: Any = None,
    path: str | None = None,
) -> list[dict]:
    """Captain recovery (ADR 0062 §6): clear every non-OK sticky_stop row.

    The state machine's ``clear()`` is the ONLY backward transition. Returns
    the cleared rows (customer, persona, prior level); empty when nothing was
    pinned or no state file exists yet.

    ``audit_client`` is optional. Passed (agent-process callers): each cleared
    row emits an audited AGENT_RESUMED through the ledger sink. None (the
    gate-driven clear): no Machine-ledger row is written — the broker refuses
    gate-process appends by design, and the resume is audited control-plane
    side. The state reset happens regardless.

    Caller responsibility (module contract): verify the actor is a Captain
    BEFORE invoking. The gate endpoint enforces the console-proxy bearer
    (WEBHOOK_SECRET_MCP) — the console authenticated the Captain upstream.
    """
    if not captain_id or not reason:
        raise ValueError("captain_id and reason are required")
    resolved = Path(path or db_path())
    if not resolved.exists():
        return []
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        rows = conn.execute(
            "SELECT customer, persona, level FROM sticky_stop_state WHERE level != 'OK'"
        ).fetchall()
        if not rows:
            return []
        sink = AuditLedgerSink(audit_client) if audit_client is not None else _NoAuditSink()
        machine = StickyStopMachine(
            store=SqliteStickyStopStore(conn),
            audit_writer=sink,
        )
        cleared: list[dict] = []
        for customer, persona, level in rows:
            asyncio.run(
                machine.clear(
                    customer=customer, persona=persona, captain_id=captain_id, reason=reason
                )
            )
            cleared.append({"customer": customer, "persona": persona, "prior_level": level})
        return cleared
    finally:
        conn.close()


def pin_hard_stops(
    *,
    actor_id: str,
    reason: str,
    path: str | None = None,
) -> list[dict]:
    """Operator-initiated pause (ss#2003): pin HARD_STOP on every row.

    The sticky-stop module's own docstring names this path — "a human pinning
    a stop" — as the operator-initiated twin of the system-initiated trips.
    Pins the ``_machine`` row (the key both the interactive meter and the job
    worker assert against) plus every other existing row, creating the
    ``_machine`` row if the state file is fresh. Sticky by construction (same
    persistence the system trips use; survives reboot); the ONLY way back is
    ``clear_hard_stops`` — the state machine's sole backward transition.

    No Machine-ledger audit row is written from here, for the same reason as
    the gate-driven clear: the broker PID-gates ledger appends to the gateway
    process, and the pause is a governance action audited control-plane-side
    where the actor was authenticated. Returns the pinned rows
    (customer, persona, prior level).

    Caller responsibility (module contract, mirror of clear_hard_stops):
    authenticate the actor BEFORE invoking.
    """
    if not actor_id or not reason:
        raise ValueError("actor_id and reason are required")
    resolved = Path(path or db_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute(_CREATE_TABLE_SQL)
        slug = os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "_machine"
        now = iso_utc()
        stamped_reason = f"operator_pause by {actor_id}: {reason}"
        rows = conn.execute("SELECT customer, persona, level FROM sticky_stop_state").fetchall()
        pinned: list[dict] = []
        seen_machine = False
        for customer, persona, level in rows:
            if persona == "_machine":
                seen_machine = True
            conn.execute(
                "UPDATE sticky_stop_state SET level = ?, reason = ?, updated_at = ? "
                "WHERE customer = ? AND persona = ?",
                (StickyStopLevel.HARD_STOP.value, stamped_reason, now, customer, persona),
            )
            pinned.append({"customer": customer, "persona": persona, "prior_level": level})
        if not seen_machine:
            conn.execute(
                "INSERT INTO sticky_stop_state (customer, persona, level, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, "_machine", StickyStopLevel.HARD_STOP.value, stamped_reason, now),
            )
            pinned.append({"customer": slug, "persona": "_machine", "prior_level": "OK"})
        conn.commit()
        return pinned
    finally:
        conn.close()


# Longest reason string the observer will carry off the box. Real reasons are
# structured counters (~80 chars); the cap only bounds a pathological write.
_REASON_MAX_CHARS = 300


@dataclasses.dataclass(frozen=True)
class StopStateView:
    """What an off-box observer can learn about the ladder, read-only.

    ``reason`` and ``condition`` belong to the SAME row that produced
    ``level`` — the worst row, tie-broken by most-recent ``updated_at`` — so a
    reader never pairs one persona's level with another's cause.
    """

    level: str | None
    reason: str | None = None
    condition: str | None = None


def read_stop_state(path: str | None = None) -> StopStateView:
    """Read the worst persisted level across personas WITH its cause.

    Used by the webhook gate (root) and the heartbeat payload. The level alone
    tells an operator that a seat stopped; it cannot tell them why, and the
    four meters that drive the ladder (tool failures, refusals, runtime,
    cost) produce very different investigations. The store already records
    ``reason`` and ``condition`` on the transition — this carries them out.

    Failure modes, in the fail-toward-unknown direction:
      * state file absent (fresh Machine)  -> level None, callers read OK
      * read error                         -> level "unknown", no cause
      * pre-cause schema on an old seat    -> level only, no cause
    """
    resolved = path or db_path()
    if not os.path.exists(resolved):
        return StopStateView(level=None)
    order = [
        StickyStopLevel.OK.value,
        StickyStopLevel.WARN.value,
        StickyStopLevel.SOFT_STOP.value,
        StickyStopLevel.HARD_STOP.value,
    ]
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        try:
            try:
                rows = conn.execute(
                    "SELECT level, reason, condition, updated_at FROM sticky_stop_state"
                ).fetchall()
            except sqlite3.OperationalError:
                # A seat still running a pre-cause schema. The level is the
                # contract; the cause is the enhancement. Degrade, never fail.
                rows = [
                    (level, None, None, None)
                    for (level,) in conn.execute("SELECT level FROM sticky_stop_state").fetchall()
                ]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — read-only observer
        logger.warning("cost_breaker: read_stop_state failed: %s", exc)
        return StopStateView(level="unknown")
    if not rows:
        return StopStateView(level=StickyStopLevel.OK.value)
    worst = StopStateView(level=StickyStopLevel.OK.value)
    worst_rank = 0
    worst_updated = ""
    for level, reason, condition, updated_at in rows:
        if level not in order:
            continue
        rank = order.index(level)
        stamp = updated_at or ""
        # Strictly-worse wins; an equal level wins only on a later stamp, so
        # the cause always belongs to the row whose level we are reporting.
        if rank < worst_rank or (rank == worst_rank and stamp <= worst_updated):
            continue
        text = str(reason)[:_REASON_MAX_CHARS] if reason else None
        worst = StopStateView(
            level=level,
            reason=text,
            condition=str(condition) if condition else None,
        )
        worst_rank = rank
        worst_updated = stamp
    return worst


def read_level(path: str | None = None) -> str | None:
    """Read the worst (max) persisted level across personas, read-only.

    Thin wrapper over :func:`read_stop_state` for callers that only gate on
    the level. See that function for the failure modes.
    """
    return read_stop_state(path).level


async def run_boot_probe() -> tuple[bool, str]:
    """Negative-fire self-probe (ADR 0062 §6, ss-console #1701): prove the
    breaker actually HALTS. In a throwaway db, record spend far past a 1-cent
    cap and verify (a) the ladder trips HARD_STOP and (b) ``assert_allowed``
    then REFUSES. Returns ``(ok, reason)``; ok=False means the breaker is inert
    this boot and the caller must fail closed.

    Async by design: the boot activation handler runs inside the gateway's
    event loop, so the ``CostBreaker`` sync facade (which calls ``asyncio.run``)
    cannot be used there — this drives the async state machine directly.
    """
    from dataclasses import replace

    fd, tmp = tempfile.mkstemp(prefix="smd-breaker-probe-", suffix=".db")
    os.close(fd)
    os.unlink(tmp)  # recreated below; mkstemp just reserves a unique name
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(tmp, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
        thresholds = replace(DEFAULT_THRESHOLDS, cost_daily_cents=1)
        machine = StickyStopMachine(
            store=SqliteStickyStopStore(conn),
            audit_writer=_NoAuditSink(),
            thresholds=thresholds,
        )
        # 1000c against a 1c cap = 100000% >> the 200% hard-stop rung.
        recorded = await machine.record_cost_cents(
            customer="_probe", persona="_probe", amount_cents=1000
        )
        state = await machine.get_state("_probe", "_probe")
        if state.level != StickyStopLevel.HARD_STOP:
            # Capture the intermediate values so a future firing is diagnosable
            # (ss #1701): the reason string is all that reaches the boot log /
            # Sentry, and a bare "level=OK" cannot distinguish a transient
            # persistence blip from a real ladder defect. The three signatures:
            #   recorded=HARD_STOP but readback != HARD_STOP -> store/read did
            #     not persist the transition (transient sqlite/fs at boot)
            #   recorded != HARD_STOP                        -> ladder/threshold
            #     logic (a real defect)
            #   cost_cents_today != 1000                     -> the date-reset
            #     path zeroed the recorded spend
            return False, (
                "ladder did not trip HARD_STOP "
                f"(readback level={state.level.value} "
                f"cost_cents_today={state.cost_cents_today} cost_date={state.cost_date!r}; "
                f"record_cost_cents returned level={recorded.level.value} "
                f"cost_cents_today={recorded.cost_cents_today}; "
                f"cap={thresholds.cost_daily_cents}c rungs warn/soft/hard="
                f"{thresholds.cost_warn_pct}/{thresholds.cost_soft_stop_pct}/"
                f"{thresholds.cost_hard_stop_pct}%)"
            )
        try:
            await machine.assert_allowed(customer="_probe", persona="_probe")
        except StickyStopError:
            return True, ""  # correct: the guard refuses at HARD_STOP
        return False, "assert_allowed did not refuse at HARD_STOP"
    except Exception as exc:  # noqa: BLE001
        return False, f"probe raised: {type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass


__all__ = [
    "AuditLedgerSink",
    "CostBreaker",
    "DEFAULT_DB_PATH",
    "StickyStopError",
    "StopStateView",
    "build_breaker",
    "clear_hard_stops",
    "pin_hard_stops",
    "db_path",
    "read_level",
    "read_stop_state",
    "run_boot_probe",
    "thresholds_from_config",
]
