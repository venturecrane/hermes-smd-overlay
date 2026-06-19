"""Tests for the B1 segment adapter (ADR 0051).

Covers the trailing-tool_call repair (a resume-correctness guard the critique
called out) and the segment binding's logic — first-run vs resume, pre-spend
refusal, tip rotation, completion mapping, and job-context env injection — with
a fake agent + fake session_db. The real AIAgent construction is the staging
seam.
"""

from __future__ import annotations

import os

from shared.job_segment import make_run_segment, repair_trailing_tool_call


# -- repair_trailing_tool_call -------------------------------------------------
def test_repair_noop_on_empty():
    assert repair_trailing_tool_call([]) == []


def test_repair_noop_when_last_is_tool_result():
    h = [{"role": "assistant", "tool_calls": [{"id": "c1"}]}, {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    assert repair_trailing_tool_call(h) == h


def test_repair_noop_when_assistant_has_no_tool_calls():
    h = [{"role": "assistant", "content": "final answer"}]
    assert repair_trailing_tool_call(h) == h


def test_repair_appends_synthetic_results_for_unmatched_calls():
    h = [{"role": "assistant", "tool_calls": [{"id": "c1"}, {"id": "c2"}]}]
    out = repair_trailing_tool_call(h)
    assert len(out) == 3
    assert out[1]["role"] == "tool" and out[1]["tool_call_id"] == "c1"
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "c2"
    assert "interrupted" in out[1]["content"]


# -- make_run_segment ----------------------------------------------------------
class _FakeSessionDB:
    def __init__(self, history):
        self._history = history

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return list(self._history)


class _FakeAgent:
    def __init__(self, result, *, new_session_id=None):
        self.session_id = new_session_id or "sess-in"
        self._result = result
        self.seen_env = {}
        self.seen_history = "unset"
        self.seen_message = None

    def run_conversation(self, user_message, conversation_history=None):
        self.seen_env = {
            "HERMES_JOB_ID": os.environ.get("HERMES_JOB_ID"),
            "HERMES_JOB_LEASE_EPOCH": os.environ.get("HERMES_JOB_LEASE_EPOCH"),
        }
        self.seen_history = conversation_history
        self.seen_message = user_message
        return self._result


def _job(**over):
    return {"id": "J", "model": "m", "brief": "do the thing", "budget_cents": 1000,
            "spent_cents": 0, "root_session_id": "", "current_tip_session_id": "", **over}


def _make(agent, history, *, preflight=1, seg_cost=5):
    built = {}

    def build_agent(**kw):
        built.update(kw)
        return agent

    rs = make_run_segment(
        session_db=_FakeSessionDB(history),
        build_agent=build_agent,
        preflight_cost=lambda model, h: preflight,
        segment_cost=lambda a: seg_cost,
    )
    return rs, built


def test_first_run_uses_brief_and_no_history():
    agent = _FakeAgent({"completed": True, "final_response": "answer"}, new_session_id="sess-tip")
    rs, built = _make(agent, history=[])
    out = rs(_job(current_tip_session_id="root"), 1)
    assert agent.seen_message == "do the thing"
    assert agent.seen_history is None
    assert out.completed is True
    assert out.result_text == "answer"
    assert out.spent_cents_delta == 5
    assert out.tip_session_id == "sess-tip"  # the agent's (rotated) session
    assert built["session_id"] == "root"


def test_resume_uses_continue_and_repaired_history():
    history = [{"role": "assistant", "tool_calls": [{"id": "c1"}]}]
    agent = _FakeAgent({"completed": False, "final_response": None})
    rs, _ = _make(agent, history=history)
    out = rs(_job(current_tip_session_id="tip5"), 2)
    assert "Continue" in agent.seen_message
    # History was repaired (synthetic tool result appended).
    assert agent.seen_history[-1]["role"] == "tool"
    assert out.completed is False
    assert out.result_text is None


def test_preflight_refusal_skips_the_call():
    agent = _FakeAgent({"completed": True, "final_response": "x"})
    rs, built = _make(agent, history=[], preflight=10_000)
    out = rs(_job(budget_cents=100, spent_cents=0, current_tip_session_id="root"), 1)
    assert out.refused_budget is True
    assert built == {}  # build_agent never called


def test_job_context_env_injected_during_call_and_restored():
    os.environ.pop("HERMES_JOB_ID", None)
    agent = _FakeAgent({"completed": True, "final_response": "x"})
    rs, _ = _make(agent, history=[])
    rs(_job(current_tip_session_id="root"), 7)
    assert agent.seen_env["HERMES_JOB_ID"] == "J"
    assert agent.seen_env["HERMES_JOB_LEASE_EPOCH"] == "7"
    # Restored (no leak) after the segment.
    assert "HERMES_JOB_ID" not in os.environ


def test_run_conversation_error_becomes_segment_error():
    class _Boom:
        session_id = "s"

        def run_conversation(self, *a, **k):
            raise RuntimeError("kaboom")

    rs, _ = _make(_Boom(), history=[])
    out = rs(_job(current_tip_session_id="root"), 1)
    assert out.error is not None and "kaboom" in out.error
    assert out.completed is False


# -- worker-session taint (B0, ADR 0051 Decision 7a) ---------------------------
def test_worker_session_is_tainted_unknown_external():
    """The worker's session is taint-marked unknown_external before the agent
    runs, so the trust gate withholds autonomous sensitive actions for the whole
    job (a background job is untrusted-by-default, fail-closed). Nothing else
    marks it — the inbound chokepoints only fire on inbound turns."""
    from shared import inbound

    agent = _FakeAgent({"completed": True, "final_response": "x"}, new_session_id="rotated")
    rs, _ = _make(agent, history=[])
    tip = "job-tip-b0"
    # Clean before: the session is not yet tainted (reads as internal).
    assert inbound.SESSION_TAINT.trust_class(tip) == inbound.TRUST_CLASS_INTERNAL
    rs(_job(current_tip_session_id=tip), 1)
    # After the segment, the worker's session is tainted at the untrusted class.
    assert inbound.SESSION_TAINT.trust_class(tip) == inbound.TRUST_CLASS_UNKNOWN_EXTERNAL
    assert inbound.SESSION_TAINT.is_tainted(tip) is True


def test_worker_session_taint_uses_derived_tip_when_no_recorded_tip():
    """When no tip/root is recorded, run_segment derives ``job_<id>`` and that
    derived session is the one taint-marked — so the fail-closed default holds on
    a brand-new job's first segment too."""
    from shared import inbound

    agent = _FakeAgent({"completed": True, "final_response": "x"})
    rs, _ = _make(agent, history=[])
    rs(_job(id="J-b0-derived", current_tip_session_id="", root_session_id=""), 1)
    assert inbound.SESSION_TAINT.trust_class("job_J-b0-derived") == inbound.TRUST_CLASS_UNKNOWN_EXTERNAL
