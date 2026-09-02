"""Tests for shared/cost_breaker.py + the job-path wiring (ADR 0062, #1661).

Covers:
  - build_breaker: creates the state file + table, records cents, trips the
    ladder at the vendored thresholds, and emits AGENT_STOPPED through the
    audit sink on the HARD_STOP transition
  - assert_allowed raises StickyStopError at HARD_STOP
  - thresholds_from_config: authored cap, absent block, malformed value
  - read_level: missing file → None, rows → worst level, unreadable → unknown
  - read_stop_state: the cause travels with the worst row's level, degrades on
    a pre-cause schema, caps a pathological reason, never fabricates a cause
  - job_segment wiring: cost_capped outcome pre-fire; record after spend
  - job_worker: cost_capped dead-letters to needs_review
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.audit_contract import INSERT_SQL
from shared.cost_breaker import (
    _CREATE_TABLE_SQL,
    StickyStopError,
    build_breaker,
    read_level,
    read_stop_state,
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
    # Two states since 2026-09-02: cap 5000, hard stop at 200% = 10000 cents,
    # and nothing in between. The 80% / 100% rungs this test used to assert
    # restricted nothing and paged nobody -- see StickyStopLevel.
    state = b.record_cost_cents(4000)  # 80% of cap
    assert state.level.value == "OK"
    state = b.record_cost_cents(1000)  # 100% of cap: still not a stop
    assert state.level.value == "OK"
    state = b.record_cost_cents(4999)  # 199.98%: one cent short
    assert state.level.value == "OK"
    state = b.record_cost_cents(1)  # 200% -> HARD_STOP
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
# read_stop_state: the level AND the cause that produced it
# ---------------------------------------------------------------------------


def _seed(path: str, rows, *, with_cause_columns: bool = True) -> None:
    """Write sticky_stop rows directly, so a reader test controls every field.

    ``with_cause_columns=False`` reproduces a seat still running the schema
    from before reason/condition existed.

    Stamps here MUST use the millisecond-Z shape the seat actually writes
    (``sticky_stop._iso``: ``%Y-%m-%dT%H:%M:%S.mmmZ``). The tie-break compares
    ``updated_at`` lexically, which is only sound because every writer emits
    one fixed-width format -- so a fixture in a different shape would prove the
    ordering on input production never produces. Second-precision "…:00Z" and
    millisecond "…:00.123Z" invert the compare outright ('Z' > '.').
    """
    import sqlite3

    ddl = (
        _CREATE_TABLE_SQL
        if with_cause_columns
        else """
        CREATE TABLE sticky_stop_state (
          customer TEXT NOT NULL, persona TEXT NOT NULL,
          level TEXT NOT NULL DEFAULT 'OK', updated_at TEXT NOT NULL,
          PRIMARY KEY (customer, persona)
        )
        """
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute(ddl)
        for persona, level, reason, condition, updated_at in rows:
            if with_cause_columns:
                conn.execute(
                    "INSERT INTO sticky_stop_state "
                    "(customer, persona, level, updated_at, reason, condition) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("acme", persona, level, updated_at, reason, condition),
                )
            else:
                conn.execute(
                    "INSERT INTO sticky_stop_state "
                    "(customer, persona, level, updated_at) VALUES (?, ?, ?, ?)",
                    ("acme", persona, level, updated_at),
                )
        conn.commit()
    finally:
        conn.close()


def test_read_stop_state_carries_the_cause(tmp_path):
    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path,
        [
            (
                "_machine",
                "HARD_STOP",
                "consecutive_tool_failures=8 (window=600s, skill=mcp_smokeball_list_matters)",
                "consecutive_tool_failures",
                "2026-09-01T18:32:00.000Z",
            )
        ],
    )
    state = read_stop_state(path)
    assert state.level == "HARD_STOP"
    assert state.condition == "consecutive_tool_failures"
    assert "skill=mcp_smokeball_list_matters" in (state.reason or "")


def test_read_stop_state_cause_belongs_to_the_worst_row(tmp_path):
    """The whole point: a level paired with another persona's reason would
    send an operator to investigate the wrong meter.

    The winning row sits in the MIDDLE deliberately. A reader that takes the
    first row, or the last, returns a WARN cause here — so both naive
    implementations fail this, which a two-row fixture could not do.
    """
    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path,
        [
            (
                "first",
                "WARN",
                "cost_threshold=900c / cap=1000c",
                "cost_threshold",
                "2026-09-01T19:00:00.000Z",
            ),
            (
                "worst",
                "HARD_STOP",
                "refusal_cascade=5",
                "refusal_cascade",
                "2026-09-01T18:32:00.000Z",
            ),
            (
                "last",
                "WARN",
                "cost_threshold=910c / cap=1000c",
                "cost_threshold",
                "2026-09-01T19:30:00.000Z",
            ),
        ],
    )
    state = read_stop_state(path)
    assert state.level == "HARD_STOP"
    assert state.reason == "refusal_cascade=5"
    assert state.condition == "refusal_cascade"


def test_read_stop_state_ties_break_on_the_later_stamp(tmp_path):
    """Same middle-row construction: neither row order nor insertion order
    picks the winner, only the stamp does."""
    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path,
        [
            ("a", "HARD_STOP", "older", "cost_threshold", "2026-09-01T10:00:00.000Z"),
            ("b", "HARD_STOP", "newest", "refusal_cascade", "2026-09-01T20:00:00.000Z"),
            ("c", "HARD_STOP", "middling", "cost_threshold", "2026-09-01T15:00:00.000Z"),
        ],
    )
    assert read_stop_state(path).reason == "newest"


def test_read_stop_state_degrades_on_a_pre_cause_schema(tmp_path):
    """An un-reprovisioned seat still reports its level; only the cause is
    absent. Without the fallback this raises OperationalError and the level —
    the field the fleet actually gates on — is lost."""
    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path,
        [("_machine", "HARD_STOP", None, None, "2026-09-01T18:32:00.000Z")],
        with_cause_columns=False,
    )
    state = read_stop_state(path)
    assert state.level == "HARD_STOP"
    assert state.reason is None
    assert state.condition is None


def test_read_stop_state_caps_a_pathological_reason(tmp_path):
    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path, [("_machine", "HARD_STOP", "x" * 5000, "cost_threshold", "2026-09-01T18:32:00.000Z")]
    )
    assert len(read_stop_state(path).reason or "") == 300


def test_read_stop_state_failure_modes(tmp_path):
    path = str(tmp_path / "sticky_stop.db")
    # Absent file: fresh Machine.
    assert read_stop_state(path).level is None
    # Unreadable file: unknown, and never a fabricated cause.
    Path(path).write_text("not a database", encoding="utf-8")
    state = read_stop_state(path)
    assert state.level == "unknown"
    assert state.reason is None
    # Empty table: OK.
    Path(path).unlink()
    _seed(path, [])
    assert read_stop_state(path).level == "OK"


def test_read_stop_state_never_pairs_an_ok_level_with_a_cause(tmp_path):
    """An OK row must not carry a cause off the box. It would store a non-null
    condition against a healthy seat in D1 and defeat any later grouping."""
    path = str(tmp_path / "sticky_stop.db")
    _seed(path, [("_machine", "OK", "cleared", "captain_clear", "2026-09-01T10:00:00.000Z")])
    state = read_stop_state(path)
    assert state.level == "OK"
    assert state.reason is None
    assert state.condition is None


def test_read_stop_state_fails_toward_unknown_on_an_unrecognised_level(tmp_path):
    """Rows exist but none carry a ladder word: a writer we do not understand.
    OK here would be a FABRICATED healthy seat -- the same widening that burned
    the supervisor-state vocabulary at overlay#339."""
    path = str(tmp_path / "sticky_stop.db")
    _seed(path, [("_machine", "PANIC_STOP", "boom", "cost_threshold", "2026-09-01T10:00:00.000Z")])
    assert read_stop_state(path).level == "unknown"
    # An empty table is still a genuine OK -- absence of rows is not a
    # misunderstanding, and this must not regress into unknown.
    path2 = str(tmp_path / "empty.db")
    _seed(path2, [])
    assert read_stop_state(path2).level == "OK"


def test_pin_hard_stops_clears_the_condition_with_the_reason(tmp_path):
    """An operator pause overwrites the reason; leaving the prior meter's
    condition beside it makes the page name a meter unrelated to this stop."""
    from shared.cost_breaker import pin_hard_stops

    path = str(tmp_path / "sticky_stop.db")
    _seed(
        path,
        [
            (
                "_machine",
                "WARN",
                "cost_threshold=900c / cap=1000c",
                "cost_threshold",
                "2026-09-01T10:00:00.000Z",
            )
        ],
    )
    pin_hard_stops(actor_id="captain", reason="maintenance window", path=path)
    state = read_stop_state(path)
    assert state.level == "HARD_STOP"
    assert "operator_pause by captain" in (state.reason or "")
    assert state.condition is None


def test_read_level_still_agrees_with_read_stop_state(tmp_path):
    """read_level is a wrapper; prove the delegation did not change it."""
    path = str(tmp_path / "sticky_stop.db")
    assert read_level(path) is None
    _seed(path, [("_machine", "HARD_STOP", "r", "cost_threshold", "2026-09-01T18:00:00.000Z")])
    assert read_level(path) == read_stop_state(path).level == "HARD_STOP"


def test_a_seat_latched_at_a_removed_level_reads_ok(tmp_path):
    """The upgrade path, and the reason pilot-smokeball needed no clear.

    That seat latched SOFT_STOP on 2026-08-31 and sat there five days
    restricting nothing. Once the level ceases to exist there is nothing to
    clear: it reads OK, and its stale reason/condition go with it rather than
    hanging off a healthy seat.
    """
    for legacy in ("WARN", "SOFT_STOP"):
        path = str(tmp_path / f"{legacy}.db")
        _seed(
            path,
            [
                (
                    "_machine",
                    legacy,
                    "refusal_cascade=10",
                    "refusal_cascade",
                    "2026-08-31T13:30:51.823Z",
                )
            ],
        )
        state = read_stop_state(path)
        assert state.level == "OK", legacy
        assert state.reason is None, legacy
        assert state.condition is None, legacy

    # NEGATIVE CONTROL: a word from neither vocabulary is still not silently
    # accepted as a level -- it must not ride through as if it were known.
    junk = str(tmp_path / "junk.db")
    _seed(junk, [("_machine", "PANIC_STOP", "boom", "cost_threshold", "2026-09-01T10:00:00.000Z")])
    assert read_stop_state(junk).level == "unknown"


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


# ---------------------------------------------------------------------------
# Operator pause surface (ss#2003 — the portal kill switch's Machine leg)
# ---------------------------------------------------------------------------


def test_pin_hard_stops_pins_machine_row_on_fresh_state(tmp_path):
    from shared.cost_breaker import pin_hard_stops

    path = str(tmp_path / "sticky_stop.db")
    pinned = pin_hard_stops(actor_id="christa@firm.example", reason="firm pause", path=path)
    assert pinned == [{"customer": "_machine", "persona": "_machine", "prior_level": "OK"}]
    assert read_level(path) == "HARD_STOP"


def test_pin_hard_stops_pins_every_existing_row_and_breaker_refuses(tmp_path):
    import pytest as _pytest

    from shared.cost_breaker import StickyStopError, pin_hard_stops

    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(1)  # creates the (acme, _machine) row at OK
    path = str(tmp_path / "sticky_stop.db")
    assert read_level(path) == "OK"

    pinned = pin_hard_stops(actor_id="portal-admin", reason="client pause", path=path)
    assert {(p["customer"], p["persona"]) for p in pinned} == {("acme", "_machine")}
    assert read_level(path) == "HARD_STOP"
    with _pytest.raises(StickyStopError):
        b.assert_allowed()


def test_pin_then_clear_round_trip(tmp_path):
    from shared.cost_breaker import clear_hard_stops, pin_hard_stops

    audit = FakeAuditClient()
    b = _breaker(tmp_path, audit)
    b.record_cost_cents(1)
    path = str(tmp_path / "sticky_stop.db")
    pin_hard_stops(actor_id="portal-admin", reason="client pause", path=path)
    assert read_level(path) == "HARD_STOP"

    cleared = clear_hard_stops(
        captain_id="portal-admin", reason="client resume", audit_client=None, path=path
    )
    assert cleared == [{"customer": "acme", "persona": "_machine", "prior_level": "HARD_STOP"}]
    assert read_level(path) == "OK"
    b.assert_allowed()


def test_pin_hard_stops_requires_actor_and_reason(tmp_path):
    import pytest as _pytest

    from shared.cost_breaker import pin_hard_stops

    path = str(tmp_path / "sticky_stop.db")
    with _pytest.raises(ValueError):
        pin_hard_stops(actor_id="", reason="r", path=path)
    with _pytest.raises(ValueError):
        pin_hard_stops(actor_id="a", reason="", path=path)


# ---------------------------------------------------------------------------
# The two-state collapse (2026-09-02): what must NOT have changed
# ---------------------------------------------------------------------------


def test_the_hard_stop_thresholds_did_not_move():
    """The load-bearing claim of the collapse, pinned.

    Removing WARN and SOFT_STOP was meant to delete two states that did
    nothing -- NOT to make seats stop more or less easily. These numbers are
    where a seat stopped before the collapse; a change to any of them is a
    change to when a client's Operator halts, and must be deliberate rather
    than a side effect of tidying the ladder.
    """
    t = DEFAULT_THRESHOLDS
    assert t.tool_failure_hard_stop == 8
    assert t.tool_failure_window_seconds == 600
    assert t.refusal_hard_stop == 20
    assert t.refusal_window_seconds == 1800
    assert t.cost_daily_cents == 5_000
    assert t.cost_hard_stop_pct == 200
    # And the removed rungs are actually gone, not merely unused: a stale
    # attribute would let a reader think the middle of the ladder still exists.
    for dead in (
        "tool_failure_warn",
        "tool_failure_soft_stop",
        "refusal_warn",
        "refusal_soft_stop",
        "cost_warn_pct",
        "cost_soft_stop_pct",
    ):
        assert not hasattr(t, dead), dead


def test_a_time_budget_overrun_is_recorded_and_stops_nothing(tmp_path):
    """The one meter the collapse forced a choice on.

    Its only outcome was SOFT_STOP and it has no hard threshold, so it either
    stops nothing or starts halting seats. It stops nothing -- exactly what it
    did before, since SOFT_STOP restricted nothing -- but the overrun must
    still leave an audit row, because that row is the evidence a later
    decision to enforce the budget would rest on.
    """
    import asyncio

    from shared.sticky_stop import DEFAULT_THRESHOLDS as T
    from shared.sticky_stop import SqliteStickyStopStore, StickyStopMachine

    written: list = []

    class _Sink:
        async def write(self, record):
            written.append(record)

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "s.db"))
    conn.execute(_CREATE_TABLE_SQL)
    store = SqliteStickyStopStore(conn)
    machine = StickyStopMachine(store=store, audit_writer=_Sink())

    state = asyncio.run(
        machine.record_runtime_seconds(
            customer="acme", persona="_machine", seconds=T.time_budget_seconds + 1
        )
    )
    assert state.level.value == "OK"  # stops nothing
    assert state.condition is None  # and leaves no cause on a healthy seat
    assert len(written) == 1, "the overrun must still be recorded"
    row = written[0]
    assert row.action_type == "INVARIANT_VIOLATION"
    assert row.metadata["condition_triggered"] == "time_budget_exceeded"
    assert row.metadata["sticky_stop_transition"] is False
    assert row.metadata["level_unchanged_by_design"] is True

    # Under budget: nothing recorded at all.
    written.clear()
    asyncio.run(
        machine.record_runtime_seconds(
            customer="acme", persona="_machine", seconds=T.time_budget_seconds - 1
        )
    )
    assert written == []
