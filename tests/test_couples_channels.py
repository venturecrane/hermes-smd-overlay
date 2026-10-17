"""Couples role channels (ADR 0091, ss-console #3264, workstream B).

Three things, each with a falsifier:

* the Telegram block passes the group keys through to the pinned Hermes keys
  and refuses the shapes that would open the group to non-people;
* a cron ``space`` decides delivery (a person's DM, or the shared group) and
  never guesses a chat that is not bound yet;
* the cron run's space is readable back from the job (``shared.cron_space``).

Seats without ``people`` are pinned unchanged (the scott and Brokery shapes).
"""

from __future__ import annotations

import itertools
import json
import logging

import pytest

from bootstrap import translate as _wh
from bootstrap.cron_materialize import CronMaterializeError, materialize_cron
from shared import cron_attribution, cron_space

P1, P2, GROUP = "1111111111", "2222222222", "-1001234567890"


def _couple(**over) -> dict:
    cust = {
        "customer_id": "captain-mark",
        "people": [
            {"id": "p1", "name": "Pat One", "telegram_id": P1, "email": "p1@example.com"},
            {"id": "p2", "name": "Sam Two", "telegram_id": P2, "email": "p2@example.com"},
        ],
        "spaces": {"shared_telegram_chat": GROUP},
        "telegram": {
            "enabled": True,
            "allow_from": [P1, P2],
            "group_allow_from": [P1, P2],
            "group_chats": [GROUP],
            "group_sessions_per_user": False,
        },
    }
    cust.update(over)
    return cust


# ---------------------------------------------------------------- telegram


def test_space_mode_block_uses_the_pinned_hermes_keys():
    block = _wh._materialize_telegram_platform(_couple())
    assert block == {
        "allow_from": [P1, P2],
        "group_allow_from": [P1, P2],
        "allowed_chats": [GROUP],
        "group_sessions_per_user": False,
    }
    # group_allowed_chats is a chat-scoped GRANT in the Hermes runner (every
    # member of the chat is authorized, no sender check). It must never appear.
    assert "group_allowed_chats" not in block
    assert "group_chats" not in block


def test_group_sessions_per_user_reaches_the_config_root():
    persona = {"slug": "couple", "name": "Couple", "status": "active", "skills": []}
    config = _wh._persona_config(persona, _couple(), {})
    assert config["group_sessions_per_user"] is False
    assert config["telegram"]["group_sessions_per_user"] is False


def test_space_mode_defaults_to_one_shared_group_conversation():
    cust = _couple()
    del cust["telegram"]["group_sessions_per_user"]
    assert _wh._materialize_telegram_platform(cust)["group_sessions_per_user"] is False


def test_group_chats_without_sender_allowlist_fails_closed():
    cust = {"telegram": {"enabled": True, "allow_from": [P1], "group_chats": [GROUP]}}
    with pytest.raises(ValueError, match="group_allow_from is empty"):
        _wh._materialize_telegram_platform(cust)
    cust["telegram"]["group_allow_from"] = []
    with pytest.raises(ValueError, match="group_allow_from is empty"):
        _wh._materialize_telegram_platform(cust)


def test_space_mode_refuses_a_non_person_group_sender():
    cust = _couple()
    cust["telegram"]["group_allow_from"] = [P1, P2, "9999999999"]
    with pytest.raises(ValueError, match="not in people"):
        _wh._materialize_telegram_platform(cust)


def test_space_mode_refuses_a_group_other_than_the_shared_chat():
    cust = _couple()
    cust["telegram"]["group_chats"] = [GROUP, "-1009999999999"]
    with pytest.raises(ValueError, match="shared group only"):
        _wh._materialize_telegram_platform(cust)


def test_space_mode_allowed_chats_comes_from_the_shared_chat_even_if_unauthored():
    cust = _couple()
    del cust["telegram"]["group_chats"]
    assert _wh._materialize_telegram_platform(cust)["allowed_chats"] == [GROUP]


def test_space_mode_unbound_shared_chat_denies_every_group_sender(caplog):
    # An empty allowed_chats means "every group" in Hermes, so the unbound case
    # is closed on the SENDER side: an empty group_allow_from denies all.
    cust = _couple(spaces={"shared_telegram_chat": ""})
    cust["telegram"]["group_chats"] = []
    with caplog.at_level(logging.WARNING):
        block = _wh._materialize_telegram_platform(cust)
    assert block["group_allow_from"] == []
    assert "allowed_chats" not in block
    assert "not bound yet" in caplog.text


def test_space_mode_shared_chat_with_no_senders_fails_closed():
    cust = _couple()
    cust["telegram"]["group_allow_from"] = []
    with pytest.raises(ValueError, match="group_allow_from is empty"):
        _wh._materialize_telegram_platform(cust)


def test_non_space_seat_passes_group_keys_through():
    cust = {
        "telegram": {
            "enabled": True,
            "allow_from": [P1],
            "group_allow_from": [P1],
            "group_chats": [GROUP],
            "group_sessions_per_user": True,
        }
    }
    assert _wh._materialize_telegram_platform(cust) == {
        "allow_from": [P1],
        "group_allow_from": [P1],
        "allowed_chats": [GROUP],
        "group_sessions_per_user": True,
    }


@pytest.mark.parametrize(
    "telegram, expected",
    [
        # scott-shaped (after the phase-2 migration)
        ({"enabled": True, "allow_from": ["7367659986"]}, {"allow_from": ["7367659986"]}),
        # Brokery-shaped
        (
            {"enabled": True, "allow_from": ["7367659986", "5550001111"], "require_mention": False},
            {"allow_from": ["7367659986", "5550001111"], "require_mention": False},
        ),
    ],
)
def test_seats_without_people_are_byte_identical(telegram, expected):
    cust = {"customer_id": "x", "telegram": telegram}
    assert _wh._materialize_telegram_platform(cust) == expected
    persona = {"slug": "p", "name": "P", "status": "active", "skills": []}
    config = _wh._persona_config(persona, cust, {})
    assert "group_sessions_per_user" not in config
    assert config["telegram"] == expected


# -------------------------------------------------------------------- cron


class _Store:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self._ids = (f"job-{n}" for n in itertools.count(1))

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return list(self.jobs)

    def create_job(self, **kwargs) -> dict:
        job = {"id": next(self._ids), **kwargs}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True


def _with_cron(cust: dict, entries: list[dict]) -> dict:
    skills = sorted({e["skill"] for e in entries})
    cust["personas"] = [
        {
            "slug": "couple",
            "cron": entries,
            "skills": [
                {"name": s, "enabled": True, "initiation": {"scheduled": True}} for s in skills
            ],
        }
    ]
    return cust


def _run(cust: dict) -> dict[str, dict]:
    store = _Store()
    materialize_cron(cust, lambda _slug: store)
    return {j["name"]: j for j in store.jobs}


CHECKIN = {"skill": "couples-checkin", "schedule": "0 15 * * 1"}


def test_person_routine_delivers_to_that_persons_dm_only():
    jobs = _run(_with_cron(_couple(), [{**CHECKIN, "space": "person:p1"}]))
    job = jobs["op-managed:couple:couples-checkin@person:p1"]
    assert job["deliver"] == f"telegram:{P1}"
    # falsifier: never the partner's DM, never the group
    assert job["deliver"] not in (f"telegram:{P2}", f"telegram:{GROUP}")


def test_each_partner_gets_their_own_job_and_the_shared_one_goes_to_the_group():
    jobs = _run(
        _with_cron(
            _couple(),
            [
                {**CHECKIN, "space": "person:p1"},
                {**CHECKIN, "space": "person:p2"},
                {"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"},
            ],
        )
    )
    assert {name: j["deliver"] for name, j in jobs.items()} == {
        "op-managed:couple:couples-checkin@person:p1": f"telegram:{P1}",
        "op-managed:couple:couples-checkin@person:p2": f"telegram:{P2}",
        "op-managed:couple:date-ideas@shared": f"telegram:{GROUP}",
    }


def test_unbound_person_delivers_locally_never_to_a_guessed_chat(caplog):
    cust = _couple()
    cust["people"][0]["telegram_id"] = ""
    with caplog.at_level(logging.WARNING):
        jobs = _run(_with_cron(cust, [{**CHECKIN, "space": "person:p1"}]))
    job = jobs["op-managed:couple:couples-checkin@person:p1"]
    assert job["deliver"] == "local"
    assert "people[p1].telegram_id is not bound yet" in caplog.text


def test_unbound_shared_chat_delivers_locally():
    cust = _couple(spaces={"shared_telegram_chat": ""})
    jobs = _run(
        _with_cron(cust, [{"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"}])
    )
    assert jobs["op-managed:couple:date-ideas@shared"]["deliver"] == "local"


@pytest.mark.parametrize("space", [None, "", "person:p3", "person:P1", "group", "person:"])
def test_space_mode_routine_without_a_valid_space_is_refused(space):
    entry = dict(CHECKIN)
    if space is not None:
        entry["space"] = space
    with pytest.raises(CronMaterializeError, match="space"):
        _run(_with_cron(_couple(), [entry]))


def test_same_routine_twice_in_one_space_is_refused():
    entries = [{**CHECKIN, "space": "person:p1"}, {**CHECKIN, "space": "person:p1"}]
    with pytest.raises(CronMaterializeError, match="same managed name"):
        _run(_with_cron(_couple(), entries))


def test_seat_without_people_ignores_space_and_sets_no_deliver():
    cust = _with_cron({"customer_id": "brokery"}, [{**CHECKIN, "space": "person:p1"}])
    jobs = _run(cust)
    assert list(jobs) == ["op-managed:couple:couples-checkin"]
    assert "deliver" not in jobs["op-managed:couple:couples-checkin"]


def test_removed_spaced_routine_reports_the_bare_skill():
    cust = _with_cron(_couple(), [{**CHECKIN, "space": "person:p1"}])
    store = _Store()
    materialize_cron(cust, lambda _s: store)
    cust["personas"][0]["cron"] = []
    seen = []
    materialize_cron(
        cust, lambda _s: store, reconcile_slugs=["couple"], on_routine_change=seen.append
    )
    assert [(c.skill, c.enabled) for c in seen] == [("couples-checkin", False)]


# --------------------------------------------------------- bound run space


@pytest.fixture(autouse=True)
def _clear_attribution_cache():
    cron_attribution._CACHE.clear()
    yield
    cron_attribution._CACHE.clear()


def _store_on_disk(home, jobs):
    d = home / "profiles" / "couple" / "cron"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def test_attribution_reads_space_and_keeps_skill_bare(tmp_path):
    _store_on_disk(
        tmp_path,
        [
            {"id": "aaa111", "name": "op-managed:couple:couples-checkin@person:p1"},
            {"id": "bbb222", "name": "op-managed:operator:inbox-triage"},
        ],
    )
    spaced = cron_attribution.resolve_routine(
        "cron_aaa111_20261019_150000", hermes_home=str(tmp_path)
    )
    assert (spaced.persona, spaced.skill, spaced.space) == (
        "couple",
        "couples-checkin",
        "person:p1",
    )
    plain = cron_attribution.resolve_routine(
        "cron_bbb222_20261019_150000", hermes_home=str(tmp_path)
    )
    assert (plain.skill, plain.space) == ("inbox-triage", None)


def _cron_env(monkeypatch, chat_id: str):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", chat_id)


def test_bound_space_from_the_job_name(tmp_path, monkeypatch):
    _store_on_disk(
        tmp_path, [{"id": "aaa111", "name": "op-managed:couple:couples-checkin@person:p1"}]
    )
    _cron_env(monkeypatch, P1)
    got = cron_space.bound_cron_space(
        "cron_aaa111_20261019_150000", customer=_couple(), hermes_home=str(tmp_path)
    )
    assert got == "person:p1"


def test_bound_space_none_when_job_and_delivery_disagree(tmp_path, monkeypatch):
    _store_on_disk(
        tmp_path, [{"id": "aaa111", "name": "op-managed:couple:couples-checkin@person:p1"}]
    )
    _cron_env(monkeypatch, P2)  # delivering to p2 while named for p1
    got = cron_space.bound_cron_space(
        "cron_aaa111_20261019_150000", customer=_couple(), hermes_home=str(tmp_path)
    )
    assert got is None


def test_bound_space_none_for_a_cron_job_with_no_space(tmp_path, monkeypatch):
    _store_on_disk(tmp_path, [{"id": "bbb222", "name": "op-managed:couple:couples-checkin"}])
    _cron_env(monkeypatch, P1)
    got = cron_space.bound_cron_space(
        "cron_bbb222_20261019_150000", customer=_couple(), hermes_home=str(tmp_path)
    )
    assert got is None


def test_bound_space_from_delivery_when_no_session_id(monkeypatch):
    _cron_env(monkeypatch, GROUP)
    assert cron_space.bound_cron_space(customer=_couple()) == "shared"
    _cron_env(monkeypatch, P2)
    assert cron_space.bound_cron_space(customer=_couple()) == "person:p2"
    _cron_env(monkeypatch, "3333333333")
    assert cron_space.bound_cron_space(customer=_couple()) is None


def test_bound_space_none_off_cron_and_off_space_mode(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", P1)
    assert cron_space.bound_cron_space(customer=_couple()) is None
    _cron_env(monkeypatch, P1)
    assert cron_space.bound_cron_space(customer={"customer_id": "brokery"}) is None
    assert cron_space.bound_cron_space("not-a-cron-id", customer=_couple()) == "person:p1"
