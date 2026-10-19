"""The space resolver (``shared.space``): whose space a turn is in, structurally.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import pytest

from shared import space
from shared.customer_config import CustomerConfig
from shared.inbound import SESSION_INBOUND_ORIGIN
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_EMAIL,
    B_TG,
    GROUP,
    OTHER_GROUP,
    clear_turn,
    couples_yaml,
    cron_to,
    email_turn,
    telegram_dm,
    telegram_group,
    write_seat,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    write_seat(monkeypatch, tmp_path)
    yield
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    space._reset_cache_for_tests()


def test_partner_dms_resolve_to_different_spaces(monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    a = space.resolve_space()
    telegram_dm(monkeypatch, B_TG)
    b = space.resolve_space()
    assert a is not None and a.key == "person:p1" and a.source == space.SOURCE_TELEGRAM_DM
    assert b is not None and b.key == "person:p2"


def test_unknown_dm_user_is_unresolved(monkeypatch):
    """FALSIFIER: drop ``if person is None: return None`` in _resolve_telegram."""
    telegram_dm(monkeypatch, "3003")
    assert space.resolve_space() is None


def test_dm_whose_chat_is_not_the_user_is_unresolved(monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", B_TG)
    assert space.resolve_space() is None


def test_shared_group_resolves_to_shared(monkeypatch):
    telegram_group(monkeypatch, A_TG)
    got = space.resolve_space()
    assert got is not None and got.key == "shared" and got.source == space.SOURCE_TELEGRAM_GROUP


def test_other_group_is_unresolved(monkeypatch):
    """FALSIFIER: compare chat_id to anything but shared_telegram_chat."""
    telegram_group(monkeypatch, A_TG, chat=OTHER_GROUP)
    assert space.resolve_space() is None


def test_group_with_a_person_id_as_chat_is_not_that_persons_space(monkeypatch):
    """A non-DM chat type never resolves to a person, even if its id matches one."""
    telegram_group(monkeypatch, A_TG, chat=A_TG)
    assert space.resolve_space() is None


def test_cron_space_is_its_delivery_target(monkeypatch):
    cron_to(monkeypatch, B_TG)
    got = space.resolve_space()
    assert got is not None and got.key == "person:p2" and got.source == space.SOURCE_CRON
    cron_to(monkeypatch, GROUP)
    assert space.resolve_space().key == "shared"


def test_cron_without_a_telegram_target_is_unresolved(monkeypatch):
    cron_to(monkeypatch, A_EMAIL, platform="email")
    assert space.resolve_space() is None
    cron_to(monkeypatch, "")
    assert space.resolve_space() is None


def test_cron_ignores_a_stale_telegram_session(monkeypatch):
    """A cron run is bound to its target only. FALSIFIER: move the cron branch
    after the telegram branch in resolve_space."""
    cron_to(monkeypatch, GROUP)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", A_TG)
    assert space.resolve_space().key == "shared"


def test_email_from_a_person_resolves_to_them(monkeypatch):
    email_turn(monkeypatch, "s-email", B_EMAIL.upper())
    got = space.resolve_space()
    assert got is not None and got.key == "person:p2" and got.source == space.SOURCE_EMAIL


def test_email_session_id_argument_wins(monkeypatch):
    email_turn(monkeypatch, "s-a", A_EMAIL)
    monkeypatch.delenv("HERMES_SESSION_ID")
    assert space.resolve_space() is None
    assert space.resolve_space("s-a").key == "person:p1"


def test_email_from_a_stranger_is_unresolved(monkeypatch):
    email_turn(monkeypatch, "s-x", "stranger@example.com")
    assert space.resolve_space() is None


def test_no_context_is_unresolved():
    assert space.resolve_space() is None


def test_unbound_telegram_id_never_matches_an_empty_user(monkeypatch, tmp_path):
    """An unbound person ('' telegram_id) must not match a turn with no user id."""
    doc = couples_yaml()
    doc["people"][0]["telegram_id"] = ""
    write_seat(monkeypatch, tmp_path, doc)
    telegram_dm(monkeypatch, "")
    assert space.resolve_space() is None
    cron_to(monkeypatch, "")
    assert space.resolve_space() is None


def test_unbound_shared_group_never_matches(monkeypatch, tmp_path):
    doc = couples_yaml(spaces={"shared_telegram_chat": ""})
    write_seat(monkeypatch, tmp_path, doc)
    telegram_group(monkeypatch, A_TG, chat="")
    assert space.resolve_space() is None


def test_prompt_text_cannot_move_a_turn(monkeypatch):
    """The resolver takes no prompt input at all: its only argument is a session
    id, used for the verified email origin."""
    telegram_dm(monkeypatch, A_TG)
    assert space.resolve_space("person:p2").key == "person:p1"


# ---------------------------------------------------------------------------
# readable sets
# ---------------------------------------------------------------------------


def test_readable_sets():
    p1 = space.Space(kind="person", person_id="p1", source="t")
    shared = space.Space(kind="shared", person_id=None, source="t")
    assert space.readable_spaces(p1) == {"shared", "person:p1"}
    assert space.readable_spaces(shared) == {"shared"}
    assert space.readable_spaces(None) == frozenset()
    assert not space.may_read(shared, "person:p1")
    assert not space.may_read(p1, "person:p2")
    assert not space.may_read(None, "shared")


# ---------------------------------------------------------------------------
# space mode and config faults
# ---------------------------------------------------------------------------


def test_seat_without_people_is_not_space_mode(monkeypatch, tmp_path):
    write_seat(monkeypatch, tmp_path, {"customer_id": "scott", "telegram": {"enabled": True}})
    assert space.space_mode() is False
    telegram_dm(monkeypatch, A_TG)
    assert space.resolve_space() is None


def test_missing_customer_yaml_is_not_space_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    space._reset_cache_for_tests()
    assert space.space_mode() is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(people="ana"),
        lambda d: d.update(people=[]),
        lambda d: d["people"].append({"id": "p1", "name": "dup"}),
        lambda d: d["people"][0].update(id="Bad Id"),
        lambda d: d["people"][0].update(telegram_id="abc"),
        lambda d: d["people"][1].update(telegram_id=A_TG),
        lambda d: d["people"][1].update(email=A_EMAIL),
        lambda d: d.pop("spaces"),
        lambda d: d.update(spaces={"shared_telegram_chat": "12345"}),
    ],
)
def test_malformed_people_block_raises(monkeypatch, tmp_path, mutate):
    """Could-not-tell is never not-space-mode. FALSIFIER: catch SpaceConfigError
    in space_config and return None."""
    doc = couples_yaml()
    mutate(doc)
    write_seat(monkeypatch, tmp_path, doc)
    with pytest.raises(space.SpaceConfigError):
        space.space_mode()


def test_unparseable_yaml_raises(monkeypatch, tmp_path):
    path = tmp_path / "customer.yaml"
    path.write_text("people: [unterminated\n")
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    space._reset_cache_for_tests()
    with pytest.raises(space.SpaceConfigError):
        space.space_mode()


def test_config_cache_picks_up_an_edit(monkeypatch, tmp_path):
    assert space.space_mode() is True
    write_seat(monkeypatch, tmp_path, {"customer_id": "scott"})
    assert space.space_mode() is False


def test_explicit_config_seam():
    cfg = CustomerConfig(couples_yaml())
    assert space.space_mode(cfg) is True
    assert space.person_for_sender(B_TG, config=cfg).id == "p2"
    assert space.person_for_sender(A_EMAIL.upper(), config=cfg).id == "p1"
    assert space.person_for_sender("someone@else.com", config=cfg) is None
    assert space.person_for_sender("", config=cfg) is None


def test_chat_for_space_and_keys():
    cfg = space.parse_space_config(couples_yaml())
    assert cfg is not None
    assert cfg.chat_for_space(cfg.space_for_key("shared", source="x")) == GROUP
    assert cfg.chat_for_space(cfg.space_for_key("person:p1", source="x")) == A_TG
    assert cfg.space_for_key("person:nobody", source="x") is None
    assert cfg.space_for_key("everyone", source="x") is None
