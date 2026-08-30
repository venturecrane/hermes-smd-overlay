"""Agent-facing tools for the chronology-package runner (routine 11, ss#2614).

Three thin verbs over the capability broker's ``medchron_*`` surface
(ss-console ``operator/workspace_broker/medchron_verbs.py``):

* ``medchron_job_submit``: hand a resolved matter and its clients to the
  runner on this Machine. The broker validates the envelope, checks the
  firm's monthly document allowance, writes the ledger row and the queue file,
  and answers with a job id, or with a prose reason it was not accepted. The
  agent relays that sentence; it never sees a path or a file.
* ``medchron_job_status``: one job's state and counts, or the last twenty.
* ``medchron_allowance``: the month's allowance, what is used, what remains.

The runner itself is a root daemon the agent cannot reach; every transition it
reports is a broker-written audit row. Nothing here returns document content:
the projection is states, counts, cents, and the delivery folder id.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.medchron_client import MedchronBrokerClient
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

STRING = {"type": "string"}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_UNIT_SCHEMA = {
    "type": "object",
    "properties": {
        "client_name": {
            "type": "string",
            "description": "The client's full name as it appears on the matter.",
        },
        "surname": {
            "type": "string",
            "description": "The client's surname, for the cross-client check.",
        },
        "dob": {
            "type": "string",
            "description": "Date of birth, MM/DD/YYYY, read from the matter (never guessed).",
        },
        "folder_prefix": {
            "type": "string",
            "description": "On a joint matter only: the document folder that holds this client's records.",
        },
    },
    "required": ["client_name", "surname", "dob"],
    "additionalProperties": False,
}


def _medchron_job_submit(args: dict[str, Any], **_: Any) -> str:
    envelope = {
        "matter": {
            "id": str(args.get("matter_id") or "").strip(),
            "number": str(args.get("matter_number") or "").strip(),
            "title": str(args.get("matter_title") or ""),
        },
        "units": list(args.get("units") or []),
        "incident": {
            "date": str(args.get("incident_date") or "").strip(),
            "source": str(args.get("incident_source") or "").strip(),
        },
    }
    for key in ("injuries", "requested_by", "request_ref"):
        if args.get(key):
            envelope[key] = str(args[key])
    resp = MedchronBrokerClient().submit(envelope)
    if not resp.get("accepted"):
        return json.dumps({"accepted": False, "reason": resp.get("reason")}, ensure_ascii=False)
    logger.info("medchron_job_submit: queued %s", resp.get("job_id"))
    return json.dumps(
        {
            "accepted": True,
            "job_id": resp.get("job_id"),
            "state": resp.get("state"),
            "allowance_remaining_documents": resp.get("allowance_remaining_documents"),
            # A factual ticket, no timeline promise (the no-fabricated-content rule).
            "message": (
                f"Chronology package {resp.get('job_id')} is queued on this Machine. "
                "Ask for its status with this id; the delivery lands on the matter in its own dated folder."
            ),
        },
        ensure_ascii=False,
    )


def _medchron_job_status(args: dict[str, Any], **_: Any) -> str:
    job_id = str(args.get("job_id") or "").strip() or None
    resp = MedchronBrokerClient().status(job_id)
    if job_id:
        return json.dumps({"job": resp.get("job")}, ensure_ascii=False)
    return json.dumps({"jobs": resp.get("jobs") or []}, ensure_ascii=False)


def _medchron_allowance(args: dict[str, Any], **_: Any) -> str:
    client = MedchronBrokerClient()
    resp = client.allowance()
    out = {k: resp.get(k) for k in ("month", "allowance", "used", "remaining", "authored")}
    if args.get("include_recent_jobs"):
        out["recent_jobs"] = client.status().get("jobs") or []
    return json.dumps(out, ensure_ascii=False)


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "medchron_job_submit": (
        "Queue a medical chronology package for a matter on this Machine's runner. "
        "The matter and its clients must already be resolved from the practice-management system; "
        "the broker checks the firm's monthly allowance and answers with a job id or a reason.",
        _schema(
            {
                "matter_id": {
                    "type": "string",
                    "description": "The practice-management matter id.",
                },
                "matter_number": {"type": "string", "description": "The firm's matter number."},
                "matter_title": STRING,
                "units": {"type": "array", "items": _UNIT_SCHEMA, "minItems": 1},
                "incident_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD, from an authored matter field.",
                },
                "incident_source": {
                    "type": "string",
                    "enum": [
                        "matter_layout",
                        "intake_document",
                        "administrator_request",
                        "record_citation",
                    ],
                },
                "injuries": {
                    "type": "string",
                    "description": "The claimed injuries in plain words, if authored.",
                },
                "requested_by": {
                    "type": "string",
                    "description": "Who asked (an address or a name).",
                },
                "request_ref": {
                    "type": "string",
                    "description": "The thread or message this request came from.",
                },
            },
            ["matter_id", "matter_number", "units", "incident_date", "incident_source"],
        ),
        _medchron_job_submit,
    ),
    "medchron_job_status": (
        "Check a chronology package job by id (state, documents, pages, spend, delivery folder), "
        "or list the last twenty when no id is given.",
        _schema({"job_id": STRING}),
        _medchron_job_status,
    ),
    "medchron_allowance": (
        "The firm's monthly chronology document allowance: authored, used this month, remaining.",
        _schema(
            {
                "include_recent_jobs": {
                    "type": "boolean",
                    "description": "Also list the last twenty jobs with their states and counts.",
                }
            }
        ),
        _medchron_allowance,
    ),
}


def register(ctx: Any) -> None:
    """Register the chronology-package tools. All require the broker socket."""
    for name, (description, schema, handler) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="medchron",
            schema=schema,
            handler=handler,
            requires_env=["SMD_WORKSPACE_BROKER_SOCKET"],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-medchron registered %d chronology-package tools", len(TOOLS))
