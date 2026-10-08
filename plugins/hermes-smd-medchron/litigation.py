"""Agent-facing tools for the litigation status job (the firm's litigation
status list, re-read from the matters and filed as a workbook; ss-console
litigation_verbs.py).

The drafting job's shape (drafting.py). WHO ASKED IS NEVER THE MODEL'S TO SAY:
``requester``, ``message_ref`` and ``request_text`` are filled HERE from the
turn's verified inbound origin; the tool schema has no field for any of them.
A turn no email opened submits nothing. The scheduled run is not submitted
here at all: the seat's pre_run peer submits it to the broker directly, with
the requester the firm authored.

The model chooses only the scope: the attorneys whose matters the list covers,
by name (the broker resolves each against the firm's staff and refuses a name
it cannot), or the whole firm. The broker checks the rest (the skill enabled,
the requester on scope.admins, the filing target, no unfinished litigation job
for that scope, the monthly cap) and answers with a job id or a reason to relay
as it comes back.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.medchron_client import MedchronBrokerClient, MedchronBrokerError

from .demand import STRING, _origin, _refuse, _schema

logger = logging.getLogger(__name__)

MAX_ATTORNEYS = 20
MAX_NAME_CHARS = 100
#: What the status read projects: states and counts. Never spend, never a
#: matter fact: the completion reply carries counts only.
_STATUS_KEYS = ("job_id", "state", "stage", "matters_total", "matters_reread", "flags_new")


def _scope(args: dict[str, Any]) -> dict[str, Any] | str:
    """The broker scope for the tool's arguments, or a refusal sentence."""
    names = args.get("attorneys")
    whole = args.get("all")
    if names in (None, []):
        return {"all": True}
    if whole:
        return "name attorneys or ask for all of them, not both; nothing was queued"
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return "attorneys must be a list of names; nothing was queued"
    cleaned = [" ".join(n.split()) for n in names]
    if any(not n or len(n) > MAX_NAME_CHARS for n in cleaned) or len(cleaned) > MAX_ATTORNEYS:
        return (
            f"attorneys must be 1 to {MAX_ATTORNEYS} non-empty names of at most "
            f"{MAX_NAME_CHARS} characters; nothing was queued"
        )
    return {"attorneys": list(dict.fromkeys(cleaned))}


def litigation_job_submit(args: dict[str, Any], **_: Any) -> str:
    origin = _origin()
    if origin is None or not origin.sender_address:
        return _refuse(
            "a litigation status list is submitted only from the turn of the email that asked "
            "for it; this turn was not opened by one, so nothing was queued"
        )
    if not origin.internet_message_id:
        return _refuse(
            "the request email's message id is not available on this turn, so the completion "
            "reply could not find its thread; nothing was queued"
        )
    if not (origin.reply_text or "").strip():
        return _refuse(
            "the request email's own words could not be read on this turn; nothing was queued"
        )
    scope = _scope(args if isinstance(args, dict) else {})
    if isinstance(scope, str):
        return _refuse(scope)
    envelope = {
        "trigger": "request",
        "requester": origin.sender_address,
        "message_ref": origin.internet_message_id,
        "request_text": origin.reply_text,
        "scope": scope,
    }
    try:
        resp = MedchronBrokerClient().litigation_submit(envelope)
    except MedchronBrokerError as exc:
        return _refuse(f"the request could not be queued: {exc}")
    accepted = resp.get("accepted") if "accepted" in resp else bool(resp.get("job_id"))
    if not accepted:
        out = {"accepted": False, "reason": resp.get("reason")}
        if resp.get("job_id"):
            out["job_id"] = resp["job_id"]
        return json.dumps(out, ensure_ascii=False)
    logger.info("litigation_job_submit: queued %s", resp.get("job_id"))
    return json.dumps(
        {
            "accepted": True,
            "job_id": resp.get("job_id"),
            "state": resp.get("state"),
            # A factual ticket, no timing (the no-fabricated-content rule).
            "message": (
                f"Litigation status job {resp.get('job_id')} is queued on this Machine. The "
                "workbook is filed when it finishes, and the reply goes to this thread."
            ),
        },
        ensure_ascii=False,
    )


def _project(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    out = {k: job.get(k) for k in _STATUS_KEYS if k in job}
    f = job.get("file")
    out["file"] = {"name": f.get("name"), "size": f.get("size")} if isinstance(f, dict) else None
    return out


def litigation_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return json.dumps({"job": None, "reason": "job_id is required"}, ensure_ascii=False)
    resp = MedchronBrokerClient().litigation_status(job_id)
    job = _project(resp.get("job") if "job" in resp else resp)
    if job is not None:
        job.setdefault("job_id", job_id)
    out: dict[str, Any] = {"job": job}
    if job is not None and job.get("state") == "failed":
        # A failed litigation job is SMD's to resolve, never the client's. This
        # shape is what the audit plugin records as a shortfall (code
        # litigation_job_failed), which raises SMD's shortfall alert; the
        # client is told nothing.
        out.update(
            {
                "status": "refused",
                "reason": "litigation_job_failed",
                "message": (
                    "This litigation status job failed on SMD's side and SMD has been alerted. "
                    "Send the client nothing and end the turn."
                ),
            }
        )
    return json.dumps(out, ensure_ascii=False)


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "litigation_job_submit": (
        "Queue the firm's litigation status list for the Named Administrator whose email opened "
        "this turn. Never compile the list in the turn: this queues a job that re-reads the "
        "litigated matters on the Machine's runner and files the workbook. Name the attorneys "
        "whose matters it covers, or leave them out (or pass all) for the whole firm. Who asked, "
        "the email's id and the request's words are taken from the email itself, never from you. "
        "It answers with a job id, or a reason to relay as it comes back.",
        _schema(
            {
                "attorneys": {
                    "type": "array",
                    "items": STRING,
                    "maxItems": MAX_ATTORNEYS,
                    "description": "Attorneys' names as the request gives them; the firm's staff list resolves them.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Every litigated matter in the firm (the default when no attorney is named).",
                },
            }
        ),
        litigation_job_submit,
    ),
    "litigation_job_status": (
        "A litigation status job by id: its state and stage, and counts (matters, matters re-read "
        "this run, new flags) and the filed workbook's name and size. Never matter facts.",
        _schema({"job_id": STRING}, ["job_id"]),
        litigation_job_status,
    ),
}

__all__ = ["TOOLS", "litigation_job_status", "litigation_job_submit"]
