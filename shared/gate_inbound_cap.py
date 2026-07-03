"""Webhook-gate inbound wake guard — ADR 0062, ss-console #1661.

Bounds the interactive spend driver the cost breaker cannot meter (the
Hermes ``post_llm_call`` hook exposes no token counts): every VERIFIED
vendor webhook that would wake the agent passes through this guard first.

Two checks, both fail-closed toward "park" (acknowledge, audit, do not
wake) and never toward silently dropping or waking:

1. **Breaker level.** While the Machine-wide sticky_stop ladder is at
   HARD_STOP (tripped by job-path spend or pinned by the Captain), no
   inbound wakes the agent. Recovery is Captain ``clear()``.
2. **Inbound daily cap.** At most N routed wakes per UTC day (authored
   ``safety.sticky_stop.inbound_daily_cap``, platform default 200 — an
   integrity control per ADR 0035). This is a plain rate limit, not a
   cents estimate: no fabricated per-turn pricing on a seam that has no
   token counts.

Counting rules: only VERIFIED deliveries count (an attacker without a
valid vendor signature cannot exhaust the cap), and only deliveries that
actually forward increment the counter (parked deliveries are recorded in
their own column for visibility, not against the cap).

State lives in a small gate-owned SQLite file (default
``/opt/data/smd-gate/wake_counter.db``; ``SMD_GATE_CAP_DB_PATH`` overrides
for tests). The gate runs as root before the SEC-28 env strip, so it can
also write the park audit row through the broker audit client — that write
is best-effort: if it fails the delivery STAYS parked (refusing more than
we record is safe; waking without a record is not) and the failure is
logged loudly.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from shared import cost_breaker
from shared.audit_contract import INSERT_SQL, agent_event_params
from shared.sticky_stop import StickyStopLevel

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_DAILY_CAP = 200
DEFAULT_CAP_DB_PATH = "/opt/data/smd-gate/wake_counter.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS wake_counter (
  date          TEXT PRIMARY KEY,   -- UTC YYYY-MM-DD
  routed_count  INTEGER NOT NULL DEFAULT 0,
  parked_count  INTEGER NOT NULL DEFAULT 0
)
"""


def _cap_db_path() -> str:
    return os.environ.get("SMD_GATE_CAP_DB_PATH") or DEFAULT_CAP_DB_PATH


def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def resolve_inbound_cap(config: Any) -> int:
    """Authored ``safety.sticky_stop.inbound_daily_cap`` or the platform
    default. Malformed values fail toward the default, never fail-open."""
    try:
        block = config.sticky_stop if config is not None else {}
        raw = block.get("inbound_daily_cap")
        if raw is None:
            return DEFAULT_INBOUND_DAILY_CAP
        cap = int(raw)
        if cap <= 0:
            raise ValueError("inbound_daily_cap must be positive")
        return cap
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "gate-cap: invalid safety.sticky_stop.inbound_daily_cap; using default %s: %s",
            DEFAULT_INBOUND_DAILY_CAP,
            exc,
        )
        return DEFAULT_INBOUND_DAILY_CAP


class InboundWakeGuard:
    """The per-gate-process guard. One instance per gate; sqlite serializes."""

    def __init__(
        self,
        *,
        cap_resolver,
        audit_client: Any = None,
        db_path: str | None = None,
        breaker_level_fn=None,
        today_fn=None,
    ) -> None:
        """``cap_resolver`` is a zero-arg callable returning the current cap
        (read live per delivery so an authored cap change applies without a
        gate restart, ADR 0044 style). ``audit_client`` is the broker audit
        client for park rows (None ⇒ log-only). ``breaker_level_fn`` /
        ``today_fn`` are injectable for tests."""
        self._cap_resolver = cap_resolver
        self._audit_client = audit_client
        self._path = Path(db_path or _cap_db_path())
        self._breaker_level = breaker_level_fn or cost_breaker.read_level
        self._today = today_fn or _utc_today
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(_CREATE_SQL)
            conn.commit()
            self._conn = conn
        return self._conn

    def check(self, *, route: str, request_id: str) -> tuple[bool, str | None]:
        """Decide one verified delivery. Returns ``(forward, park_reason)``.

        ``forward=True`` also counts the wake. ``forward=False`` records the
        park (counter column + best-effort audit row) and the caller must
        acknowledge (202) WITHOUT forwarding.

        Guard faults (sqlite unavailable, etc.) fail toward FORWARD with a
        loud log: this guard is a spend limiter, not a security boundary —
        the signature check upstream is the wall, and a broken limiter must
        not turn every verified webhook into a dropped delivery.
        """
        # 1. Breaker level — HARD_STOP parks everything.
        try:
            level = self._breaker_level()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gate-cap: breaker level read failed (treating OK): %s", exc)
            level = None
        if level == StickyStopLevel.HARD_STOP.value:
            self._record_park(route=route, request_id=request_id, reason="sticky_stop_hard_stop")
            return False, "sticky_stop_hard_stop"

        # 2. Daily cap.
        try:
            cap = int(self._cap_resolver())
            conn = self._connection()
            today = self._today()
            with conn:
                conn.execute(
                    "INSERT INTO wake_counter(date, routed_count, parked_count) "
                    "VALUES (?, 0, 0) ON CONFLICT(date) DO NOTHING",
                    (today,),
                )
                row = conn.execute(
                    "SELECT routed_count FROM wake_counter WHERE date = ?", (today,)
                ).fetchone()
                routed = int(row[0]) if row else 0
                if routed >= cap:
                    conn.execute(
                        "UPDATE wake_counter SET parked_count = parked_count + 1 WHERE date = ?",
                        (today,),
                    )
                else:
                    conn.execute(
                        "UPDATE wake_counter SET routed_count = routed_count + 1 WHERE date = ?",
                        (today,),
                    )
        except Exception as exc:  # noqa: BLE001 — limiter fault ≠ delivery fault
            logger.error("gate-cap: counter failed (forwarding uncounted): %s", exc)
            return True, None

        if routed >= cap:
            self._audit_park(
                route=route,
                request_id=request_id,
                reason="inbound_daily_cap",
                extra={"routed_today": routed, "cap": cap},
            )
            logger.warning(
                "gate-cap: inbound daily cap reached (%s/%s) — parked route=%s id=%s",
                routed,
                cap,
                route,
                request_id,
            )
            return False, "inbound_daily_cap"
        return True, None

    def _record_park(self, *, route: str, request_id: str, reason: str) -> None:
        try:
            conn = self._connection()
            today = self._today()
            with conn:
                conn.execute(
                    "INSERT INTO wake_counter(date, routed_count, parked_count) "
                    "VALUES (?, 0, 0) ON CONFLICT(date) DO NOTHING",
                    (today,),
                )
                conn.execute(
                    "UPDATE wake_counter SET parked_count = parked_count + 1 WHERE date = ?",
                    (today,),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gate-cap: park counter failed: %s", exc)
        self._audit_park(route=route, request_id=request_id, reason=reason, extra={})

    def _audit_park(self, *, route: str, request_id: str, reason: str, extra: dict) -> None:
        """Best-effort INVARIANT_VIOLATION audit row for a parked delivery.

        The park stands whether or not this write succeeds (refusing more
        than we record is the safe direction); failure is logged loudly so
        the operator's audit trail gap is visible in gate logs.
        """
        if self._audit_client is None:
            logger.warning(
                "gate-cap: parked %s/%s (%s) — no audit client, log-only", route, request_id, reason
            )
            return
        try:
            metadata = {
                "gate_inbound_park": True,
                "reason": reason,
                "route": route,
                "request_id": request_id,
                **extra,
            }
            params = agent_event_params(action_type="INVARIANT_VIOLATION", metadata=metadata)
            self._audit_client.execute(INSERT_SQL, *params)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "gate-cap: park audit write failed (park stands): route=%s id=%s reason=%s err=%s",
                route,
                request_id,
                reason,
                exc,
            )


def default_cap_resolver():
    """Live-read the authored cap from customer.yaml per delivery (ADR 0044
    read-fresh posture); any read failure yields the platform default."""
    try:
        from shared.customer_config import CustomerConfig

        return resolve_inbound_cap(CustomerConfig.from_volume())
    except Exception:  # noqa: BLE001
        return DEFAULT_INBOUND_DAILY_CAP


__all__ = [
    "DEFAULT_INBOUND_DAILY_CAP",
    "InboundWakeGuard",
    "default_cap_resolver",
    "resolve_inbound_cap",
]
