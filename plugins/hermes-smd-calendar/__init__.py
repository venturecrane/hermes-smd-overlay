"""The seat's own calendar: invitations it sends, and ones it is sent.

WHY THIS PLUGIN EXISTS (ss-console#3264, ADR 0092 pending). A hosted agent
that serves people (the couples role first; any hosted agent after) keeps
their calendar by sending them invitations, an iMIP ``text/calendar`` part
their own calendar client files: ``METHOD:REQUEST`` to add or move an event,
``METHOD:CANCEL`` to remove it. Proven in Google and Apple calendars on
2026-10-16 (vfy_01M55T51PV1YB4D56PW7KK8WK0, vfy_01M55THDN2KSHQ8YKAN0QQKPTE,
vfy_01M55TK7D5M02RNYGZ85WZAQR5). No calendar API, no OAuth grant.

``calendar_invite_create``  a new event, invited to named people
``calendar_invite_update``  change an event this seat sent (same UID, SEQUENCE + 1)
``calendar_invite_cancel``  cancel an event this seat sent
``calendar_invite_list``    the events this turn may see

The names say ``invite`` on purpose: the generic connector surface already
uses ``calendar_list_events`` / ``calendar_create_event_draft`` for a calendar
API the seat reads, which this is not.

HOW AN INVITATION LEAVES (``tools.py``). Through the seat's ONE out-of-turn
sender (``shared.send_dispatch`` -> the trust plugin's gate and scans -> the
workspace broker), never a vendor call from here. The broker fences the
recipients against its own copy of customer.yaml, validates the invitation
(every ATTENDEE a recipient, the ORGANIZER this inbox, no email alarm), builds
the part, and writes the CONFIRM_SEND row with the invitation's sha256, so the
unaudited-send reconciler sees every one. The participant fence admits the
seat's people through the lane ``skill:calendar``, which this code names.

WHO MAY BE INVITED (``seat.py``). On a seat people share, person ids mapped to
``people[].email`` in code (never an address the model names); elsewhere the
exact roster. One name that does not resolve refuses the whole invitation. An
event belongs to a space; either partner may invite both of them from their own
conversation, and the event then belongs to ``shared``.

WHAT ARRIVES BY EMAIL (``ingest.py``, ``pre_gateway_dispatch``). A person's
invitation is kept in their space; a person's Accept/Decline (METHOD:REPLY) is
recorded on the event and never wakes a turn.

WHERE EVENTS LIVE (``shared.calendar_store``). ``/opt/data/smd/calendar.db``.
The file tools are refused on that path here, on every seat with the tools
(the record-store path fence covers seats that keep records, not all).
``terminal`` and ``execute_code`` cannot be fenced by path; the couples
persona does not carry them (workstream A, R2).

THE GATE IS THE AUTHORED SKILL. ``register`` offers the tools only on a seat
that authors the ``calendar`` skill enabled with ``settings.recipients:
roster`` and sends through AgentMail. Every other seat is unchanged: no tools,
no hooks.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared import calendar_store, provenance
from shared.customer_config import CustomerConfig, CustomerConfigError
from shared.inbound import SESSION_TAINT
from shared.tool_registration import register_wrapped_tool

from . import ingest, tools
from . import seat as seat_mod

logger = logging.getLogger(__name__)

TOOL_CREATE = "calendar_invite_create"
TOOL_UPDATE = "calendar_invite_update"
TOOL_CANCEL = "calendar_invite_cancel"
TOOL_LIST = "calendar_invite_list"
WRITE_TOOLS = frozenset({TOOL_CREATE, TOOL_UPDATE, TOOL_CANCEL})

STRING = {"type": "string"}
_TIME_ARGS: dict[str, Any] = {
    "start": {
        **STRING,
        "description": (
            "When it starts: a date (YYYY-MM-DD) for an all-day event, or a date and time "
            "(YYYY-MM-DDTHH:MM) for a timed one."
        ),
    },
    "end": {
        **STRING,
        "description": (
            "When it ends, in the same form as start. Optional: a timed event defaults to one "
            "hour, an all-day event to one day. An all-day end is the day AFTER the last day."
        ),
    },
    "timezone": {
        **STRING,
        "description": (
            "The time zone name the times are in, like America/Phoenix. Required for a timed "
            "event unless start carries a UTC offset, and always for a repeating timed event."
        ),
    },
}
_DETAIL_ARGS: dict[str, Any] = {
    "title": {**STRING, "description": "What the event is, as it should appear on the calendar."},
    "description": {**STRING, "description": "Optional notes shown with the event."},
    "location": {**STRING, "description": "Optional place."},
    "rrule": {
        **STRING,
        "description": (
            "Optional repeat rule, RFC 5545 RRULE form: FREQ=DAILY|WEEKLY|MONTHLY|YEARLY with "
            "optional INTERVAL, COUNT or UNTIL, BYDAY, BYMONTHDAY, BYMONTH. Example: "
            "FREQ=YEARLY for an anniversary."
        ),
    },
    "reminders_minutes": {
        "type": "array",
        "items": {"type": "integer"},
        "description": "Optional reminders, in minutes before the event (at most three).",
    },
}
_UID_ARG = {"uid": {**STRING, "description": "The event's uid, from calendar_invite_list."}}

#: Every tool this plugin registers. The classification and fence completeness
#: suites read this.
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    TOOL_CREATE: (
        "Put a new event on the calendar of people this seat serves, by sending them a "
        "calendar invitation from this seat's inbox; their calendar files it. Invite only "
        "people the request is for. On a seat people share, name each attendee by person "
        "id (p1, p2), never by address; on any other seat, by roster address. The reply "
        "says whether the invitation was sent; if it says not sent, nobody's calendar "
        "shows the event.",
        {
            "type": "object",
            "properties": {
                **_DETAIL_ARGS,
                **_TIME_ARGS,
                "attendees": {
                    "type": "array",
                    "items": STRING,
                    "description": (
                        "Who to invite: person ids (p1, p2) on a seat people share; "
                        "roster addresses on any other seat."
                    ),
                },
            },
            "required": ["title", "start", "attendees"],
            "additionalProperties": False,
        },
    ),
    TOOL_UPDATE: (
        "Change an event this seat sent: its title, time, place, notes, repeat rule or "
        "reminders. Sends the updated invitation to the same people; their calendars move "
        "the event in place. Who is invited cannot change (cancel and create instead).",
        {
            "type": "object",
            "properties": {**_UID_ARG, **_DETAIL_ARGS, **_TIME_ARGS},
            "required": ["uid"],
            "additionalProperties": False,
        },
    ),
    TOOL_CANCEL: (
        "Cancel an event this seat sent. Sends the cancellation to everyone invited; their "
        "calendars remove it.",
        {
            "type": "object",
            "properties": {**_UID_ARG},
            "required": ["uid"],
            "additionalProperties": False,
        },
    ),
    TOOL_LIST: (
        "The events on this seat's calendar that this conversation may see: the ones this "
        "seat sent, and invitations its people sent it. Each carries its uid, times, who is "
        "invited, its status, and whether this seat's invitation was delivered.",
        {
            "type": "object",
            "properties": {
                "include_cancelled": {
                    "type": "boolean",
                    "description": "True to include canceled events. Default false.",
                }
            },
            "additionalProperties": False,
        },
    ),
}


def _session(kwargs: dict[str, Any]) -> str:
    try:
        return provenance.resolve_session(str(kwargs.get("session_id") or ""))
    except Exception:  # noqa: BLE001 - an unresolvable session is no session
        return ""


def _handler(tool_name: str) -> Any:
    def handle(args: dict[str, Any], **kwargs: Any) -> str:
        return tools.run(tool_name, args, _session(kwargs))

    return handle


_TAINTED = (
    "{tool} refused: this turn read content from outside the seat's people, and a "
    "calendar invitation is sent to them. Ask the person to say what they want on the "
    "calendar again, on its own."
)


def _turn_is_tainted(session_id: str) -> bool:
    try:
        return SESSION_TAINT.is_tainted(session_id)
    except Exception:  # noqa: BLE001 - an unreadable taint state refuses
        logger.exception("hermes-smd-calendar: taint unresolved; refusing")
        return True


# -- the store's file fence ---------------------------------------------------

FILE_TOOLS = frozenset({"read_file", "search_files", "write_file", "patch"})
_PATH_KEYS = ("path", "file_path", "filename", "directory", "dir")
FILE_REFUSAL = (
    "could not open that path: this seat's calendar is read only with calendar_invite_list, "
    "which shows each conversation the events it may see. Say plainly it could not be read."
)


def _store_forms() -> list[str]:
    path = str(calendar_store.db_path())
    return sorted({path, os.path.realpath(path)})


def _touches_store(tool_name: str, args: Any) -> bool:
    """True when a file tool's path could reach the calendar file (or its sqlite
    sidecars), or a search could walk over it. Unresolvable reads as touching."""
    if not isinstance(args, dict):
        return True
    if tool_name == "patch" and isinstance(args.get("patch"), str) and args["patch"].strip():
        return any(form in args["patch"] for form in _store_forms())
    paths = [
        args[k].strip() for k in _PATH_KEYS if isinstance(args.get(k), str) and args[k].strip()
    ]
    if tool_name == "search_files" and not paths:
        paths = ["."]
    for raw in paths:
        try:
            resolved = os.path.realpath(os.path.expanduser(raw))
        except (OSError, ValueError):
            return True
        for form in _store_forms():
            if resolved == form or resolved.startswith(form + "-"):
                return True
            if tool_name == "search_files" and form.startswith(resolved.rstrip(os.sep) + os.sep):
                return True
    return False


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    """Refuse a calendar change on a tainted turn, and any file tool aimed at the
    calendar store. Exception-safe: a hook that cannot decide refuses."""
    tool_name = str(kwargs.get("tool_name") or "")
    try:
        if tool_name in WRITE_TOOLS and _turn_is_tainted(_session(kwargs)):
            return {"action": "block", "message": _TAINTED.format(tool=tool_name)}
        if tool_name in FILE_TOOLS and _touches_store(tool_name, kwargs.get("args")):
            return {"action": "block", "message": FILE_REFUSAL}
    except Exception:  # noqa: BLE001 - never raise out of a hook
        logger.exception("hermes-smd-calendar: pre_tool_call failed; refusing %s", tool_name)
        if tool_name in WRITE_TOOLS or tool_name in FILE_TOOLS:
            return {
                "action": "block",
                "message": FILE_REFUSAL
                if tool_name in FILE_TOOLS
                else _TAINTED.format(tool=tool_name),
            }
    return None


#: The audit transport, bound at register when the seat's audit env is set.
_AUDIT: Any | None = None


def _audit_response(message_id: str, outcome: ingest.Outcome) -> None:
    """One WEBHOOK_SUPPRESSED row for a calendar response that woke no turn:
    what each REPLY did (recorded / dropped and why). Never the email's text,
    never an address. Best effort: the suppression stands either way."""
    if _AUDIT is None:
        logger.info("hermes-smd-calendar: calendar response suppressed; no audit client, log only")
        return
    try:
        from shared.audit_contract import INSERT_SQL, agent_event_params

        params = agent_event_params(
            action_type="WEBHOOK_SUPPRESSED",
            metadata={
                "reason": ingest.SUPPRESSED_REASON,
                "message_id": message_id,
                "replies": list(outcome.replies),
            },
        )
        _AUDIT.execute(INSERT_SQL, *params)
    except Exception:  # noqa: BLE001 - an audit failure never wakes the turn back up
        logger.warning("hermes-smd-calendar: suppression audit write failed", exc_info=True)


def on_pre_gateway_dispatch(**kwargs: Any) -> dict[str, Any] | None:
    """Apply an inbound email's calendar parts (``ingest``). An email that is only
    a calendar response is consumed: the router is told to leave it, the turn is
    skipped, and the act is audited. Exception-safe: a failure dispatches the
    email as it would have been."""
    try:
        seat = seat_mod.load_seat()
        result = ingest.process(ingest._payload(kwargs), seat)
        if result is None:
            return None
        message_id, outcome = result
        if not outcome.response_only:
            return None
        ingest.calendar_inbox.mark(message_id)
        _audit_response(message_id, outcome)
        return {"action": "skip", "reason": ingest.SUPPRESSED_REASON}
    except Exception:  # noqa: BLE001 - never raise out of a hook
        logger.warning("hermes-smd-calendar: inbound calendar parts not read", exc_info=True)
        return None


def register(ctx: Any) -> None:
    """Register the four tools and the two hooks (pre_tool_call,
    pre_gateway_dispatch) on a seat that authors a
    calendar; nothing anywhere else."""
    try:
        authored = seat_mod.calendar_authored(CustomerConfig.from_volume())
    except CustomerConfigError as exc:
        logger.info("hermes-smd-calendar: customer.yaml unreadable (%s); nothing registered", exc)
        return
    if not authored:
        logger.info("hermes-smd-calendar: no calendar skill authored on this seat; no tools")
        return
    for name, (description, schema) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="calendar_invite",
            schema=schema,
            handler=_handler(name),
            description=description,
            emoji="",
        )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    global _AUDIT
    try:
        from shared.audit_client import audit_client_from_env
        from shared.secrets import require

        _AUDIT = audit_client_from_env(
            customer_slug=require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")["SMD_CUSTOMER_SLUG"]
        )
    except Exception:  # noqa: BLE001 - no audit env: suppressions are logged only
        _AUDIT = None
    logger.info("hermes-smd-calendar registered %d tools", len(TOOLS))


__all__ = [
    "FILE_REFUSAL",
    "FILE_TOOLS",
    "TOOLS",
    "TOOL_CANCEL",
    "TOOL_CREATE",
    "TOOL_LIST",
    "TOOL_UPDATE",
    "WRITE_TOOLS",
    "on_pre_gateway_dispatch",
    "on_pre_tool_call",
    "register",
]
