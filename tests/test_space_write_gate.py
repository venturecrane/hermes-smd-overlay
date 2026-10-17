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
    assert space_writes.exact_text(args) in blocked["message"]
    assert len(space_writes.SPACE_WRITES.pending()) == 1


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


def test_from_email_a_crossing_is_refused_not_held(gate, monkeypatch):
    email_turn(monkeypatch, "s", A_EMAIL)
    _, blocked = _write(gate, A_SECRET)
    assert blocked and "Telegram chat" in blocked["message"]
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


def test_calendar_invite_to_the_other_partner_is_held(gate, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _held(
        gate, "calendar_invite_create", {"summary": A_SECRET, "attendees": [A_EMAIL, B_EMAIL]}
    )
    space_writes.SPACE_WRITES.clear()
    assert not _held(gate, "calendar_invite_create", {"summary": "dentist", "attendees": [A_EMAIL]})
    assert _held(gate, "calendar_invite_update", {"attendees": [{"email": B_EMAIL.upper()}]})


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
