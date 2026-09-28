"""A reply's confirmation names what it quieted, in the words that were sent.

Live evidence (pilot-smokeball, 2026-09-28): "Got it on 1." to a deadline digest
came back "Got it: 1 is quiet for 7 days. Still open: 2." The reader had to go
find the digest to learn what went quiet. The properties pinned here:

* a label is a prefix of the line as SENT (``shared.sent_lines``), captured at
  the send, keyed by that send's ``dispatch_ref``;
* the digest confirmation keeps its numbers and its "quiet for N days" /
  "Still open" sentence, and adds each item's label beside its number;
* a row with no kept label (legacy) renders its bare number;
* no label carries a dash the firm's voice refuses, or runs long;
* unknown-number and nothing-parsed replies are unchanged.
"""

from __future__ import annotations

import json

import pytest

from shared import (
    escalation_ledger,
    identifier_filter,
    inbound,
    prerendered_dispatch,
    provenance,
    send_dispatch,
    sent_lines,
)
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin
from tests.test_prerendered_dispatch import (
    SESSION as CRON_SESSION,
)
from tests.test_prerendered_dispatch import (
    _dispatch_entry,
    _routine,
    _Sender,
    _write_envelope,
)

REPLY_SESSION = "sess-reply-named"
THREAD = "thread-digest-0928"
ADDRESS = "dana@firm.example"

DIGEST_BODY = (
    "## Needs you today (2)\n\n"
    "Most overdue first.\n\n"
    "1. 2026-PI-101 Chen: Send preservation letter to Sunrise Plaza, Mon Sep 21, 2026 "
    "(7 days ago)\n"
    "2. 2026-PI-105 Okafor: Update exhibit list, Fri Oct 2, 2026 (in 4 days)\n"
    "   the task is marked URGENT in Smokeball\n\n"
    "Reply to this email with the numbers you have, or say all. Each one you answer goes "
    "quiet for 7 days; finishing it in Smokeball clears it for good. This is an internal "
    "note; no client was contacted.\n"
)
CHEN = "2026-PI-101 Chen: Send preservation letter to Sunrise Plaza, Mon Sep 21, 2026"
OKAFOR = "2026-PI-105 Okafor: Update exhibit list, Fri Oct 2, 2026"


# ---------------------------------------------------------------------------
# Labels (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "group", "label"),
    [
        # A digest line: the trailing aside is true only the day it was sent.
        (
            "2026-PI-105 Okafor: Final Status Conference, Fri Oct 2, 2026 (in 4 days)",
            None,
            "2026-PI-105 Okafor: Final Status Conference, Fri Oct 2, 2026",
        ),
        # The digest's legacy line shape still names itself.
        (
            'matter 2026-PI-101, "Discovery scan review", due 2026-09-20 (overdue by 5 days)',
            None,
            'matter 2026-PI-101, "Discovery scan review", due 2026-09-20',
        ),
        # A review line: the first sentence, under its group heading. A period
        # inside the quoted task name does not end the sentence.
        (
            '"Call Dr. Reyes re: records", due 2026-09-20. It is overdue. Close it?',
            "matter 2026-PI-101",
            'matter 2026-PI-101: "Call Dr. Reyes re: records", due 2026-09-20',
        ),
        # A line that already starts with its group is not prefixed twice.
        (
            "2026-PI-104: Serve discovery responses",
            "2026-PI-104",
            "2026-PI-104: Serve discovery responses",
        ),
        ("", None, None),
        (None, None, None),
    ],
)
def test_line_label(line, group, label):
    assert sent_lines.line_label(line, group) == label


@pytest.mark.parametrize(
    "label",
    ["x" * (sent_lines.MAX_LABEL + 1), "Chen — letter", "Chen – letter", "Chen - letter"],
)
def test_a_long_or_dashed_line_is_not_a_label(label):
    assert sent_lines.line_label(label) is None
    assert sent_lines.usable(label) is False
    # ...and its number renders bare.
    assert sent_lines.name_numbers([1], {1: label}) == "1"


def test_numbered_lines_names_only_the_numbers_the_rows_carry():
    assert sent_lines.numbered_lines(DIGEST_BODY, {1, 2}) == {1: CHEN, 2: OKAFOR}
    assert sent_lines.numbered_lines(DIGEST_BODY, {2}) == {2: OKAFOR}


def test_name_numbers_keeps_every_number_and_falls_back_per_item():
    assert sent_lines.name_numbers([1, 2, 3], {1: "A", 3: "C"}) == "1 (A), 2 and 3 (C)"
    assert sent_lines.name_numbers([1, 2], {1: "Same", 2: "Same"}) == "1 and 2 (Same)"
    assert sent_lines.name_numbers([4, 9], {}, "or") == "4 or 9"


def test_record_and_read_back_by_dispatch_ref():
    ref = "a" * 32
    assert sent_lines.record(ref, {1: CHEN, 2: "bad — dash"})
    assert sent_lines.labels(ref) == {1: CHEN}
    assert sent_lines.labels("b" * 32) == {}
    assert sent_lines.labels("../etc/passwd") == {}
    assert sent_lines.record("not-a-ref", {1: CHEN}) is False


# ---------------------------------------------------------------------------
# The digest: sent, then answered
# ---------------------------------------------------------------------------


@pytest.fixture
def digest(monkeypatch, tmp_path):
    """Send a two-item digest through the real pre-rendered dispatch, then put
    its raise rows on the ledger the way the broker stamps them."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    written: list[dict] = []
    monkeypatch.setattr(
        prerendered_dispatch, "_broker_request", lambda p: written.append(p) or {"ok": True}
    )
    _routine(monkeypatch)
    appends = [
        {
            "item_key": key,
            "matter_id": matter,
            "event": "fired",
            "attempt": 1,
            "token": None,
            "n": n,
            "snooze_days": 7,
        }
        for n, key, matter in ((1, "a" * 16, "m-101"), (2, "b" * 16, "m-105"))
    ]
    _write_envelope(tmp_path, dispatches=[_dispatch_entry(full_body=DIGEST_BODY, appends=appends)])
    sender = _Sender([DispatchResult(sent=True, message_id="m1")])
    send_dispatch.set_sender(sender)
    try:
        prerendered_dispatch.dispatch_prerendered(CRON_SESSION)
    finally:
        send_dispatch.set_sender(None)
    ref = sender.calls[0]["audit_extra"]["dispatch_ref"]
    ledger = tmp_path / "escalation-ledger.jsonl"
    rows = [
        {**r["event"], "ts": "2026-09-28T14:00:01Z", "id": f"id-{i}", "thread_ref": THREAD}
        for i, r in enumerate(written)
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger))

    plugin = load_plugin("hermes-smd-escalation")
    monkeypatch.setattr(plugin, "_broker_request", lambda p: {"ok": True})
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    real = plugin.CustomerConfig

    class _Config:
        @staticmethod
        def from_volume(*_a, **_k):
            return real({"scope": {"inbound_allow_from": [ADDRESS]}, "users": []})

    monkeypatch.setattr(plugin, "CustomerConfig", _Config)
    return {"plugin": plugin, "ref": ref, "ledger": ledger}


def _answer(digest, text: str) -> dict:
    inbound.SESSION_INBOUND_ORIGIN.record(
        REPLY_SESSION,
        inbound.InboundOrigin(
            sender_address=ADDRESS,
            message_id="msg-r",
            conversation_id=THREAD,
            reply_text=text,
        ),
    )
    return json.loads(digest["plugin"]._escalation_reply_ack({}, session_id=REPLY_SESSION))


def test_the_send_keeps_each_numbered_line_as_it_went_out(digest):
    assert sent_lines.labels(digest["ref"]) == {1: CHEN, 2: OKAFOR}


def test_a_digest_ack_of_1_of_2_names_both(digest):
    out = _answer(digest, "Got it on 1.")
    assert out["status"] == "acked" and out["acked"] == [1] and out["still_open"] == [2]
    # Before: "Got it: 1 is quiet for 7 days. Still open: 2."
    assert out["confirmation_text"] == (
        f"Got it: 1 ({CHEN}) is quiet for 7 days. Still open: 2 ({OKAFOR})."
    )


def test_a_digest_ack_of_all_names_each(digest):
    out = _answer(digest, "all")
    # Before: "Got it: all 2 are quiet for 7 days."
    assert out["confirmation_text"] == (
        f"Got it: 1 ({CHEN}) and 2 ({OKAFOR}) are quiet for 7 days."
    )


def test_the_named_digest_ack_passes_the_identifier_gate(digest):
    text = _answer(digest, "Got it on 1.")["confirmation_text"]
    assert identifier_filter.check(text, identifier_filter.ProvenanceRegister()).unverified
    assert not identifier_filter.check(text, provenance.register_for(REPLY_SESSION)).unverified


def test_a_legacy_digest_with_no_kept_labels_confirms_by_number(digest):
    (sent_lines._dir() / f"{digest['ref']}.json").unlink()
    out = _answer(digest, "Got it on 1.")
    assert out["confirmation_text"] == "Got it: 1 is quiet for 7 days. Still open: 2."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("got it on 9", "I don't see 9 on that list. The numbers were 1 to 2."),
        ("thanks", "Which numbers do you have? Reply with the numbers from the list, or say all."),
    ],
)
def test_unknown_and_unparsed_replies_are_unchanged(digest, text, expected):
    assert _answer(digest, text)["confirmation_text"] == expected


def test_no_confirmation_frame_uses_a_dash(digest):
    for reply in ("Got it on 1.", "all", "got it on 9", "thanks"):
        text = _answer(digest, reply)["confirmation_text"]
        assert "—" not in text and "–" not in text and " - " not in text


def test_a_confirmation_never_quotes_the_digest_aside(digest):
    text = _answer(digest, "all")["confirmation_text"]
    assert "(7 days ago)" not in text and "(in 4 days)" not in text
    assert "URGENT" not in text


def test_escalation_ledger_rows_are_untouched_by_labels(digest):
    # A label is not a ledger fact: no raise row carries one.
    for row in escalation_ledger.read_ledger(str(digest["ledger"])):
        assert not any(isinstance(v, str) and "Sunrise" in v for v in row.values())
