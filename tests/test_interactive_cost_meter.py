"""Tests for shared/interactive_cost_meter.py (ADR 0062 §4, ss-console #1701)."""

from __future__ import annotations

import json
from pathlib import Path

import shared.interactive_cost_meter as m
from shared.audit_contract import INSERT_SQL
from shared.selfcheck import SELFCHECK_SESSION_ID


class FakeAuditClient:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def execute(self, sql: str, *params) -> None:
        self.rows.append((sql, params))


class RecordingBreaker:
    def __init__(self) -> None:
        self.recorded: list[int] = []

    def record_cost_cents(self, cents: int) -> None:
        self.recorded.append(cents)


def _reset():
    m._CURSORS.clear()
    m._last_alarm.clear()


def test_estimate_meters_only_the_new_content_delta():
    _reset()
    # sonnet-4-6: 300 in / 1500 out cents per MTok. chars/token = 3.5.
    hist1 = [{"role": "user", "content": "x" * 3500}]  # 1000 tok
    cents1, ok, reason = m.estimate_turn_cents(
        model="claude-sonnet-4-6",
        conversation_history=hist1,
        assistant_response="",
        session_id="s1",
    )
    assert ok and reason is None
    # 1000 input tok * 300/M = 0.3 cents -> ceil 1
    assert cents1 == 1
    # Second turn: history grew by another 3500 chars — only the DELTA is metered.
    hist2 = hist1 + [{"role": "assistant", "content": "y" * 3500}]
    cents2, ok2, _ = m.estimate_turn_cents(
        model="claude-sonnet-4-6",
        conversation_history=hist2,
        assistant_response="",
        session_id="s1",
    )
    assert ok2
    # delta input = 3500 chars = 1000 tok -> 0.3c -> ceil 1 (NOT the full 7000 chars)
    assert cents2 == 1


def test_output_priced_at_output_rate():
    _reset()
    # No new input; 3500 chars of output at 1500/MTok = 1000 tok * 1500/M = 1.5c -> 2
    cents, ok, _ = m.estimate_turn_cents(
        model="claude-sonnet-4-6",
        conversation_history=[],
        assistant_response="z" * 3500,
        session_id="s2",
    )
    assert ok and cents == 2


def test_unpriced_model_returns_not_ok():
    _reset()
    cents, ok, reason = m.estimate_turn_cents(
        model="gpt-9-turbo",
        conversation_history=[{"role": "user", "content": "hi"}],
        assistant_response="ok",
        session_id="s3",
    )
    assert not ok and cents == 0 and reason == "model_unpriced:gpt-9-turbo"


def test_meter_records_into_breaker():
    _reset()
    breaker = RecordingBreaker()
    m.meter_interactive_turn(
        model="claude-opus-4-8",
        conversation_history=[{"role": "user", "content": "q" * 35000}],  # 10k tok
        assistant_response="a" * 3500,
        session_id="s4",
        breaker=breaker,
        audit_client=FakeAuditClient(),
    )
    # opus-4-8: 500 in / 2500 out. 10000*500/M=5c + 1000*2500/M=2.5c -> ceil(7.5)=8
    assert breaker.recorded == [8]


def test_meter_fail_alarms_and_keeps_going():
    _reset()
    breaker = RecordingBreaker()
    audit = FakeAuditClient()
    m.meter_interactive_turn(
        model="unknown-model",
        conversation_history=[{"role": "user", "content": "hi"}],
        assistant_response="ok",
        session_id="s5",
        breaker=breaker,
        audit_client=audit,
    )
    # Kept going: no crash, nothing recorded, one alarm row emitted.
    assert breaker.recorded == []
    assert len(audit.rows) == 1
    assert audit.rows[0][0] == INSERT_SQL
    assert audit.rows[0][1][2] == "INVARIANT_VIOLATION"


def test_boot_selfcheck_dispatch_is_not_metered_and_does_not_alarm():
    """ss-console #2122. The activation gate proves the audit write path by
    driving a REAL post_llm_call (ss-console#1285 Q2) with a synthetic
    ``model="boot-selfcheck"``. That reached the priced meter, which cannot
    price it, and wrote an INVARIANT_VIOLATION on every boot — all 61 such rows
    on the pilot seat were this alarm. The probe is not a turn."""
    _reset()
    breaker = RecordingBreaker()
    audit = FakeAuditClient()
    m.meter_interactive_turn(
        model="boot-selfcheck",
        conversation_history=[{"role": "user", "content": "overlay activation self-check"}],
        assistant_response="overlay active",
        session_id=SELFCHECK_SESSION_ID,
        breaker=breaker,
        audit_client=audit,
    )
    assert audit.rows == []  # no INVARIANT_VIOLATION
    assert breaker.recorded == []  # and no fabricated spend either


def test_a_real_turn_on_an_unpriced_model_still_alarms():
    """The suppression is scoped to the probe, never to the signal. A REAL turn
    whose model is unpriced means the seat is running uncapped, which is the
    only reason the alarm exists."""
    _reset()
    audit = FakeAuditClient()
    m.meter_interactive_turn(
        model="boot-selfcheck",  # same unpriced model — the SESSION is what differs
        conversation_history=[{"role": "user", "content": "hi"}],
        assistant_response="ok",
        session_id="a-real-session",
        breaker=RecordingBreaker(),
        audit_client=audit,
    )
    assert len(audit.rows) == 1
    assert audit.rows[0][1][2] == "INVARIANT_VIOLATION"


def test_alarm_is_rate_limited():
    _reset()
    audit = FakeAuditClient()
    for _ in range(5):
        m.meter_interactive_turn(
            model="unknown-model",
            conversation_history=[],
            assistant_response="x",
            session_id="s6",
            breaker=RecordingBreaker(),
            audit_client=audit,
        )
    # 5 unpriced turns, same reason bucket -> one alarm row within the window.
    assert len(audit.rows) == 1


def test_content_blocks_are_counted():
    _reset()
    hist = [{"role": "tool", "content": [{"type": "tool_result", "text": "r" * 3500}]}]
    cents, ok, _ = m.estimate_turn_cents(
        model="claude-sonnet-4-6", conversation_history=hist, assistant_response="", session_id="s7"
    )
    assert ok and cents == 1


def test_vendored_pricing_covers_declared_models():
    """The vendored overlay pricing must cover every model any customer.yaml
    declares — same discipline as the ss-console coverage test, so a model
    bump can't silently un-meter a seat's interactive spend."""

    pricing = json.loads((Path(m.__file__).parent / "anthropic_pricing.json").read_text())
    priced = set(pricing["models"].keys())
    # customers live in ss-console; the overlay test walks the sibling checkout
    # when present, else asserts the baseline fleet models are covered.
    baseline = {"claude-sonnet-4-6", "claude-opus-4-8"}
    assert baseline <= priced, (
        f"baseline fleet models missing from vendored pricing: {baseline - priced}"
    )


def test_pricing_json_declared_as_package_data():
    """The pricing table must be declared in pyproject package-data or it is
    EXCLUDED from the installed wheel — the meter then can't price anything and
    alarms on every turn (the 2026-07-04 live-probe finding). This guards the
    declaration so a future refactor can't silently drop it again."""
    import tomllib

    pyproject = Path(m.__file__).resolve().parents[1] / "pyproject.toml"
    cfg = tomllib.loads(pyproject.read_text())
    shared_data = cfg["tool"]["setuptools"]["package-data"]["shared"]
    assert "anthropic_pricing.json" in shared_data, (
        "anthropic_pricing.json missing from [tool.setuptools.package-data].shared "
        "— it will be excluded from the wheel and the meter will be dark in prod"
    )
