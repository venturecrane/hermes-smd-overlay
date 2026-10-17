"""The four calendar tools' handlers.

Every change follows one order, so a failure at any step leaves an honest state:

1. read the seat (who may be invited, whose turn this is) and refuse early;
2. write the event to the store with ``delivery: pending``, taking the next
   SEQUENCE from the store (it only goes up);
3. render the invitation (``shared.ical``) and send it through the seat's ONE
   out-of-turn sender (``shared.send_dispatch``): the trust plugin's gate, its
   scans, then the broker, which fences the recipients, validates the
   invitation, builds the part and writes the CONFIRM_SEND row;
4. record what the send did (``sent:<message id>`` or ``not_sent: <reason>``)
   and say exactly that to the model.

No step sends mail any other way. A send that did not go is reported as not
sent, with the gate's or the broker's own reason.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from shared import calendar_store, ical, send_anchor, send_dispatch, space
from shared.calendar_store import StoredEvent

from . import seat as seat_mod
from .seat import CalendarRefused

logger = logging.getLogger(__name__)

UID_DOMAIN = "smd.services"


def _reminders(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 3:
        raise CalendarRefused("reminders_minutes is a list of at most three numbers of minutes")
    out: list[int] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= ical.MAX_REMINDER_MINUTES
        ):
            raise CalendarRefused(
                "each reminder is a whole number of minutes before the event, up to four weeks"
            )
        out.append(item)
    return out


def _text(value: Any, what: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CalendarRefused(f"{what} must be text")
    text = value.strip()
    if len(text) > limit:
        raise CalendarRefused(f"{what} is longer than {limit} characters")
    return text


def _when_of(event: StoredEvent) -> ical.When:
    return ical.resolve_when(
        event.start,
        event.end,
        event.tzid or None,
        recurring=bool(event.rrule) and not event.all_day,
    )


def _ical_event(event: StoredEvent, seat: seat_mod.Seat) -> ical.Event:
    return ical.Event(
        uid=event.uid,
        sequence=event.sequence,
        title=event.title,
        when=_when_of(event),
        organizer=seat.organizer,
        attendees=tuple(
            ical.Attendee(email=a["email"], name=a.get("name", "")) for a in event.attendees
        ),
        description=event.description,
        location=event.location,
        rrule=event.rrule,
        reminders=tuple(event.reminders),
    )


def _body(kind: str, event: StoredEvent) -> tuple[str, str]:
    """The subject and plain text the invitation rides in. A fixed frame around
    the event's own fields; nothing here is said that the event does not say."""
    when = ical.describe_when(_when_of(event))
    if kind == "cancel":
        return f"Canceled: {event.title}", f"{event.title}\n{when}\n\nThis event is canceled."
    lines = [event.title, when]
    if event.location:
        lines.append(f"Where: {event.location}")
    if event.rrule:
        lines.append(f"Repeats: {event.rrule}")
    if event.description:
        lines += ["", event.description]
    prefix = "Updated invitation" if kind == "update" else "Invitation"
    return f"{prefix}: {event.title}", "\n".join(lines)


def _send(kind: str, event: StoredEvent, seat: seat_mod.Seat, session_id: str) -> dict[str, Any]:
    """Render and send ``event``; record and return what happened."""
    method = ical.METHOD_CANCEL if kind == "cancel" else ical.METHOD_REQUEST
    ics = ical.build_invitation(_ical_event(event, seat), method)
    subject, text = _body(kind, event)
    result = send_dispatch.dispatch(
        to=event.attendee_emails(),
        subject=subject,
        text=text,
        # The request this answers, when an email opened the turn; the lane is
        # what admits the seat's people (seat.py), named here and nowhere else.
        anchor=send_anchor.turn_anchor(session_id),
        lane=seat_mod.LANE,
        session_id=session_id,
        templated=False,
        calendar={"method": method, "ics": ics},
    )
    delivery = f"sent:{result.message_id}" if result.sent else f"not_sent: {result.reason}"
    calendar_store.STORE.set_delivery(event.uid, delivery)
    event.delivery = delivery
    outcome: dict[str, Any] = {
        "uid": event.uid,
        "sequence": event.sequence,
        "status": event.status,
        "sent": result.sent,
        "invited": event.attendee_emails(),
    }
    if result.sent:
        outcome["message_id"] = result.message_id
    else:
        note = send_anchor.refusal_note(result.reason)
        outcome["not_sent_because"] = note or result.reason
        outcome["say"] = (
            "The calendar change is recorded on this seat but the invitation was NOT sent, "
            "so nobody's calendar shows it. Say so plainly."
        )
    return outcome


def _attendee_rows(seat: seat_mod.Seat, addresses: list[str]) -> list[dict[str, str]]:
    return [{"email": a, "name": seat.audience.get(a, "")} for a in addresses]


def create(args: dict[str, Any], session_id: str) -> dict[str, Any]:
    seat = seat_mod.load_seat()
    turn = seat_mod.turn_space(session_id, seat)
    attendees = seat_mod.resolve_attendees(seat, args.get("attendees"))
    target = seat_mod.target_space(seat, turn, attendees)
    refusal = seat_mod.private_to_shared_gate(turn, target, act="calendar_invite_create")
    if refusal:
        raise CalendarRefused(refusal)
    title = _text(args.get("title"), "title", 200)
    if not title:
        raise CalendarRefused("title is required")
    rrule = ical.parse_rrule(args.get("rrule"))
    when = ical.resolve_when(
        str(args.get("start") or ""), args.get("end"), args.get("timezone"), recurring=bool(rrule)
    )
    event = StoredEvent(
        uid=f"{uuid.uuid4()}@{UID_DOMAIN}",
        sequence=0,
        title=title,
        start=when.start.isoformat(),
        end=when.end.isoformat(),
        all_day=when.all_day,
        tzid=when.tzid,
        rrule=rrule,
        description=_text(args.get("description"), "description", 4000),
        location=_text(args.get("location"), "location", 300),
        reminders=_reminders(args.get("reminders_minutes")),
        attendees=_attendee_rows(seat, attendees),
        organizer=seat.organizer,
        status=calendar_store.STATUS_CONFIRMED,
        space=target,
        origin=calendar_store.ORIGIN_AGENT,
        delivery="pending",
    )
    ical.build_invitation(_ical_event(event, seat), ical.METHOD_REQUEST)  # refuse before storing
    calendar_store.STORE.put(event)
    return _send("create", event, seat, session_id)


def _owned(uid: Any, seat: seat_mod.Seat, turn: space.Space | None, act: str) -> StoredEvent:
    """The agent-organized event ``uid`` this turn may change, or a refusal."""
    if not isinstance(uid, str) or not uid.strip():
        raise CalendarRefused("uid is required (calendar_invite_list shows each event's uid)")
    event = calendar_store.STORE.get(uid.strip())
    if event is None or (seat.space_mode and not space.may_read(turn, event.space)):
        # The same answer for "absent" and "not yours": an event in the other
        # person's private space does not exist from here.
        raise CalendarRefused("no event with that uid is on this calendar")
    if event.origin != calendar_store.ORIGIN_AGENT:
        raise CalendarRefused(
            "this event came in as an invitation from someone else; only its organizer can change it"
        )
    if event.status == calendar_store.STATUS_CANCELLED:
        raise CalendarRefused("this event is already canceled")
    refusal = (
        seat_mod.private_to_shared_gate(turn, event.space, act=act) if seat.space_mode else None
    )
    if refusal:
        raise CalendarRefused(refusal)
    return event


_UPDATABLE = (
    "title",
    "start",
    "end",
    "timezone",
    "description",
    "location",
    "rrule",
    "reminders_minutes",
)


def update(args: dict[str, Any], session_id: str) -> dict[str, Any]:
    seat = seat_mod.load_seat()
    turn = seat_mod.turn_space(session_id, seat)
    event = _owned(args.get("uid"), seat, turn, "calendar_invite_update")
    if "attendees" in args:
        raise CalendarRefused(
            "who is invited cannot change on an existing event; cancel it and send a new invitation"
        )
    if not any(key in args for key in _UPDATABLE):
        raise CalendarRefused(f"name at least one of {', '.join(_UPDATABLE)} to change")
    if "title" in args:
        event.title = _text(args.get("title"), "title", 200)
        if not event.title:
            raise CalendarRefused("title cannot be empty")
    if "description" in args:
        event.description = _text(args.get("description"), "description", 4000)
    if "location" in args:
        event.location = _text(args.get("location"), "location", 300)
    if "rrule" in args:
        event.rrule = ical.parse_rrule(args.get("rrule"))
    if "reminders_minutes" in args:
        event.reminders = _reminders(args.get("reminders_minutes"))
    if any(key in args for key in ("start", "end", "timezone")):
        tzid = args.get("timezone") if "timezone" in args else (event.tzid or None)
        start = str(args.get("start") or event.start)
        end = args.get("end")
        if "end" not in args:
            # A moved start keeps the event's length.
            old = _when_of(event)
            moved = ical.resolve_when(start, None, tzid, recurring=bool(event.rrule))
            if moved.all_day == old.all_day:
                end = (moved.start + (old.end - old.start)).isoformat()  # type: ignore[operator]
        when = ical.resolve_when(start, end, tzid, recurring=bool(event.rrule))
        event.start, event.end, event.all_day, event.tzid = (
            when.start.isoformat(),
            when.end.isoformat(),
            when.all_day,
            when.tzid,
        )
    event.sequence = calendar_store.STORE.next_sequence(event.uid)
    event.delivery = "pending"
    ical.build_invitation(_ical_event(event, seat), ical.METHOD_REQUEST)  # refuse before storing
    calendar_store.STORE.put(event)
    return _send("update", event, seat, session_id)


def cancel(args: dict[str, Any], session_id: str) -> dict[str, Any]:
    seat = seat_mod.load_seat()
    turn = seat_mod.turn_space(session_id, seat)
    event = _owned(args.get("uid"), seat, turn, "calendar_invite_cancel")
    event.sequence = calendar_store.STORE.next_sequence(event.uid)
    event.status = calendar_store.STATUS_CANCELLED
    event.delivery = "pending"
    calendar_store.STORE.put(event)
    return _send("cancel", event, seat, session_id)


def list_events(args: dict[str, Any], session_id: str) -> dict[str, Any]:
    seat = seat_mod.load_seat()
    turn = seat_mod.turn_space(session_id, seat)
    events = calendar_store.STORE.list(include_cancelled=bool(args.get("include_cancelled")))
    visible = [e for e in events if not seat.space_mode or space.may_read(turn, e.space)]
    rows = [
        {
            "uid": e.uid,
            "title": e.title,
            "start": e.start,
            "end": e.end,
            "all_day": e.all_day,
            "timezone": e.tzid,
            "repeats": e.rrule,
            "location": e.location,
            "description": e.description,
            "invited": e.attendee_emails(),
            "status": e.status,
            "sequence": e.sequence,
            "came_from": "this seat"
            if e.origin == calendar_store.ORIGIN_AGENT
            else f"an invitation from {e.organizer}",
            "invitation_delivered": e.delivery.startswith("sent:")
            if e.origin == calendar_store.ORIGIN_AGENT
            else None,
            **({"space": e.space} if seat.space_mode else {}),
        }
        for e in visible
    ]
    return {"events": rows, "count": len(rows)}


HANDLERS = {
    "calendar_invite_create": create,
    "calendar_invite_update": update,
    "calendar_invite_cancel": cancel,
    "calendar_invite_list": list_events,
}


def run(tool_name: str, args: dict[str, Any], session_id: str) -> str:
    """Run one tool. A refusal raises with a sentence the model relays."""
    try:
        return json.dumps(
            HANDLERS[tool_name](args if isinstance(args, dict) else {}, session_id),
            ensure_ascii=False,
        )
    except (CalendarRefused, ical.InvitationError) as exc:
        raise RuntimeError(f"{tool_name} refused: {exc}") from exc
    except calendar_store.CalendarStoreError as exc:
        logger.error("hermes-smd-calendar: store failure in %s (%s)", tool_name, exc)
        raise RuntimeError(f"{tool_name} failed: {exc}. Nothing was sent.") from exc


__all__ = ["HANDLERS", "run"]
