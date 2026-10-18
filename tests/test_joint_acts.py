"""Two-person approval for joint settings (ADR 0090, ss-console #3283).

A change to what the couple decides together applies only after both partners
said yes, each in their own DM. Each falsifier names the line whose removal
makes it fail.
"""

from __future__ import annotations

import time

import pytest

from shared import joint_acts, record_store, space, space_writes
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_TG,
    B_TG,
    clear_turn,
    cron_to,
    telegram_dm,
    telegram_group,
    write_seat,
)

TOPICS = "---\nowner: shared\nkind: joint-settings\ntopics: money; time together\n---\nTopics.\n"
NEW_TOPICS = "---\nowner: shared\nkind: joint-settings\ntopics: money; travel\n---\nTopics.\n"

POSTED: list[tuple[str, str]] = []


@pytest.fixture
def seat(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    root = tmp_path / "couples-checkin"
    policies = {"couples-checkin": record_store.StorePolicy(root=root, owner_field="owner")}
    roots = {"couples-checkin": root}
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: policies)
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: roots)
    record_store.write_record("couples-checkin", "joint-settings.md", TOPICS, stores=roots)
    monkeypatch.setenv("SMD_JOINT_ACTS_PATH", str(tmp_path / "joint-acts.json"))
    POSTED.clear()
    monkeypatch.setattr(space_writes, "post_notice", lambda chat, text: POSTED.append((chat, text)))
    space_writes.SPACE_WRITES.clear()
    gate = load_plugin("hermes-smd-space-gate")
    yield {"gate": gate, "roots": roots}
    space._reset_cache_for_tests()


def _stored(seat) -> str:
    return record_store.read_record("couples-checkin", "joint-settings.md", stores=seat["roots"])


def _propose(seat, content: str = NEW_TOPICS, name: str = "joint-settings.md") -> dict | None:
    args = {"store": "couples-checkin", "name": name, "content": content, "overwrite": True}
    return seat["gate"].on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s")


def _say(seat, monkeypatch, user: str, text: str, *, group: bool = False) -> dict | None:
    (telegram_group if group else telegram_dm)(monkeypatch, user)
    return seat["gate"].on_pre_llm_call(
        session_id="s",
        turn_id=f"t-{time.time_ns()}",
        user_message=text,
        platform="telegram",
        sender_id=user,
    )


def test_one_yes_changes_nothing_and_the_other_partner_is_shown_the_exact_change(seat, monkeypatch):
    """The proposer's request is their yes; one yes changes nothing.
    FALSIFIER: write the record in hold() instead of proposing."""
    telegram_dm(monkeypatch, A_TG)
    blocked = _propose(seat)
    assert blocked and blocked["message"].startswith("HELD for both of you")
    assert _stored(seat) == TOPICS
    assert len(POSTED) == 1 and POSTED[0][0] == B_TG and NEW_TOPICS in POSTED[0][1]
    act = joint_acts.open_act()
    assert act is not None and act.approvals == ["p1"]


def test_both_yeses_apply_the_stored_change(seat, monkeypatch):
    """FALSIFIER: drop the _apply call in respond."""
    telegram_dm(monkeypatch, A_TG)
    _propose(seat)
    out = _say(seat, monkeypatch, B_TG, "Agree")
    assert out and "in place" in out["context"]
    stored = _stored(seat)
    assert "topics: money; travel" in stored and "confirmed_by: p1, p2" in stored
    assert joint_acts.open_act() is None
    assert {chat for chat, text in POSTED if "now in place" in text} == {A_TG, B_TG}


def test_the_same_person_saying_yes_twice_is_one_yes(seat, monkeypatch):
    """FALSIFIER: drop ``if person in act.approvals: return act, "already"``."""
    telegram_dm(monkeypatch, A_TG)
    _propose(seat)
    out = _say(seat, monkeypatch, A_TG, "agree")
    assert out and "already" in out["context"]
    _say(seat, monkeypatch, A_TG, "agree")
    assert _stored(seat) == TOPICS
    assert joint_acts.open_act().approvals == ["p1"]


def test_either_partner_can_reject(seat, monkeypatch):
    for rejecter in (B_TG, A_TG):
        telegram_dm(monkeypatch, A_TG)
        _propose(seat)
        out = _say(seat, monkeypatch, rejecter, "reject")
        assert out and "declined" in out["context"]
        assert joint_acts.open_act() is None
        assert _say(seat, monkeypatch, B_TG, "agree") is None
        assert _stored(seat) == TOPICS


def test_an_act_expires_after_seven_days(seat, monkeypatch):
    """FALSIFIER: drop the expiry in _expire."""
    telegram_dm(monkeypatch, A_TG)
    _propose(seat)
    real = time.time
    monkeypatch.setattr(joint_acts.time, "time", lambda: real() + joint_acts.TTL_SECONDS + 60)
    assert _say(seat, monkeypatch, B_TG, "agree") is None
    assert _stored(seat) == TOPICS
    assert joint_acts.open_act() is None


def test_a_yes_in_the_group_does_not_count(seat, monkeypatch):
    """Each partner answers in their own DM. FALSIFIER: accept a group answer."""
    telegram_dm(monkeypatch, A_TG)
    _propose(seat)
    assert _say(seat, monkeypatch, B_TG, "agree", group=True) is None
    assert joint_acts.open_act().approvals == ["p1"]


def test_a_proposal_from_the_group_notifies_the_other_partner(seat, monkeypatch):
    telegram_group(monkeypatch, B_TG)
    assert _propose(seat)["message"].startswith("HELD")
    assert POSTED[0][0] == A_TG
    out = _say(seat, monkeypatch, A_TG, "agree")
    assert out and "in place" in out["context"]


def test_a_second_proposal_waits_for_the_first(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _propose(seat)
    telegram_dm(monkeypatch, B_TG)
    blocked = _propose(seat, NEW_TOPICS.replace("travel", "chores"))
    assert "still waiting" in blocked["message"]
    telegram_dm(monkeypatch, A_TG)
    _propose(seat, NEW_TOPICS.replace("travel", "house"))
    assert "house" in joint_acts.open_act().args["content"]


def test_a_kind_joint_settings_record_by_another_name_is_gated(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _propose(seat, name="our-settings.md")["message"].startswith("HELD")


def test_a_joint_change_is_never_proposed_by_a_scheduled_run(seat, monkeypatch):
    """A routine has no person to propose for. (By email it is proposed:
    tests/test_space_email.py.)"""
    cron_to(monkeypatch, A_TG)
    blocked = _propose(seat)
    assert blocked and "refused" in blocked["message"]
    assert joint_acts.open_act() is None


def test_a_notice_that_cannot_be_sent_withdraws_the_proposal(seat, monkeypatch):
    def fail(chat, text):
        raise RuntimeError("Bot API down")

    monkeypatch.setattr(space_writes, "post_notice", fail)
    telegram_dm(monkeypatch, A_TG)
    assert "could not be shown" in _propose(seat)["message"]
    assert joint_acts.open_act() is None


def test_ordinary_records_are_not_gated(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    args = {
        "store": "couples-checkin",
        "name": "answer-1-p1.md",
        "content": "---\nowner: p1\n---\nfine",
    }
    assert (
        seat["gate"].on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s")
        is None
    )


def test_seat_without_people_is_untouched(monkeypatch, tmp_path):
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    gate = load_plugin("hermes-smd-space-gate")
    telegram_dm(monkeypatch, A_TG)
    args = {"store": "couples-checkin", "name": "joint-settings.md", "content": TOPICS}
    assert gate.on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s") is None
    assert (
        gate.on_pre_llm_call(
            session_id="s", turn_id="t", user_message="agree", platform="telegram", sender_id=A_TG
        )
        is None
    )
