"""Tests for shared/gate_inbound_cap.py (ADR 0062, ss-console #1661).

The guard's contract: verified deliveries forward and count; HARD_STOP parks
everything; the authored daily cap parks the overflow with an audit row;
guard faults fail toward FORWARD (limiter, not a security boundary); parked
deliveries never count against the routed cap.
"""

from __future__ import annotations

from shared.audit_contract import INSERT_SQL
from shared.gate_inbound_cap import (
    DEFAULT_INBOUND_DAILY_CAP,
    InboundWakeGuard,
    resolve_inbound_cap,
)


class FakeAuditClient:
    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params) -> None:
        self.rows.append((sql, params))


class _Config:
    def __init__(self, block: dict) -> None:
        self.sticky_stop = block


def _guard(tmp_path, *, cap=3, level="OK", audit=None, today=None):
    holder = {"today": today or "2026-07-03"}
    return (
        InboundWakeGuard(
            cap_resolver=lambda: cap,
            audit_client=audit,
            db_path=str(tmp_path / "wake.db"),
            breaker_level_fn=lambda: level,
            today_fn=lambda: holder["today"],
        ),
        holder,
    )


def test_forwards_and_counts_under_cap(tmp_path):
    guard, _ = _guard(tmp_path, cap=2)
    assert guard.check(route="agentmail", request_id="a")[0] is True
    assert guard.check(route="agentmail", request_id="b")[0] is True
    forward, reason = guard.check(route="agentmail", request_id="c")
    assert forward is False
    assert reason == "inbound_daily_cap"


def test_cap_resets_on_utc_day_rollover(tmp_path):
    guard, holder = _guard(tmp_path, cap=1)
    assert guard.check(route="r", request_id="1")[0] is True
    assert guard.check(route="r", request_id="2")[0] is False
    holder["today"] = "2026-07-04"
    assert guard.check(route="r", request_id="3")[0] is True


def test_hard_stop_parks_everything(tmp_path):
    audit = FakeAuditClient()
    guard, _ = _guard(tmp_path, cap=100, level="HARD_STOP", audit=audit)
    forward, reason = guard.check(route="smokeball", request_id="x")
    assert forward is False
    assert reason == "sticky_stop_hard_stop"
    # Park emitted an INVARIANT_VIOLATION row through the shared contract.
    assert audit.rows and audit.rows[0][0] == INSERT_SQL
    assert audit.rows[0][1][2] == "INVARIANT_VIOLATION"


def test_cap_park_emits_audit_row(tmp_path):
    audit = FakeAuditClient()
    guard, _ = _guard(tmp_path, cap=1, audit=audit)
    guard.check(route="agentmail", request_id="1")
    guard.check(route="agentmail", request_id="2")
    assert len(audit.rows) == 1
    assert audit.rows[0][1][2] == "INVARIANT_VIOLATION"


def test_park_audit_failure_still_parks(tmp_path):
    class _Boom:
        def execute(self, sql, *params):
            raise RuntimeError("ledger down")

    guard, _ = _guard(tmp_path, cap=1, audit=_Boom())
    assert guard.check(route="r", request_id="1")[0] is True
    forward, reason = guard.check(route="r", request_id="2")
    assert forward is False  # refuse-more-than-recorded is the safe direction
    assert reason == "inbound_daily_cap"


def test_breaker_read_failure_fails_toward_forward(tmp_path):
    def _boom():
        raise RuntimeError("db gone")

    guard = InboundWakeGuard(
        cap_resolver=lambda: 100,
        audit_client=None,
        db_path=str(tmp_path / "wake.db"),
        breaker_level_fn=_boom,
        today_fn=lambda: "2026-07-03",
    )
    assert guard.check(route="r", request_id="1")[0] is True


def test_resolve_inbound_cap():
    assert resolve_inbound_cap(None) == DEFAULT_INBOUND_DAILY_CAP
    assert resolve_inbound_cap(_Config({})) == DEFAULT_INBOUND_DAILY_CAP
    assert resolve_inbound_cap(_Config({"inbound_daily_cap": 50})) == 50
    assert resolve_inbound_cap(_Config({"inbound_daily_cap": 0})) == DEFAULT_INBOUND_DAILY_CAP
    assert resolve_inbound_cap(_Config({"inbound_daily_cap": "junk"})) == DEFAULT_INBOUND_DAILY_CAP


# ---------------------------------------------------------------------------
# Gate clear endpoint core (ADR 0062 §6) — auth glue is the handler; this
# exercises the pure core like test_mcp_channel does for _mcp_turn.
# ---------------------------------------------------------------------------


def test_gate_sticky_stop_clear_core(tmp_path, monkeypatch):
    import webhook_gate as gate
    from shared.cost_breaker import build_breaker, read_level

    db = str(tmp_path / "sticky_stop.db")
    monkeypatch.setenv("SMD_STICKY_STOP_DB_PATH", db)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")

    class _Client:
        def execute(self, sql, *params):
            pass

    # Missing fields -> 400.
    status, body = gate._sticky_stop_clear({})
    assert status == 400

    # Trip, then clear through the gate core. The gate does NOT write the
    # Machine audit ledger (broker PID-gates to the gateway process), so the
    # clear must succeed WITHOUT an audit client — proving the recovery is
    # decoupled from the (impossible-from-the-gate) Machine audit write.
    b = build_breaker(customer="acme", persona="_machine", audit_client=_Client(), path=db)
    b.record_cost_cents(10_000)
    assert read_level(db) == "HARD_STOP"
    status, body = gate._sticky_stop_clear({"captain_id": "captain-scott", "reason": "probe"})
    assert status == 200
    assert body["level"] == "OK"
    assert body["cleared"][0]["prior_level"] == "HARD_STOP"
