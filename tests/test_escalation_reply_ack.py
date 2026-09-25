"""Plain-word replies to a numbered deadline digest (``escalation_reply_ack``).

The contract sentence: a rostered staffer replies "got it on 1" to a digest,
and exactly item 1 gets an ``acked`` ledger row, with a confirmation rendered in
code. The properties pinned here:

* the model supplies NOTHING (empty schema; arguments are ignored);
* the digest is found by THREAD on the verified origin, and the numbers are read
  from the reader's own words only, so quoted history never acks anything;
* one unknown number writes nothing (all or nothing);
* a group number acks every row in its group;
* every refusal is its own status with its own plain question (or silence);
* ``acked_by`` comes from the verified sender and the firm's authored users.
"""

from __future__ import annotations

import json

import pytest

from shared import escalation_ledger, inbound
from tests.conftest import load_plugin

SESSION = "sess-reply-1"
THREAD = "thread-digest-0925"
REF = "d" * 32
ADDRESS = "dana@firm.example"
NAME = "Dana Whitfield"

QUOTED_DIGEST = (
    '1. matter 2026-PI-101, "Discovery scan review", due 2026-09-20 (overdue by 5 days)\n'
    '2. matter 2026-PI-102, "Medical records request", due 2026-09-21\n'
    "3. matter 2026-PI-103: 2 more open items\n"
)


def _raise(
    key: str,
    n: int | None,
    *,
    event: str = "fired",
    thread: str = THREAD,
    ref=REF,
    token: str | None = "derived",
    ts: str = "2026-09-25T14:00:01Z",
    v: int = 2,
) -> dict:
    row = {
        "v": v,
        "ts": ts,
        "id": f"id-{key}-{ts}",
        "skill": "deadline-miss-escalator",
        "matter_id": f"matter-{key}",
        "item_key": key,
        "event": event,
        "attempt": 2 if event == "chased" else 1,
        "token": f"ACK-{key.upper()[:6]}" if token == "derived" else token,
        "session_id": "cron_deadline-miss-escalator_20260925_140000",
    }
    if thread is not None:
        row["thread_ref"] = thread
    if ref is not None:
        row["dispatch_ref"] = ref
    if n is not None:
        row["n"] = n
    return row


def _digest_rows() -> list[dict]:
    """Items 1 and 2 are single rows; item 3 is a per-matter group of two."""
    return [
        _raise("aaaaaa", 1),
        _raise("bbbbbb", 2, event="chased"),
        _raise("cccccc", 3),
        _raise("dddddd", 3, token=None),
    ]


@pytest.fixture
def env(monkeypatch, tmp_path):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": f"evt-{len(requests)}"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    ledger = tmp_path / "escalation-ledger.jsonl"
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger))
    config = {"scope": {"inbound_allow_from": [ADDRESS]}, "users": []}
    real = plugin.CustomerConfig

    class _Config:
        @staticmethod
        def from_volume(*_a, **_k):
            return real(config)

    monkeypatch.setattr(plugin, "CustomerConfig", _Config)

    class Env:
        pass

    e = Env()
    e.plugin = plugin
    e.requests = requests
    e.ledger = ledger
    e.config = config
    e.write_ledger = lambda rows: ledger.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    e.write_ledger(_digest_rows())
    return e


def _reply(
    text: str,
    *,
    thread: str = THREAD,
    address: str = ADDRESS,
    auto_submitted: bool = False,
    session: str = SESSION,
) -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        session,
        inbound.InboundOrigin(
            sender_address=address,
            message_id=f"msg-{session}",
            conversation_id=thread,
            reply_text=text,
            auto_submitted=auto_submitted,
        ),
    )


def _call(env, args: dict | None = None, session: str = SESSION) -> dict:
    return json.loads(env.plugin._escalation_reply_ack(args or {}, session_id=session))


def _acked(env) -> list[dict]:
    return [
        r["event"]
        for r in env.requests
        if r.get("action") == "escalation_event_append" and r["event"]["event"] == "acked"
    ]


# ---------------------------------------------------------------------------
# The act
# ---------------------------------------------------------------------------


def test_got_it_on_1_acks_exactly_item_1(env):
    _reply("got it on 1")
    out = _call(env)
    assert out["status"] == "acked"
    assert out["acked"] == [1]
    assert out["still_open"] == [2, 3]
    assert out["confirmation_text"] == "Got it: 1 is quiet for now. Still open: 2 and 3."
    [event] = _acked(env)
    # The identity is the raise row's, copied: key, token, matter, skill, attempt.
    assert event["item_key"] == "aaaaaa"
    assert event["token"] == "ACK-AAAAAA"
    assert event["matter_id"] == "matter-aaaaaa"
    assert event["skill"] == "deadline-miss-escalator"
    assert event["attempt"] == 1
    assert event["session_id"] == SESSION
    assert event["ts"] is None  # the broker stamps; nobody here can backdate
    assert "thread_ref" not in event and "n" not in event and "dispatch_ref" not in event


def test_several_numbers_ack_each(env):
    _reply("Done with 1 and 2, thanks!")
    out = _call(env)
    assert out["acked"] == [1, 2]
    assert out["still_open"] == [3]
    assert out["confirmation_text"] == "Got it: 1 and 2 are quiet for now. Still open: 3."
    assert {e["item_key"] for e in _acked(env)} == {"aaaaaa", "bbbbbb"}


def test_a_group_number_acks_every_row_in_the_group(env):
    _reply("3")
    out = _call(env)
    assert out["acked"] == [3]
    assert {e["item_key"] for e in _acked(env)} == {"cccccc", "dddddd"}
    # The idless row acks by item_key with no token, exactly as it was raised.
    idless = next(e for e in _acked(env) if e["item_key"] == "dddddd")
    assert idless["token"] is None


def test_all_acks_every_row(env):
    _reply("all of them, thank you")
    out = _call(env)
    assert out["status"] == "acked"
    assert out["acked"] == [1, 2, 3]
    assert out["still_open"] == []
    assert out["confirmation_text"] == "Got it: all 3 are quiet for now."
    assert {e["item_key"] for e in _acked(env)} == {"aaaaaa", "bbbbbb", "cccccc", "dddddd"}


def test_the_acker_is_the_verified_sender_by_the_firms_authored_name(env):
    env.config["users"] = [{"email": ADDRESS, "full_name": NAME}]
    _reply("1")
    _call(env)
    [event] = _acked(env)
    assert event["acked_by"]["name"] == NAME
    assert len(event["acked_by"]["key"]) == 64


def test_an_unauthored_rostered_sender_acks_unattributed(env):
    _reply("1")
    _call(env)
    [event] = _acked(env)
    assert "acked_by" not in event


def test_numbers_already_quiet_are_not_reported_open(env):
    rows = _digest_rows()
    rows.append(
        {
            **_raise("bbbbbb", None, ref=None, thread=None),
            "event": "acked",
            "ts": "2026-09-25T15:00:00Z",
            "id": "ack-b",
        }
    )
    env.write_ledger(rows)
    _reply("1")
    out = _call(env)
    assert out["still_open"] == [3]


# ---------------------------------------------------------------------------
# The model supplies nothing
# ---------------------------------------------------------------------------


def test_the_schema_takes_no_arguments(env):
    schema = env.plugin.TOOLS["escalation_reply_ack"][1]
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
    assert schema["required"] == []


def test_model_supplied_arguments_change_nothing(env):
    _reply("got 1")
    out = _call(
        env,
        {
            "numbers": [2, 3],
            "all": True,
            "item_key": "bbbbbb",
            "token": "ACK-BBBBBB",
            "session_id": "some-other-session",
            "thread_ref": "some-other-thread",
            "reply_text": "all",
        },
    )
    assert out["acked"] == [1]
    assert [e["item_key"] for e in _acked(env)] == ["aaaaaa"]
    assert _acked(env)[0]["session_id"] == SESSION


# ---------------------------------------------------------------------------
# Quoted history is not the reader's words
# ---------------------------------------------------------------------------


def test_thanks_above_the_quoted_digest_parses_to_nothing(env):
    """reply_text is the reader's own words; the provider stripped the quote.
    The numbers in the quoted list are nowhere the parser looks."""
    inbound.SESSION_INBOUND_ORIGIN.record(
        SESSION,
        inbound.InboundOrigin(
            sender_address=ADDRESS,
            message_id="msg-1",
            content_digest=inbound.content_digest("thanks\n\n> " + QUOTED_DIGEST),
            conversation_id=THREAD,
            reply_text="thanks",
        ),
    )
    out = _call(env)
    assert out["status"] == "nothing_parsed"
    assert out["acked"] == []
    assert out["confirmation_text"] == (
        "Which numbers do you have? Reply with the numbers from the list, or say all."
    )
    assert env.requests == []


# ---------------------------------------------------------------------------
# Refusals: each writes nothing
# ---------------------------------------------------------------------------


def test_no_verified_reply(env):
    out = _call(env)
    assert out["status"] == "no_verified_reply"
    assert out["confirmation_text"] == ""
    assert env.requests == []


def test_a_verified_origin_on_another_session_is_not_this_reply(env):
    _reply("1", session="another-session")
    assert _call(env)["status"] == "no_verified_reply"
    assert env.requests == []


def test_auto_reply(env):
    _reply(
        "I am out of the office until Monday. For 1 urgent matter call 602.", auto_submitted=True
    )
    out = _call(env)
    assert out["status"] == "auto_reply"
    assert out["confirmation_text"] == ""  # never answer a machine: that is a loop
    assert env.requests == []


def test_not_rostered(env):
    _reply("1", address="stranger@elsewhere.example")
    out = _call(env)
    assert out["status"] == "not_rostered"
    assert out["confirmation_text"] == ""
    assert env.requests == []


def test_an_unreadable_roster_authorizes_nobody(env, monkeypatch):
    class _Broken:
        @staticmethod
        def from_volume(*_a, **_k):
            raise OSError("volume unreadable")

    monkeypatch.setattr(env.plugin, "CustomerConfig", _Broken)
    _reply("1")
    assert _call(env)["status"] == "not_rostered"
    assert env.requests == []


def test_not_a_digest_reply_when_the_thread_carries_no_numbered_raise(env):
    _reply("1", thread="some-unrelated-thread")
    out = _call(env)
    assert out["status"] == "not_a_digest_reply"
    assert out["confirmation_text"] == (
        "I couldn't tell which list you're answering. Reply directly to the deadline email."
    )
    assert env.requests == []


def test_not_a_digest_reply_without_a_thread_id(env):
    _reply("1", thread="")
    assert _call(env)["status"] == "not_a_digest_reply"


def test_unnumbered_raises_on_the_thread_are_not_a_digest(env):
    """A digest sent before numbering (no n) cannot be answered by number; its
    ACK codes still work through escalation_append."""
    env.write_ledger([_raise("aaaaaa", None)])
    _reply("1")
    assert _call(env)["status"] == "not_a_digest_reply"


def test_pre_identity_epoch_raises_name_nothing(env):
    env.write_ledger([_raise("aaaaaa", 1, v=1)])
    _reply("1")
    assert _call(env)["status"] == "not_a_digest_reply"


def test_ambiguous_thread(env):
    rows = _digest_rows() + [_raise("eeeeee", 1, ref="e" * 32, ts="2026-09-26T14:00:01Z")]
    env.write_ledger(rows)
    _reply("1")
    out = _call(env)
    assert out["status"] == "ambiguous_thread"
    assert "most recent deadline email" in out["confirmation_text"]
    assert env.requests == []


def test_nothing_parsed(env):
    _reply("ok, on it")
    out = _call(env)
    assert out["status"] == "nothing_parsed"
    assert out["still_open"] == [1, 2, 3]
    assert env.requests == []


def test_unknown_numbers_write_nothing_at_all(env):
    """All or nothing: 1 is valid, 9 is not, and neither is written."""
    _reply("got 1 and 9")
    out = _call(env)
    assert out["status"] == "unknown_numbers"
    assert out["acked"] == []
    assert out["confirmation_text"] == "I don't see 9 on that list. The numbers were 1 to 3."
    assert env.requests == []


def test_unknown_numbers_names_each(env):
    _reply("9, 12")
    out = _call(env)
    assert out["confirmation_text"] == "I don't see 9 or 12 on that list. The numbers were 1 to 3."


def test_a_broker_refusal_is_reported_not_claimed(env, monkeypatch):
    def refuse(payload):
        env.requests.append(payload)
        if payload["event"]["item_key"] == "bbbbbb":
            return {"ok": False, "error": "no prior raise"}
        return {"ok": True}

    monkeypatch.setattr(env.plugin, "_broker_request", refuse)
    _reply("1 and 2")
    out = _call(env)
    assert out["status"] == "acked"
    assert out["acked"] == [1]
    assert out["still_open"] == [2, 3]
    assert "I couldn't record 2 just now" in out["confirmation_text"]


def test_nothing_recorded_when_every_write_fails(env, monkeypatch):
    def down(payload):
        raise ConnectionRefusedError("broker down")

    monkeypatch.setattr(env.plugin, "_broker_request", down)
    _reply("1")
    out = _call(env)
    assert out["status"] == "not_recorded"
    assert out["acked"] == []
    assert "nothing was marked" in out["confirmation_text"]


def test_no_confirmation_text_carries_an_em_dash(env):
    reply_items = env.plugin.reply_items
    for status in (
        "acked",
        "no_verified_reply",
        "auto_reply",
        "not_rostered",
        "not_a_digest_reply",
        "ambiguous_thread",
        "nothing_parsed",
        "unknown_numbers",
        "not_recorded",
    ):
        text = reply_items.render_confirmation(
            status, acked=[1, 2], still_open=[3], failed=[4], unknown=[9], valid=[1, 2, 3]
        )
        assert "—" not in text and "–" not in text


def test_the_legacy_ack_token_path_is_unchanged(env):
    """Codes already in inboxes keep working through escalation_append."""
    out = json.loads(
        env.plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "acked",
                "attempt": 0,
                "ack_token": "ACK-AAAAAA",
            },
            session_id=SESSION,
        )
    )
    assert out["ok"] is True
    assert out["item_key"] == "aaaaaa"


# ---------------------------------------------------------------------------
# parse_reply_items (pure)
# ---------------------------------------------------------------------------


def _parse(text):
    return load_plugin("hermes-smd-escalation").reply_items.parse_reply_items(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("got it on 1", {"all": False, "numbers": [1]}),
        ("1, 3 and 5", {"all": False, "numbers": [1, 3, 5]}),
        ("#2 done", {"all": False, "numbers": [2]}),
        ("3 3 3", {"all": False, "numbers": [3]}),
        ("all", {"all": True, "numbers": []}),
        ("All of them", {"all": True, "numbers": []}),
        ("every one is handled", {"all": True, "numbers": []}),
        ("everything", {"all": True, "numbers": []}),
        ("thanks", {"all": False, "numbers": []}),
        ("", {"all": False, "numbers": []}),
        (None, {"all": False, "numbers": []}),
        (12, {"all": False, "numbers": []}),
        # Out of range or not a standalone token.
        ("0", {"all": False, "numbers": []}),
        ("1000", {"all": False, "numbers": []}),
        ("2nd one", {"all": False, "numbers": []}),
        ("item12", {"all": False, "numbers": []}),
        ("01", {"all": False, "numbers": []}),
        # Phone numbers, dates, times and decimals are not items.
        ("call 602-555-1234", {"all": False, "numbers": []}),
        ("due 9/25", {"all": False, "numbers": []}),
        ("at 3:30", {"all": False, "numbers": []}),
        ("1.5 hours", {"all": False, "numbers": []}),
        # Negated numbers are not confirmed.
        ("got 1, not 2", {"all": False, "numbers": [1]}),
        ("1 and 2 are done but 3 is waiting", {"all": False, "numbers": [1, 2]}),
        ("still working on 2", {"all": False, "numbers": []}),
        ("no problem, got 1", {"all": False, "numbers": [1]}),
        # "all" as a greeting is not "all of them".
        ("Hi all, got 2", {"all": False, "numbers": [2]}),
        ("thanks all", {"all": False, "numbers": []}),
        ("not at all", {"all": False, "numbers": []}),
        ("that's all for now, got 1", {"all": False, "numbers": [1]}),
        ("1 is all I have", {"all": False, "numbers": [1]}),
        # "all except 2" is an exception the parser will not model: ask.
        ("all except 2", {"all": False, "numbers": []}),
        # The signature is not the reader's words.
        (
            "got 1\n\nThanks,\nDana Whitfield\nSuite 200\n(602) 555-1234",
            {"all": False, "numbers": [1]},
        ),
        ("1\n-- \nDana | 602 555 1234", {"all": False, "numbers": [1]}),
        ("2\n\nSent from my iPhone", {"all": False, "numbers": [2]}),
    ],
)
def test_parse_reply_items(text, expected):
    assert _parse(text) == expected


def test_parse_is_pure():
    text = "got 1 and 3"
    assert _parse(text) == _parse(text)
    assert text == "got 1 and 3"


def test_the_ledger_twin_is_not_edited_for_this_feature():
    """The broker's validate_append accepts an acked row as written above; the
    twin gained no field (tests/test_escalation_ledger_sync.py guards bytes)."""
    event = escalation_ledger.make_event(
        skill="deadline-miss-escalator",
        matter_id="m",
        item_key="aaaaaa",
        event="acked",
        attempt=1,
        token="ACK-AAAAAA",
    )
    escalation_ledger.validate_append([_raise("aaaaaa", 1)], event, send_witness=lambda _e: True)
