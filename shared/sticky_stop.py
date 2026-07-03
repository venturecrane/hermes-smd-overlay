# VENDORED from ss-console operator/safety-substrate/sticky_stop.py (ADR 0062,
# ss-console #1661). Changes land there first, then re-vendor byte-identically
# below this header. The module is self-contained by design (StickyStopAuditRecord
# + StickyStopAuditSink Protocol) so no adapter imports are needed here; the
# overlay-side sink adapter lives in shared/cost_breaker.py.
"""Sticky-stop circuit breaker for runaway agent loops (issue #843).

The platform PRD §7.5 invariant #4 says "don't act" instructions are sticky:
they survive context compaction, restart, and skill reload. That invariant
covers the OPERATOR-INITIATED pause path — a human pinning a stop. This
module covers the SYSTEM-INITIATED path: the substrate noticing the agent
is misbehaving (looping on a failing tool, refusing on every call, burning
budget, exceeding wall-clock time) and pinning a stop on its behalf before
a runaway loop can land work nobody asked for.

The two paths share a sticky-stop persistence layer (this module's
`sticky_stop_state` table, Machine-local SQLite per ADR 0062). An operator
pause and a system-pinned stop produce the same effect
on the dispatch path; the difference is the action_type tag recorded in
the audit log and the recovery surface (operator pauses clear by the same
human; system stops clear via Captain investigation).

Design rules:

* States are forward-only by default. WARN < SOFT_STOP < HARD_STOP. The
  state machine never DECREASES state autonomously. Only Captain-initiated
  clear() resets to OK.

* Conditions are read from customer.yaml `safety.sticky_stop` if present;
  module-level DEFAULT_CONDITIONS apply otherwise. The defaults are
  intentionally conservative — easier to loosen via customer.yaml than to
  recover from a tighter-than-needed loop that ran for hours unnoticed.

* SOFT_STOP semantics: the dispatch path still calls skills, but the
  trust_ceiling for every skill is pinned to draft_for_review for the
  duration. No autonomous escalation. The UI shows a banner. The dashboard
  surface is filed separately — this module only persists the state and
  exposes a read API.

* HARD_STOP semantics: dispatch refuses every skill invocation. The caller
  receives a StickyStopError it must propagate, NOT swallow. Same invariant
  as the audit log writer (issue #891): a state the substrate cannot
  enforce is a state the agent does not run.

* Captain recovery: `clear(customer, persona, captain_id, reason)` resets
  state to OK and emits an audit row. The Captain-verification mechanism
  is upstream (control-plane RBAC); this module trusts the caller to have
  established identity before invoking.

* Persistence: table `sticky_stop_state`, one row per (customer, persona)
  tuple, in Machine-local SQLite on the Fly volume (ADR 0062 — the
  per-customer-D1 placement this module originally assumed was never built
  and is retracted doctrine). The row's persona slug matches
  `customer.yaml.personas[].slug`.

* Audit emission: every state transition writes one StickyStopAuditRecord
  to the injected StickyStopAuditSink; each runtime adapts the record onto
  its native audit writer (ss-console: adapter.audit_log.AuditLogWriter;
  overlay: the broker-owned ledger emit path). Action type re-uses the
  existing closed-set vocabulary (ACCEPTED_ACTION_TYPES):

    - HARD_STOP entry          -> action_type=AGENT_STOPPED
    - WARN / SOFT_STOP entries -> action_type=INVARIANT_VIOLATION
    - clear()                  -> action_type=AGENT_RESUMED

  The transition detail (from_state, to_state, condition_triggered,
  sampled metric values) lives in the metadata column. This keeps the
  audit-log schema closed-set per d1-schema.md §1 while still carrying
  the structural information the compliance-evidence packet needs.

* Thread safety: SqliteStickyStopStore is the production store (Machine-
  local SQLite, ADR 0062) and the test store. It uses a single-writer
  connection and attempts no cross-row locks because state is partitioned
  per (customer, persona) and within one Hermes Machine the runtime is
  single-tenant. (The HttpD1StickyStopStore this docstring once promised
  is retired unbuilt with the per-customer-D1 premise.)

Module shape:

    state = StickyStopMachine(store=store, audit_writer=writer)

    # Record a tool failure and let the machine decide whether to transition
    new_state = await state.record_tool_failure(
        customer="acme",
        persona="marcus",
        skill_name="inbox-triage",
    )

    # Check current state on the dispatch path
    current = await state.get_state("acme", "marcus")
    if current.level == StickyStopLevel.HARD_STOP:
        raise StickyStopError(current)

    # Captain clears after investigation
    await state.clear(
        customer="acme",
        persona="marcus",
        captain_id="captain-scott",
        reason="confirmed false-positive tool flap; vendor recovered",
    )

The state machine has no async wakeup of its own. It transitions in
response to caller-driven events. Time-budget enforcement is therefore
a poll: callers feed `record_runtime_seconds()` per turn or per minute,
and the machine compares against the configured budget.
"""

from __future__ import annotations

import enum
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

log = logging.getLogger("aie.sticky_stop")


# ---------------------------------------------------------------------------
# Audit sink seam (self-contained — no adapter import)
# ---------------------------------------------------------------------------
#
# This module is consumed in two runtimes: ss-console's safety substrate
# (where audit rows flow through adapter.audit_log.AuditLogWriter) and the
# hermes-smd-overlay (where they flow through the overlay's emit path to the
# broker-owned ledger). To vendor cleanly as a twin, the module defines its
# own plain-data record and a one-method sink Protocol; each runtime supplies
# an adapter that maps the record onto its native audit writer. action_type
# stays inside the audit log's closed-set vocabulary (AGENT_STOPPED /
# INVARIANT_VIOLATION / AGENT_RESUMED).


@dataclass(frozen=True)
class StickyStopAuditRecord:
    """One audit emission from the state machine, as plain data."""

    action_type: str
    actor: str
    actor_role: str  # "agent" | "captain"
    metadata: dict = field(default_factory=dict)
    skill_name: str | None = None


class StickyStopAuditSink(Protocol):
    """Where transition audit records go. Implementations MUST NOT swallow
    write failures silently — a transition the substrate cannot record is a
    transition that did not safely happen (same invariant as issue #891)."""

    async def write(self, record: StickyStopAuditRecord) -> None: ...


# ---------------------------------------------------------------------------
# State enum + dataclasses
# ---------------------------------------------------------------------------


class StickyStopLevel(str, enum.Enum):
    """Forward-only sticky-stop state. clear() is the only path back to OK."""

    OK = "OK"
    WARN = "WARN"
    SOFT_STOP = "SOFT_STOP"
    HARD_STOP = "HARD_STOP"


_LEVEL_ORDER = {
    StickyStopLevel.OK: 0,
    StickyStopLevel.WARN: 1,
    StickyStopLevel.SOFT_STOP: 2,
    StickyStopLevel.HARD_STOP: 3,
}


class StickyStopCondition(str, enum.Enum):
    """The four conditions that drive transitions. Stored in metadata so the
    compliance-evidence packet can bucket transitions by cause."""

    CONSECUTIVE_TOOL_FAILURES = "consecutive_tool_failures"
    REFUSAL_CASCADE = "refusal_cascade"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    COST_THRESHOLD = "cost_threshold"
    CAPTAIN_CLEAR = "captain_clear"


@dataclass(frozen=True)
class StickyStopThresholds:
    """Per-condition transition thresholds. Defaults documented in
    docs/specs/operator/sticky-stop.md §3. Read from
    customer.yaml.safety.sticky_stop when present.
    """

    # Consecutive tool-failure counts within tool_failure_window_seconds.
    tool_failure_warn: int = 3
    tool_failure_soft_stop: int = 5
    tool_failure_hard_stop: int = 8
    tool_failure_window_seconds: int = 600  # 10 minutes

    # Refusal-cascade counts within refusal_window_seconds.
    refusal_warn: int = 5
    refusal_soft_stop: int = 10
    refusal_hard_stop: int = 20
    refusal_window_seconds: int = 1800  # 30 minutes

    # Wall-clock seconds for a single run. Single-step transition to
    # SOFT_STOP; the agent has already exceeded its envelope.
    time_budget_seconds: int = 3600  # 1 hour

    # Daily $ cap on LLM costs. cost_warn_pct / cost_soft_stop_pct /
    # cost_hard_stop_pct are percentages of cost_daily_cents.
    cost_daily_cents: int = 5_000  # $50/day default
    cost_warn_pct: int = 80
    cost_soft_stop_pct: int = 100
    cost_hard_stop_pct: int = 200


DEFAULT_THRESHOLDS = StickyStopThresholds()


@dataclass(frozen=True)
class StickyStopState:
    """Persisted state for a (customer, persona) tuple."""

    customer: str
    persona: str
    level: StickyStopLevel
    updated_at: str  # ISO 8601 UTC
    reason: str | None = None
    condition: StickyStopCondition | None = None
    # Rolling counters used by record_* methods. Carried in the row so the
    # state machine survives restarts without losing failure history.
    consecutive_tool_failures: int = 0
    tool_failure_window_started_at: str | None = None
    refusal_count: int = 0
    refusal_window_started_at: str | None = None
    cost_cents_today: int = 0
    cost_date: str | None = None  # YYYY-MM-DD; resets cost_cents_today

    def __post_init__(self) -> None:
        if not self.customer:
            raise ValueError("customer is required")
        if not self.persona:
            raise ValueError("persona is required")


class StickyStopError(RuntimeError):
    """Raised by the dispatch path when the current state is HARD_STOP.

    The caller (Hermes dispatch) MUST NOT catch and proceed. The whole
    point of HARD_STOP is "no further actions until Captain clears."
    """

    def __init__(self, state: StickyStopState) -> None:
        super().__init__(
            f"sticky-stop HARD_STOP active for {state.customer}/{state.persona}: "
            f"{state.reason or '(no reason)'}"
        )
        self.state = state


# ---------------------------------------------------------------------------
# Store protocol + implementations
# ---------------------------------------------------------------------------


class StickyStopStore(Protocol):
    """Persistence backend for sticky-stop state. Implementations must be
    safe to call concurrently from the single Hermes runtime; cross-process
    coordination is not provided because each customer Machine is single-tenant.
    """

    async def get(self, customer: str, persona: str) -> StickyStopState | None: ...

    async def put(self, state: StickyStopState) -> None: ...


# ---------------------------------------------------------------------------
# Sqlite-backed store (tests + local dev)
# ---------------------------------------------------------------------------


_SQLITE_UPSERT = (
    "INSERT INTO sticky_stop_state ("
    "customer, persona, level, updated_at, reason, condition, "
    "consecutive_tool_failures, tool_failure_window_started_at, "
    "refusal_count, refusal_window_started_at, "
    "cost_cents_today, cost_date"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(customer, persona) DO UPDATE SET "
    "level=excluded.level, updated_at=excluded.updated_at, "
    "reason=excluded.reason, condition=excluded.condition, "
    "consecutive_tool_failures=excluded.consecutive_tool_failures, "
    "tool_failure_window_started_at=excluded.tool_failure_window_started_at, "
    "refusal_count=excluded.refusal_count, "
    "refusal_window_started_at=excluded.refusal_window_started_at, "
    "cost_cents_today=excluded.cost_cents_today, "
    "cost_date=excluded.cost_date"
)

_SQLITE_SELECT = (
    "SELECT customer, persona, level, updated_at, reason, condition, "
    "consecutive_tool_failures, tool_failure_window_started_at, "
    "refusal_count, refusal_window_started_at, "
    "cost_cents_today, cost_date "
    "FROM sticky_stop_state WHERE customer=? AND persona=?"
)


class SqliteStickyStopStore:
    """Sqlite-backed StickyStopStore.

    The caller passes a sqlite3.Connection with the sticky_stop_state schema
    applied (via migration 0004). Used by tests and local-dev scripts.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    async def get(self, customer: str, persona: str) -> StickyStopState | None:
        cur = self._conn.cursor()
        row = cur.execute(_SQLITE_SELECT, (customer, persona)).fetchone()
        if row is None:
            return None
        (
            cust,
            pers,
            level,
            updated_at,
            reason,
            condition,
            tool_fails,
            tool_win_start,
            refusals,
            refusal_win_start,
            cost_today,
            cost_date,
        ) = row
        return StickyStopState(
            customer=cust,
            persona=pers,
            level=StickyStopLevel(level),
            updated_at=updated_at,
            reason=reason,
            condition=StickyStopCondition(condition) if condition else None,
            consecutive_tool_failures=tool_fails or 0,
            tool_failure_window_started_at=tool_win_start,
            refusal_count=refusals or 0,
            refusal_window_started_at=refusal_win_start,
            cost_cents_today=cost_today or 0,
            cost_date=cost_date,
        )

    async def put(self, state: StickyStopState) -> None:
        self._conn.execute(
            _SQLITE_UPSERT,
            (
                state.customer,
                state.persona,
                state.level.value,
                state.updated_at,
                state.reason,
                state.condition.value if state.condition else None,
                state.consecutive_tool_failures,
                state.tool_failure_window_started_at,
                state.refusal_count,
                state.refusal_window_started_at,
                state.cost_cents_today,
                state.cost_date,
            ),
        )
        self._conn.commit()


# ---------------------------------------------------------------------------
# Time helpers — injectable for deterministic tests
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(s: str) -> datetime:
    # Accept the millisecond-Z format we emit; tolerate plain trailing Z.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


@dataclass
class _Decision:
    """Internal value object describing a transition computed but not yet
    persisted. Lets test code assert against the decision separately from
    the write."""

    next_state: StickyStopState
    transitioned: bool
    condition: StickyStopCondition | None


class StickyStopMachine:
    """The sticky-stop state machine.

    Construction takes a store and a StickyStopAuditSink. The thresholds default
    to DEFAULT_THRESHOLDS; callers wire customer.yaml-derived thresholds in
    explicitly via the `thresholds` keyword.

    All `record_*` methods follow the same shape:

      1. Load the current state (creating an OK row if absent).
      2. Apply the counter update and decide whether to transition.
      3. If transitioning, write the new state, emit an audit row, return
         the new state.
      4. If not transitioning, write the updated counters, return the same
         level.

    Transitions are forward-only. If the computed level would be lower than
    the current level, we keep the current level (counters still update).
    Captain `clear()` is the only path that decreases level.
    """

    def __init__(
        self,
        *,
        store: StickyStopStore,
        audit_writer: StickyStopAuditSink,
        thresholds: StickyStopThresholds = DEFAULT_THRESHOLDS,
        clock: callable | None = None,
    ) -> None:
        self._store = store
        self._audit = audit_writer
        self._thresholds = thresholds
        self._clock = clock or _now_utc

    # ---- Public read ------------------------------------------------------

    async def get_state(self, customer: str, persona: str) -> StickyStopState:
        """Return the current state, materializing an OK row if absent.

        The OK row is materialized in-memory; it is NOT persisted on a read
        — only the first transition or counter update causes a write.
        """
        existing = await self._store.get(customer, persona)
        if existing is not None:
            return existing
        return StickyStopState(
            customer=customer,
            persona=persona,
            level=StickyStopLevel.OK,
            updated_at=_iso(self._clock()),
        )

    # ---- Counter updates --------------------------------------------------

    async def record_tool_failure(
        self,
        *,
        customer: str,
        persona: str,
        skill_name: str | None = None,
    ) -> StickyStopState:
        """Record a tool-call failure. May transition to WARN, SOFT_STOP, or
        HARD_STOP depending on consecutive-failure thresholds.

        Window: failures within `tool_failure_window_seconds` count toward
        the same streak. A failure outside the window resets the streak to 1
        and starts a new window.
        """
        state = await self.get_state(customer, persona)
        now = self._clock()
        new_count, new_window = self._tick_window(
            count=state.consecutive_tool_failures,
            window_started_at=state.tool_failure_window_started_at,
            now=now,
            window_seconds=self._thresholds.tool_failure_window_seconds,
        )
        target = self._level_for_tool_failures(new_count)
        next_level = self._forward_only(state.level, target)
        return await self._commit_transition(
            current=state,
            next_state=replace(
                state,
                level=next_level,
                updated_at=_iso(now),
                consecutive_tool_failures=new_count,
                tool_failure_window_started_at=new_window,
                reason=(
                    f"consecutive_tool_failures={new_count} "
                    f"(window={self._thresholds.tool_failure_window_seconds}s, "
                    f"skill={skill_name or 'unknown'})"
                )
                if next_level != state.level
                else state.reason,
                condition=StickyStopCondition.CONSECUTIVE_TOOL_FAILURES
                if next_level != state.level
                else state.condition,
            ),
            condition=StickyStopCondition.CONSECUTIVE_TOOL_FAILURES,
            transitioned=next_level != state.level,
            skill_name=skill_name,
            extra_metadata={
                "consecutive_tool_failures": new_count,
                "window_seconds": self._thresholds.tool_failure_window_seconds,
                "thresholds": {
                    "warn": self._thresholds.tool_failure_warn,
                    "soft_stop": self._thresholds.tool_failure_soft_stop,
                    "hard_stop": self._thresholds.tool_failure_hard_stop,
                },
            },
        )

    async def record_tool_success(
        self,
        *,
        customer: str,
        persona: str,
    ) -> StickyStopState:
        """Record a successful tool call. Resets the consecutive-failure
        streak to 0. Does NOT downgrade level — that is Captain-only via
        clear()."""
        state = await self.get_state(customer, persona)
        if state.consecutive_tool_failures == 0:
            return state
        now = self._clock()
        next_state = replace(
            state,
            updated_at=_iso(now),
            consecutive_tool_failures=0,
            tool_failure_window_started_at=None,
        )
        await self._store.put(next_state)
        return next_state

    async def record_refusal(
        self,
        *,
        customer: str,
        persona: str,
        skill_name: str | None = None,
    ) -> StickyStopState:
        """Record a trust-ceiling refusal. Tracks refusal-cascade counts
        within the configured window.
        """
        state = await self.get_state(customer, persona)
        now = self._clock()
        new_count, new_window = self._tick_window(
            count=state.refusal_count,
            window_started_at=state.refusal_window_started_at,
            now=now,
            window_seconds=self._thresholds.refusal_window_seconds,
        )
        target = self._level_for_refusals(new_count)
        next_level = self._forward_only(state.level, target)
        return await self._commit_transition(
            current=state,
            next_state=replace(
                state,
                level=next_level,
                updated_at=_iso(now),
                refusal_count=new_count,
                refusal_window_started_at=new_window,
                reason=(
                    f"refusal_cascade={new_count} "
                    f"(window={self._thresholds.refusal_window_seconds}s, "
                    f"skill={skill_name or 'unknown'})"
                )
                if next_level != state.level
                else state.reason,
                condition=StickyStopCondition.REFUSAL_CASCADE
                if next_level != state.level
                else state.condition,
            ),
            condition=StickyStopCondition.REFUSAL_CASCADE,
            transitioned=next_level != state.level,
            skill_name=skill_name,
            extra_metadata={
                "refusal_count": new_count,
                "window_seconds": self._thresholds.refusal_window_seconds,
                "thresholds": {
                    "warn": self._thresholds.refusal_warn,
                    "soft_stop": self._thresholds.refusal_soft_stop,
                    "hard_stop": self._thresholds.refusal_hard_stop,
                },
            },
        )

    async def record_runtime_seconds(
        self,
        *,
        customer: str,
        persona: str,
        seconds: float,
    ) -> StickyStopState:
        """Record observed wall-clock runtime for a single agent run.

        If the value exceeds the configured budget, the machine transitions
        directly to SOFT_STOP. Forward-only: if the machine is already in
        HARD_STOP, the level is unchanged.
        """
        state = await self.get_state(customer, persona)
        if seconds <= self._thresholds.time_budget_seconds:
            return state
        now = self._clock()
        next_level = self._forward_only(state.level, StickyStopLevel.SOFT_STOP)
        return await self._commit_transition(
            current=state,
            next_state=replace(
                state,
                level=next_level,
                updated_at=_iso(now),
                reason=(
                    f"time_budget_exceeded={seconds:.1f}s "
                    f"(budget={self._thresholds.time_budget_seconds}s)"
                )
                if next_level != state.level
                else state.reason,
                condition=StickyStopCondition.TIME_BUDGET_EXCEEDED
                if next_level != state.level
                else state.condition,
            ),
            condition=StickyStopCondition.TIME_BUDGET_EXCEEDED,
            transitioned=next_level != state.level,
            skill_name=None,
            extra_metadata={
                "observed_seconds": seconds,
                "budget_seconds": self._thresholds.time_budget_seconds,
            },
        )

    async def record_cost_cents(
        self,
        *,
        customer: str,
        persona: str,
        amount_cents: int,
    ) -> StickyStopState:
        """Add incremental LLM cost in cents to today's running total.

        Resets the running total when the UTC date rolls over. Transitions
        on the cost-threshold ladder against the configured daily cap.
        """
        if amount_cents < 0:
            raise ValueError("amount_cents must be non-negative")

        state = await self.get_state(customer, persona)
        now = self._clock()
        today = now.strftime("%Y-%m-%d")
        prior_today = state.cost_date if state.cost_date == today else None
        new_total = (state.cost_cents_today if prior_today else 0) + amount_cents

        target = self._level_for_cost(new_total)
        next_level = self._forward_only(state.level, target)
        return await self._commit_transition(
            current=state,
            next_state=replace(
                state,
                level=next_level,
                updated_at=_iso(now),
                cost_cents_today=new_total,
                cost_date=today,
                reason=(
                    f"cost_threshold={new_total}c / cap={self._thresholds.cost_daily_cents}c "
                    f"({(new_total / max(self._thresholds.cost_daily_cents, 1)) * 100:.0f}%)"
                )
                if next_level != state.level
                else state.reason,
                condition=StickyStopCondition.COST_THRESHOLD
                if next_level != state.level
                else state.condition,
            ),
            condition=StickyStopCondition.COST_THRESHOLD,
            transitioned=next_level != state.level,
            skill_name=None,
            extra_metadata={
                "cost_cents_today": new_total,
                "cost_daily_cents_cap": self._thresholds.cost_daily_cents,
                "warn_pct": self._thresholds.cost_warn_pct,
                "soft_stop_pct": self._thresholds.cost_soft_stop_pct,
                "hard_stop_pct": self._thresholds.cost_hard_stop_pct,
            },
        )

    # ---- Dispatch guard ---------------------------------------------------

    async def assert_allowed(
        self,
        *,
        customer: str,
        persona: str,
    ) -> StickyStopState:
        """Call from the dispatch path before invoking any skill.

        Raises StickyStopError if the current level is HARD_STOP. Returns
        the current state otherwise so the caller can pin trust_ceiling to
        draft_for_review when SOFT_STOP is active.
        """
        state = await self.get_state(customer, persona)
        if state.level == StickyStopLevel.HARD_STOP:
            raise StickyStopError(state)
        return state

    # ---- Captain recovery -------------------------------------------------

    async def clear(
        self,
        *,
        customer: str,
        persona: str,
        captain_id: str,
        reason: str,
    ) -> StickyStopState:
        """Captain-initiated reset to OK. The ONLY backward transition.

        Caller responsibility: verify the actor is a Captain BEFORE invoking.
        This module trusts the caller; identity verification lives in the
        control-plane RBAC layer.

        Emits one audit row with action_type=AGENT_RESUMED and metadata
        including the reason and the prior state, so the compliance-evidence
        packet can show the full transition history.
        """
        if not captain_id:
            raise ValueError("captain_id is required for clear()")
        if not reason:
            raise ValueError("reason is required for clear()")
        prior = await self.get_state(customer, persona)
        now = self._clock()
        cleared = StickyStopState(
            customer=customer,
            persona=persona,
            level=StickyStopLevel.OK,
            updated_at=_iso(now),
            reason=None,
            condition=None,
            consecutive_tool_failures=0,
            tool_failure_window_started_at=None,
            refusal_count=0,
            refusal_window_started_at=None,
            cost_cents_today=prior.cost_cents_today,
            cost_date=prior.cost_date,
        )
        await self._store.put(cleared)
        await self._audit.write(
            StickyStopAuditRecord(
                action_type="AGENT_RESUMED",
                actor=captain_id,
                actor_role="captain",
                metadata={
                    "sticky_stop_cleared": True,
                    "customer": customer,
                    "persona": persona,
                    "from_state": prior.level.value,
                    "to_state": StickyStopLevel.OK.value,
                    "condition_triggered": StickyStopCondition.CAPTAIN_CLEAR.value,
                    "prior_reason": prior.reason,
                    "prior_condition": prior.condition.value if prior.condition else None,
                    "clear_reason": reason,
                },
            )
        )
        return cleared

    # ---- Internals --------------------------------------------------------

    def _level_for_tool_failures(self, count: int) -> StickyStopLevel:
        t = self._thresholds
        if count >= t.tool_failure_hard_stop:
            return StickyStopLevel.HARD_STOP
        if count >= t.tool_failure_soft_stop:
            return StickyStopLevel.SOFT_STOP
        if count >= t.tool_failure_warn:
            return StickyStopLevel.WARN
        return StickyStopLevel.OK

    def _level_for_refusals(self, count: int) -> StickyStopLevel:
        t = self._thresholds
        if count >= t.refusal_hard_stop:
            return StickyStopLevel.HARD_STOP
        if count >= t.refusal_soft_stop:
            return StickyStopLevel.SOFT_STOP
        if count >= t.refusal_warn:
            return StickyStopLevel.WARN
        return StickyStopLevel.OK

    def _level_for_cost(self, cents: int) -> StickyStopLevel:
        t = self._thresholds
        if t.cost_daily_cents <= 0:
            return StickyStopLevel.OK
        pct = (cents * 100) // t.cost_daily_cents
        if pct >= t.cost_hard_stop_pct:
            return StickyStopLevel.HARD_STOP
        if pct >= t.cost_soft_stop_pct:
            return StickyStopLevel.SOFT_STOP
        if pct >= t.cost_warn_pct:
            return StickyStopLevel.WARN
        return StickyStopLevel.OK

    def _forward_only(
        self,
        current: StickyStopLevel,
        proposed: StickyStopLevel,
    ) -> StickyStopLevel:
        return proposed if _LEVEL_ORDER[proposed] > _LEVEL_ORDER[current] else current

    def _tick_window(
        self,
        *,
        count: int,
        window_started_at: str | None,
        now: datetime,
        window_seconds: int,
    ) -> tuple[int, str]:
        """Increment a window counter; reset to 1 if outside the window.

        Returns (new_count, new_window_started_at_iso).
        """
        if window_started_at is None:
            return 1, _iso(now)
        try:
            started = _parse_iso(window_started_at)
        except ValueError:
            return 1, _iso(now)
        if (now - started) > timedelta(seconds=window_seconds):
            return 1, _iso(now)
        return count + 1, window_started_at

    async def _commit_transition(
        self,
        *,
        current: StickyStopState,
        next_state: StickyStopState,
        condition: StickyStopCondition,
        transitioned: bool,
        skill_name: str | None,
        extra_metadata: dict,
    ) -> StickyStopState:
        await self._store.put(next_state)
        if not transitioned:
            return next_state

        # Pick the action_type that best fits the audit-log closed-set
        # vocabulary (ACCEPTED_ACTION_TYPES). HARD_STOP -> AGENT_STOPPED;
        # WARN / SOFT_STOP -> INVARIANT_VIOLATION (the substrate noticed
        # something wrong before it became unsafe).
        if next_state.level == StickyStopLevel.HARD_STOP:
            action_type = "AGENT_STOPPED"
        else:
            action_type = "INVARIANT_VIOLATION"

        metadata: dict = {
            "sticky_stop_transition": True,
            "customer": next_state.customer,
            "persona": next_state.persona,
            "from_state": current.level.value,
            "to_state": next_state.level.value,
            "condition_triggered": condition.value,
            "reason": next_state.reason,
        }
        metadata.update(extra_metadata)

        await self._audit.write(
            StickyStopAuditRecord(
                action_type=action_type,
                actor="agent",
                actor_role="agent",
                skill_name=skill_name,
                metadata=metadata,
            )
        )
        return next_state


__all__ = [
    "DEFAULT_THRESHOLDS",
    "SqliteStickyStopStore",
    "StickyStopAuditRecord",
    "StickyStopAuditSink",
    "StickyStopCondition",
    "StickyStopError",
    "StickyStopLevel",
    "StickyStopMachine",
    "StickyStopState",
    "StickyStopStore",
    "StickyStopThresholds",
]
