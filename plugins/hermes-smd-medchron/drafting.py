"""Agent-facing tools for the drafting job (a litigation document a firm
administrator asks the Operator for, drafted in the firm's authored house
style; ss-console drafting_verbs.py).

The demand job's shape exactly (demand.py), for five document classes.

WHO ASKED IS NEVER THE MODEL'S TO SAY. ``requested_by``, ``request_ref`` and
``request_text`` are filled HERE from the turn's verified inbound origin, the
same way demand.py fills them; the tool schema has no field for any of them.
A turn no email opened submits nothing: a draft is always person-initiated.

The broker then checks the rest (the lane enabled on the seat, the class
switched on for the firm, the allowance, the requester on scope.admins, the
filing target, no unfinished draft of that class on the matter) and answers
with a job id or a reason to relay as it comes back.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.medchron_client import MedchronBrokerClient, MedchronBrokerError

from .demand import STRING, _matter, _origin, _refuse, _schema

logger = logging.getLogger(__name__)

#: ss-console drafting_ledger.DOCUMENT_CLASSES, in its order.
DOCUMENT_CLASSES = (
    "mediation_brief",
    "discovery_set",
    "discovery_response",
    "memo",
    "depo_outline",
)


def drafting_job_submit(args: dict[str, Any], **_: Any) -> str:
    origin = _origin()
    if origin is None or not origin.sender_address:
        return _refuse(
            "a draft is submitted only from the turn of the email that asked for it; "
            "this turn was not opened by one, so nothing was queued"
        )
    if not origin.internet_message_id:
        return _refuse(
            "the request email's message id is not available on this turn, so the completion "
            "reply could not find its thread; nothing was queued"
        )
    if not (origin.reply_text or "").strip():
        return _refuse(
            "the request email's own words could not be read on this turn, and they are the "
            "drafting instruction; nothing was queued"
        )
    klass = str(args.get("document_class") or "").strip()
    if klass not in DOCUMENT_CLASSES:
        return _refuse(
            f"document_class must be one of {list(DOCUMENT_CLASSES)}; nothing was queued"
        )
    envelope = {
        "matter": _matter(args, "matter_id", "matter_number"),
        "file_to": _matter(args, "file_to_matter_id", "file_to_matter_number"),
        "requested_by": origin.sender_address,
        "request_ref": origin.internet_message_id,
        "request_text": origin.reply_text,
        "document_class": klass,
    }
    try:
        resp = MedchronBrokerClient().drafting_submit(envelope)
    except MedchronBrokerError as exc:
        return _refuse(f"the request could not be queued: {exc}")
    if not resp.get("accepted"):
        out = {"accepted": False, "reason": resp.get("reason")}
        if resp.get("job_id"):
            out["job_id"] = resp["job_id"]
        return json.dumps(out, ensure_ascii=False)
    logger.info("drafting_job_submit: queued %s", resp.get("job_id"))
    return json.dumps(
        {
            "accepted": True,
            "job_id": resp.get("job_id"),
            "state": resp.get("state"),
            "document_class": resp.get("document_class") or klass,
            # A factual ticket, no timing (the no-fabricated-content rule).
            "message": (
                f"Drafting job {resp.get('job_id')} is queued on this Machine. The draft is filed "
                "in the matter when it finishes, and the reply goes to this thread."
            ),
        },
        ensure_ascii=False,
    )


def drafting_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip() or None
    resp = MedchronBrokerClient().drafting_status(job_id)
    if not job_id:
        return json.dumps({"jobs": resp.get("jobs") or []}, ensure_ascii=False)
    job = resp.get("job")
    out: dict[str, Any] = {"job": job}
    if isinstance(job, dict) and job.get("state") == "failed":
        # A failed drafting job is SMD's to resolve, never the client's. This
        # shape is what the audit plugin records as a shortfall (code
        # drafting_job_failed), which raises SMD's shortfall alert; the client
        # is told nothing (the broker refuses the reply for a failed job).
        out.update(
            {
                "status": "refused",
                "reason": "drafting_job_failed",
                "message": (
                    "This drafting job failed on SMD's side and SMD has been alerted. Send the "
                    "client nothing and end the turn."
                ),
            }
        )
    return json.dumps(out, ensure_ascii=False)


def drafting_allowance(args: dict[str, Any], **_: Any) -> str:
    client = MedchronBrokerClient()
    resp = client.drafting_allowance()
    keys = ("unit", "cycle", "allowance", "used", "remaining", "authored", "enabled_classes")
    out = {k: resp.get(k) for k in keys}
    if args.get("include_recent_jobs"):
        out["recent_jobs"] = client.drafting_status().get("jobs") or []
    return json.dumps(out, ensure_ascii=False)


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "drafting_job_submit": (
        "Queue a litigation draft on a matter (a mediation brief, a discovery set, discovery "
        "responses, a memo, or a deposition outline) for the Named Administrator whose email opened "
        "this turn. Never draft in the turn: this queues a job that drafts on the Machine's runner "
        "and files the document in the matter. Resolve the matter first. Who asked, the email's id "
        "and the request's words are taken from the email itself, never from you. It answers with "
        "a job id, or a reason to relay as it comes back.",
        _schema(
            {
                "matter_id": {
                    "type": "string",
                    "description": "The practice-management matter id, resolved this turn.",
                },
                "matter_number": {"type": "string", "description": "The firm's matter number."},
                "document_class": {
                    "type": "string",
                    "enum": list(DOCUMENT_CLASSES),
                    "description": "Which document was asked for. One job per document.",
                },
                "file_to_matter_id": {
                    "type": "string",
                    "description": "Only for a rehearsal: the matter the document is filed on instead.",
                },
                "file_to_matter_number": STRING,
            },
            ["matter_id", "matter_number", "document_class"],
        ),
        drafting_job_submit,
    ),
    "drafting_job_status": (
        "A drafting job by id (state, document class, delivery folder, filed file roles), or the last twenty.",
        _schema({"job_id": STRING}),
        drafting_job_status,
    ),
    "drafting_allowance": (
        "The firm's drafting allowance for the current billing cycle: authored, used, remaining "
        "(a count of drafts), and which document classes are switched on.",
        _schema(
            {
                "include_recent_jobs": {
                    "type": "boolean",
                    "description": "Also list the last twenty drafting jobs with their states.",
                }
            }
        ),
        drafting_allowance,
    ),
}

__all__ = [
    "DOCUMENT_CLASSES",
    "TOOLS",
    "drafting_allowance",
    "drafting_job_status",
    "drafting_job_submit",
]
