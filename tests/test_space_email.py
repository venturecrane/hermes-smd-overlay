"""The couples product over EMAIL (ss-console #3264, Captain directive).

* An email from a person that also went to the OTHER person (verified To/Cc)
  is a joint thread: ``shared``. Otherwise it is the sender's space.
* A partner's request by email to write to the shared space lands as asked:
  nothing is held for a confirmation, nothing waits for the other partner
  (Captain decision 2026-10-19).
* A write from an email whose space cannot be resolved is refused.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import pytest

from shared import inbound, space
from shared.inbound import SESSION_INBOUND_ORIGIN, InboundOrigin
from shared.inbound_message import InboundMessage
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_EMAIL,
    clear_turn,
    couples_yaml,
    cron_to,
    write_seat,
)

SECRET = "Ana's idea: a weekend in Sedona"
THREAD = "thr-ana-1"


def _prompt(message_id: str) -> str:
    return f"from: x\nmessage_id: {message_id}\n{inbound.UNTRUSTED_EMAIL_DELIMITER}\nbody"


def email(
    monkeypatch,
    session_id: str,
    sender: str,
    *,
    thread: str = THREAD,
    recipients: tuple[str, ...] = (),
) -> InboundOrigin:
    """One verified inbound email bound to its own turn, as the router records
    it and the inbound plugin binds it."""
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    origin = InboundOrigin(
        sender_address=sender,
        message_id=f"<{session_id}@mail>",
        inbox_id="couples@agentmail.to",
        conversation_id=thread,
        recipients=recipients,
        subject="Re: our plans",
    )
    SESSION_INBOUND_ORIGIN.record("", origin)
    inbound.bind_origin_from_prompt(session_id, _prompt(origin.message_id))
    return origin


@pytest.fixture
def seat(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    doc = couples_yaml()
    doc["people"][1]["telegram_id"] = ""  # Ben has no Telegram bound: email only
    write_seat(monkeypatch, tmp_path, doc)
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    gate = load_plugin("hermes-smd-space-gate")
    yield {"gate": gate}
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    space._reset_cache_for_tests()


def _call(gate, sid: str, tool: str, args: dict) -> dict | None:
    return gate.on_pre_tool_call(tool_name=tool, args=args, session_id=sid)


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
# 2. A partner's shared write by email lands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("record_store_write", {"store": "lists", "name": "ideas.md", "content": SECRET}),
        ("calendar_invite_create", {"title": "Sedona", "attendees": ["p1", "p2"]}),
        ("calendar_invite_update", {"uid": "e1@x", "title": "Sedona"}),
        ("calendar_invite_cancel", {"uid": "e1@x"}),
        ("mcp_agentmail_send_message", {"to": [B_EMAIL], "text": SECRET}),
        ("workspace_docs_create", {"title": "Sedona"}),
        (
            "record_store_write",
            {
                "store": "couples-checkin",
                "name": "joint-settings.md",
                "content": "---\nowner: shared\nkind: joint-settings\n---\nTopics.\n",
            },
        ),
    ],
)
def test_a_private_email_writing_to_the_shared_space_is_not_held(seat, monkeypatch, tool, args):
    """FALSIFIER: hold or refuse a resolved private turn's write in on_pre_tool_call."""
    email(monkeypatch, "h", A_EMAIL)
    assert space.resolve_space("h").key == "person:p1"
    before = dict(args)
    assert _call(seat["gate"], "h", tool, args) is None
    assert args == before  # nothing is rewritten


def test_a_scheduled_run_may_write_to_shared(seat, monkeypatch):
    """A cron run bound to an authored space was authored by the couple."""
    cron_to(monkeypatch, A_TG)
    args = {"store": "lists", "name": "ideas.md", "content": "x"}
    assert _call(seat["gate"], "cron", "record_store_write", args) is None


def test_an_email_from_someone_not_on_the_seat_cannot_write(seat, monkeypatch):
    """Unresolved space fails closed. FALSIFIER: drop the ``current is None`` block."""
    email(monkeypatch, "z", "stranger@example.com")
    assert space.resolve_space("z") is None
    out = _call(seat["gate"], "z", "calendar_invite_create", {"title": "x"})
    assert out and out["action"] == "block" and "could not be resolved" in out["message"]


# ---------------------------------------------------------------------------
# Seats without people:
# ---------------------------------------------------------------------------


def test_a_seat_without_people_is_untouched(monkeypatch, tmp_path):
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    gate = load_plugin("hermes-smd-space-gate")
    email(monkeypatch, "x", "stranger@example.com")
    assert _call(gate, "x", "calendar_invite_create", {"title": "x"}) is None
    assert _call(gate, "x", "mcp_agentmail_send_message", {"to": [B_EMAIL]}) is None
