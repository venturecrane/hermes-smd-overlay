"""An invitation a person sends the seat is kept on the seat's calendar.

WHEN IT RUNS. ``pre_llm_call`` on a turn an email opened (the origin the
webhook router verified and the inbound plugin bound to THIS turn,
``SESSION_INBOUND_ORIGIN.bound_this_turn``), on a seat that has a calendar.
The ``message.received`` event carries no attachment list, so the seat asks
the vendor for the message's attachments with its own inbox-scoped read key
(``shared.agentmail_broker``) and reads each calendar part, at most 64 KiB.

WHOSE INVITATIONS ARE KEPT. Only a person the seat already treats as its own:
the verified sender must be one of the seat's people (space mode) or an exact
roster address, AND the invitation's ORGANIZER must be that same sender. An
invitation a person forwards from someone else (a dentist, a venue) is not
theirs to put on the shared record: its words are a third party's, and
``calendar_invite_list`` is unfenced on the promise that it holds only what the
seat's own people and the seat itself wrote. Such a part is skipped, counted
and logged, never kept.

WHAT A KEPT INVITATION IS. A row with ``origin: inbound``, in the sender's own
private space on a space-mode seat (``person:<id>``): the person invited the
seat, not the couple. A REQUEST adds the event or, at a SEQUENCE no lower than
the stored one, replaces it; a CANCEL marks it canceled. An event this seat
organized is never touched by an inbound part (a UID collision is refused).
Nothing is sent: keeping an invitation answers nobody.

EXCEPTION-SAFE. A hook never raises; a failure is logged and the turn goes on
with no calendar note. The person's email is still answered.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import agentmail_broker, calendar_store, ical, space
from shared.calendar_store import StoredEvent
from shared.inbound import SESSION_INBOUND_ORIGIN

from . import seat as seat_mod

logger = logging.getLogger(__name__)

#: At most this many calendar parts are read from one email.
MAX_PARTS = 5


def _owner_space(seat: seat_mod.Seat, sender: str) -> str | None:
    """The space a sender's invitation is kept in, or ``None`` when the sender is
    not someone whose invitations this seat keeps."""
    if sender not in seat.audience:
        return None
    if not seat.space_mode:
        return ""
    for person_id, email in seat.person_emails.items():
        if email == sender:
            return space.person_key(person_id)
    return None


def keep(received: ical.Received, *, sender: str, target_space: str) -> str:
    """Apply one received event to the store. Returns ``kept`` / ``canceled`` /
    a ``skipped: <why>`` string."""
    if received.organizer != sender:
        return "skipped: organized by someone other than its sender"
    if not received.uid or not received.start:
        return "skipped: no uid or no start"
    existing = calendar_store.STORE.get(received.uid)
    if existing is not None and (
        existing.origin != calendar_store.ORIGIN_INBOUND or existing.organizer != sender
    ):
        return "skipped: the uid belongs to another event"
    if existing is not None and received.sequence < existing.sequence:
        return "skipped: older than the version kept"
    if received.method == ical.METHOD_CANCEL:
        if existing is None:
            return "skipped: cancels an event never kept"
        existing.sequence = max(existing.sequence, received.sequence)
        existing.status = calendar_store.STATUS_CANCELLED
        calendar_store.STORE.put(existing)
        return "canceled"
    end = received.end or received.start
    calendar_store.STORE.put(
        StoredEvent(
            uid=received.uid,
            sequence=received.sequence,
            title=received.title[:200] or "(untitled)",
            start=received.start,
            end=end,
            all_day=received.all_day,
            rrule=received.rrule[:300],
            description=received.description[:4000],
            location=received.location[:300],
            attendees=[{"email": a, "name": ""} for a in received.attendees[:20] if a],
            organizer=sender,
            status=calendar_store.STATUS_CANCELLED
            if received.status == "CANCELLED"
            else calendar_store.STATUS_CONFIRMED,
            space=target_space,
            origin=calendar_store.ORIGIN_INBOUND,
            delivery="",
        )
    )
    return "kept"


def ingest_turn(session_id: str, seat: seat_mod.Seat) -> list[str]:
    """Keep every invitation the email that opened this turn carries. Returns a
    short line per kept or canceled event, for the turn's context."""
    origin = SESSION_INBOUND_ORIGIN.bound_this_turn(session_id)
    if origin is None or not origin.sender_address or not origin.message_id:
        return []
    sender = origin.sender_address.strip().lower()
    target = _owner_space(seat, sender)
    if target is None:
        return []
    parts = [
        p
        for p in agentmail_broker.list_attachments(origin.message_id)
        if agentmail_broker.is_calendar_attachment(p)
    ]
    notes: list[str] = []
    skipped = 0
    for part in parts[:MAX_PARTS]:
        text = agentmail_broker.read_calendar_attachment(
            origin.message_id, str(part["attachment_id"])
        )
        for received in ical.parse_invitation(text):
            outcome = keep(received, sender=sender, target_space=target)
            if outcome == "kept":
                notes.append(
                    f"kept the invitation {received.title!r} ({received.start}) on this seat's calendar"
                )
            elif outcome == "canceled":
                notes.append(f"marked {received.title!r} canceled on this seat's calendar")
            else:
                skipped += 1
                logger.info("hermes-smd-calendar: inbound calendar part %s", outcome)
    if skipped:
        notes.append(f"{skipped} calendar item(s) in this email were not kept")
    return notes


def context_for(session_id: str, seat: seat_mod.Seat) -> dict[str, Any] | None:
    """The ``pre_llm_call`` contribution, or ``None``."""
    notes = ingest_turn(session_id, seat)
    if not notes:
        return None
    return {"context": "[Calendar: " + "; ".join(notes) + ".]"}


__all__ = ["MAX_PARTS", "context_for", "ingest_turn", "keep"]
