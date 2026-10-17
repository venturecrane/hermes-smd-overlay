"""RFC 5545 / RFC 5546 calendar text: the invitations a seat sends, and the ones
it receives.

WHY THIS EXISTS (ss-console#3264, ADR 0092 pending). A hosted agent keeps a
calendar for the people it serves by sending them invitations, the same
``text/calendar`` part every calendar client already understands: ``METHOD:
REQUEST`` creates an event or moves it (same ``UID``, higher ``SEQUENCE``),
``METHOD:CANCEL`` removes it. Proven in Google and Apple calendars on
2026-10-16 (vfy_01M55T51PV1YB4D56PW7KK8WK0, vfy_01M55THDN2KSHQ8YKAN0QQKPTE,
vfy_01M55TK7D5M02RNYGZ85WZAQR5). There is no calendar API and no OAuth grant:
the person's own client files the invitation.

WHAT IS BUILT HERE. :func:`build_invitation` renders one event as a complete
iMIP calendar:

* TEXT values escaped (``\\``, ``;``, ``,``, newlines) and every content line
  folded at 75 octets without splitting a UTF-8 sequence (RFC 5545 3.1, 3.3.11);
* a timed event in UTC (``...Z``) when it names no time zone; with ``TZID`` and
  a ``VTIMEZONE`` built from the IANA database when it does, so a repeating
  event keeps its wall-clock time across a daylight-saving change;
* an all-day event as ``VALUE=DATE`` with an exclusive ``DTEND``;
* an ``RRULE`` from a closed grammar (:func:`parse_rrule`), never passed through;
* ``VALARM`` reminders as ``ACTION:DISPLAY`` only: an ``EMAIL`` alarm can mail
  people the message never addressed, and the broker refuses one.

The broker re-validates every invitation (ss-console
``workspace_broker/agentmail_calendar.py``); this module is the half that makes
them correct, that one the half that makes them safe.

WHAT IS PARSED HERE. :func:`parse_invitation` reads one received invitation into
plain fields, for the seat's own calendar store. It is a reader, not a
validator: what it returns is data a person sent, and the caller decides
whether to keep it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//SMD Services//Hosted Agent Calendar//EN"
METHOD_REQUEST = "REQUEST"
METHOD_CANCEL = "CANCEL"
METHODS = (METHOD_REQUEST, METHOD_CANCEL)
_FOLD_OCTETS = 75
UTC = dt.timezone.utc

#: The RRULE parts a seat may author, and the values each may take.
_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_BYDAY_RE = re.compile(r"^([+-]?(?:[1-9]|[1-4][0-9]|5[0-3]))?(MO|TU|WE|TH|FR|SA|SU)$")
_INT_RE = re.compile(r"^[0-9]{1,4}$")
_UNTIL_RE = re.compile(r"^[0-9]{8}(T[0-9]{6}Z)?$")
_MONTHDAY_RE = re.compile(r"^-?(?:[1-9]|[12][0-9]|3[01])$")
_MONTH_RE = re.compile(r"^(?:[1-9]|1[0-2])$")
#: A reminder longer than four weeks ahead is not a reminder.
MAX_REMINDER_MINUTES = 4 * 7 * 24 * 60
#: Years of daylight-saving rules a VTIMEZONE carries past the event's start.
_TZ_YEARS = 6


class InvitationError(ValueError):
    """The event cannot be written as a valid invitation. The message says which
    field and why, in words fit to repeat to the person who asked."""


# ---------------------------------------------------------------------------
# Content lines
# ---------------------------------------------------------------------------


def escape_text(value: str) -> str:
    """RFC 5545 3.3.11 TEXT escaping. Carriage returns are dropped; a newline is
    written ``\\n``."""
    out = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    return out.replace("\n", "\\n")


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _param_value(value: str) -> str:
    """A parameter value: quoted when it holds ``:``, ``;`` or ``,``; a double
    quote (which no parameter may carry) becomes a single quote."""
    clean = value.replace('"', "'").replace("\r", " ").replace("\n", " ")
    return f'"{clean}"' if any(c in clean for c in ":;,") else clean


def fold(line: str) -> str:
    """One content line folded at 75 octets, continuation lines led by a space,
    never splitting a UTF-8 sequence."""
    data = line.encode("utf-8")
    if len(data) <= _FOLD_OCTETS:
        return line
    parts: list[str] = []
    current = ""
    size = 0
    limit = _FOLD_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current, size = char, width
            limit = _FOLD_OCTETS - 1  # the leading space counts
        else:
            current += char
            size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _calendar(lines: list[str]) -> str:
    return "".join(fold(line) + "\r\n" for line in lines)


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def _utc_stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _local_stamp(value: dt.datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvitationError(
            f"{name!r} is not a time zone name (use one like America/Phoenix)"
        ) from exc


def _offset(delta: dt.timedelta | None) -> str:
    seconds = int((delta or dt.timedelta()).total_seconds())
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{sign}{hours:02d}{minutes:02d}" + (f"{secs:02d}" if secs else "")


def _transitions(tz: ZoneInfo, first: int, last: int) -> list[dt.datetime]:
    """Every UTC instant in [first, last] years where ``tz``'s offset changes."""
    found: list[dt.datetime] = []
    probe = dt.datetime(first, 1, 1, tzinfo=UTC)
    end = dt.datetime(last + 1, 1, 1, tzinfo=UTC)
    step = dt.timedelta(days=1)
    previous = probe.astimezone(tz).utcoffset()
    while probe < end:
        nxt = probe + step
        offset = nxt.astimezone(tz).utcoffset()
        if offset != previous:
            low, high = probe, nxt
            while high - low > dt.timedelta(minutes=1):
                mid = low + (high - low) / 2
                if mid.astimezone(tz).utcoffset() == previous:
                    low = mid
                else:
                    high = mid
            found.append(high.replace(second=0, microsecond=0))
            previous = offset
        probe = nxt
    return found


def vtimezone(name: str, start_year: int, years: int = _TZ_YEARS) -> list[str]:
    """A VTIMEZONE for ``name`` covering ``start_year`` and the years after it:
    a block for the offset in force on January 1 of ``start_year`` (so a time
    before the year's first change is defined), then one STANDARD or DAYLIGHT
    block per offset change, each with an explicit DTSTART (no RRULE to get
    wrong)."""
    tz = zone(name)
    moment = dt.datetime(start_year, 1, 1, tzinfo=UTC).astimezone(tz)
    base_kind = "DAYLIGHT" if moment.dst() else "STANDARD"
    base_offset = _offset(moment.utcoffset())
    lines = [
        "BEGIN:VTIMEZONE",
        f"TZID:{name}",
        f"BEGIN:{base_kind}",
        f"DTSTART:{start_year}0101T000000",
        f"TZOFFSETFROM:{base_offset}",
        f"TZOFFSETTO:{base_offset}",
        f"TZNAME:{escape_text(moment.tzname() or name)}",
        f"END:{base_kind}",
    ]
    for change in _transitions(tz, start_year, start_year + years):
        before = (change - dt.timedelta(minutes=1)).astimezone(tz)
        after = change.astimezone(tz)
        kind = "DAYLIGHT" if after.dst() else "STANDARD"
        # DTSTART is local time in the offset BEFORE the change (RFC 5545 3.6.5).
        local = (change + (before.utcoffset() or dt.timedelta())).replace(tzinfo=None)
        lines += [
            f"BEGIN:{kind}",
            f"DTSTART:{_local_stamp(local)}",
            f"TZOFFSETFROM:{_offset(before.utcoffset())}",
            f"TZOFFSETTO:{_offset(after.utcoffset())}",
            f"TZNAME:{escape_text(after.tzname() or name)}",
            f"END:{kind}",
        ]
    lines.append("END:VTIMEZONE")
    return lines


@dataclass(frozen=True)
class When:
    """When an event happens, resolved from what was asked.

    ``all_day``: ``start``/``end`` are dates, ``end`` exclusive. Otherwise both
    are aware datetimes and ``tzid`` is the IANA zone they are written in, or
    ``""`` for UTC."""

    all_day: bool
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    tzid: str = ""


def _parse_date(value: str, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise InvitationError(f"{what} {value!r} is not a date (YYYY-MM-DD)") from exc


def _parse_datetime(value: str, what: str, tzid: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvitationError(
            f"{what} {value!r} is not a date and time (YYYY-MM-DDTHH:MM)"
        ) from exc
    if tzid:
        tz = zone(tzid)
        return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)
    if parsed.tzinfo is None:
        raise InvitationError(
            f"{what} {value!r} names no time zone: give a timezone (like America/Phoenix) "
            "or a UTC offset"
        )
    return parsed


def resolve_when(start: str, end: str | None, timezone: str | None, *, recurring: bool) -> When:
    """The event's times from the tool's arguments.

    A bare date is an all-day event. A date and time needs a time zone, by name
    (``timezone``) or as an offset in the value; a repeating timed event needs
    the NAME, because an offset cannot follow a daylight-saving change."""
    start = (start or "").strip()
    end = (end or "").strip()
    tzid = (timezone or "").strip()
    if not start:
        raise InvitationError("start is required")
    if "T" not in start:
        first = _parse_date(start, "start")
        last = _parse_date(end, "end") if end else first + dt.timedelta(days=1)
        if "T" in end:
            raise InvitationError("an all-day event's end is a date, not a time")
        if last <= first:
            raise InvitationError(
                "an all-day event's end must be after its start (end is exclusive)"
            )
        return When(all_day=True, start=first, end=last)
    if recurring and not tzid:
        raise InvitationError("a repeating event at a time of day needs a timezone name")
    begin = _parse_datetime(start, "start", tzid)
    finish = _parse_datetime(end, "end", tzid) if end else begin + dt.timedelta(hours=1)
    if finish <= begin:
        raise InvitationError("the event's end must be after its start")
    return When(all_day=False, start=begin, end=finish, tzid=tzid)


def parse_rrule(value: str | None) -> str:
    """An RRULE from the closed grammar, normalized, or ``""``.

    FREQ (DAILY, WEEKLY, MONTHLY, YEARLY), INTERVAL, COUNT, UNTIL, BYDAY,
    BYMONTHDAY, BYMONTH. Anything else is refused rather than passed through:
    a rule is a calendar instruction to someone else's client."""
    text = (value or "").strip()
    if text.upper().startswith("RRULE:"):
        text = text[len("RRULE:") :]
    if not text:
        return ""
    parts: dict[str, str] = {}
    for item in text.split(";"):
        key, sep, val = item.partition("=")
        key, val = key.strip().upper(), val.strip().upper()
        if not sep or not val:
            raise InvitationError(f"recurrence part {item!r} is not KEY=VALUE")
        if key in parts:
            raise InvitationError(f"recurrence names {key} twice")
        parts[key] = val
    if parts.get("FREQ") not in _FREQS:
        raise InvitationError(f"recurrence FREQ must be one of {', '.join(_FREQS)}")
    if "COUNT" in parts and "UNTIL" in parts:
        raise InvitationError("recurrence takes COUNT or UNTIL, not both")
    for key, val in parts.items():
        ok = {
            "FREQ": lambda v: True,
            "INTERVAL": lambda v: bool(_INT_RE.match(v)) and int(v) >= 1,
            "COUNT": lambda v: bool(_INT_RE.match(v)) and int(v) >= 1,
            "UNTIL": lambda v: bool(_UNTIL_RE.match(v)),
            "BYDAY": lambda v: all(_BYDAY_RE.match(x) for x in v.split(",")),
            "BYMONTHDAY": lambda v: all(_MONTHDAY_RE.match(x) for x in v.split(",")),
            "BYMONTH": lambda v: all(_MONTH_RE.match(x) for x in v.split(",")),
        }.get(key)
        if ok is None:
            raise InvitationError(f"recurrence part {key} is not one this seat writes")
        if not ok(val):
            raise InvitationError(f"recurrence {key}={val} is not valid")
    order = ("FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH")
    return ";".join(f"{k}={parts[k]}" for k in order if k in parts)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attendee:
    email: str
    name: str = ""


@dataclass(frozen=True)
class Event:
    """One event as the seat's calendar holds it."""

    uid: str
    sequence: int
    title: str
    when: When
    organizer: str
    attendees: tuple[Attendee, ...]
    organizer_name: str = ""
    description: str = ""
    location: str = ""
    rrule: str = ""
    reminders: tuple[int, ...] = ()
    status: str = "CONFIRMED"


def _time_lines(name: str, when: When, value: dt.date | dt.datetime) -> list[str]:
    if when.all_day:
        assert isinstance(value, dt.date)
        return [f"{name};VALUE=DATE:{value.strftime('%Y%m%d')}"]
    assert isinstance(value, dt.datetime)
    if when.tzid:
        return [f"{name};TZID={when.tzid}:{_local_stamp(value.astimezone(zone(when.tzid)))}"]
    return [f"{name}:{_utc_stamp(value)}"]


def build_invitation(event: Event, method: str, *, now: dt.datetime | None = None) -> str:
    """The complete calendar text for ``event`` under ``method``."""
    if method not in METHODS:
        raise InvitationError(f"method must be one of {METHODS}")
    if event.sequence < 0:
        raise InvitationError("SEQUENCE must not be negative")
    if not event.attendees:
        raise InvitationError("an invitation names at least one attendee")
    stamp = _utc_stamp(now or dt.datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
    ]
    if event.when.tzid:
        start = event.when.start
        assert isinstance(start, dt.datetime)
        lines += vtimezone(event.when.tzid, start.year)
    organizer_cn = f";CN={_param_value(event.organizer_name)}" if event.organizer_name else ""
    lines += [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"SEQUENCE:{event.sequence}",
        f"DTSTAMP:{stamp}",
        *_time_lines("DTSTART", event.when, event.when.start),
        *_time_lines("DTEND", event.when, event.when.end),
        f"SUMMARY:{escape_text(event.title)}",
        f"STATUS:{'CANCELLED' if method == METHOD_CANCEL else event.status}",
        f"ORGANIZER{organizer_cn}:mailto:{event.organizer}",
    ]
    if event.rrule:
        lines.append(f"RRULE:{event.rrule}")
    if event.description:
        lines.append(f"DESCRIPTION:{escape_text(event.description)}")
    if event.location:
        lines.append(f"LOCATION:{escape_text(event.location)}")
    for person in event.attendees:
        cn = f";CN={_param_value(person.name)}" if person.name else ""
        lines.append(
            f"ATTENDEE{cn};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{person.email}"
        )
    if method == METHOD_REQUEST:
        for minutes in event.reminders:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"TRIGGER:-PT{int(minutes)}M",
                f"DESCRIPTION:{escape_text(event.title)}",
                "END:VALARM",
            ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _calendar(lines)


def describe_when(when: When) -> str:
    """The event's time in words, for the message body that carries it."""
    if when.all_day:
        assert isinstance(when.start, dt.date) and isinstance(when.end, dt.date)
        last = when.end - dt.timedelta(days=1)
        first = when.start.strftime("%A, %B %-d, %Y")
        return first if last == when.start else f"{first} to {last.strftime('%A, %B %-d, %Y')}"
    assert isinstance(when.start, dt.datetime) and isinstance(when.end, dt.datetime)
    start = when.start.astimezone(zone(when.tzid)) if when.tzid else when.start
    end = when.end.astimezone(zone(when.tzid)) if when.tzid else when.end
    label = when.tzid or (start.tzname() or "UTC")
    same_day = start.date() == end.date()
    tail = end.strftime("%-I:%M %p") if same_day else end.strftime("%A, %B %-d, %Y, %-I:%M %p")
    return f"{start.strftime('%A, %B %-d, %Y, %-I:%M %p')} to {tail} ({label})"


# ---------------------------------------------------------------------------
# Parsing a received invitation
# ---------------------------------------------------------------------------


@dataclass
class Received:
    """One received event, as plain fields. Every value is sender-authored."""

    method: str
    uid: str
    sequence: int
    title: str
    start: str
    end: str
    all_day: bool
    organizer: str
    attendees: list[str] = field(default_factory=list)
    description: str = ""
    location: str = ""
    rrule: str = ""
    status: str = ""


def _unfold(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return [line for line in re.sub(r"\n[ \t]", "", normalized).split("\n") if line]


def _split_line(line: str) -> tuple[str, dict[str, str], str]:
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        return line.upper(), {}, ""
    name, *raw_params = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', head)
    params: dict[str, str] = {}
    for raw in raw_params:
        key, _, val = raw.partition("=")
        params[key.upper()] = val.strip('"')
    return name.upper(), params, value


def _mailto(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("mailto:"):
        text = text[len("mailto:") :]
    return text.strip().lower()


def _received_time(value: str, params: dict[str, str]) -> tuple[str, bool]:
    """An ISO form of a DTSTART/DTEND value, and whether it is a date."""
    raw = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"[0-9]{8}", raw):
        try:
            return dt.datetime.strptime(raw[:8], "%Y%m%d").date().isoformat(), True
        except ValueError:
            return "", True
    try:
        moment = dt.datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return "", False
    if raw.endswith("Z"):
        return moment.replace(tzinfo=UTC).isoformat(), False
    tzid = params.get("TZID", "")
    if tzid:
        try:
            return moment.replace(tzinfo=ZoneInfo(tzid)).isoformat(), False
        except (ZoneInfoNotFoundError, ValueError):
            return moment.isoformat(), False
    return moment.isoformat(), False


def parse_invitation(text: str) -> list[Received]:
    """Every VEVENT in a received calendar, with the calendar's METHOD. A
    calendar with no METHOD or a method this seat does not keep yields nothing."""
    method = ""
    events: list[Received] = []
    current: Received | None = None
    depth: list[str] = []
    for line in _unfold(text):
        name, params, value = _split_line(line)
        if name == "BEGIN":
            depth.append(value.strip().upper())
            if depth[-1] == "VEVENT":
                current = Received(
                    method="",
                    uid="",
                    sequence=0,
                    title="",
                    start="",
                    end="",
                    all_day=False,
                    organizer="",
                )
            continue
        if name == "END":
            closing = value.strip().upper()
            if closing == "VEVENT" and current is not None:
                events.append(current)
                current = None
            if depth:
                depth.pop()
            continue
        if depth == ["VCALENDAR"] and name == "METHOD":
            method = value.strip().upper()
        if current is None or depth[-1:] != ["VEVENT"]:
            continue
        if name == "UID":
            current.uid = value.strip()
        elif name == "SEQUENCE":
            current.sequence = int(value) if value.strip().isdigit() else 0
        elif name == "SUMMARY":
            current.title = unescape_text(value)
        elif name == "DESCRIPTION":
            current.description = unescape_text(value)
        elif name == "LOCATION":
            current.location = unescape_text(value)
        elif name == "DTSTART":
            current.start, current.all_day = _received_time(value, params)
        elif name == "DTEND":
            current.end, _ = _received_time(value, params)
        elif name == "ORGANIZER":
            current.organizer = _mailto(value)
        elif name == "ATTENDEE":
            current.attendees.append(_mailto(value))
        elif name == "RRULE":
            current.rrule = value.strip()
        elif name == "STATUS":
            current.status = value.strip().upper()
    if method not in METHODS:
        return []
    for event in events:
        event.method = method
    return [e for e in events if e.uid]


__all__ = [
    "METHODS",
    "METHOD_CANCEL",
    "METHOD_REQUEST",
    "MAX_REMINDER_MINUTES",
    "Attendee",
    "Event",
    "InvitationError",
    "Received",
    "When",
    "build_invitation",
    "describe_when",
    "escape_text",
    "fold",
    "parse_invitation",
    "parse_rrule",
    "resolve_when",
    "unescape_text",
    "vtimezone",
]
