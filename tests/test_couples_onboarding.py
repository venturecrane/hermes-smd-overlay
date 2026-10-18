"""Couples onboarding, seat half (ss-console ADR 0090/0091 R4).

``hermes-smd-couples-onboarding`` + ``shared.telegram_bind`` +
``shared.couples_seed``. Falsifiers:

* an unknown Telegram DM never reaches a turn (skip), with or without a token;
* a ``/start <token>`` is spooled and answered with the one fixed line; nothing
  admits the sender (no config, allowlist or pairing write);
* an authored person's DM dispatches normally;
* a seat without ``people:`` is untouched;
* the gate leg posts each token once with the seat's own credential, stops on
  any 200, retries otherwise, and its file never holds a token;
* the seed writes the joint record from authored settings, never overwrites
  one the couple changed together, and keeps a partner's own settings;
* translate authors ``unauthorized_dm_behavior: pair`` on space-mode seats only.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from bootstrap import translate
from shared import couples_seed, record_store, space, telegram_bind
from tests.conftest import load_plugin
from tests.couples_fixtures import A_TG, couples_yaml, write_seat

TOKEN = "A" * 20 + "b" * 20 + "_-9"
STRANGER = "5551234"

plugin = load_plugin("hermes-smd-couples-onboarding")


@pytest.fixture(autouse=True)
def _paths(monkeypatch, tmp_path):
    monkeypatch.setenv("SMD_TELEGRAM_BIND_SPOOL_PATH", str(tmp_path / "binds.db"))
    monkeypatch.setenv("SMD_TELEGRAM_BIND_SENT_PATH", str(tmp_path / "gate" / "sent.db"))
    write_seat(monkeypatch, tmp_path)
    yield
    space._reset_cache_for_tests()


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeGateway:
    def __init__(self) -> None:
        self.adapter = FakeAdapter()

    def _adapter_for_source(self, _source):
        return self.adapter


def _event(text: str, user: str, chat_type: str = "dm"):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_type=chat_type,
        user_id=user,
        chat_id=user if chat_type == "dm" else "-100",
    )
    return SimpleNamespace(text=text, source=source)


def _dispatch(text: str, user: str, chat_type: str = "dm"):
    gateway = FakeGateway()

    async def run():
        result = plugin.on_pre_gateway_dispatch(
            event=_event(text, user, chat_type), gateway=gateway
        )
        await asyncio.sleep(0)  # let the scheduled reply run
        return result

    return asyncio.run(run()), gateway.adapter.sent


def _spooled(tmp_path: Path) -> list[tuple[str, str]]:
    path = tmp_path / "binds.db"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT token, telegram_user_id FROM binds").fetchall()


def test_start_token_from_unknown_user_is_spooled_answered_and_dropped(tmp_path):
    result, sent = _dispatch(f"/start {TOKEN}", STRANGER)
    assert result["action"] == "skip"
    assert _spooled(tmp_path) == [(TOKEN, STRANGER)]
    assert sent == [(STRANGER, telegram_bind.CONFIRMATION_TEXT)]


def test_start_with_bot_suffix_is_recognized(tmp_path):
    _dispatch(f"/start@juniper_bot {TOKEN}", STRANGER)
    assert _spooled(tmp_path) == [(TOKEN, STRANGER)]


def test_FALSIFIER_unknown_dm_without_a_token_is_dropped_silently(tmp_path):
    result, sent = _dispatch("hello, who are you?", STRANGER)
    assert result["action"] == "skip"
    assert sent == []
    assert _spooled(tmp_path) == []


@pytest.mark.parametrize("text", [f"/start {TOKEN}x", f"/start {TOKEN[:-1]}", f"hi /start {TOKEN}"])
def test_FALSIFIER_a_malformed_token_is_not_spooled(tmp_path, text):
    result, sent = _dispatch(text, STRANGER)
    assert result["action"] == "skip"
    assert sent == [] and _spooled(tmp_path) == []


def test_FALSIFIER_an_authored_person_dispatches_normally(tmp_path):
    result, sent = _dispatch(f"/start {TOKEN}", A_TG)
    assert result is None
    assert sent == [] and _spooled(tmp_path) == []


def test_FALSIFIER_nothing_admits_the_sender(tmp_path):
    before = (tmp_path / "customer.yaml").read_text()
    _dispatch(f"/start {TOKEN}", STRANGER)
    assert (tmp_path / "customer.yaml").read_text() == before
    assert space.space_config().person_for_telegram(STRANGER) is None


def test_groups_are_left_to_the_adapter_fence(tmp_path):
    result, _ = _dispatch(f"/start {TOKEN}", STRANGER, chat_type="group")
    assert result is None and _spooled(tmp_path) == []


def test_FALSIFIER_a_seat_without_people_is_untouched(monkeypatch, tmp_path):
    doc = couples_yaml()
    doc.pop("people")
    doc.pop("spaces")
    write_seat(monkeypatch, tmp_path, doc)
    result, sent = _dispatch(f"/start {TOKEN}", STRANGER)
    assert result is None and sent == [] and _spooled(tmp_path) == []


def test_register_only_on_space_mode(monkeypatch, tmp_path):
    hooks: list[str] = []
    ctx = SimpleNamespace(register_hook=lambda name, _cb: hooks.append(name))
    plugin.register(ctx)
    assert hooks == ["pre_gateway_dispatch"]
    doc = couples_yaml()
    doc.pop("people")
    doc.pop("spaces")
    write_seat(monkeypatch, tmp_path, doc)
    hooks.clear()
    plugin.register(ctx)
    assert hooks == []


# ---- gate leg ---------------------------------------------------------------


class Poster:
    def __init__(self, statuses: list[int | Exception]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, dict[str, str], dict]] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body)))
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return status


def _send(poster, store, now):
    return telegram_bind.send_due_binds(
        slug="couple-test",
        key="seat-key",
        url=telegram_bind.bind_url("https://smd.services/api/internal/heartbeat"),
        post_fn=poster,
        store=store,
        now=now,
    )


def test_gate_posts_once_with_the_seat_credential(tmp_path):
    telegram_bind.spool_start(TOKEN, STRANGER, now=1000.0)
    store = telegram_bind.SentStore()
    poster = Poster([200])
    assert _send(poster, store, 1010.0) == 1
    url, headers, body = poster.calls[0]
    assert url == "https://smd.services/api/internal/telegram-bind"
    assert headers["Authorization"] == "Bearer seat-key"
    assert headers["X-Tenant-Slug"] == "couple-test"
    assert body == {"token": TOKEN, "telegram_user_id": STRANGER}
    assert _send(poster, store, 1070.0) == 0  # done: never posted again


def test_FALSIFIER_a_failure_is_retried_and_then_given_up(tmp_path):
    telegram_bind.spool_start(TOKEN, STRANGER, now=1000.0)
    store = telegram_bind.SentStore()
    poster = Poster([503, OSError("down"), 200])
    _send(poster, store, 1010.0)
    _send(poster, store, 1070.0)
    _send(poster, store, 1130.0)
    assert len(poster.calls) == 3
    assert _send(poster, store, 1190.0) == 0
    telegram_bind.spool_start("B" * 43, STRANGER, now=2000.0)
    stale = Poster([503])
    _send(stale, store, 2010.0)
    assert _send(stale, store, 2000.0 + telegram_bind.RETRY_FOR + 60) == 0


def test_FALSIFIER_the_gate_file_holds_no_token(tmp_path):
    telegram_bind.spool_start(TOKEN, STRANGER, now=1000.0)
    _send(Poster([200]), telegram_bind.SentStore(), 1010.0)
    raw = (tmp_path / "gate" / "sent.db").read_bytes()
    assert TOKEN.encode() not in raw
    assert STRANGER.encode() not in raw


def test_spool_prunes_old_rows(tmp_path):
    telegram_bind.spool_start(TOKEN, STRANGER, now=0.0)
    telegram_bind.spool_start("C" * 43, STRANGER, now=telegram_bind.SPOOL_RETENTION_SECONDS + 10)
    assert [t for t, _ in _spooled(tmp_path)] == ["C" * 43]


# ---- seed -------------------------------------------------------------------


def _seat_with_settings(tmp_path: Path, version: int = 2) -> tuple[dict, dict[str, Path]]:
    doc = couples_yaml(
        personas=[
            {
                "slug": "juniper",
                "skills": [
                    {
                        "name": "couples-checkin",
                        "settings": {
                            "timezone": "America/Phoenix",
                            "topics": "money; time together",
                            "quiet_hours": "21:00-08:00",
                            "preferred_channels": "p1=email; p2=telegram",
                            "onboarding_version": version,
                        },
                    }
                ],
            }
        ],
    )
    stores = {"couples-checkin": tmp_path / "checkin"}
    return doc, stores


def test_seed_writes_the_joint_record_and_each_partners_quiet_hours(tmp_path):
    doc, stores = _seat_with_settings(tmp_path)
    written = couples_seed.seed(doc, stores=stores)
    assert written == ["joint-settings.md", "settings-p1.md", "settings-p2.md"]
    joint = record_store.read_frontmatter(
        record_store.read_record("couples-checkin", "joint-settings.md", stores=stores)
    )
    assert joint["owner"] == "shared"
    assert joint["topics"] == "money; time together"
    assert joint["confirmed_by"] == "p1, p2"
    assert joint["source"] == "onboarding"
    p2 = record_store.read_frontmatter(
        record_store.read_record("couples-checkin", "settings-p2.md", stores=stores)
    )
    assert p2 == {**p2, "owner": "p2", "quiet_hours": "21:00-08:00"}


def test_FALSIFIER_seed_never_overwrites_what_the_couple_changed_together(tmp_path):
    doc, stores = _seat_with_settings(tmp_path, version=5)
    couple_owned = "---\nowner: shared\nkind: joint-settings\ntopics: the house\n---\n"
    record_store.write_record("couples-checkin", "joint-settings.md", couple_owned, stores=stores)
    couples_seed.seed(doc, stores=stores)
    text = record_store.read_record("couples-checkin", "joint-settings.md", stores=stores)
    assert text == couple_owned


def test_seed_replaces_an_older_onboarding_record_only(tmp_path):
    doc, stores = _seat_with_settings(tmp_path, version=1)
    couples_seed.seed(doc, stores=stores)
    doc2, _ = _seat_with_settings(tmp_path, version=3)
    assert "joint-settings.md" in couples_seed.seed(doc2, stores=stores)
    assert "joint-settings.md" not in couples_seed.seed(doc2, stores=stores)
    doc_old, _ = _seat_with_settings(tmp_path, version=2)
    assert couples_seed.seed(doc_old, stores=stores) == []


def test_FALSIFIER_seed_keeps_a_partners_own_settings(tmp_path):
    doc, stores = _seat_with_settings(tmp_path)
    own = "---\nowner: p1\nkind: settings\nquiet_hours: 22:00-07:00\n---\n"
    record_store.write_record("couples-checkin", "settings-p1.md", own, stores=stores)
    couples_seed.seed(doc, stores=stores)
    assert record_store.read_record("couples-checkin", "settings-p1.md", stores=stores) == own


def test_seed_does_nothing_without_onboarding_settings(tmp_path):
    assert couples_seed.seed(couples_yaml(), stores={"couples-checkin": tmp_path / "c"}) == []


# ---- translate --------------------------------------------------------------


def test_translate_authors_pair_mode_on_space_mode_seats_only():
    doc = couples_yaml(telegram={"enabled": True, "allow_from": [A_TG], "group_allow_from": [A_TG]})
    block = translate._materialize_telegram_platform(doc)
    assert block["unauthorized_dm_behavior"] == "pair"
    plain = {"telegram": {"enabled": True, "allow_from": ["42"]}}
    assert "unauthorized_dm_behavior" not in translate._materialize_telegram_platform(plain)
