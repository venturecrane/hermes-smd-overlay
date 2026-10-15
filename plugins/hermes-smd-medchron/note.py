"""Agent-facing tool for an SMD note (SMD has the Operator tell a firm colleague
something, as a reply in a thread they started; ss-console note_verbs.py).

There is NO submit tool: a note is recorded only by root on the seat, on SMD's
command (ss-console ``note_by_smd.py`` -> broker ``note_record``), after the
broker corroborated every identifier in its text against the colleague's own
email. The one tool here is the status read the completion wake uses: the
job's state and the recorded text, which the wake's turn sends word for word
to the person the reply binding names. It never words it.

A note with no text to send (an unknown id, a broker refusal) is a shortfall
(code ``note_job_unavailable``) so SMD is alerted; the firm is told nothing.
"""

from __future__ import annotations

import json
from typing import Any

from shared.medchron_client import MedchronBrokerClient

from .demand import STRING, _schema

#: What the status read projects: the text is the email; nothing else is.
_KEYS = ("job_id", "state", "text")


def _project(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    return {k: job.get(k) for k in _KEYS if k in job}


def note_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return json.dumps({"job": None, "reason": "job_id is required"}, ensure_ascii=False)
    resp = MedchronBrokerClient().note_status(job_id)
    job = _project(resp.get("job") if "job" in resp else None)
    if job is not None:
        job.setdefault("job_id", job_id)
    out: dict[str, Any] = {"job": job}
    if job is None or not str(job.get("text") or "").strip():
        # Nothing recorded to send is SMD's to resolve, never the firm's. This
        # shape is what the audit plugin records as a shortfall (code
        # note_job_unavailable), which raises SMD's shortfall alert.
        out.update(
            {
                "status": "refused",
                "reason": "note_job_unavailable",
                "message": (
                    "This SMD note has no recorded text to send and SMD has been alerted. "
                    "Send the firm nothing and end the turn."
                ),
            }
        )
    return json.dumps(out, ensure_ascii=False)


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "note_job_status": (
        "An SMD note by job id: its state and the text SMD recorded for the colleague whose "
        "email the reply binding names. Send the text word for word; never reword it.",
        _schema({"job_id": STRING}, ["job_id"]),
        note_job_status,
    ),
}

__all__ = ["TOOLS", "note_job_status"]
