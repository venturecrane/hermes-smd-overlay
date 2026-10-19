"""What the seat authors for its calendar: whether it has one, who it may invite,
who it invites as, and which space a turn's invitation belongs to.

THE GATE IS AN AUTHORED SKILL. A seat has a calendar when one of its personas
authors the skill ``calendar`` ``enabled: true`` with ``settings.recipients:
roster``. The setting is not decoration: it is what opens the broker's
participant fence for these sends (ss-console ``participant_fence._skill_lanes``
maps the lane ``skill:calendar`` to the seat's exact ``scope.inbound_allow_from``
addresses). Without it every invitation to a rostered person would be refused
at the broker, so a seat that does not author it gets no tools rather than four
tools that cannot work. The lane is named by THIS code on every calendar send;
the model never names one.

WHO MAY BE INVITED.

* A seat more than one person shares (``people:``, space mode): exactly the
  authored people's addresses.
* Any other calendar seat: the exact addresses of ``scope.inbound_allow_from``
  (never a ``@domain`` entry: a domain is not a person to invite).

Either way the broker checks again, against its own copy of the config.

WHICH SPACE (space mode, ``shared.space``). An invitation from ``person:X``
that invites only X stays in X's space. Anything else (the partner invited, or
both) belongs to ``shared``. Either partner may add to the shared calendar from
their own conversation (Captain decision 2026-10-19).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from shared import send_anchor, space
from shared.customer_config import CustomerConfig, CustomerConfigError

logger = logging.getLogger(__name__)

CALENDAR_SKILL = "calendar"
LANE = send_anchor.skill_lane(CALENDAR_SKILL)


class CalendarRefused(RuntimeError):
    """A calendar act is refused. The message is written for the model to relay."""


@dataclass(frozen=True)
class Seat:
    """The calendar facts of one seat, read fresh per call."""

    #: The seat's own inbox. No display name: none is authored for the seat as
    #: an organizer, and a calendar client shows the address plainly.
    organizer: str
    #: Who may be invited: address -> display name.
    audience: dict[str, str]
    #: ``people[].id`` -> address, on a space-mode seat; empty otherwise.
    person_emails: dict[str, str]
    space_mode: bool


def _skill_opted_in(personas: list[dict[str, Any]]) -> bool:
    for persona in personas:
        for entry in persona.get("skills") or [] if isinstance(persona, dict) else []:
            if not isinstance(entry, dict) or entry.get("name") != CALENDAR_SKILL:
                continue
            settings = entry.get("settings")
            if (
                entry.get("enabled") is True
                and isinstance(settings, dict)
                and settings.get(send_anchor.ROSTER_RECIPIENTS_KEY)
                == send_anchor.ROSTER_RECIPIENTS_VALUE
            ):
                return True
    return False


def calendar_authored(config: CustomerConfig) -> bool:
    """True when the seat authors the calendar skill on the roster lane AND sends
    through AgentMail (the only transport that carries an invitation)."""
    if not _skill_opted_in(config.personas):
        return False
    record = config.connectors.get("Email")
    adapter = record.get("adapter") if isinstance(record, dict) else None
    adapter = (
        adapter.strip().lower() if isinstance(adapter, str) and adapter.strip() else "agentmail"
    )
    return adapter == "agentmail"


def _organizer(config: CustomerConfig) -> str:
    """The seat's own inbox: the authored ``connectors.Email.inbox_address``, else
    the slug convention. The same rule the broker pins the From with (ss-console
    ``agentmail_auth.seat_inbox_address``), and the broker refuses an ORGANIZER
    that is not it."""
    record = config.connectors.get("Email")
    authored = record.get("inbox_address") if isinstance(record, dict) else None
    if isinstance(authored, str) and "@" in authored:
        return authored.strip().lower()
    return f"{config.slug.strip().lower()}@agentmail.to"


def load_seat(config: CustomerConfig | None = None) -> Seat:
    """The seat's calendar facts. Raises :class:`CalendarRefused` when they cannot
    be read: an invitation whose audience could not be decided is not sent."""
    try:
        cfg = config or CustomerConfig.from_volume()
        if not calendar_authored(cfg):
            raise CalendarRefused("this seat does not author a calendar")
        spaces = space.space_config(cfg)
        organizer = _organizer(cfg)
        roster = cfg.inbound_roster
    except (CustomerConfigError, space.SpaceConfigError) as exc:
        raise CalendarRefused(f"the seat's configuration could not be read ({exc})") from exc
    if spaces is not None:
        return Seat(
            organizer=organizer,
            audience={p.email: p.name for p in spaces.people if p.email},
            person_emails={p.id: p.email for p in spaces.people if p.email},
            space_mode=True,
        )
    audience = {
        entry: cfg.authored_person_name(entry) or ""
        for entry in roster
        if "@" in entry and not entry.startswith("@")
    }
    return Seat(organizer=organizer, audience=audience, person_emails={}, space_mode=False)


def resolve_attendees(seat: Seat, requested: Any) -> list[str]:
    """The invitees, as addresses, from what the model asked.

    On a space-mode seat the model names PERSON IDS (``p1``, ``p2``) and nothing
    else; this code maps each to its authored ``people[].email``. An address is
    refused there even when it is on ``scope.inbound_allow_from`` (an SMD
    address is, on every seat): the roster lane admits the whole roster at the
    broker, so the lock that keeps an invitation to the seat's people is HERE,
    in code, before any send (the 2026-10-16 Brokery rehearsal reached the real
    client because a model picked a rostered address). On any other calendar
    seat the model names exact roster addresses. One name that does not resolve
    refuses the whole invitation, never a partial send."""
    if not isinstance(requested, list) or not requested:
        raise CalendarRefused("name at least one person to invite")
    out: list[str] = []
    refused: list[str] = []
    for item in requested:
        if not isinstance(item, str) or not item.strip():
            refused.append(repr(item))
            continue
        value = item.strip().lower()
        address = seat.person_emails.get(value) if seat.space_mode else value
        if not address or address not in seat.audience:
            refused.append(item.strip())
            continue
        if address not in out:
            out.append(address)
    if refused:
        who = (
            f"the seat's people, named by id ({', '.join(sorted(seat.person_emails))})"
            if seat.space_mode
            else "the people on this seat's roster"
        )
        raise CalendarRefused(
            f"an invitation goes only to {who}; not sent to anyone: {', '.join(refused)}"
        )
    return out


def turn_space(session_id: str, seat: Seat) -> space.Space | None:
    """This turn's space on a space-mode seat (``None`` elsewhere). Refuses when
    a space-mode seat cannot say whose turn this is: fail closed."""
    if not seat.space_mode:
        return None
    try:
        found = space.resolve_space(session_id)
    except space.SpaceConfigError as exc:
        raise CalendarRefused(f"whose conversation this is could not be read ({exc})") from exc
    if found is None:
        raise CalendarRefused(
            "this conversation could not be tied to one of the seat's people, "
            "so no calendar change can be made from it"
        )
    return found


def target_space(seat: Seat, turn: space.Space | None, attendees: list[str]) -> str:
    """The space an invitation belongs to: the person's own when it invites only
    them, ``shared`` otherwise; ``""`` on a seat that is not in space mode."""
    if not seat.space_mode or turn is None:
        return ""
    if turn.private and turn.person_id:
        own = seat.person_emails.get(turn.person_id, "")
        if own and attendees == [own]:
            return turn.key
    return space.SHARED


__all__ = [
    "CALENDAR_SKILL",
    "LANE",
    "CalendarRefused",
    "Seat",
    "calendar_authored",
    "load_seat",
    "resolve_attendees",
    "target_space",
    "turn_space",
]
