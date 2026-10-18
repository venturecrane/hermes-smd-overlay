"""Calendar parts that arrive by email: invitations kept, responses recorded.

WHEN IT RUNS. ``pre_gateway_dispatch``, once per inbound email, BEFORE any
agent turn exists, on a seat that has a calendar. The delivery was verified
upstream (the SMD gate's Svix check and the webhook adapter's HMAC) before the
event reached this hook; the sender is the address the vendor recorded, the
same one the webhook router records as the turn's origin. The
``message.received`` event carries no attachment list, so the seat asks the
vendor with its own inbox-scoped read key (``shared.agentmail_broker``) and
reads each calendar part, at most 64 KiB.

A RESPONSE NEVER WAKES THE AGENT (METHOD:REPLY). Every time a person taps
Accept or Decline, their calendar app emails the seat a REPLY. Left alone, each
one would be an inbound email turn: noise at best, and at worst the agent
answering a calendar robot. So an email whose calendar parts are all REPLY is
consumed here: the dispatch is skipped (``{"action": "skip"}``), the webhook
router is told to leave it alone (``shared.calendar_inbox``) so no origin,
request card or routed row is written for it, and one ``WEBHOOK_SUPPRESSED``
audit row says what happened. A REPLY is RECORDED only when it is from one of
the seat's people (space mode) or an exact roster address, for an event THIS
SEAT organized, naming that same sender as the attendee with a PARTSTAT of
ACCEPTED, DECLINED or TENTATIVE; that attendee's status changes in the store
and ``calendar_invite_list`` shows it. Any other REPLY (an unknown UID, a
stranger, a forged attendee) is dropped from the turn just the same, and
logged and audited as dropped.

INVITATIONS ARE KEPT, AND THE TURN GOES ON (REQUEST, CANCEL). Only from a
person the seat already treats as its own, and only when the invitation's
ORGANIZER is that same sender. An invitation a person forwards from someone
else (a dentist, a venue) is not theirs to put on the shared record: its words
are a third party's, and ``calendar_invite_list`` is unfenced on the promise
that it holds only what the seat's own people and the seat itself wrote. A kept
invitation is a row with ``origin: inbound``, in the sender's own private space
on a space-mode seat. An event this seat organized is never touched by an
inbound REQUEST or CANCEL (a UID collision is refused). The email still reaches
the agent: a person who invites the seat may also have written to it.

EXCEPTION-SAFE. A hook never raises; a failure is logged and the email is
dispatched as it would have been.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from shared import (
    agentmail_broker,
    calendar_inbox,
    calendar_store,
    ical,
    inbound_message,
    sender_auth,
    space,
)
from shared.calendar_store import StoredEvent

from . import seat as seat_mod

logger = logging.getLogger(__name__)

#: At most this many calendar parts are read from one email.
MAX_PARTS = 5
PARTSTATS = frozenset({"ACCEPTED", "DECLINED", "TENTATIVE"})
SUPPRESSED_REASON = "calendar_reply"


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
    """Apply one received REQUEST or CANCEL to the store. Returns ``kept`` /
    ``canceled`` / a ``skipped: <why>`` string."""
    if received.organizer != sender:
        return "skipped: organized by someone other than its sender"
    if not received.uid or (not received.start and received.method != ical.METHOD_CANCEL):
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
    calendar_store.STORE.put(
        StoredEvent(
            uid=received.uid,
            sequence=received.sequence,
            title=received.title[:200] or "(untitled)",
            start=received.start,
            end=received.end or received.start,
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


def record_reply(received: ical.Received, *, sender: str, seat: seat_mod.Seat) -> str:
    """Apply one received REPLY. Returns ``recorded:<PARTSTAT>`` or
    ``dropped: <why>``. Never sends, never wakes anyone."""
    if sender not in seat.audience:
        return "dropped: not from one of the seat's people"
    partstat = received.partstats.get(sender, "")
    if partstat not in PARTSTATS:
        return "dropped: the reply names no response from its sender"
    event = calendar_store.STORE.get(received.uid) if received.uid else None
    if event is None or event.origin != calendar_store.ORIGIN_AGENT:
        return "dropped: not an event this seat organized"
    if sender not in event.attendee_emails():
        return "dropped: the sender was not invited to this event"
    if not calendar_store.STORE.set_partstat(event.uid, sender, partstat):
        return "dropped: the store did not take it"
    return f"recorded:{partstat}"


@dataclass
class Outcome:
    """What one email's calendar parts did."""

    replies: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    #: A person wrote a note with their answer (a REPLY's COMMENT). The answer
    #: is still recorded, but the email reaches its turn: a note is a message.
    noted: bool = False

    @property
    def response_only(self) -> bool:
        """True only for an email that is nothing but calendar answers: every
        calendar part a REPLY, at least one, and no note written with any."""
        return bool(self.replies) and not self.others and not self.noted


def _payload(kwargs: dict[str, Any]) -> Any:
    event = kwargs.get("event")
    raw = getattr(event, "raw_message", None)
    if raw is None:
        raw = kwargs.get("payload")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def process(payload: Any, seat: seat_mod.Seat) -> tuple[str, Outcome] | None:
    """Read and apply one email's calendar parts. ``(message_id, outcome)``, or
    ``None`` for an email that carries none (or is not an AgentMail email)."""
    dto = inbound_message.normalize_inbound(inbound_message.PROVIDER_AGENTMAIL, payload)
    if dto is None or not dto.message_id or not dto.from_addr:
        return None
    sender = dto.from_addr.strip().lower()
    parts = [
        p
        for p in agentmail_broker.list_attachments(dto.message_id)
        if agentmail_broker.is_calendar_attachment(p)
    ]
    if not parts:
        return None
    # A calendar part is filed against the person its From names: an RSVP marks
    # that attendee, an invitation lands in that person's space. An
    # unauthenticated From is only a claim (shared/sender_auth.py), so it files
    # nothing; returning None lets the email flow on to the webhook router,
    # which delivers it as an unknown external sender. Never dropped.
    authenticated, reason = sender_auth.sender_authenticated(dto)
    if not authenticated:
        logger.warning(
            "hermes-smd-calendar: From %s is not authenticated (%s); calendar parts not filed",
            sender,
            reason,
        )
        return None
    outcome = Outcome()
    target = _owner_space(seat, sender)
    for part in parts[:MAX_PARTS]:
        text = agentmail_broker.read_calendar_attachment(dto.message_id, str(part["attachment_id"]))
        for received in ical.parse_invitation(text):
            if received.method == ical.METHOD_REPLY:
                outcome.replies.append(record_reply(received, sender=sender, seat=seat))
                outcome.noted = outcome.noted or bool(received.comment.strip())
            elif target is None:
                outcome.others.append("skipped: not from one of the seat's people")
            else:
                outcome.others.append(keep(received, sender=sender, target_space=target))
    if not outcome.replies and not outcome.others:
        return None
    for line in outcome.replies + outcome.others:
        logger.info("hermes-smd-calendar: inbound calendar part %s", line)
    return dto.message_id, outcome


__all__ = [
    "MAX_PARTS",
    "PARTSTATS",
    "SUPPRESSED_REASON",
    "Outcome",
    "calendar_inbox",
    "keep",
    "process",
    "record_reply",
]
