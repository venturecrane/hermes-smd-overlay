"""In-gateway durable-job worker — the B1 engine (ADR 0051).

Runs as a background *thread* in the gateway process (the cron model, off the
asyncio loop — V4), driven by the broker-owned job ledger and resume-capable via
the Hermes ``state.db`` session lineage. The worker process is disposable;
durable state lives in the ledger + ``state.db``, and a boot-sweep re-claims
non-terminal jobs after a readiness barrier.

DESIGN: the safety-critical lifecycle (claim → lease → resume → per-segment
cost/cancel → checkpoint → deliver → dead-letter) lives in :class:`JobWorker`
with the *segment execution* injected as a callable. That separation is what
makes the durability invariants (no double-fire on resume, fencing, pre-spend
cost ceiling, cancel) deterministically unit-testable with a fake segment — no
live LLM. The real Hermes wiring (construct ``AIAgent`` on our session lineage,
run a bounded ``run_conversation``, read provider usage, derive the rotated tip)
is the thin ``make_hermes_run_segment`` seam, exercised on staging.

Cost note: ``run_conversation`` exposes no per-tool-iteration hook, so the
worker bounds each segment to a small ``max_iterations`` and checks cost/cancel
*between* segments. A small segment bounds the worst-case overshoot; the seam
also pre-flight-refuses a segment whose estimate alone would exceed the
remaining budget (``SegmentOutcome.refused_budget``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Terminal ledger statuses (kept in lockstep with the console JobLedgerWriter).
TERMINAL = frozenset({"delivered", "done", "needs_review", "cancelled"})
DEFAULT_MAX_ATTEMPTS = 5


@dataclass
class SegmentOutcome:
    """Result of running one bounded agent segment.

    ``completed`` — the agent produced a final answer; the job is done.
    ``spent_cents_delta`` — real provider-reported cost of this segment (V2).
    ``tip_session_id`` — the current session tip after this segment (rotates on
        compaction; the worker records it so resume reloads the right lineage).
    ``result_text`` — the final result, set when ``completed``.
    ``refused_budget`` — the seam pre-flight-refused: the next request's
        estimated input cost alone would exceed the remaining budget.
    ``error`` — a non-fatal segment error; the worker leaves the lease to expire
        and the job is retried (until ``max_attempts``).
    """

    completed: bool = False
    spent_cents_delta: int = 0
    tip_session_id: str = ""
    result_text: str | None = None
    refused_budget: bool = False
    error: str | None = None


class JobClient(Protocol):
    """The subset of BrokerJobClient the worker needs (so tests can fake it)."""

    def read(self, job_id: str) -> dict | None: ...
    def list_claimable(self) -> list[dict]: ...
    def claim(self, job_id: str, worker_id: str) -> int | None: ...
    def record(self, job_id: str, lease_epoch: int, fields: dict) -> bool: ...


# run_segment(job: dict, lease_epoch: int) -> SegmentOutcome
RunSegment = Callable[[dict, int], SegmentOutcome]
# deliver(job: dict, result_ref: str) -> bool   (broker / gateway adapter)
Deliver = Callable[[dict, str], bool]
# put_result(job: dict, result_text: str) -> str   (persist to R2, return ref)
PutResult = Callable[[dict, str], str]


class JobWorker:
    """Drives one job to a terminal state, and sweeps claimable jobs."""

    def __init__(
        self,
        client: JobClient,
        *,
        worker_id: str,
        run_segment: RunSegment,
        deliver: Deliver,
        put_result: PutResult,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.client = client
        self.worker_id = worker_id
        self.run_segment = run_segment
        self.deliver = deliver
        self.put_result = put_result
        self.max_attempts = max_attempts

    # -- boot-sweep --------------------------------------------------------
    def sweep(self) -> list[str]:
        """Claim and run every currently-claimable job. Returns the per-job
        terminal/intermediate outcome strings. Callers gate this behind a
        readiness barrier (broker + plugins + adapter ready)."""
        outcomes: list[str] = []
        for row in self.client.list_claimable():
            try:
                outcomes.append(self.run_one(row["id"]))
            except Exception as exc:  # never let one job kill the sweep
                logger.exception("job %s: sweep run_one crashed: %s", row.get("id"), exc)
                outcomes.append("crashed")
        return outcomes

    # -- one job -----------------------------------------------------------
    def run_one(self, job_id: str) -> str:
        epoch = self.client.claim(job_id, self.worker_id)
        if epoch is None:
            return "not_claimable"

        job = self.client.read(job_id)
        if job is None:
            return "vanished"

        # Identity assertion (ADR 0051 Decision 9): never run under a row whose
        # model/identity is unset — park for review rather than guess a default.
        if not job.get("model"):
            self._dead_letter(job_id, epoch, "needs_review", "job row has no resolved model")
            return "needs_review"
        if job.get("attempts", 0) > self.max_attempts:
            self._dead_letter(job_id, epoch, "needs_review", "max attempts exceeded")
            return "needs_review"

        # First claim: establish the durable root session the lineage hangs from.
        # The tip starts at the root; the adapter resumes from
        # current_tip_session_id thereafter (it rotates on compaction).
        if not job.get("root_session_id"):
            root = "job_" + job_id
            if not self.client.record(
                job_id, epoch, {"root_session_id": root, "current_tip_session_id": root}
            ):
                return "fenced"

        # Segment loop: cost/cancel checked between bounded segments.
        while True:
            job = self.client.read(job_id)  # refresh cancel flag + spend
            if job is None:
                return "vanished"
            if job.get("cancel_requested"):
                self._dead_letter(job_id, epoch, "cancelled", "cancel requested")
                return "cancelled"
            if job["spent_cents"] >= job["budget_cents"]:
                self._dead_letter(job_id, epoch, "needs_review", "budget exhausted")
                return "needs_review"

            seg = self.run_segment(job, epoch)

            if seg.refused_budget:
                # Pre-flight: the next request's input cost alone would exceed
                # the remaining budget — don't fire it.
                self._dead_letter(job_id, epoch, "needs_review", "segment would exceed budget (pre-spend)")
                return "needs_review"
            if seg.error:
                # Non-fatal: record and stop; the lease expires and the job is
                # re-claimed and resumed (attempts is the dead-letter backstop).
                self.client.record(job_id, epoch, {"error": seg.error})
                return "errored"

            # Record real usage + the rotated tip, transactionally (epoch-fenced).
            new_spent = job["spent_cents"] + max(0, seg.spent_cents_delta)
            fields: dict[str, Any] = {"spent_cents": new_spent}
            if seg.tip_session_id:
                fields["current_tip_session_id"] = seg.tip_session_id
            if not self.client.record(job_id, epoch, fields):
                # Fenced out — another worker re-claimed this job; stop silently.
                return "fenced"

            if new_spent > job["budget_cents"]:
                self._dead_letter(job_id, epoch, "needs_review", "budget exceeded mid-segment")
                return "needs_review"

            if seg.completed:
                return self._finish(job_id, epoch, job, seg.result_text or "")

    def _finish(self, job_id: str, epoch: int, job: dict, result_text: str) -> str:
        # Persist the result to durable storage (R2) BEFORE delivering, so a
        # host reschedule can't orphan it.
        result_ref = self.put_result(job, result_text)
        if not self.client.record(job_id, epoch, {"status": "complete", "result_ref": result_ref}):
            return "fenced"
        # Delivery is its own fenced, retried state. A job is not done until
        # delivered.
        if not self.client.record(job_id, epoch, {"status": "delivering"}):
            return "fenced"
        try:
            delivered = self.deliver(job, result_ref)
        except Exception as exc:
            logger.exception("job %s: delivery raised: %s", job_id, exc)
            delivered = False
        if not delivered:
            self._dead_letter(job_id, epoch, "needs_review", "delivery failed")
            return "needs_review"
        if not self.client.record(job_id, epoch, {"status": "delivered"}):
            return "fenced"
        self.client.record(job_id, epoch, {"status": "done"})
        return "done"

    def _dead_letter(self, job_id: str, epoch: int, status: str, error: str) -> None:
        logger.warning("job %s -> %s: %s", job_id, status, error)
        self.client.record(job_id, epoch, {"status": status, "error": error})


__all__ = ["SegmentOutcome", "JobWorker", "TERMINAL", "DEFAULT_MAX_ATTEMPTS"]
