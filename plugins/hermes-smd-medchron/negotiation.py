"""Agent-facing tool for the negotiation watch (new offers entered on the firm's
Negotiation Details tabs, one email per new offer; ss-console
negotiation_verbs.py).

Scheduled only, so there is NO submit tool: the seat's cron pre_run submits
each run to the broker directly, with the recipient the firm authored. The one
tool here is the status read the completion wake uses:

* a NOTICE (the wake's id, one per new offer): the message the runner composed
  in code from the offer as read and the entry as read back, and the broker's
  subject. The wake's turn sends that message word for word to the person the
  reply binding names; it never words it.
* a JOB that failed: the shortfall shape, which the audit plugin records as a
  shortfall (code ``negotiation_job_failed``) so SMD is alerted; the firm is
  told nothing.
"""

from __future__ import annotations

import json
from typing import Any

from shared.medchron_client import MedchronBrokerClient

from .demand import STRING, _schema

#: What the status read projects. A notice's message is the email; a job's
#: counts are never matter facts.
_NOTICE_KEYS = ("job_id", "kind", "state", "matter_number", "status", "message", "subject")
_JOB_KEYS = (
    "job_id",
    "kind",
    "state",
    "stage",
    "matters_total",
    "matters_seeded",
    "docs_read",
    "docs_failed",
    "notices_count",
)


def _project(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    keys = _NOTICE_KEYS if job.get("kind") == "notice" else _JOB_KEYS
    return {k: job.get(k) for k in keys if k in job}


def negotiation_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return json.dumps({"job": None, "reason": "job_id is required"}, ensure_ascii=False)
    resp = MedchronBrokerClient().negotiation_status(job_id)
    job = _project(resp.get("job") if "job" in resp else resp)
    if job is not None:
        job.setdefault("job_id", job_id)
    out: dict[str, Any] = {"job": job}
    if job is not None and job.get("state") == "failed":
        # A failed negotiation job is SMD's to resolve, never the firm's. This
        # shape is what the audit plugin records as a shortfall (code
        # negotiation_job_failed), which raises SMD's shortfall alert.
        out.update(
            {
                "status": "refused",
                "reason": "negotiation_job_failed",
                "message": (
                    "This negotiation watch run failed on SMD's side and SMD has been alerted. "
                    "Send the firm nothing and end the turn."
                ),
            }
        )
    return json.dumps(out, ensure_ascii=False)


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "negotiation_job_status": (
        "A negotiation watch notice or job by id. A notice (one per new offer) carries the "
        "message to send, composed from the offer letter and the Negotiation Details entry, "
        "and its subject; send the message word for word. A job carries its state and counts.",
        _schema({"job_id": STRING}, ["job_id"]),
        negotiation_job_status,
    ),
}

__all__ = ["TOOLS", "negotiation_job_status"]
