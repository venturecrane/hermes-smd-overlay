"""shared.ical: the invitations a seat sends, byte for byte (ss-console#3264).

The golden files under ``tests/fixtures/calendar/`` are what a calendar client
receives. They were rendered by :func:`shared.ical.build_invitation` and read by
eye against RFC 5545/5546 and the live-proven shape (vfy_01M55T51PV1YB4D56PW7KK8WK0,
vfy_01M55THDN2KSHQ8YKAN0QQKPTE, vfy_01M55TK7D5M02RNYGZ85WZAQR5). A change to the
renderer that changes a byte fails here; regenerate only on purpose, with
``UPDATE_CALENDAR_GOLDEN=1``, and read the diff.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from shared import ical

GOLDEN = Path(__file__).parent / "fixtures" / "calendar"
NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
ORGANIZER = "couples-demo@agentmail.to"
ANA = ical.Attendee("ana@example.com", "Ana")
BEN = ical.Attendee("ben@example.com", "Ben, Jr.")
UID = "3f1c8f2e-1111-4a5b-9c0d-000000000001@smd.services"


def _date_night(sequence: int, start: str = "2026-10-24T18:30") -> ical.Event:
    return ical.Event(
        uid=UID,
        sequence=sequence,
        title="Date night",
        when=ical.resolve_when(start, None, "America/Phoenix", recurring=False),
        organizer=ORGANIZER,
        attendees=(ANA, BEN),
        location="Pizzeria Bianco, Phoenix",
        reminders=(120,),
    )


CASES: dict[str, tuple[ical.Event, str]] = {
    "create.ics": (_date_night(0), ical.METHOD_REQUEST),
    "update.ics": (_date_night(1, "2026-10-24T19:30"), ical.METHOD_REQUEST),
    "cancel.ics": (_date_night(2, "2026-10-24T19:30"), ical.METHOD_CANCEL),
    "all-day-yearly.ics": (
        ical.Event(
            uid="3f1c8f2e-2222-4a5b-9c0d-000000000002@smd.services",
            sequence=0,
            title="Anniversary",
            when=ical.resolve_when("2026-10-30", None, None, recurring=True),
            organizer=ORGANIZER,
            attendees=(ANA, BEN),
            rrule=ical.parse_rrule("FREQ=YEARLY"),
        ),
        ical.METHOD_REQUEST,
    ),
    "recurring-weekly-dst.ics": (
        ical.Event(
            uid="3f1c8f2e-3333-4a5b-9c0d-000000000003@smd.services",
            sequence=0,
            title="Sunday check-in",
            when=ical.resolve_when(
                "2026-11-01T09:00", "2026-11-01T09:30", "America/New_York", recurring=True
            ),
            organizer=ORGANIZER,
            attendees=(ANA,),
            description="Ten minutes, phones down.\nWhat went well; what is next.",
            rrule=ical.parse_rrule("FREQ=WEEKLY;BYDAY=SU;COUNT=12"),
        ),
        ical.METHOD_REQUEST,
    ),
    "utc-one-off.ics": (
        ical.Event(
            uid="3f1c8f2e-4444-4a5b-9c0d-000000000004@smd.services",
            sequence=0,
            title="Call the venue",
            when=ical.resolve_when("2026-10-20T16:00:00-07:00", None, None, recurring=False),
            organizer=ORGANIZER,
            attendees=(BEN,),
        ),
        ical.METHOD_REQUEST,
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_invitation_matches_its_golden_file(name: str) -> None:
    event, method = CASES[name]
    text = ical.build_invitation(event, method, now=NOW)
    path = GOLDEN / name
    if os.environ.get("UPDATE_CALENDAR_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    assert path.read_bytes() == text.encode("utf-8")


def test_update_and_cancel_keep_the_uid_and_raise_the_sequence() -> None:
    parsed = [
        ical.parse_invitation((GOLDEN / n).read_bytes().decode())[0]
        for n in ("create.ics", "update.ics", "cancel.ics")
    ]
    assert {p.uid for p in parsed} == {UID}
    assert [p.sequence for p in parsed] == [0, 1, 2]
    assert [p.method for p in parsed] == ["REQUEST", "REQUEST", "CANCEL"]
    assert parsed[2].status == "CANCELLED"


def test_every_line_is_crlf_and_at_most_75_octets() -> None:
    for path in GOLDEN.glob("*.ics"):
        data = path.read_bytes()
        assert data.endswith(b"\r\n")
        assert b"\n" not in data.replace(b"\r\n", b""), path.name
        for line in data.split(b"\r\n"):
            assert len(line) <= 75, (path.name, line)


def test_folding_never_splits_a_multibyte_character() -> None:
    line = "DESCRIPTION:" + "é" * 80
    folded = ical.fold(line)
    for part in folded.split("\r\n"):
        assert len(part.encode("utf-8")) <= 75
        part.encode("utf-8").decode("utf-8")  # each piece is whole UTF-8
    assert folded.replace("\r\n ", "") == line


def test_text_is_escaped_and_round_trips() -> None:
    raw = "a, b; c \\ d\nnext"
    assert ical.escape_text(raw) == "a\\, b\\; c \\\\ d\\nnext"
    assert ical.unescape_text(ical.escape_text(raw)) == raw


def test_all_day_is_value_date_with_an_exclusive_end() -> None:
    text = (GOLDEN / "all-day-yearly.ics").read_bytes().decode()
    assert "DTSTART;VALUE=DATE:20261030\r\n" in text
    assert "DTEND;VALUE=DATE:20261031\r\n" in text
    assert "RRULE:FREQ=YEARLY\r\n" in text


def test_a_repeating_timed_event_carries_its_zone_across_the_dst_change() -> None:
    text = (GOLDEN / "recurring-weekly-dst.ics").read_bytes().decode()
    assert "DTSTART;TZID=America/New_York:20261101T090000\r\n" in text
    assert "BEGIN:VTIMEZONE\r\nTZID:America/New_York\r\n" in text
    # The 2026 fall-back is the day of the first occurrence.
    assert "DTSTART:20261101T020000\r\nTZOFFSETFROM:-0400\r\nTZOFFSETTO:-0500\r\n" in text


def test_a_one_off_with_only_an_offset_is_written_in_utc() -> None:
    text = (GOLDEN / "utc-one-off.ics").read_bytes().decode()
    assert "DTSTART:20261020T230000Z\r\n" in text
    assert "VTIMEZONE" not in text


def test_cancel_carries_no_alarm_and_says_cancelled() -> None:
    text = (GOLDEN / "cancel.ics").read_bytes().decode()
    assert "METHOD:CANCEL\r\n" in text and "STATUS:CANCELLED\r\n" in text
    assert "VALARM" not in text


@pytest.mark.parametrize(
    ("start", "end", "tz", "recurring", "why"),
    [
        ("2026-10-24T18:30", None, None, False, "names no time zone"),
        ("2026-10-24T18:30-07:00", None, None, True, "needs a timezone name"),
        ("2026-10-24", "2026-10-24", None, False, "must be after its start"),
        (
            "2026-10-24T18:30",
            "2026-10-24T17:00",
            "America/Phoenix",
            False,
            "must be after its start",
        ),
        ("2026-10-24T18:30", None, "Mars/Olympus", False, "not a time zone"),
        ("tomorrow", None, None, False, "not a date"),
    ],
)
def test_times_that_cannot_be_written_are_refused(start, end, tz, recurring, why) -> None:
    with pytest.raises(ical.InvitationError, match=why):
        ical.resolve_when(start, end, tz, recurring=recurring)


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=HOURLY",
        "FREQ=WEEKLY;BYSETPOS=1",
        "FREQ=WEEKLY;COUNT=2;UNTIL=20270101",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ",
    ],
)
def test_a_recurrence_outside_the_closed_grammar_is_refused(rule: str) -> None:
    with pytest.raises(ical.InvitationError):
        ical.parse_rrule(rule)


def test_a_recurrence_inside_the_grammar_is_normalized() -> None:
    assert (
        ical.parse_rrule("rrule:byday=MO,WE;freq=weekly;interval=2")
        == "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"
    )


def test_a_received_invitation_parses_to_plain_fields() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:g-1@google.com\r\n"
        "SEQUENCE:2\r\nDTSTART;TZID=America/Phoenix:20261101T100000\r\nDTEND;TZID=America/Phoenix:2026"
        "1101T110000\r\nSUMMARY:Brunch\\, with Sam\r\nORGANIZER;CN=Ana:mailto:ANA@example.com\r\n"
        'ATTENDEE;CN="Agent: couples":mailto:couples-demo@agentmail.to\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
    )
    (got,) = ical.parse_invitation(text)
    assert (got.uid, got.sequence, got.title, got.organizer) == (
        "g-1@google.com",
        2,
        "Brunch, with Sam",
        "ana@example.com",
    )
    assert got.start == "2026-11-01T10:00:00-07:00"
    assert got.attendees == ["couples-demo@agentmail.to"]


def test_a_reply_or_publish_is_not_kept() -> None:
    for method in ("REPLY", "PUBLISH", ""):
        text = f"BEGIN:VCALENDAR\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\nUID:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        assert ical.parse_invitation(text) == []
