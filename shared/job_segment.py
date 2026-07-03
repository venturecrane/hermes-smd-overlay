"""The B1 segment adapter — binds one bounded agent run to the worker (ADR 0051).

``make_run_segment`` builds the ``run_segment`` callable :class:`JobWorker`
injects. Like the orchestrator, the binding takes its Hermes dependencies as
parameters (``session_db``, ``build_agent``, the cost functions), so the
segment logic — first-run vs resume, the rotated-tip read, the usage delta, the
pre-spend refusal, the completion mapping, and the trailing-``tool_call`` repair
— is unit-testable with a fake agent. The only untested piece is the real
``build_hermes_agent`` (constructing ``AIAgent`` the way ``run_job`` does),
which is exercised on staging.

Resume correctness (the load-bearing fact): Hermes ``state.db`` is an
append-only, lineage-linked log; on compaction the ``session_id`` rotates
(``run_agent.py:10732``). So the worker resumes from the RECORDED tip
(``current_tip_session_id``) and reloads with ``include_ancestors=True`` to walk
the full lineage — the recorded tip disambiguates a lineage that has branched
into a tree. ``run_conversation`` returns ``{"completed": bool,
"final_response": str, ...}`` so completion is read, not guessed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from shared.job_worker import SegmentOutcome
from shared.sticky_stop import StickyStopError

logger = logging.getLogger(__name__)

_INTERRUPT_MARKER = "[interrupted — not executed; re-plan from here]"


def repair_trailing_tool_call(history: list[dict]) -> list[dict]:
    """If the reloaded history ends on an assistant turn with tool_calls and no
    matching tool results (the crash-mid-tool-call window), append a synthetic
    'interrupted' tool result for each unmatched call. Without this, on resume
    the runtime either errors on an unmatched tool_call or the model re-issues
    the call (a double-fire). The synthetic result tells the model the step did
    NOT run, so it re-plans. Idempotent and a no-op for well-formed history.
    """
    if not history:
        return history
    last = history[-1]
    if last.get("role") != "assistant":
        return history
    tool_calls = last.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return history
    repaired = list(history)
    for tc in tool_calls:
        tc_id = tc.get("id") if isinstance(tc, dict) else None
        if not tc_id:
            continue
        repaired.append({"role": "tool", "tool_call_id": tc_id, "content": _INTERRUPT_MARKER})
    return repaired


# build_agent(model, session_id, max_iterations, session_db) -> agent
BuildAgent = Callable[..., Any]
# preflight_cost(model, history) -> cents   (estimate the next request's input)
PreflightCost = Callable[[str, list[dict]], int]
# segment_cost(agent) -> cents              (real provider-reported usage, V2)
SegmentCost = Callable[[Any], int]


def make_run_segment(
    *,
    session_db: Any,
    build_agent: BuildAgent,
    preflight_cost: PreflightCost,
    segment_cost: SegmentCost,
    segment_max_iterations: int = 8,
    breaker: Any = None,
):
    """Return a ``run_segment(job, lease_epoch) -> SegmentOutcome`` closure.

    ``breaker`` is an optional ``shared.cost_breaker.CostBreaker`` (ADR 0062,
    ss-console #1661). When present the segment loop asserts the Machine-wide
    daily cost ladder before firing a segment (HARD_STOP →
    ``SegmentOutcome(cost_capped=True)``, dead-lettered by the worker) and
    records the segment's real cents after it runs. None (tests, breaker
    construction failure) preserves today's behavior.
    """

    def run_segment(job: dict, lease_epoch: int) -> SegmentOutcome:
        model = job["model"]
        tip = (
            job.get("current_tip_session_id") or job.get("root_session_id") or ("job_" + job["id"])
        )

        # Taint the worker session BEFORE the agent runs (ADR 0051 Decision 7a).
        # A background job is untrusted-by-default: nothing else taint-marks the
        # worker's session (the inbound chokepoints only mark inbound turns), so
        # without this a job that reads untrusted content would run as INTERNAL
        # and the trust gate would permit autonomous EXTERNAL_SEND / DESTRUCTIVE /
        # CODE_EXECUTION. Marking unknown_external here makes the gate withhold
        # those sensitive actions for the whole job (fail-closed); the gate still
        # allows READ and INTERNAL_WRITE (drafts). SESSION_TAINT is a pure
        # in-process register, so this runs in unit tests too. Guarded so a
        # missing shared.inbound never crashes a segment (the gate is the wall).
        try:
            from shared import inbound

            inbound.SESSION_TAINT.mark(tip, inbound.TRUST_CLASS_UNKNOWN_EXTERNAL)
        except Exception as exc:  # noqa: BLE001 — never fail a job on the taint mark
            logger.warning("job %s: worker-session taint-mark failed: %s", job["id"], exc)

        # Reload the lineage from the recorded tip; first run has no messages yet.
        history = session_db.get_messages_as_conversation(tip, include_ancestors=True) or []
        if history:
            history = repair_trailing_tool_call(history)
            user_message = "Continue the task. Pick up exactly where you left off; do not repeat completed steps."
            convo = history
        else:
            user_message = job["brief"]
            convo = None

        # Machine-wide cost breaker (ADR 0062): refuse the segment while the
        # sticky_stop ladder is at HARD_STOP. Checked before the per-job
        # budget guard because it is the wider scope (the Machine's whole
        # daily spend, not this job's allowance).
        if breaker is not None:
            try:
                breaker.assert_allowed()
            except StickyStopError:
                return SegmentOutcome(cost_capped=True)
            except Exception as exc:  # noqa: BLE001 — breaker fault ≠ job fault
                logger.warning("job %s: cost-breaker assert failed open: %s", job["id"], exc)

        # Pre-spend guard: refuse a segment whose estimated input cost alone
        # would exceed the remaining budget (don't fire the request).
        remaining = job["budget_cents"] - job["spent_cents"]
        if preflight_cost(model, history) > remaining:
            return SegmentOutcome(refused_budget=True)

        agent = build_agent(
            model=model,
            session_id=tip,
            max_iterations=segment_max_iterations,
            session_db=session_db,
        )
        # The plugin's job_record_sideeffect journals against these.
        prev_env = {k: os.environ.get(k) for k in ("HERMES_JOB_ID", "HERMES_JOB_LEASE_EPOCH")}
        os.environ["HERMES_JOB_ID"] = job["id"]
        os.environ["HERMES_JOB_LEASE_EPOCH"] = str(lease_epoch)
        try:
            result = agent.run_conversation(user_message, conversation_history=convo)
        except Exception as exc:  # non-fatal: lease expires, job is re-claimed
            logger.exception("job %s: segment run_conversation raised: %s", job["id"], exc)
            return SegmentOutcome(error=f"segment error: {exc}")
        finally:
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        spent = max(0, int(segment_cost(agent)))
        # Feed the Machine-wide daily ladder with the segment's real cents.
        # A transition to HARD_STOP here emits AGENT_STOPPED via the breaker's
        # audit sink; the NEXT segment's assert refuses. Recording failure is
        # logged loudly but never voids a segment that already ran — the
        # per-job budget guard still bounds this job regardless.
        if breaker is not None and spent > 0:
            try:
                breaker.record_cost_cents(spent)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "job %s: cost-breaker record failed (spend uncounted): %s", job["id"], exc
                )
        new_tip = getattr(agent, "session_id", tip) or tip  # rotates on compaction
        completed = bool(result.get("completed"))
        final = result.get("final_response") if completed else None
        return SegmentOutcome(
            completed=completed,
            spent_cents_delta=spent,
            tip_session_id=new_tip,
            result_text=final,
        )

    return run_segment


__all__ = ["repair_trailing_tool_call", "make_run_segment"]
