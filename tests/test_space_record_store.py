"""The record-store fence on a seat a couple shares (ADR 0091, ss-console #3264).

The viewer is the turn's space, resolved on Telegram and cron turns too (the
address viewer is None there, and before this every record was open). Partner B
must never list, read or overwrite a record private to partner A; an unresolved
space lists, reads and writes nothing; seats without people: are unchanged.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shared import record_store, space
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_TG,
    B_TG,
    GROUP,
    OTHER_GROUP,
    clear_turn,
    couples_yaml,
    cron_to,
    telegram_dm,
    telegram_group,
    write_seat,
)

A_SECRET = "Gift idea for Ben: the vintage camera from the Saturday market"


def _rec(owner: str, body: str = "note") -> str:
    return f"---\nowner: {owner}\ntitle: {body[:20]}\n---\n{body}\n"


@pytest.fixture
def seat(monkeypatch, tmp_path) -> dict[str, Any]:
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    private_root = tmp_path / "private"
    lists_root = tmp_path / "lists"
    policies = {
        "notes": record_store.StorePolicy(root=private_root, owner_field="owner"),
        "lists": record_store.StorePolicy(root=lists_root),
    }
    roots = {"notes": private_root, "lists": lists_root}
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: policies)
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: roots)
    record_store.write_record("notes", "a-gift.md", _rec("p1", A_SECRET), stores=roots)
    record_store.write_record("notes", "b-plan.md", _rec("person:p2", "B plan"), stores=roots)
    record_store.write_record("notes", "both.md", _rec("shared", "Shared plan"), stores=roots)
    record_store.write_record("notes", "nobody.md", "---\ntitle: x\n---\nunstamped", stores=roots)
    record_store.write_record("lists", "groceries.md", "milk\n", stores=roots)
    plugin = load_plugin("hermes-smd-record-store")
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda sid: False)
    yield {"plugin": plugin, "roots": roots}
    space._reset_cache_for_tests()


def _pre(plugin, tool: str, **args) -> dict | None:
    return plugin.on_pre_tool_call(tool_name=tool, session_id="s", args=args)


def _listed(plugin, store: str) -> list[str]:
    out = json.loads(plugin._list_handler({"store": store}, session_id="s"))
    return sorted(r["name"] for r in out["records"])


# ---------------------------------------------------------------------------
# Partner B cannot reach partner A's private record
# ---------------------------------------------------------------------------


def test_b_cannot_read_a_private_record_in_b_dm(seat, monkeypatch):
    """FALSIFIER: drop ``if owner not in readable`` in space_access_problem."""
    telegram_dm(monkeypatch, B_TG)
    blocked = _pre(seat["plugin"], "record_store_read", store="notes", name="a-gift.md")
    assert blocked and "private to another person" in blocked["message"]


def test_b_cannot_read_it_in_the_group_either(seat, monkeypatch):
    telegram_group(monkeypatch, B_TG)
    assert _pre(seat["plugin"], "record_store_read", store="notes", name="a-gift.md")


def test_a_cannot_read_own_private_record_in_the_group(seat, monkeypatch):
    """B reads the group reply. FALSIFIER: make readable_spaces(shared)
    include person spaces."""
    telegram_group(monkeypatch, A_TG)
    assert _pre(seat["plugin"], "record_store_read", store="notes", name="a-gift.md")


def test_the_handler_refuses_too_whatever_the_hook_order(seat, monkeypatch):
    """FALSIFIER: drop the _space_fence_problem call in _read_handler."""
    telegram_dm(monkeypatch, B_TG)
    with pytest.raises(RuntimeError, match="private to another person"):
        seat["plugin"]._read_handler({"store": "notes", "name": "a-gift.md"}, session_id="s")


def test_b_listing_never_shows_a_record(seat, monkeypatch):
    """Not even its name or index fields. FALSIFIER: return ``records``
    unfiltered from _visible_in_space."""
    telegram_dm(monkeypatch, B_TG)
    assert _listed(seat["plugin"], "notes") == ["b-plan.md", "both.md"]
    telegram_group(monkeypatch, A_TG)
    assert _listed(seat["plugin"], "notes") == ["both.md"]


def test_b_cannot_overwrite_a_record(seat, monkeypatch):
    telegram_dm(monkeypatch, B_TG)
    blocked = _pre(
        seat["plugin"],
        "record_store_write",
        store="notes",
        name="a-gift.md",
        content=_rec("p2", "mine now"),
        overwrite=True,
    )
    assert blocked and "not yours to change" in blocked["message"]


def test_b_cannot_stamp_a_record_into_a_space(seat, monkeypatch):
    telegram_dm(monkeypatch, B_TG)
    blocked = _pre(
        seat["plugin"], "record_store_write", store="notes", name="new.md", content=_rec("p1")
    )
    assert blocked and "your own space or the shared one" in blocked["message"]


def test_a_record_keeps_its_space(seat, monkeypatch):
    """A cannot move a shared record into A's private space (B would lose it)."""
    telegram_dm(monkeypatch, A_TG)
    blocked = _pre(
        seat["plugin"],
        "record_store_write",
        store="notes",
        name="both.md",
        content=_rec("p1"),
        overwrite=True,
    )
    assert blocked and "keeps its space" in blocked["message"]


def test_a_cron_digest_for_b_cannot_read_a_record(seat, monkeypatch):
    cron_to(monkeypatch, B_TG)
    assert _pre(seat["plugin"], "record_store_read", store="notes", name="a-gift.md")
    cron_to(monkeypatch, GROUP)
    assert _pre(seat["plugin"], "record_store_read", store="notes", name="a-gift.md")


def test_an_unstamped_record_is_nobodys_and_unreadable(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    blocked = _pre(seat["plugin"], "record_store_read", store="notes", name="nobody.md")
    assert blocked and "names no owner" in blocked["message"]


# ---------------------------------------------------------------------------
# The positive half (the refusals above are not "everything refuses")
# ---------------------------------------------------------------------------


def test_a_reads_own_and_shared_records(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    for name in ("a-gift.md", "both.md"):
        assert _pre(seat["plugin"], "record_store_read", store="notes", name=name) is None
    out = json.loads(
        seat["plugin"]._read_handler({"store": "notes", "name": "a-gift.md"}, session_id="s")
    )
    assert A_SECRET in out["content"]
    assert _listed(seat["plugin"], "notes") == ["a-gift.md", "both.md"]


def test_a_writes_own_record_and_group_writes_shared(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert (
        _pre(seat["plugin"], "record_store_write", store="notes", name="a2.md", content=_rec("p1"))
        is None
    )
    telegram_group(monkeypatch, B_TG)
    assert (
        _pre(
            seat["plugin"],
            "record_store_write",
            store="notes",
            name="s2.md",
            content=_rec("shared"),
        )
        is None
    )
    blocked = _pre(
        seat["plugin"], "record_store_write", store="notes", name="s3.md", content=_rec("p2")
    )
    assert blocked, "the shared space writes only shared records"


def test_shared_store_is_readable_from_any_resolved_space(seat, monkeypatch):
    for bind in (lambda: telegram_dm(monkeypatch, B_TG), lambda: telegram_group(monkeypatch, A_TG)):
        bind()
        assert _pre(seat["plugin"], "record_store_read", store="lists", name="groceries.md") is None
        assert _listed(seat["plugin"], "lists") == ["groceries.md"]


# ---------------------------------------------------------------------------
# Fail closed on an unresolved space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["record_store_list", "record_store_read", "record_store_write"])
def test_unresolved_space_refuses_everything(seat, monkeypatch, tool):
    """FALSIFIER: return None from space_access_problem when space is None."""
    telegram_group(monkeypatch, A_TG, chat=OTHER_GROUP)
    blocked = _pre(seat["plugin"], tool, store="lists", name="groceries.md", content="x")
    assert blocked and "could not be resolved" in blocked["message"]


def test_no_context_at_all_refuses_on_a_shared_seat(seat, monkeypatch):
    """The legacy "seat's own turn is not fenced" exemption does not exist here."""
    clear_turn(monkeypatch)
    assert _pre(seat["plugin"], "record_store_read", store="notes", name="both.md")
    with pytest.raises(RuntimeError, match="could not be resolved"):
        seat["plugin"]._list_handler({"store": "notes"}, session_id="s")


def test_unreadable_people_block_refuses(seat, monkeypatch, tmp_path):
    doc = couples_yaml()
    doc["people"][0]["id"] = "Not A Slug"
    write_seat(monkeypatch, tmp_path, doc)
    telegram_dm(monkeypatch, A_TG)
    assert _pre(seat["plugin"], "record_store_read", store="lists", name="groceries.md")


def test_writers_bind_on_a_shared_seat(seat, monkeypatch):
    policies = record_store.authored_policies()
    policies["lists"] = record_store.StorePolicy(
        root=seat["roots"]["lists"], writers=frozenset({"ana@example.com"})
    )
    telegram_dm(monkeypatch, B_TG)
    assert _pre(seat["plugin"], "record_store_write", store="lists", name="g.md", content="x")
    telegram_dm(monkeypatch, A_TG)
    assert (
        _pre(seat["plugin"], "record_store_write", store="lists", name="g.md", content="x") is None
    )
    telegram_group(monkeypatch, A_TG)
    assert _pre(seat["plugin"], "record_store_write", store="lists", name="g.md", content="x")


# ---------------------------------------------------------------------------
# Seats without people: are unchanged
# ---------------------------------------------------------------------------


def test_seat_without_people_keeps_the_address_fence(monkeypatch, tmp_path):
    """REGRESSION: a Telegram turn on a brokery-shaped seat is the seat's own
    and unfenced, listing is never refused, and handlers do not judge."""
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    root = tmp_path / "ohv"
    policy = record_store.StorePolicy(root=root, owner_field="agent")
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: {"ohv": policy})
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {"ohv": root})
    record_store.write_record("ohv", "a.md", "---\nagent: a@x.com\n---\nn", stores={"ohv": root})
    plugin = load_plugin("hermes-smd-record-store")
    telegram_dm(monkeypatch, A_TG)
    assert _pre(plugin, "record_store_read", store="ohv", name="a.md") is None
    assert _pre(plugin, "record_store_list", store="ohv") is None
    clear_turn(monkeypatch)
    assert _pre(plugin, "record_store_read", store="ohv", name="a.md") is None
    out = json.loads(plugin._list_handler({"store": "ohv"}, session_id="s"))
    assert out["records"][0]["owner"] == "a@x.com"


def test_owner_space_normalizes(tmp_path: Path):
    policy = record_store.StorePolicy(root=tmp_path, owner_field="owner")
    assert record_store.owner_space(_rec("P1"), policy) == "person:p1"
    assert record_store.owner_space(_rec("person:p2"), policy) == "person:p2"
    assert record_store.owner_space(_rec("shared"), policy) == "shared"
    assert record_store.owner_space(_rec("a@x.com"), policy) is None
    assert record_store.target_space(record_store.StorePolicy(root=tmp_path), "x") == "shared"
    assert record_store.target_space(policy, _rec("p1")) == "person:p1"
