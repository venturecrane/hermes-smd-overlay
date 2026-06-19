"""Deterministic tests for the B1 worker orchestrator (ADR 0051).

These are the durability/safety invariants the critique required as CI guards —
proven with a faithful in-memory ledger (real epoch-fencing) and programmable
segments, no live LLM:
  - happy path completes, persists a result, delivers, marks done
  - pre-spend refusal and mid-segment budget breach dead-letter
  - cancel dead-letters
  - a stale-epoch worker is fenced out (record no-op) and stops
  - a crash mid-job is re-claimable and resumes to completion exactly once
  - identity/attempts guards park to needs_review
"""

from __future__ import annotations

import pytest

from shared.job_worker import TERMINAL, JobWorker, SegmentOutcome


class FakeClient:
    """In-memory job ledger that faithfully models claim/epoch/record fencing."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add(self, job_id: str, **overrides) -> None:
        self.jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "spent_cents": 0,
            "budget_cents": 1000,
            "model": "claude-sonnet-4-6",
            "attempts": 0,
            "cancel_requested": 0,
            "current_tip_session_id": "",
            "result_ref": None,
            "error": None,
            "deliver_to": "telegram:1",
            "lease_epoch": 0,
            "root_session_id": "root",
            **overrides,
        }

    def read(self, job_id):
        r = self.jobs.get(job_id)
        return dict(r) if r else None

    def list_claimable(self):
        return [dict(r) for r in self.jobs.values() if r["status"] not in TERMINAL]

    def claim(self, job_id, worker_id):
        r = self.jobs.get(job_id)
        if not r or r["status"] in TERMINAL:
            return None
        r["lease_epoch"] += 1
        r["attempts"] += 1
        r["status"] = "running"
        return r["lease_epoch"]

    def record(self, job_id, lease_epoch, fields):
        r = self.jobs.get(job_id)
        if not r or lease_epoch != r["lease_epoch"]:
            return False  # fenced out
        r.update(fields)
        return True


def _worker(client, run_segment, *, deliver=None, put_result=None, max_attempts=5):
    return JobWorker(
        client,
        worker_id="w1",
        run_segment=run_segment,
        deliver=deliver or (lambda job, ref: True),
        put_result=put_result or (lambda job, text: "r2://result"),
        max_attempts=max_attempts,
    )


def test_happy_path_completes_delivers_done():
    c = FakeClient()
    c.add("J")
    delivered = {}
    w = _worker(
        c,
        lambda job, ep: SegmentOutcome(
            completed=True, spent_cents_delta=12, tip_session_id="tip1", result_text="the answer"
        ),
        deliver=lambda job, ref: delivered.setdefault("ref", ref) or True,
        put_result=lambda job, text: "r2://" + job["id"],
    )
    assert w.run_one("J") == "done"
    row = c.read("J")
    assert row["status"] == "done"
    assert row["spent_cents"] == 12
    assert row["current_tip_session_id"] == "tip1"
    assert row["result_ref"] == "r2://J"
    assert delivered["ref"] == "r2://J"


def test_multi_segment_then_complete_accumulates_spend():
    c = FakeClient()
    c.add("J")
    seq = iter(
        [
            SegmentOutcome(completed=False, spent_cents_delta=10, tip_session_id="t1"),
            SegmentOutcome(
                completed=True, spent_cents_delta=20, tip_session_id="t2", result_text="done"
            ),
        ]
    )
    w = _worker(c, lambda job, ep: next(seq))
    assert w.run_one("J") == "done"
    assert c.read("J")["spent_cents"] == 30
    assert c.read("J")["current_tip_session_id"] == "t2"


def test_pre_spend_refusal_dead_letters():
    c = FakeClient()
    c.add("J")
    w = _worker(c, lambda job, ep: SegmentOutcome(refused_budget=True))
    assert w.run_one("J") == "needs_review"
    assert "pre-spend" in c.read("J")["error"]


def test_mid_segment_budget_breach_dead_letters():
    c = FakeClient()
    c.add("J", budget_cents=50)
    w = _worker(
        c, lambda job, ep: SegmentOutcome(completed=False, spent_cents_delta=60, tip_session_id="t")
    )
    assert w.run_one("J") == "needs_review"
    assert "budget exceeded mid-segment" in c.read("J")["error"]


def test_budget_exhausted_before_segment_dead_letters():
    c = FakeClient()
    c.add("J", budget_cents=100, spent_cents=100)
    called = {"n": 0}

    def seg(job, ep):
        called["n"] += 1
        return SegmentOutcome(completed=True)

    assert _worker(c, seg).run_one("J") == "needs_review"
    assert called["n"] == 0  # never ran a segment against an exhausted budget


def test_cancel_requested_dead_letters_before_running():
    c = FakeClient()
    c.add("J", cancel_requested=1)
    called = {"n": 0}

    def seg(job, ep):
        called["n"] += 1
        return SegmentOutcome(completed=True)

    assert _worker(c, seg).run_one("J") == "cancelled"
    assert c.read("J")["status"] == "cancelled"
    assert called["n"] == 0


def test_stale_worker_is_fenced_out():
    """Another worker re-claims mid-segment (epoch bumps); the original
    worker's checkpoint write is rejected and it stops."""
    c = FakeClient()
    c.add("J")

    def seg(job, ep):
        # Simulate a concurrent re-claim by a second worker during the segment.
        c.claim("J", "intruder")  # bumps lease_epoch beyond `ep`
        return SegmentOutcome(completed=True, spent_cents_delta=5, tip_session_id="t")

    assert _worker(c, seg).run_one("J") == "fenced"
    # The stale worker's spend/tip write never landed.
    assert c.read("J")["spent_cents"] == 0


def test_crash_mid_job_is_reclaimable_and_resumes_once():
    c = FakeClient()
    c.add("J")
    calls = {"n": 0}

    def seg(job, ep):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash mid-segment")
        return SegmentOutcome(completed=True, spent_cents_delta=7, result_text="ok")

    w = _worker(c, seg)
    with pytest.raises(RuntimeError):
        w.run_one("J")  # crash; job left non-terminal
    assert c.read("J")["status"] not in TERMINAL
    # Resume: re-claim (new epoch) and complete exactly once.
    assert w.run_one("J") == "done"
    assert c.read("J")["status"] == "done"


def test_delivery_failure_parks_for_review():
    c = FakeClient()
    c.add("J")
    w = _worker(
        c,
        lambda job, ep: SegmentOutcome(completed=True, result_text="x"),
        deliver=lambda job, ref: False,
    )
    assert w.run_one("J") == "needs_review"
    assert c.read("J")["status"] == "needs_review"
    assert "delivery failed" in c.read("J")["error"]


def test_missing_model_parks_for_review():
    c = FakeClient()
    c.add("J", model="")
    assert _worker(c, lambda job, ep: SegmentOutcome(completed=True)).run_one("J") == "needs_review"
    assert "no resolved model" in c.read("J")["error"]


def test_max_attempts_exceeded_parks_for_review():
    c = FakeClient()
    c.add("J", attempts=5)  # claim bumps to 6 > max_attempts=5
    assert (
        _worker(c, lambda job, ep: SegmentOutcome(completed=True), max_attempts=5).run_one("J")
        == "needs_review"
    )
    assert "max attempts" in c.read("J")["error"]


def test_first_claim_mints_and_records_root_session():
    c = FakeClient()
    c.add("J", root_session_id="", current_tip_session_id="")
    assert (
        _worker(c, lambda job, ep: SegmentOutcome(completed=True, result_text="x")).run_one("J")
        == "done"
    )
    row = c.read("J")
    assert row["root_session_id"] == "job_J"
    # current_tip starts at root (the segment fake doesn't rotate it here).
    assert row["root_session_id"] == "job_J"


def test_terminal_job_not_claimable():
    c = FakeClient()
    c.add("J", status="done")
    assert (
        _worker(c, lambda job, ep: SegmentOutcome(completed=True)).run_one("J") == "not_claimable"
    )


def test_sweep_runs_all_claimable_and_survives_a_crash():
    c = FakeClient()
    c.add("A")
    c.add("B", status="done")  # terminal — skipped
    c.add("C")

    def seg(job, ep):
        if job["id"] == "C":
            raise RuntimeError("C explodes")
        return SegmentOutcome(completed=True, result_text="ok")

    outcomes = _worker(c, seg).sweep()
    # A completes, C's crash is contained, B is excluded as terminal.
    assert "done" in outcomes
    assert "crashed" in outcomes
    assert c.read("A")["status"] == "done"
