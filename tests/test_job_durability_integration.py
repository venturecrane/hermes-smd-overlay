"""Integration scenarios: the REAL worker + REAL segment loop (B1, ADR 0051).

``test_job_worker.py`` drives :class:`JobWorker` with a *programmable* segment;
``test_job_segment.py`` drives ``make_run_segment`` with a fake agent. Neither
proves the two wired TOGETHER. This file does — the real ``JobWorker``
orchestrator running the real ``make_run_segment`` adapter, against a faithful
epoch-fencing ledger double and a fake agent — so the design's named scenarios
hold across the seam:

  * crash mid-tool-call -> resume -> the trailing-tool_call repair prevents the
    runtime from re-issuing the interrupted call (no double-execution);
  * oversized tool payload mid-segment -> the per-segment spend breaches budget
    -> hard-stop to needs_review at the iteration boundary;
  * readiness barrier -> the worker claims NOTHING until broker/plugins/adapter
    report ready;
  * identity -> the worker loads model from the row and parks a row with no
    resolved identity to needs_review (never guesses a default);
  * a construction-equivalence smoke on the staging seam (the worker thread wires
    ``build_hermes_agent`` with exactly the kwargs the adapter passes).

The fake ledger faithfully models claim/epoch/record fencing (the same contract
the real ``JobLedgerWriter`` enforces in the broker repo); the cross-repo real
ledger is covered there (``workspace_broker/tests/test_job_durability_scenarios``).
"""

from __future__ import annotations

import pytest

from shared.job_segment import make_run_segment
from shared.job_worker import TERMINAL, JobWorker, SegmentOutcome


class FakeLedger:
    """In-memory twin of JobLedgerWriter's fencing contract (claim bumps epoch,
    record is epoch-fenced). Mirrors the FakeClient in test_job_worker but is
    shared across these wired scenarios."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}

    def add(self, job_id: str, **overrides) -> None:
        self.jobs[job_id] = {
            "id": job_id, "status": "queued", "spent_cents": 0, "budget_cents": 1000,
            "model": "claude-sonnet-4-6", "persona_id": "intake-coordinator",
            "attempts": 0, "cancel_requested": 0, "brief": "do the long thing",
            "current_tip_session_id": "", "result_ref": None, "error": None,
            "deliver_to": "telegram:1", "lease_epoch": 0, "root_session_id": "",
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
            return False
        r.update(fields)
        return True


class FakeSessionDB:
    """Returns a fixed reloaded history for a tip (the resume lineage)."""

    def __init__(self, history_by_tip: dict[str, list[dict]] | None = None) -> None:
        self._by_tip = history_by_tip or {}

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return list(self._by_tip.get(session_id, []))


class ScriptedAgent:
    """A fake AIAgent: yields a programmed run_conversation result, records what
    history/message it was handed, and can rotate its session id."""

    def __init__(self, results, *, rotate_to=None):
        self._results = iter(results)
        self.session_id = "sess-in"
        self._rotate_to = rotate_to
        self.calls: list[dict] = []

    def run_conversation(self, user_message, conversation_history=None):
        self.calls.append({"message": user_message, "history": conversation_history})
        if self._rotate_to:
            self.session_id = self._rotate_to
        return next(self._results)


def _worker(ledger, run_segment, **kw):
    return JobWorker(
        ledger,
        worker_id="w1",
        run_segment=run_segment,
        deliver=kw.get("deliver", lambda job, ref: True),
        put_result=kw.get("put_result", lambda job, text: "r2://" + job["id"]),
        max_attempts=kw.get("max_attempts", 5),
    )


def _segment(agent, session_db, *, preflight=1, seg_cost=5):
    return make_run_segment(
        session_db=session_db,
        build_agent=lambda **kw: agent,
        preflight_cost=lambda model, h: preflight,
        segment_cost=lambda a: seg_cost,
    )


# -- crash mid-tool-call -> repair-on-resume -> no double-execution -----------
def test_resume_repairs_trailing_tool_call_and_completes_once(tmp_path):
    """A first segment crashed after emitting a tool_call with no result (the
    interrupted-mid-tool window). On resume the real segment loop reloads that
    history, the repair appends a synthetic 'interrupted' result, and the agent
    re-plans to completion — proving the wired worker+segment does not re-issue
    the interrupted call."""
    led = FakeLedger()
    led.add("J", root_session_id="job_J", current_tip_session_id="tip-1")
    # The reloaded lineage ends on an assistant tool_call with no tool result.
    session_db = FakeSessionDB({"tip-1": [{"role": "assistant", "tool_calls": [{"id": "c1"}]}]})
    agent = ScriptedAgent([{"completed": True, "final_response": "re-planned answer"}])
    run_segment = _segment(agent, session_db)

    assert _worker(led, run_segment).run_one("J") == "done"

    # The agent was handed the REPAIRED history (synthetic interrupted result),
    # and a resume message — never the original brief.
    handed = agent.calls[0]["history"]
    assert handed[-1]["role"] == "tool"
    assert "interrupted" in handed[-1]["content"]
    assert "Continue" in agent.calls[0]["message"]
    assert led.read("J")["status"] == "done"


def test_crash_then_reclaim_resumes_to_completion_exactly_once():
    """The worker+segment crash-resume path: segment 1 raises (crash); the job is
    left non-terminal and re-claimable; a second run resumes and completes. The
    completing segment runs exactly once."""
    led = FakeLedger()
    led.add("J", root_session_id="job_J", current_tip_session_id="job_J")
    session_db = FakeSessionDB()  # empty history -> first-run path
    completions = {"n": 0}

    def build_agent(**kw):
        # Two distinct agents across the two runs; the second completes.
        if completions["n"] == 0:
            class _Boom:
                session_id = "s"

                def run_conversation(self, *a, **k):
                    raise RuntimeError("crash mid-segment")

            return _Boom()
        return ScriptedAgent([{"completed": True, "final_response": "ok"}])

    run_segment = make_run_segment(
        session_db=session_db, build_agent=build_agent,
        preflight_cost=lambda m, h: 1, segment_cost=lambda a: 3,
    )
    w = _worker(led, run_segment)

    # First run: the segment raises a non-fatal error -> worker records and stops.
    assert w.run_one("J") == "errored"
    assert led.read("J")["status"] not in TERMINAL
    completions["n"] = 1
    # Resume: re-claim (new epoch) and complete.
    assert w.run_one("J") == "done"


# -- oversized payload mid-segment -> budget hard-stop ------------------------
def test_oversized_segment_payload_hard_stops_at_iteration_boundary():
    """A tool returns an oversized payload, so the segment's real provider usage
    (segment_cost) comes back far above the budget. At the post-segment iteration
    boundary the worker records the spend, sees new_spent > budget, and
    dead-letters to needs_review — the hard-stop the design's cost test names."""
    led = FakeLedger()
    led.add("J", budget_cents=50, root_session_id="job_J", current_tip_session_id="job_J")
    agent = ScriptedAgent([{"completed": False, "final_response": None}])
    # segment_cost dwarfs the budget (an oversized tool result blew the context).
    run_segment = _segment(agent, FakeSessionDB(), preflight=1, seg_cost=5000)

    assert _worker(led, run_segment).run_one("J") == "needs_review"
    assert "budget exceeded mid-segment" in led.read("J")["error"]


def test_preflight_refuses_segment_that_would_exceed_budget():
    """The pre-spend half of the cost guard, wired through the real segment: the
    estimated next-request input cost alone exceeds the remaining budget, so the
    segment is refused BEFORE the agent is built (no spend, no call)."""
    led = FakeLedger()
    led.add("J", budget_cents=100, root_session_id="job_J", current_tip_session_id="job_J")
    built = {"n": 0}

    def build_agent(**kw):
        built["n"] += 1
        return ScriptedAgent([{"completed": True, "final_response": "x"}])

    run_segment = make_run_segment(
        session_db=FakeSessionDB(), build_agent=build_agent,
        preflight_cost=lambda m, h: 10_000, segment_cost=lambda a: 1,
    )
    assert _worker(led, run_segment).run_one("J") == "needs_review"
    assert "pre-spend" in led.read("J")["error"]
    assert built["n"] == 0  # the agent was never constructed


# -- readiness barrier gates the sweep ----------------------------------------
def test_worker_claims_nothing_until_ready():
    """The boot-sweep is gated behind the readiness barrier: while readiness_ok
    is False the worker claims nothing; once ready it sweeps. We model the
    barrier the way the runtime loop does — call sweep() only when readiness_ok
    passes — and assert no claim happens while not-ready."""
    from shared.job_worker_runtime import readiness_ok

    led = FakeLedger()
    led.add("J", root_session_id="job_J", current_tip_session_id="job_J")
    agent = ScriptedAgent([{"completed": True, "final_response": "x"}])
    w = _worker(led, _segment(agent, FakeSessionDB()))

    broker_ready = {"v": False}
    checks = [lambda: broker_ready["v"]]

    # Not ready: the gate withholds the sweep; nothing is claimed.
    if readiness_ok(checks):
        w.sweep()
    assert led.read("J")["status"] == "queued"
    assert led.read("J")["attempts"] == 0

    # Ready: the gate opens and the sweep runs the job to done.
    broker_ready["v"] = True
    if readiness_ok(checks):
        w.sweep()
    assert led.read("J")["status"] == "done"


# -- identity from the row ----------------------------------------------------
def test_worker_runs_under_row_model_and_persona():
    """The segment is built with the model carried on the row (Decision 9 — never
    a default). We assert the model the adapter passed to build_agent is the
    row's model."""
    led = FakeLedger()
    led.add("J", model="claude-opus-4-8", root_session_id="job_J", current_tip_session_id="job_J")
    seen = {}

    def build_agent(**kw):
        seen.update(kw)
        return ScriptedAgent([{"completed": True, "final_response": "x"}])

    run_segment = make_run_segment(
        session_db=FakeSessionDB(), build_agent=build_agent,
        preflight_cost=lambda m, h: 1, segment_cost=lambda a: 1,
    )
    assert _worker(led, run_segment).run_one("J") == "done"
    assert seen["model"] == "claude-opus-4-8"


def test_row_without_resolved_model_parks_for_review():
    """Identity assertion: a row whose model never resolved parks to needs_review
    rather than running under a guessed default."""
    led = FakeLedger()
    led.add("J", model="")
    agent = ScriptedAgent([{"completed": True, "final_response": "x"}])
    assert _worker(led, _segment(agent, FakeSessionDB())).run_one("J") == "needs_review"
    assert "no resolved model" in led.read("J")["error"]


# -- construction-equivalence smoke (staging seam) ----------------------------
def test_build_hermes_agent_accepts_the_adapter_kwargs():
    """The worker thread wires make_run_segment with build_agent=build_hermes_agent;
    the adapter calls build_agent(model=, session_id=, max_iterations=, session_db=).
    Assert build_hermes_agent's signature accepts exactly those keyword args (so a
    drift between the adapter's call and the seam's signature fails in CI, not on
    the Machine). The body imports Hermes lazily, so we inspect the signature
    rather than invoke it."""
    import inspect

    from shared.job_worker_runtime import build_hermes_agent

    sig = inspect.signature(build_hermes_agent)
    # All four adapter kwargs are accepted, keyword-only (the seam declares them so).
    for name in ("model", "session_id", "max_iterations", "session_db"):
        assert name in sig.parameters, f"build_hermes_agent missing kwarg {name!r}"
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_run_segment_calls_build_agent_with_seam_kwarg_names():
    """The other half of the equivalence: the adapter actually passes the four
    kwargs build_hermes_agent expects. Capture the kwargs the segment hands to
    build_agent and assert they are exactly the seam's parameter names."""
    import inspect

    from shared.job_worker_runtime import build_hermes_agent

    led = FakeLedger()
    led.add("J", root_session_id="job_J", current_tip_session_id="job_J")
    seen = {}

    def build_agent(**kw):
        seen.update(kw)
        return ScriptedAgent([{"completed": True, "final_response": "x"}])

    run_segment = make_run_segment(
        session_db=FakeSessionDB(), build_agent=build_agent,
        preflight_cost=lambda m, h: 1, segment_cost=lambda a: 1,
    )
    _worker(led, run_segment).run_one("J")

    seam_kwargs = {
        n for n, p in inspect.signature(build_hermes_agent).parameters.items()
        if p.kind == inspect.Parameter.KEYWORD_ONLY
    }
    assert set(seen) == seam_kwargs
