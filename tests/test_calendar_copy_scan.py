"""A calendar invitation meets the copy scans on its words, never its protocol.

The 2026-10-19 incident (hermes-captain-mark, ss-console#3264): a partner asked
for an all-day event every other week on Friday, Saturday and Sunday. The
invitation was refused three times by the outbound scan for "the capitals
'FREQ'": the message body the invitation rides in echoed the raw RFC 5545 rule
(``Repeats: FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU``), so no repeating invitation
could ever be sent from any seat.

The seam: the body is the human-readable frame and says the rule in words
(``ical.describe_rrule``); the iCalendar text rides ``CALENDAR_ARG``, which no
prose scan reads. These tests run the REAL trust sender and its REAL scans.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from shared import calendar_store, ical, provenance, send_dispatch, space
from shared.send_dispatch import CALENDAR_ARG
from tests import couples_fixtures as cf
from tests.conftest import load_plugin
from tests.test_calendar_tools import _couples

EVERY_OTHER_WEEKEND = {
    "title": "Weekend at the cabin",
    "start": "2026-10-23",
    "end": "2026-10-26",
    "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU",
}


@pytest.fixture
def real_gate(monkeypatch, tmp_path):
    """The calendar plugin sending through the trust plugin's out-of-turn
    sender: the policy gate allows (as it did on the seat), the outbound scans
    are the real ones, and the broker records what would leave."""
    monkeypatch.setenv(calendar_store.DB_PATH_ENV, str(tmp_path / "calendar.db"))
    cf.write_seat(monkeypatch, tmp_path, _couples())
    plugin = load_plugin("hermes-smd-calendar")
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "couples-demo")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    wire: list[dict[str, Any]] = []

    def broker_send(body, **kwargs):
        wire.append(body)
        return f"msg-{len(wire)}"

    monkeypatch.setattr(trust.outbound_send.agentmail_broker, "send_message", broker_send)
    send_dispatch.set_sender(trust._dispatch_internal_message)
    yield plugin, wire
    send_dispatch.set_sender(None)
    space._reset_cache_for_tests()


#: What the partner wrote, as the seat read it: the turn's dates are traceable.
REQUEST = (
    "Please put the cabin on our calendar: all day Friday October 23, 2026 "
    "through Sunday October 25, 2026, then every other weekend, no end."
)


def _create(plugin, args: dict[str, Any]) -> dict[str, Any]:
    provenance.record_read(provenance.resolve_session("s1"), REQUEST)
    return json.loads(plugin.tools.run("calendar_invite_create", args, "s1"))


def test_FALSIFIER_a_repeating_all_day_invitation_is_sent(real_gate, monkeypatch) -> None:
    plugin, wire = real_gate
    cf.telegram_group(monkeypatch, cf.A_TG)
    out = _create(plugin, {**EVERY_OTHER_WEEKEND, "attendees": ["p1", "p2"]})
    assert out["sent"] is True, out
    (body,) = wire
    ics = body["calendar"]["ics"].replace("\r\n ", "")
    # The protocol text left intact, on the invitation part only.
    assert "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU\r\n" in ics
    assert "DTSTART;VALUE=DATE:20261023\r\n" in ics
    assert "DTEND;VALUE=DATE:20261026\r\n" in ics
    # The message body says it in words.
    assert "FREQ" not in body["text"] and "BYDAY" not in body["text"]
    assert "Repeats: every 2 weeks on Friday, Saturday and Sunday" in body["text"]
    assert CALENDAR_ARG not in body


def test_a_title_the_scan_would_correct_is_still_refused(real_gate, monkeypatch) -> None:
    """The model's own words (SUMMARY, carried in the subject and body) still
    meet the scan: the fix exempts the protocol, not the invitation."""
    plugin, wire = real_gate
    cf.telegram_group(monkeypatch, cf.A_TG)
    out = _create(
        plugin,
        {**EVERY_OTHER_WEEKEND, "title": "URGENT cabin weekend", "attendees": ["p1", "p2"]},
    )
    assert out["sent"] is False
    assert "'URGENT'" in out["not_sent_because"]
    assert wire == []


def test_a_description_the_scan_would_correct_is_still_refused(real_gate, monkeypatch) -> None:
    plugin, wire = real_gate
    cf.telegram_group(monkeypatch, cf.A_TG)
    out = _create(
        plugin,
        {
            **EVERY_OTHER_WEEKEND,
            "description": "Bring the KEYS, the road closes at dusk",
            "attendees": ["p1", "p2"],
        },
    )
    assert out["sent"] is False
    assert "'KEYS'" in out["not_sent_because"]
    assert wire == []


@pytest.mark.parametrize(
    ("rule", "words"),
    [
        ("FREQ=YEARLY", "every year"),
        ("FREQ=DAILY;COUNT=1", "every day, once"),
        ("FREQ=DAILY;INTERVAL=3;COUNT=10", "every 3 days, 10 times"),
        ("FREQ=WEEKLY;BYDAY=MO", "every week on Monday"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU", "every 2 weeks on Friday, Saturday and Sunday"),
        ("FREQ=MONTHLY;BYDAY=2TU", "every month on the second Tuesday"),
        ("FREQ=MONTHLY;BYDAY=-1FR", "every month on the last Friday"),
        ("FREQ=MONTHLY;BYMONTHDAY=15", "every month on the 15th"),
        ("FREQ=MONTHLY;BYMONTHDAY=-1", "every month on the last day"),
        ("FREQ=MONTHLY;BYMONTHDAY=-2", "every month on the second to last day"),
        ("FREQ=YEARLY;BYMONTH=1,3", "every year in January and March"),
        ("FREQ=WEEKLY;UNTIL=20261231", "every week until Thursday, December 31, 2026"),
        ("FREQ=WEEKLY;UNTIL=20261231T235959Z", "every week until Thursday, December 31, 2026"),
    ],
)
def test_every_rule_in_the_grammar_reads_as_words(rule: str, words: str) -> None:
    assert ical.describe_rrule(ical.parse_rrule(rule)) == words


def test_no_rule_in_the_grammar_reads_as_protocol() -> None:
    """Every key and frequency the grammar admits, described: no capitals word
    survives into the words, so the capitals rule can never fire on them."""
    import re

    for freq in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        for extra in ("", ";INTERVAL=4", ";COUNT=2", ";UNTIL=20270101", ";BYDAY=SU,1MO,-2WE"):
            words = ical.describe_rrule(ical.parse_rrule(f"FREQ={freq}{extra}"))
            assert not re.search(r"\b[A-Z]{2,}\b", words), words
            assert "=" not in words and ";" not in words, words


def test_the_scans_never_read_the_invitation_part(monkeypatch) -> None:
    """Structural: the iCalendar text rides CALENDAR_ARG, which is none of the
    keys a prose scan reads. A protocol-only payload passes every text check
    that would refuse the same text in a body."""
    trust = load_plugin("hermes-smd-trust")
    outbound = trust.outbound
    ics = (
        "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
        "DTSTART;VALUE=DATE:20261023\r\nDTEND;VALUE=DATE:20261026\r\n"
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU\r\nDTSTAMP:20261019T163100Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    payload = {
        "to": [cf.A_EMAIL],
        "subject": "Invitation",
        "text": "See you then.",
        CALENDAR_ARG: {"method": "REQUEST", "ics": ics},
    }
    for key in (
        *outbound._SEND_SCAN_KEYS,
        *outbound._DRAFT_SCAN_KEYS,
        *outbound._CHECKLIST_TEXT_KEYS,
        *outbound._CHECKLIST_HTML_KEYS,
    ):
        assert key != CALENDAR_ARG
    assert ics not in outbound._extract_send_body(payload)
    assert ics not in outbound._extract_draft_scan_text(payload)
    assert all(ics not in t for t in outbound._send_texts(payload))
