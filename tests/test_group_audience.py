"""The shared-group audience notice (``shared.group_audience``, ADR 0091 R7).

No network: the Bot API is a recording fake. Falsifiers: warns when the count
is above people + bot, never repeats for the same count, and seats without
``people`` never call the Bot API at all.
"""

from __future__ import annotations

import json
import logging

import pytest

from shared import group_audience, space
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_TG,
    GROUP,
    OTHER_GROUP,
    clear_turn,
    cron_to,
    telegram_dm,
    telegram_group,
    write_seat,
)


class FakeBot:
    def __init__(self, count: int = 3, fail: set[str] | None = None) -> None:
        self.count = count
        self.fail = fail or set()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, method: str, params: dict[str, str]):
        self.calls.append((method, dict(params)))
        if method in self.fail:
            raise RuntimeError(f"Bot API {method} failed: URLError")
        if method == "getChatMemberCount":
            return self.count
        return {"message_id": 1}

    @property
    def sent(self) -> list[dict[str, str]]:
        return [p for m, p in self.calls if m == "sendMessage"]


def _inline(job):
    job()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    group_audience._reset_for_tests()
    yield
    group_audience._reset_for_tests()
    space._reset_cache_for_tests()


def _turn(tmp_path, bot) -> bool:
    return group_audience.on_turn(run=_inline, bot_call=bot, hermes_home=str(tmp_path))


def test_warns_when_someone_outside_people_is_in_the_group(monkeypatch, tmp_path):
    bot = FakeBot(count=4)  # two people + bot = 3 expected
    telegram_group(monkeypatch, A_TG)
    assert _turn(tmp_path, bot) is True
    assert bot.sent == [{"chat_id": GROUP, "text": group_audience.NOTICE}]
    state = json.loads((tmp_path / "smd-state" / "group-audience.json").read_text())
    assert state[GROUP]["warned_at_count"] == 4


def test_no_notice_when_only_the_couple_and_the_bot_are_there(monkeypatch, tmp_path):
    bot = FakeBot(count=3)
    telegram_group(monkeypatch, A_TG)
    _turn(tmp_path, bot)
    assert [m for m, _ in bot.calls] == ["getChatMemberCount"]
    assert bot.sent == []


def test_never_repeats_for_the_same_count_even_across_restarts(monkeypatch, tmp_path):
    bot = FakeBot(count=4)
    telegram_group(monkeypatch, A_TG)
    _turn(tmp_path, bot)
    group_audience._reset_for_tests()  # a new process: interval memory gone
    _turn(tmp_path, bot)
    group_audience._reset_for_tests()
    bot.count = 3  # the outsider left ...
    _turn(tmp_path, bot)
    group_audience._reset_for_tests()
    bot.count = 4  # ... and came back: same count as already warned at
    _turn(tmp_path, bot)
    assert len(bot.sent) == 1


def test_warns_again_only_when_more_outsiders_join(monkeypatch, tmp_path):
    bot = FakeBot(count=4)
    telegram_group(monkeypatch, A_TG)
    _turn(tmp_path, bot)
    group_audience._reset_for_tests()
    bot.count = 5
    _turn(tmp_path, bot)
    assert len(bot.sent) == 2


def test_checks_at_most_once_per_interval_in_one_process(monkeypatch, tmp_path):
    bot = FakeBot(count=3)
    telegram_group(monkeypatch, A_TG)
    assert _turn(tmp_path, bot) is True
    assert _turn(tmp_path, bot) is False
    assert [m for m, _ in bot.calls] == ["getChatMemberCount"]


@pytest.mark.parametrize(
    "turn",
    [
        lambda mp: telegram_dm(mp, A_TG),
        lambda mp: telegram_group(mp, A_TG, chat=OTHER_GROUP),
        lambda mp: cron_to(mp, GROUP),  # a cron run in the shared space is not a group turn
        lambda mp: None,
    ],
    ids=["dm", "other-group", "cron-to-shared", "no-session"],
)
def test_only_a_shared_group_turn_triggers_a_check(monkeypatch, tmp_path, turn):
    bot = FakeBot(count=9)
    turn(monkeypatch)
    assert _turn(tmp_path, bot) is False
    assert bot.calls == []


@pytest.mark.parametrize(
    "doc",
    [
        {"customer_id": "scott", "telegram": {"enabled": True, "allow_from": [A_TG]}},
        {
            "customer_id": "brokery",
            "telegram": {"enabled": True, "allow_from": [A_TG], "require_mention": False},
        },
    ],
    ids=["scott", "brokery"],
)
def test_seats_without_people_never_call_the_bot_api(monkeypatch, tmp_path, doc):
    write_seat(monkeypatch, tmp_path, doc)
    bot = FakeBot(count=9)
    telegram_group(monkeypatch, A_TG)
    assert _turn(tmp_path, bot) is False
    assert bot.calls == []


def test_unbound_shared_chat_never_calls_the_bot_api(monkeypatch, tmp_path):
    from tests.couples_fixtures import couples_yaml

    doc = couples_yaml()
    doc["spaces"]["shared_telegram_chat"] = ""
    write_seat(monkeypatch, tmp_path, doc)
    bot = FakeBot(count=9)
    telegram_group(monkeypatch, A_TG)
    assert _turn(tmp_path, bot) is False
    assert bot.calls == []


@pytest.mark.parametrize("failing", ["getChatMemberCount", "sendMessage"])
def test_api_errors_are_logged_and_swallowed_and_not_recorded(
    monkeypatch, tmp_path, caplog, failing
):
    bot = FakeBot(count=4, fail={failing})
    telegram_group(monkeypatch, A_TG)
    with caplog.at_level(logging.WARNING):
        assert _turn(tmp_path, bot) is True
    assert "group_audience:" in caplog.text
    assert not (tmp_path / "smd-state" / "group-audience.json").exists()
    # a failed send is retried on a later check, since nothing was recorded
    group_audience._reset_for_tests()
    bot.fail = set()
    bot.calls.clear()
    _turn(tmp_path, bot)
    assert len(bot.sent) == 1


def test_bot_call_refuses_methods_it_does_not_use(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-not-real")
    with pytest.raises(ValueError):
        group_audience._bot_call("leaveChat", {"chat_id": GROUP})


def test_notice_is_plain_fixed_text():
    assert "—" not in group_audience.NOTICE  # no em dash in client-facing copy
    assert group_audience.NOTICE.strip() == group_audience.NOTICE


def test_plugin_hook_is_exception_safe_and_injects_nothing(monkeypatch):
    plugin = load_plugin("hermes-smd-group-audience")

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(group_audience, "on_turn", boom)
    assert plugin.on_pre_llm_call(session_id="s") is None
