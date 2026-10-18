"""The private-to-shared write gate (ADR 0090 R1, ADR 0091; ss-console #3264).

A fact partner A says in A's DM must not reach partner B through a write B
reads unless A, in A's DM, confirms the exact payload. Each falsifier names the
line whose removal makes it fail.
"""

from __future__ import annotations

import json

import pytest

from shared import record_store, space, space_writes
from tests.conftest import FakePluginContext, load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_EMAIL,
    B_TG,
    GROUP,
    OTHER_GROUP,
    clear_turn,
    cron_to,
    email_turn,
    telegram_dm,
    telegram_group,
    write_seat,
)

A_SECRET = "Ana is planning a surprise trip to Lisbon"


#: Every confirmation notice the gate sent: (chat id, text).
POSTED: list[tuple[str, str]] = []


@pytest.fixture
def gate(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    policies = {
        "lists": record_store.StorePolicy(root=tmp_path / "lists"),
        "notes": record_store.StorePolicy(root=tmp_path / "notes", owner_field="owner"),
    }
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: policies)
    space_writes.SPACE_WRITES.clear()
    space_writes._releases.clear()
    POSTED.clear()
    monkeypatch.setattr(space_writes, "post_notice", lambda chat, text: POSTED.append((chat, text)))
    plugin = load_plugin("hermes-smd-space-gate")
    yield plugin
    space_writes.SPACE_WRITES.clear()
    space._reset_cache_for_tests()


def _write(
    plugin, content: str, store: str = "lists", name: str = "trip.md"
) -> tuple[dict, dict | None]:
    args = {"store": store, "name": name, "content": content}
    return args, plugin.on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s")


def _say(plugin, monkeypatch, who_bind, text: str, turn: str) -> dict | None:
    who_bind()
    user = __import__("os").environ.get("HERMES_SESSION_USER_ID", "")
    return plugin.on_pre_llm_call(
        session_id="s", turn_id=turn, user_message=text, platform="telegram", sender_id=user
    )


# ---------------------------------------------------------------------------
# Held, and released only by the same person in the same chat
# ---------------------------------------------------------------------------


def test_a_private_fact_into_the_shared_list_is_held_with_the_exact_payload(gate, monkeypatch):
    """FALSIFIER: return None instead of capturing in on_pre_tool_call."""
    telegram_dm(monkeypatch, A_TG)
    args, blocked = _write(gate, A_SECRET)
    assert blocked and blocked["action"] == "block" and blocked["message"].startswith("HELD")
    assert len(space_writes.SPACE_WRITES.pending()) == 1


def test_the_seat_itself_posts_the_exact_stored_payload_to_the_requesting_chat(gate, monkeypatch):
    """Code-sent, not model-relayed: the posted text is the fixed frame around
    the stored payload, byte for byte, sent to the chat that raised it, and the
    model is not handed the words to restate. FALSIFIER: drop the post_notice
    call (or post anything but the stored payload)."""
    telegram_dm(monkeypatch, A_TG)
    _, blocked = _write(gate, A_SECRET)
    stored = space_writes.SPACE_WRITES.pending()[0]
    payload = space_writes.exact_text(stored.args)
    assert POSTED == [
        (A_TG, f"{space_writes.NOTICE_HEAD}\n\n{payload}\n\n{space_writes.NOTICE_TAIL}")
    ]
    body = POSTED[0][1]
    start = len(space_writes.NOTICE_HEAD) + 2
    assert body[start : start + len(payload)].encode() == payload.encode()
    assert A_SECRET not in blocked["message"]


def test_a_notice_that_cannot_be_sent_holds_nothing(gate, monkeypatch):
    """FALSIFIER: capture before (or regardless of) the notice."""

    def fail(chat, text):
        raise RuntimeError("Bot API down")

    monkeypatch.setattr(space_writes, "post_notice", fail)
    telegram_dm(monkeypatch, A_TG)
    _, blocked = _write(gate, A_SECRET)
    assert blocked and "could not be shown" in blocked["message"]
    assert not space_writes.SPACE_WRITES.pending()


def test_b_cannot_confirm_a_held_write(gate, monkeypatch):
    """From B's own DM (a different chat) and even from a turn that claims A's
    chat id with B's user id. FALSIFIER (the second half): drop the user_id
    term in pending_send._may_approve."""
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET)
    assert _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, B_TG), "share it", "t2") is None

    def b_in_a_chat():
        telegram_dm(monkeypatch, B_TG)
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", A_TG)

    assert _say(gate, monkeypatch, b_in_a_chat, "share it", "t3") is None
    assert not space_writes.SPACE_WRITES.pending()[0].approved


def test_a_group_share_it_never_releases_a_dm_held_write(gate, monkeypatch):
    """FALSIFIER: drop _same_chat from _may_approve."""
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET)
    assert (
        _say(gate, monkeypatch, lambda: telegram_group(monkeypatch, A_TG), "share it", "t2") is None
    )
    assert not space_writes.SPACE_WRITES.pending()[0].approved


def test_a_confirms_in_a_dm_and_exactly_the_held_payload_lands(gate, monkeypatch):
    """The re-invoked call carries different words; the stored payload replaces
    them. FALSIFIER: drop ``args.clear(); args.update(...)`` on release."""
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET)
    out = _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, A_TG), "Share it!", "t2")
    assert out and "confirmed" in out["context"]
    args, blocked = _write(gate, "something else entirely, plus more")
    assert blocked is None
    assert args["content"] == A_SECRET


def test_a_confirmation_is_single_use(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET)
    _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, A_TG), "share it", "t2")
    assert _write(gate, A_SECRET)[1] is None
    _, again = _write(gate, A_SECRET)
    assert again and again["message"].startswith("HELD")


def test_share_it_covers_only_writes_held_before_its_turn(gate, monkeypatch):
    """A write the model composes after the person said "share it" is not
    covered by it. FALSIFIER: drop ``rec.created_at >= arrived``."""
    telegram_dm(monkeypatch, A_TG)
    gate.on_pre_llm_call(
        session_id="s", turn_id="t1", user_message="share it", platform="telegram", sender_id=A_TG
    )
    _write(gate, A_SECRET)
    out = gate.on_pre_llm_call(
        session_id="s", turn_id="t1", user_message="share it", platform="telegram", sender_id=A_TG
    )
    assert out is None
    assert not space_writes.SPACE_WRITES.pending()[0].approved


@pytest.mark.parametrize(
    "text", ["send it", "yes", "don't share it", "share it but change it", "approve"]
)
def test_only_a_bare_share_it_confirms(gate, monkeypatch, text):
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET)
    assert _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, A_TG), text, "t2") is None


def test_a_changed_target_is_held_again(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _write(gate, A_SECRET, name="trip.md")
    _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, A_TG), "share it", "t2")
    _, blocked = _write(gate, A_SECRET, name="other.md")
    assert blocked and blocked["message"].startswith("HELD")


# ---------------------------------------------------------------------------
# Refused where no one could confirm
# ---------------------------------------------------------------------------


def test_from_an_email_without_a_thread_a_crossing_is_refused_not_held(gate, monkeypatch):
    """An email turn holds a crossing only when it is bound to its own verified
    origin with a thread (tests/test_space_email.py covers the held path)."""
    email_turn(monkeypatch, "s", A_EMAIL)
    _, blocked = _write(gate, A_SECRET)
    assert blocked and "own thread" in blocked["message"]
    assert not space_writes.SPACE_WRITES.pending()


def test_from_a_private_cron_run_a_crossing_is_refused(gate, monkeypatch):
    cron_to(monkeypatch, A_TG)
    _, blocked = _write(gate, A_SECRET)
    assert blocked and "Telegram chat" in blocked["message"]


def test_an_unresolved_space_refuses_any_candidate(gate, monkeypatch):
    telegram_group(monkeypatch, A_TG, chat=OTHER_GROUP)
    _, blocked = _write(gate, A_SECRET)
    assert blocked and "could not be resolved" in blocked["message"]


def test_a_payload_too_long_to_confirm_is_refused(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _, blocked = _write(gate, "x" * (space_writes.MAX_CONFIRM_CHARS + 1))
    assert blocked and "too long" in blocked["message"]


# ---------------------------------------------------------------------------
# Not a crossing: left alone
# ---------------------------------------------------------------------------


def test_a_write_from_the_group_is_not_gated(gate, monkeypatch):
    telegram_group(monkeypatch, B_TG)
    assert _write(gate, "milk")[1] is None


def test_a_write_into_ones_own_private_space_is_not_gated(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _write(gate, "---\nowner: p1\n---\nmine", store="notes")[1] is None
    _, held = _write(gate, "---\nowner: shared\n---\nours", store="notes")
    assert held and held["message"].startswith("HELD")


def test_non_candidate_tools_are_ignored(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    for tool in ("record_store_read", "remember", "workspace_docs_get", "web_search"):
        assert gate.on_pre_tool_call(tool_name=tool, args={}, session_id="s") is None


# ---------------------------------------------------------------------------
# The other shared targets
# ---------------------------------------------------------------------------


def _held(gate, tool: str, args: dict) -> bool:
    out = gate.on_pre_tool_call(tool_name=tool, args=args, session_id="s")
    return bool(out) and out["message"].startswith("HELD")


class _Event:
    def __init__(self, space_key: str, emails: list[str]):
        self.space = space_key
        self._emails = emails

    def attendee_emails(self) -> list[str]:
        return list(self._emails)


class _Store:
    def __init__(self, events: dict):
        self.events = events

    def get(self, uid):
        return self.events.get(uid)


@pytest.fixture
def calendar(monkeypatch):
    """The stored events ``shared.calendar_reach`` reads (overlay #475)."""
    from shared import calendar_reach

    store = _Store(
        {
            "mine": _Event("person:p1", [A_EMAIL]),
            "ours": _Event("shared", [A_EMAIL, B_EMAIL]),
            "leaky": _Event("person:p1", [A_EMAIL, B_EMAIL]),
        }
    )
    monkeypatch.setattr(calendar_reach.calendar_store, "STORE", store)
    return store


def test_an_invite_naming_the_partner_by_person_id_is_held(gate, monkeypatch, calendar):
    """``attendees`` are person ids. FALSIFIER: make _invitees_other_than
    return an empty set."""
    telegram_dm(monkeypatch, A_TG)
    assert _held(gate, "calendar_invite_create", {"title": A_SECRET, "attendees": ["p1", "p2"]})
    space_writes.SPACE_WRITES.clear()
    assert not _held(gate, "calendar_invite_create", {"title": "dentist", "attendees": ["p1"]})
    assert _held(gate, "calendar_invite_create", {"title": "x", "attendees": [B_EMAIL.upper()]})
    space_writes.SPACE_WRITES.clear()
    assert _held(gate, "calendar_invite_create", {"title": "x", "attendees": ["nobody-known"]})
    assert gate.on_pre_tool_call(tool_name="calendar_invite_list", args={}, session_id="s") is None


def test_a_change_to_a_stored_shared_event_is_held_even_naming_no_one(gate, monkeypatch, calendar):
    """A change names only a uid; its reach is the stored event's attendees.
    An unknown uid is an unreadable reach, and is held."""
    telegram_dm(monkeypatch, A_TG)
    for tool in ("calendar_invite_update", "calendar_invite_cancel"):
        for uid in ("ours", "leaky"):
            space_writes.SPACE_WRITES.clear()
            assert _held(gate, tool, {"uid": uid, "title": A_SECRET}), (tool, uid)
        space_writes.SPACE_WRITES.clear()
        assert not _held(gate, tool, {"uid": "mine"}), tool
        assert _held(gate, tool, {"uid": "unknown-uid"}), tool


def test_a_change_is_held_when_the_event_store_cannot_be_read(gate, monkeypatch, calendar):
    from shared import calendar_reach

    def boom(uid):
        raise calendar_reach.calendar_store.CalendarStoreError("unreadable")

    monkeypatch.setattr(calendar, "get", boom)
    telegram_dm(monkeypatch, A_TG)
    assert _held(gate, "calendar_invite_cancel", {"uid": "mine"})


def test_p1_invite_including_p2_is_held_then_released_only_by_p1_in_that_chat(
    gate, monkeypatch, calendar
):
    """The calendar tool's own R1 check (in_tool_gate) refuses the crossing
    unless the space gate released this very call after p1 confirmed it in p1's
    chat. FALSIFIER: make take_release always return True, or drop
    note_release on release."""
    telegram_dm(monkeypatch, A_TG)
    mine = space.Space(kind="person", person_id="p1", source=space.SOURCE_TELEGRAM_DM)
    args = {"title": A_SECRET, "attendees": ["p1", "p2"]}
    assert _held(gate, "calendar_invite_create", dict(args))
    # Without a release, the handler refuses (a call that skipped the gate).
    assert space_writes.in_tool_gate("calendar_invite_create", mine, "shared") == (
        space_writes.IN_TOOL_REFUSAL
    )
    # B's yes and a group yes release nothing.
    assert _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, B_TG), "share it", "t2") is None
    assert (
        _say(gate, monkeypatch, lambda: telegram_group(monkeypatch, A_TG), "share it", "t3") is None
    )
    assert not space_writes.SPACE_WRITES.pending()[0].approved
    assert _say(gate, monkeypatch, lambda: telegram_dm(monkeypatch, A_TG), "share it", "t4")
    call = {"title": "reworded", "attendees": ["p1", "p2"]}
    assert (
        gate.on_pre_tool_call(tool_name="calendar_invite_create", args=call, session_id="s") is None
    )
    assert call["title"] == A_SECRET
    # The handler's check now passes exactly once, for p1 in p1's chat.
    assert space_writes.in_tool_gate("calendar_invite_create", mine, "shared") is None
    assert space_writes.in_tool_gate("calendar_invite_create", mine, "shared") == (
        space_writes.IN_TOOL_REFUSAL
    )
    # A write that stays in p1's own space never needs one.
    assert space_writes.in_tool_gate("calendar_invite_create", mine, "person:p1") is None


def test_a_release_for_p1_does_not_pass_a_handler_running_for_p2(gate, monkeypatch):
    from shared.turn_principal import Principal

    space_writes.note_release("calendar_invite_create", Principal("telegram", A_TG, A_TG, "dm"))
    telegram_dm(monkeypatch, B_TG)
    theirs = space.Space(kind="person", person_id="p2", source=space.SOURCE_TELEGRAM_DM)
    assert space_writes.in_tool_gate("calendar_invite_create", theirs, "shared") == (
        space_writes.IN_TOOL_REFUSAL
    )


def test_shared_google_writes_are_held_and_reads_are_not(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _held(gate, "workspace_docs_create", {"title": "Trip", "content": A_SECRET})
    assert gate.on_pre_tool_call(tool_name="workspace_docs_get", args={}, session_id="s") is None


def test_an_email_to_the_other_partner_is_held(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _held(gate, "mcp_agentmail_send_message", {"to": [B_EMAIL], "text": A_SECRET})
    space_writes.SPACE_WRITES.clear()
    assert not _held(
        gate, "mcp_agentmail_send_message", {"to": ["florist@example.com"], "text": "x"}
    )


def test_a_message_to_the_group_is_held_and_to_ones_own_dm_is_not(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _held(gate, "send_message", {"target": f"telegram:{GROUP}", "message": A_SECRET})
    space_writes.SPACE_WRITES.clear()
    assert _held(gate, "send_message", {"target": f"telegram:{B_TG}", "message": A_SECRET})
    space_writes.SPACE_WRITES.clear()
    assert (
        gate.on_pre_tool_call(
            tool_name="send_message",
            args={"target": f"telegram:{A_TG}", "message": "x"},
            session_id="s",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Seats without people: are untouched
# ---------------------------------------------------------------------------


def test_a_seat_without_people_is_untouched(monkeypatch, tmp_path):
    """REGRESSION: every candidate passes, nothing is held, a "share it" is
    nothing. FALSIFIER: drop ``if not in_space_mode: return None``."""
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    space_writes.SPACE_WRITES.clear()
    plugin = load_plugin("hermes-smd-space-gate")
    telegram_dm(monkeypatch, A_TG)
    for tool, args in (
        ("record_store_write", {"store": "lists", "name": "x.md", "content": "x"}),
        ("mcp_agentmail_send_message", {"to": [B_EMAIL], "text": "x"}),
        ("workspace_docs_create", {"title": "x"}),
        ("send_message", {"target": f"telegram:{GROUP}", "message": "x"}),
    ):
        assert plugin.on_pre_tool_call(tool_name=tool, args=args, session_id="s") is None, tool
    assert not space_writes.SPACE_WRITES.pending()
    out = plugin.on_pre_llm_call(
        session_id="s", turn_id="t", user_message="share it", platform="telegram", sender_id=A_TG
    )
    assert out is None


def test_both_hooks_are_registered(monkeypatch, tmp_path):
    write_seat(monkeypatch, tmp_path)
    plugin = load_plugin("hermes-smd-space-gate")
    ctx = FakePluginContext()
    plugin.register(ctx)
    assert set(ctx.registered) == {"pre_tool_call", "pre_llm_call"}


def test_exact_text_is_the_sorted_json_of_the_args():
    assert json.loads(space_writes.exact_text({"b": 1, "a": "é"})) == {"a": "é", "b": 1}
