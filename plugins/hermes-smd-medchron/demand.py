"""Agent-facing tools for the demand job (the gap audit and draft demand a firm
administrator asks the Operator for; ss-console demand_verbs.py).

WHO ASKED IS NEVER THE MODEL'S TO SAY. ``requested_by``, ``request_ref`` and
``request_text`` are filled HERE from the turn's verified inbound origin
(``SESSION_INBOUND_ORIGIN``: the sender the webhook router recorded after
signature verification, the email's ``internetMessageId``, and the sender's own
words with the quoted history removed). The tool schema has no field for any of
them, so a request read out of a forwarded email, a document, or a matter note
cannot be submitted in someone else's name, and the completion reply (the
verified reply binding) can only ever go back to the person who actually wrote.
A turn no email opened submits nothing: a demand is always person-initiated.

The broker then checks the rest (the skill enabled on the seat, the allowance,
the requester on scope.admins, no unfinished demand on the matter) and answers
with a job id or a reason to relay as it comes back.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared import inbound, provenance
from shared.cron_attribution import parse_cron_session
from shared.medchron_client import MedchronBrokerClient, MedchronBrokerError

logger = logging.getLogger(__name__)

STRING = {"type": "string"}
_TRUSTED_MODES = frozenset({provenance.MODE_KEYED, provenance.MODE_THREAD, provenance.MODE_PROCESS})


def _origin() -> inbound.InboundOrigin | None:
    """The verified email that opened THIS turn, or None (never a guess).

    ``bound_this_turn``: the origin the inbound plugin bound from this turn's
    own trusted prompt prefix, in a session that has been handed exactly one
    email. Never a claim-once origin a webhook wake picked up, never a sticky
    origin from an earlier email, never a scheduled turn.
    """
    session_id, mode = provenance.resolve_session_with_mode(None)
    if not session_id or mode not in _TRUSTED_MODES or parse_cron_session(session_id):
        return None
    return inbound.SESSION_INBOUND_ORIGIN.bound_this_turn(session_id)


def _refuse(reason: str) -> str:
    return json.dumps({"accepted": False, "reason": reason}, ensure_ascii=False)


def _matter(args: dict[str, Any], id_key: str, number_key: str) -> dict[str, str] | None:
    mid = str(args.get(id_key) or "").strip()
    number = str(args.get(number_key) or "").strip()
    return {"id": mid, "number": number} if mid or number else None


def demand_job_submit(args: dict[str, Any], **_: Any) -> str:
    origin = _origin()
    if origin is None or not origin.sender_address:
        return _refuse(
            "a demand is submitted only from the turn of the email that asked for it; "
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
    wanted = args.get("deliverables") or ["gap_audit", "demand"]
    envelope = {
        "matter": _matter(args, "matter_id", "matter_number"),
        "file_to": _matter(args, "file_to_matter_id", "file_to_matter_number"),
        "requested_by": origin.sender_address,
        "request_ref": origin.internet_message_id,
        "request_text": origin.reply_text,
        "deliverables": list(wanted) if isinstance(wanted, list) else wanted,
    }
    try:
        resp = MedchronBrokerClient().demand_submit(envelope)
    except MedchronBrokerError as exc:
        return _refuse(f"the request could not be queued: {exc}")
    if not resp.get("accepted"):
        out = {"accepted": False, "reason": resp.get("reason")}
        if resp.get("job_id"):
            out["job_id"] = resp["job_id"]
        return json.dumps(out, ensure_ascii=False)
    logger.info("demand_job_submit: queued %s", resp.get("job_id"))
    return json.dumps(
        {
            "accepted": True,
            "job_id": resp.get("job_id"),
            "state": resp.get("state"),
            # A factual ticket, no timing (the no-fabricated-content rule).
            "message": (
                f"Demand job {resp.get('job_id')} is queued on this Machine. The gap audit and draft "
                "demand are filed in the matter when it finishes, and the reply goes to this thread."
            ),
        },
        ensure_ascii=False,
    )


def demand_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip() or None
    resp = MedchronBrokerClient().demand_status(job_id)
    return json.dumps(
        {"job": resp.get("job")} if job_id else {"jobs": resp.get("jobs") or []}, ensure_ascii=False
    )


def demand_allowance(args: dict[str, Any], **_: Any) -> str:
    client = MedchronBrokerClient()
    resp = client.demand_allowance()
    keys = ("unit", "cycle", "allowance", "used", "remaining", "authored")
    out = {k: resp.get(k) for k in keys}
    if args.get("include_recent_jobs"):
        out["recent_jobs"] = client.demand_status().get("jobs") or []
    return json.dumps(out, ensure_ascii=False)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "demand_job_submit": (
        "Queue the gap audit and draft demand on a matter for the Named Administrator whose email "
        "opened this turn. Never draft a demand in the turn: this job does the work on the Machine's "
        "runner and files both documents in the matter. Resolve the matter first. Who asked, the "
        "email's id and the request's words are taken from the email itself, never from you. "
        "It answers with a job id, or a reason to relay as it comes back.",
        _schema(
            {
                "matter_id": {
                    "type": "string",
                    "description": "The practice-management matter id, resolved this turn.",
                },
                "matter_number": {"type": "string", "description": "The firm's matter number."},
                "file_to_matter_id": {
                    "type": "string",
                    "description": "Only for a rehearsal: the matter the documents are filed on instead.",
                },
                "file_to_matter_number": STRING,
                "deliverables": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["gap_audit", "demand"]},
                    "description": "What was asked for; both when the email asks for demand prep.",
                },
            },
            ["matter_id", "matter_number"],
        ),
        demand_job_submit,
    ),
    "demand_job_status": (
        "A demand job by id (state, spend, delivery folder, filed file names), or the last twenty.",
        _schema({"job_id": STRING}),
        demand_job_status,
    ),
    "demand_allowance": (
        "The firm's demand allowance for the current billing cycle: authored, used, remaining (a count of demands).",
        _schema(
            {
                "include_recent_jobs": {
                    "type": "boolean",
                    "description": "Also list the last twenty demand jobs with their states.",
                }
            }
        ),
        demand_allowance,
    ),
}

__all__ = ["TOOLS", "demand_allowance", "demand_job_status", "demand_job_submit"]
