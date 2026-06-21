"""Agent-facing tools for the B1 durable task-execution substrate (ADR 0051).

These let the Operator hand a too-big task to a background job and observe it.
Intake is agent-decided (the channel stays a dumb pipe): when the agent judges
a task too large for the synchronous reply budget, it calls
``start_background_job`` and relays the returned ticket. The job is then driven
to completion by the in-gateway worker thread (see the worker module); these
tools only touch the broker-owned job ledger.

All four tools go through :class:`shared.job_ledger_client.BrokerJobClient`,
which reaches the capability broker over its Unix socket. The broker gates the
``job_*`` verbs on ``peer_pid == gateway_pid``, so these tool calls (which run
in the gateway process) are accepted while an ``execute_code`` child's direct
socket attempt would be refused.

Identity capture: ``customer_slug`` and ``model`` come from the env the gateway
already sets (``CUSTOMER_SLUG``, ``HERMES_MODEL`` — the same vars cron's
``run_job`` reads). ``persona_id`` is left for the worker to resolve and
validate authoritatively from the seat config at claim time (ADR 0051
Decision 9: worker identity is loaded from the row with a boot assertion), so we
do not invent a persona env var here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from shared.job_ledger_client import BrokerJobClient
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

# Authored default per-job budget. Materialized from customer.yaml into the env
# by translate.py; a conservative fallback applies when unset. The engagement
# raises it per seat — a long Class-D job may warrant more.
_DEFAULT_BUDGET_CENTS = 500

STRING = {"type": "string"}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _default_budget_cents() -> int:
    raw = os.environ.get("SMD_JOB_DEFAULT_BUDGET_CENTS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("SMD_JOB_DEFAULT_BUDGET_CENTS=%r is not an int; using default", raw)
    return _DEFAULT_BUDGET_CENTS


def _start_background_job(args: dict[str, Any], **_: Any) -> str:
    brief = str(args.get("brief") or "").strip()
    if not brief:
        raise ValueError("start_background_job requires a non-empty 'brief'")
    deliver_to = str(args.get("deliver_to") or "").strip()
    row = {
        "customer_slug": os.environ["CUSTOMER_SLUG"],
        # Worker resolves/validates the authoritative persona at claim time.
        "persona_id": os.environ.get("HERMES_PERSONA_SLUG", "") or "default",
        "model": os.environ.get("HERMES_MODEL", ""),
        "brief": brief,
        "brief_digest": "sha256:" + hashlib.sha256(brief.encode()).hexdigest(),
        "deliver_to": deliver_to,
        "budget_cents": _default_budget_cents(),
    }
    job_id = BrokerJobClient().create(row)
    logger.info("start_background_job: queued %s", job_id)
    # Factual ticket — no timeline promise (the no-fabricated-content rule).
    where = f" The result will be delivered to {deliver_to}." if deliver_to else ""
    return json.dumps(
        {
            "job_id": job_id,
            "status": "queued",
            "message": (
                f"Started background job {job_id}. I'll work it and come back with "
                f"the result.{where} Ask me for its status with this ticket."
            ),
        },
        ensure_ascii=False,
    )


def _job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_status requires 'job_id'")
    job = BrokerJobClient().read(job_id)
    if not job:
        return json.dumps({"job_id": job_id, "error": "no such job"})
    fields = ("id", "status", "spent_cents", "budget_cents", "result_ref", "error", "attempts")
    return json.dumps({k: job.get(k) for k in fields}, ensure_ascii=False)


def _job_cancel(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_cancel requires 'job_id'")
    requested = BrokerJobClient().cancel(job_id)
    return json.dumps({"job_id": job_id, "cancel_requested": requested})


def _job_record_sideeffect(args: dict[str, Any], **_: Any) -> str:
    """Idempotency guard a background skill calls BEFORE a side-effecting step.

    Returns a decision: 'proceed' (do the effect), 'skip' (already done), or
    'review' (a prior attempt was interrupted mid-effect — do NOT re-fire). The
    worker injects ``HERMES_JOB_ID`` / ``HERMES_JOB_LEASE_EPOCH`` into the job's
    run; outside a background job these are unset and the call is a no-op
    'proceed' (the synchronous in-turn path is not journaled).
    """
    step_key = str(args.get("step_key") or "").strip()
    if not step_key:
        raise ValueError("job_record_sideeffect requires 'step_key'")
    job_id = os.environ.get("HERMES_JOB_ID")
    epoch_raw = os.environ.get("HERMES_JOB_LEASE_EPOCH")
    if not job_id or epoch_raw is None:
        return json.dumps({"decision": "proceed", "journaled": False})
    decision = BrokerJobClient().idem_begin(job_id, step_key, int(epoch_raw))
    return json.dumps({"decision": decision, "journaled": True})


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "start_background_job": (
        "Hand a task too large for one reply to a durable background job that "
        "runs to completion and delivers the result. Returns a tracking ticket.",
        _schema(
            {
                "brief": {
                    "type": "string",
                    "description": "The full task to perform in the background.",
                },
                "deliver_to": {
                    "type": "string",
                    "description": "Authored channel to deliver the result to (e.g. an email or chat target). Validated against the engagement's allowlist at delivery.",
                },
            },
            ["brief"],
        ),
        _start_background_job,
    ),
    "job_status": (
        "Check a background job's status, spend, and result by its ticket id.",
        _schema({"job_id": STRING}, ["job_id"]),
        _job_status,
    ),
    "job_cancel": (
        "Request cancellation of a running background job by its ticket id.",
        _schema({"job_id": STRING}, ["job_id"]),
        _job_cancel,
    ),
    "job_record_sideeffect": (
        "Idempotency guard: call BEFORE a side-effecting step inside a "
        "background job. Returns proceed | skip | review.",
        _schema(
            {
                "step_key": {
                    "type": "string",
                    "description": "Stable logical-effect key (action+target+content id), NOT a regenerated payload.",
                }
            },
            ["step_key"],
        ),
        _job_record_sideeffect,
    ),
}


def register(ctx: Any) -> None:
    """Register the durable-job tools. All require the broker socket."""
    for name, (description, schema, handler) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="jobs",
            schema=schema,
            handler=handler,
            requires_env=["SMD_WORKSPACE_BROKER_SOCKET"],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-jobs registered %d durable-job tools", len(TOOLS))
    # Launch the in-gateway durable-job worker as a daemon THREAD (the cron
    # model, off the asyncio loop — V4). Idempotent; a no-op without the broker
    # socket (e.g. in unit tests). Hermes imports inside it are lazy.
    try:
        from shared.job_worker_runtime import start_worker_thread

        start_worker_thread()
    except Exception as exc:  # never let worker launch break plugin registration
        logger.warning("hermes-smd-jobs: worker thread launch failed: %s", exc)
