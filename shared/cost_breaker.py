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
import logging
import os
import sqlite3
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


def read_level(path: str | None = None) -> str | None:
    """Read the worst (max) persisted level across personas, read-only.

    Used by the webhook gate (root) and the heartbeat payload. Returns None
    when the state file does not exist yet (fresh Machine — treated as OK by
    callers) or on any read error (fail toward 'unknown', never toward a
    fabricated OK when the file exists but cannot be read).
    """
    resolved = path or db_path()
    if not os.path.exists(resolved):
        return None
    order = [
        StickyStopLevel.OK.value,
        StickyStopLevel.WARN.value,
        StickyStopLevel.SOFT_STOP.value,
        StickyStopLevel.HARD_STOP.value,
    ]
    try:
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT level FROM sticky_stop_state").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — read-only observer
        logger.warning("cost_breaker: read_level failed: %s", exc)
        return "unknown"
    if not rows:
        return StickyStopLevel.OK.value
    worst = StickyStopLevel.OK.value
    for (level,) in rows:
        if level in order and order.index(level) > order.index(worst):
            worst = level
    return worst


__all__ = [
    "AuditLedgerSink",
    "CostBreaker",
    "DEFAULT_DB_PATH",
    "StickyStopError",
    "build_breaker",
    "db_path",
    "read_level",
    "thresholds_from_config",
]
