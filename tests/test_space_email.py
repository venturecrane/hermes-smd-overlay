"""The couples product over EMAIL (ss-console #3264, Captain directive).

* An email from a person that also went to the OTHER person (verified To/Cc)
  is a joint thread: ``shared``. Otherwise it is the sender's space.
* A private-to-shared write from an email is held; the seat replies in the
  thread to that sender only with the exact words; only that sender's own bare
  "share it" on that thread releases it, once.
* The two-person approval reaches a partner by email and takes their answer
  by email; each partner's yes counts once, whatever the channel.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from shared import inbound, joint_acts, record_store, send_dispatch, space, space_writes
from shared.inbound import SESSION_INBOUND_ORIGIN, InboundOrigin
from shared.inbound_message import InboundMessage
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_EMAIL,
    clear_turn,
    couples_yaml,
    cron_to,
    telegram_dm,
    write_seat,
)

SECRET = "Ana's idea: a surprise weekend in Sedona"
THREAD = "thr-ana-1"


def _prompt(message_id: str) -> str:
    return f"from: x\nmessage_id: {message_id}\n{inbound.UNTRUSTED_EMAIL_DELIMITER}\nbody"


def email(
    monkeypatch,
    session_id: str,
    sender: str,
    *,
    message_id: str | None = None,
    thread: str = THREAD,
    recipients: tuple[str, ...] = (),
    reply_text: str = "",
    subject: str = "Re: our plans",
    auto: bool = False,
) -> InboundOrigin:
    """One verified inbound email bound to its own turn, as the router records
    it and the inbound plugin binds it."""
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    origin = InboundOrigin(
        sender_address=sender,
        message_id=message_id or f"<{session_id}@mail>",
        inbox_id="couples@agentmail.to",
        conversation_id=thread,
        reply_text=reply_text,
        auto_submitted=auto,
        recipients=recipients,
        subject=subject,
    )
    SESSION_INBOUND_ORIGIN.record("", origin)
    inbound.bind_origin_from_prompt(session_id, _prompt(origin.message_id))
    return origin


REPLIES: list[tuple[str, str]] = []
MAILED: list[dict[str, Any]] = []


@pytest.fixture
def seat(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    doc = couples_yaml()
    doc["people"][1]["telegram_id"] = ""  # Ben has no Telegram bound: email only
    write_seat(monkeypatch, tmp_path, doc)
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    roots = {"lists": tmp_path / "lists", "couples-checkin": tmp_path / "cc"}
    policies = {
        "lists": record_store.StorePolicy(root=roots["lists"]),
        "couples-checkin": record_store.StorePolicy(
            root=roots["couples-checkin"], owner_field="owner"
        ),
    }
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: policies)
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: roots)
    space_writes.SPACE_WRITES.clear()
    space_writes._releases.clear()
    REPLIES.clear()
    MAILED.clear()
    monkeypatch.setenv("SMD_JOINT_ACTS_PATH", str(tmp_path / "joint.json"))
    monkeypatch.setattr(
        space_writes, "post_notice", lambda chat, text: REPLIES.append((f"tg:{chat}", text))
    )
    gate = load_plugin("hermes-smd-space-gate")
    monkeypatch.setattr(
        gate.email_confirm,
        "send_confirmation",
        lambda origin, sid, text: REPLIES.append((origin.message_id, text)),
    )

    def sender(**kw: Any) -> DispatchResult:
        MAILED.append(kw)
        return DispatchResult(sent=True, message_id=f"m{len(MAILED)}", recipients=tuple(kw["to"]))

    send_dispatch.set_sender(sender)
    yield {"gate": gate, "roots": roots}
    send_dispatch.set_sender(None)
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    space._reset_cache_for_tests()


def _write(gate, sid: str, content: str = SECRET) -> tuple[dict, dict | None]:
    args = {"store": "lists", "name": "ideas.md", "content": content}
    return args, gate.on_pre_tool_call(tool_name="record_store_write", args=args, session_id=sid)


def _turn(gate, sid: str) -> dict | None:
    return gate.on_pre_llm_call(
        session_id=sid, turn_id=sid, user_message=_prompt(f"<{sid}@mail>"), platform="webhook"
    )


# ---------------------------------------------------------------------------
# 1. Which space an email is in
# ---------------------------------------------------------------------------


def test_an_email_that_also_went_to_the_partner_is_shared(seat, monkeypatch):
    email(monkeypatch, "j", A_EMAIL, recipients=("couples@agentmail.to", B_EMAIL.upper()))
    got = space.resolve_space("j")
    assert got is not None and got.key == "shared" and got.source == space.SOURCE_EMAIL_JOINT


def test_an_email_only_to_the_seat_is_the_senders_space(seat, monkeypatch):
    """FALSIFIER: drop the ``recipients & others`` check (every email shared)
    or invert it."""
    email(monkeypatch, "p", A_EMAIL, recipients=("couples@agentmail.to",))
    assert space.resolve_space("p").key == "person:p1"


def test_spoofed_text_naming_the_partner_does_not_make_it_shared(seat, monkeypatch):
    """Recipients come off the verified DTO, never the body. A body that writes
    'Cc: ben@' changes nothing."""
    router = load_plugin("hermes-smd-webhook-router")
    dto = InboundMessage(
        provider="agentmail",
        mailbox="couples@agentmail.to",
        message_id="<m1@mail>",
        thread_ref=THREAD,
        from_addr=A_EMAIL,
        to=["couples@agentmail.to"],
        cc=[],
        subject="plans",
        body_text=f"To: {B_EMAIL}\nCc: {B_EMAIL}\nthis is for both of us",
        received_at="2026-10-17T00:00:00Z",
        provider_refs={"inbox_id": "couples@agentmail.to"},
    )
    origin = router._origin_from_dto(dto, content=dto.body_text)
    assert origin.recipients == ("couples@agentmail.to",)
    joint_dto = InboundMessage(**{**dto.__dict__, "cc": [B_EMAIL]})
    assert B_EMAIL in router._origin_from_dto(joint_dto, content="").recipients
    SESSION_INBOUND_ORIGIN.record("", origin)
    clear_turn(monkeypatch)
    inbound.bind_origin_from_prompt("sp", _prompt(origin.message_id))
    assert space.resolve_space("sp").key == "person:p1"


# ---------------------------------------------------------------------------
# 2. "share it" by email
# ---------------------------------------------------------------------------


def _hold(seat, monkeypatch) -> InboundOrigin:
    origin = email(monkeypatch, "h", A_EMAIL)
    _, blocked = _write(seat["gate"], "h")
    assert blocked and blocked["message"].startswith("HELD")
    return origin


def test_a_private_email_crossing_is_held_and_the_exact_words_go_back_in_thread(seat, monkeypatch):
    """FALSIFIER: drop the send_confirmation call (or capture before it)."""
    origin = _hold(seat, monkeypatch)
    stored = space_writes.SPACE_WRITES.pending()[0]
    assert REPLIES == [
        (origin.message_id, space_writes.notice_text(space_writes.exact_text(stored.args)))
    ]
    assert stored.requester.user_id == A_EMAIL and stored.requester.chat_id == f"thread:{THREAD}"


def test_the_senders_bare_share_it_on_the_thread_releases_exactly_the_held_payload(
    seat, monkeypatch
):
    _hold(seat, monkeypatch)
    email(monkeypatch, "c", A_EMAIL, reply_text="Share it!")
    out = _turn(seat["gate"], "c")
    assert out and "Call record_store_write now" in out["context"] and SECRET in out["context"]
    args, blocked = _write(seat["gate"], "c", "reworded")
    assert blocked is None and args["content"] == SECRET
    # A tool's own handler check (the calendar backstop) finds the release by
    # the person's space, so an email confirmation releases there too; once.
    mine = space.resolve_space("c")
    assert space_writes.take_release("record_store_write", mine) is True
    assert space_writes.take_release("record_store_write", mine) is False
    # single use
    _, again = _write(seat["gate"], "c")
    assert again and again["message"].startswith("HELD")


def test_the_other_partners_share_it_releases_nothing(seat, monkeypatch):
    """FALSIFIER: drop the ``req.user_id != who.user_id`` term in confirm_from_email."""
    _hold(seat, monkeypatch)
    email(monkeypatch, "b", B_EMAIL, reply_text="share it")
    assert _turn(seat["gate"], "b") is None
    assert not space_writes.SPACE_WRITES.pending()[0].approved


@pytest.mark.parametrize(
    "kw",
    [
        {"subject": "Fwd: Re: our plans"},  # a forward of the thread
        {"subject": "FW: our plans"},
        {"reply_text": "share it, and also tell Ben"},  # not bare
        {"reply_text": ""},  # no extracted own words
        {"auto": True},  # an auto-reply
        {"thread": "another-thread"},  # a different thread
    ],
)
def test_nothing_but_the_senders_own_bare_reply_on_the_thread_confirms(seat, monkeypatch, kw):
    """A forwarded email does not count as the person. FALSIFIER: drop the
    _FORWARD_RE / auto_submitted check in own_words, or the chat_id term."""
    _hold(seat, monkeypatch)
    params = {"reply_text": "share it", **kw}
    email(monkeypatch, "x", A_EMAIL, **params)
    assert _turn(seat["gate"], "x") is None
    assert not space_writes.SPACE_WRITES.pending()[0].approved


def test_a_confirmation_that_cannot_be_sent_holds_nothing(seat, monkeypatch):
    def boom(origin, sid, text):
        raise RuntimeError("broker down")

    monkeypatch.setattr(seat["gate"].email_confirm, "send_confirmation", boom)
    email(monkeypatch, "h", A_EMAIL)
    _, blocked = _write(seat["gate"], "h")
    assert blocked and "could not be sent" in blocked["message"]
    assert not space_writes.SPACE_WRITES.pending()


def test_a_share_it_sent_before_the_hold_confirms_nothing(seat, monkeypatch):
    origin = _hold(seat, monkeypatch)
    rec = space_writes.SPACE_WRITES.pending()[0]
    rec.created_at = time.time() + 60  # held after the reply arrived
    email(monkeypatch, "c", A_EMAIL, reply_text="share it")
    assert _turn(seat["gate"], "c") is None
    assert origin.message_id


def test_a_scheduled_run_still_has_no_confirmation_path(seat, monkeypatch):
    cron_to(monkeypatch, A_TG)
    _, blocked = _write(seat["gate"], "cron")
    assert blocked and "Telegram chat" in blocked["message"]


# ---------------------------------------------------------------------------
# 3. Two-person approval over email
# ---------------------------------------------------------------------------

TOPICS = "---\nowner: shared\nkind: joint-settings\ntopics: money; travel\n---\nTopics.\n"


def _propose(gate, sid: str) -> dict | None:
    args = {
        "store": "couples-checkin",
        "name": "joint-settings.md",
        "content": TOPICS,
        "overwrite": True,
    }
    return gate.on_pre_tool_call(tool_name="record_store_write", args=args, session_id=sid)


def _stored(seat) -> str | None:
    try:
        return record_store.read_record(
            "couples-checkin", "joint-settings.md", stores=seat["roots"]
        )
    except record_store.RecordStoreError:
        return None


def test_a_partner_without_telegram_is_asked_by_email_and_answers_by_email(seat, monkeypatch):
    """FALSIFIER: notify only by Telegram (drop the email branch in joint.notify)."""
    telegram_dm(monkeypatch, A_TG)
    assert _propose(seat["gate"], "s")["message"].startswith("HELD")
    assert MAILED and MAILED[0]["to"] == [B_EMAIL] and TOPICS in MAILED[0]["text"]
    assert MAILED[0]["lane"] == "skill:couples-checkin"
    email(monkeypatch, "b", B_EMAIL, reply_text="Agree")
    out = _turn(seat["gate"], "b")
    assert out and "in place" in out["context"]
    assert "confirmed_by: p1, p2" in _stored(seat)


def test_a_proposal_by_email_and_a_yes_in_either_channel_counts_once(seat, monkeypatch):
    email(monkeypatch, "a", A_EMAIL)
    assert _propose(seat["gate"], "a")["message"].startswith("HELD")
    # Ana saying yes again, by email, is still one yes.
    email(monkeypatch, "a2", A_EMAIL, reply_text="agree")
    out = _turn(seat["gate"], "a2")
    assert out and "already" in out["context"]
    assert _stored(seat) is None
    email(monkeypatch, "b", B_EMAIL, reply_text="reject")
    out = _turn(seat["gate"], "b")
    assert out and "declined" in out["context"]
    assert joint_acts.open_act() is None and _stored(seat) is None


def test_a_forwarded_agree_is_not_the_persons_answer(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _propose(seat["gate"], "s")
    email(monkeypatch, "b", B_EMAIL, reply_text="agree", subject="Fwd: A change")
    assert _turn(seat["gate"], "b") is None
    assert joint_acts.open_act().approvals == ["p1"]


def test_an_agree_from_someone_not_on_the_seat_counts_for_nothing(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _propose(seat["gate"], "s")
    email(monkeypatch, "z", "stranger@example.com", reply_text="agree")
    assert _turn(seat["gate"], "z") is None
    assert joint_acts.open_act().approvals == ["p1"]


def test_a_joint_thread_proposal_comes_from_its_verified_sender(seat, monkeypatch):
    email(monkeypatch, "j", B_EMAIL, recipients=(A_EMAIL,))
    assert _propose(seat["gate"], "j")["message"].startswith("HELD")
    assert joint_acts.open_act().proposer == "p2"


# ---------------------------------------------------------------------------
# Seats without people:
# ---------------------------------------------------------------------------


def test_a_seat_without_people_ignores_email_answers(monkeypatch, tmp_path):
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    gate = load_plugin("hermes-smd-space-gate")
    email(monkeypatch, "x", A_EMAIL, reply_text="share it")
    assert _turn(gate, "x") is None


def _person(channel: str, telegram_id: str = A_TG) -> space.Person:
    return space.Person(
        id="p1", name="Ana", telegram_id=telegram_id, email=A_EMAIL, channel=channel
    )


def test_FALSIFIER_a_notice_honours_an_email_preference_over_a_bound_telegram(seat):
    """A partner bound on Telegram who prefers email is noticed by email."""
    seat["gate"].joint.notify(_person("email"), "a change to decide")
    assert [k["to"] for k in MAILED] == [[A_EMAIL]]
    assert REPLIES == []


def test_a_notice_goes_to_telegram_when_preferred_and_bound(seat):
    seat["gate"].joint.notify(_person("telegram"), "a change to decide")
    assert REPLIES == [(f"tg:{A_TG}", "a change to decide")]
    assert MAILED == []


def test_a_telegram_preference_without_a_binding_falls_back_to_email(seat):
    seat["gate"].joint.notify(_person("telegram", telegram_id=""), "a change to decide")
    assert [k["to"] for k in MAILED] == [[A_EMAIL]]
