"""Tests for shared/cost_breaker.py + the job-path wiring (ADR 0062, #1661).

Covers:
  - build_breaker: creates the state file + table, records cents, trips the
    ladder at the vendored thresholds, and emits AGENT_STOPPED through the
    audit sink on the HARD_STOP transition
  - assert_allowed raises StickyStopError at HARD_STOP
  - thresholds_from_config: authored cap, absent block, malformed value
  - read_level: missing file → None, rows → worst level, unreadable → unknown
  - job_segment wiring: cost_capped outcome pre-fire; record after spend
  - job_worker: cost_capped dead-letters to needs_review
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.audit_contract import INSERT_SQL
from shared.cost_breaker import (
    StickyStopError,
    build_breaker,
    read_level,
    thresholds_from_config,
)
from shared.sticky_stop import DEFAULT_THRESHOLDS


class FakeAuditClient:
    """Records (sql, params) like the real broker client's execute."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params) -> None:
        self.rows.append((sql, params))

    def action_types(self) -> list[str]:
        # build_audit_params order: row_id, ts, action_type, ...
        return [p[2] for (_sql, p) in self.rows]


class _Config:
    def __init__(self, block: dict) -> None:
        self.sticky_stop = block


def _breaker(tmp_path: Path, audit: FakeAuditClient, config=None):
    return build_breaker(
        customer="acme",
        persona="_machine",
        audit_client=audit,
        config=config,
        path=str(tmp_path / "sticky_stop.db"),
    )


def test_records_and_trips_hard_stop_with_agent_stopped_audit(tmp_path):
    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    # Default ladder: cap 5000; hard stop at 200% = 10000 cents.
    state = b.record_cost_cents(4000)  # 80% -> WARN
    assert state.level.value == "WARN"
    state = b.record_cost_cents(1000)  # 100% -> SOFT_STOP
    assert state.level.value == "SOFT_STOP"
    state = b.record_cost_cents(5000)  # 200% -> HARD_STOP
    assert state.level.value == "HARD_STOP"
    assert "AGENT_STOPPED" in audit.action_types()
    # All sink rows go through the shared INSERT contract.
    assert all(sql == INSERT_SQL for (sql, _p) in audit.rows)


def test_assert_allowed_raises_at_hard_stop(tmp_path):
    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(10_000)
    with pytest.raises(StickyStopError):
        b.assert_allowed()


def test_state_survives_reopen(tmp_path):
    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(10_000)
    b2 = _breaker(tmp_path, FakeAuditClient())
    with pytest.raises(StickyStopError):
        b2.assert_allowed()


def test_thresholds_from_config():
    assert thresholds_from_config(None) is DEFAULT_THRESHOLDS
    assert thresholds_from_config(_Config({})) is DEFAULT_THRESHOLDS
    assert (
        thresholds_from_config(_Config({"cost_cap_daily_cents": 12000})).cost_daily_cents == 12000
    )
    # Malformed fails toward the platform default, never fail-open.
    assert thresholds_from_config(_Config({"cost_cap_daily_cents": 0})) is DEFAULT_THRESHOLDS
    assert thresholds_from_config(_Config({"cost_cap_daily_cents": "junk"})) is DEFAULT_THRESHOLDS


def test_read_level(tmp_path):
    path = str(tmp_path / "sticky_stop.db")
    # Missing file: None (fresh Machine, callers treat as OK).
    assert read_level(path) is None
    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(1)
    assert read_level(path) == "OK"
    b.record_cost_cents(10_000)
    assert read_level(path) == "HARD_STOP"


# ---------------------------------------------------------------------------
# Job-path wiring
# ---------------------------------------------------------------------------


class _TrippedBreaker:
    def assert_allowed(self):
        from shared.sticky_stop import StickyStopLevel, StickyStopState

        raise StickyStopError(
            StickyStopState(
                customer="acme",
                persona="_machine",
                level=StickyStopLevel.HARD_STOP,
                updated_at="2026-07-03T00:00:00Z",
            )
        )

    def record_cost_cents(self, amount_cents):  # pragma: no cover — not reached
        raise AssertionError("segment must not run at HARD_STOP")


def test_run_segment_returns_cost_capped_without_firing(tmp_path):
    from shared.job_segment import make_run_segment

    fired = []

    class _SessionDBStub:
        def get_messages_as_conversation(self, tip, include_ancestors=True):
            return []

    run_segment = make_run_segment(
        session_db=_SessionDBStub(),
        build_agent=lambda **kw: fired.append(kw),
        preflight_cost=lambda model, history: 0,
        segment_cost=lambda agent: 0,
        breaker=_TrippedBreaker(),
    )
    out = run_segment(
        {"id": "j1", "model": "m", "brief": "b", "budget_cents": 100, "spent_cents": 0}, 1
    )
    assert out.cost_capped is True
    assert fired == []


def test_job_worker_dead_letters_cost_capped():
    from shared.job_worker import JobWorker, SegmentOutcome

    class _Client:
        def __init__(self) -> None:
            self.job = {
                "id": "j1",
                "status": "queued",
                "cancel_requested": 0,
                "spent_cents": 0,
                "budget_cents": 100,
                "attempts": 0,
                "model": "m",
                "root_session_id": "job_j1",
            }

        def read(self, job_id):
            return dict(self.job)

        def list_claimable(self):
            return []

        def claim(self, job_id, worker_id):
            return 1

        def record(self, job_id, epoch, fields):
            self.job.update(fields)
            return True

    client = _Client()
    worker = JobWorker(
        client,
        worker_id="t",
        run_segment=lambda job, epoch: SegmentOutcome(cost_capped=True),
        deliver=lambda *a, **k: None,
        put_result=lambda *a, **k: "",
    )
    outcome = worker.run_one("j1")
    assert outcome == "needs_review"
    assert client.job["status"] == "needs_review"
    assert "cost breaker" in client.job["error"]


# ---------------------------------------------------------------------------
# Captain clear surface (ADR 0062 §6)
# ---------------------------------------------------------------------------


def test_clear_hard_stops_clears_and_audits(tmp_path):
    from shared.cost_breaker import clear_hard_stops

    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(10_000)  # HARD_STOP
    path = str(tmp_path / "sticky_stop.db")
    assert read_level(path) == "HARD_STOP"

    clear_audit = FakeAuditClient()
    cleared = clear_hard_stops(
        captain_id="captain-scott",
        reason="staged trip probe complete",
        audit_client=clear_audit,
        path=path,
    )
    assert cleared == [{"customer": "acme", "persona": "_machine", "prior_level": "HARD_STOP"}]
    assert "AGENT_RESUMED" in [p[2] for (_s, p) in clear_audit.rows]
    assert read_level(path) == "OK"
    # And the breaker admits work again.
    b.assert_allowed()


def test_clear_hard_stops_noop_paths(tmp_path):
    import pytest as _pytest

    from shared.cost_breaker import clear_hard_stops

    # Missing state file: nothing to clear.
    assert (
        clear_hard_stops(
            captain_id="c", reason="r", audit_client=FakeAuditClient(), path=str(tmp_path / "x.db")
        )
        == []
    )
    # All-OK rows: nothing to clear, no audit rows.
    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(1)
    clear_audit = FakeAuditClient()
    path = str(tmp_path / "sticky_stop.db")
    assert clear_hard_stops(captain_id="c", reason="r", audit_client=clear_audit, path=path) == []
    assert clear_audit.rows == []
    # captain_id/reason are required (module contract).
    with _pytest.raises(ValueError):
        clear_hard_stops(captain_id="", reason="r", audit_client=clear_audit, path=path)


def test_clear_hard_stops_without_audit_client_still_resets(tmp_path):
    """The gate-driven clear passes no audit client (the broker refuses
    gate-process appends). State must still reset to OK; no Machine audit row
    is written — the resume is audited control-plane-side."""
    from shared.cost_breaker import clear_hard_stops, read_level

    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(10_000)
    path = str(tmp_path / "sticky_stop.db")
    assert read_level(path) == "HARD_STOP"

    cleared = clear_hard_stops(captain_id="captain-scott", reason="probe", path=path)
    assert cleared == [{"customer": "acme", "persona": "_machine", "prior_level": "HARD_STOP"}]
    assert read_level(path) == "OK"
    b.assert_allowed()
