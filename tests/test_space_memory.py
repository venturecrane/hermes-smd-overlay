"""Space memory on a seat a couple shares (ADR 0091, ss-console #3264).

Partner A (p1, Telegram 1001, ana@) and partner B (p2, Telegram 2002, ben@)
share one seat with a shared group. What A keeps in A's DM must never reach B:
not through injection in B's DM, not in the group, not by B asking for it.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import json

import pytest

from shared import space
from shared.d1_client import D1Client
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
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

A_SECRET = "Ana is planning a surprise trip to Lisbon for Ben's birthday"
SHARED_FACT = "Anniversary dinner is booked at Quince on June 3"


@pytest.fixture
def mod(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    SESSION_TAINT._tainted.clear()
    m = load_plugin("hermes-smd-peer-memory")
    client = D1Client(
        binding_name="UNUSED", customer_slug="captain-mark", db_path=str(tmp_path / "s.db")
    )
    m.store.ensure_schema(client)
    m.bind_runtime(customer_slug="captain-mark", client=client, space_mode=True)
    yield m
    SESSION_TAINT._tainted.clear()
    space._reset_cache_for_tests()


def _turn_context(mod, session_id: str, sender: str) -> str:
    out = mod.on_pre_llm_call(session_id=session_id, sender_id=sender, platform="telegram")
    return (out or {}).get("context", "")


def _call(mod, session_id: str, tool: str, args: dict, status: str = "ok") -> dict | None:
    """One tool call the way Hermes runs it: pre_tool_call, then (if not
    blocked) post_tool_call; a blocked call still reaches post_tool_call with
    status="blocked" (model_tools.py ``_emit(..., status="blocked")``)."""
    blocked = mod.on_pre_tool_call(tool_name=tool, args=args, session_id=session_id)
    mod.on_post_tool_call(
        tool_name=tool,
        args=args,
        session_id=session_id,
        result="{}",
        status="blocked" if blocked else status,
    )
    return blocked


def _a_keeps_a_secret(mod, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _turn_context(mod, "dm-a", A_TG)
    assert _call(mod, "dm-a", "remember", {"fact": A_SECRET}) is None


# ---------------------------------------------------------------------------
# The privacy falsifiers
# ---------------------------------------------------------------------------


def test_a_dm_fact_is_never_injected_into_b_dm(mod, monkeypatch):
    """FALSIFIER: pass ``{"shared", "person:p1", "person:p2"}`` (or drop the
    ``space IN`` clause) in store.space_rows."""
    _a_keeps_a_secret(mod, monkeypatch)
    telegram_dm(monkeypatch, B_TG)
    assert A_SECRET not in _turn_context(mod, "dm-b", B_TG)


def test_a_dm_fact_is_never_injected_into_the_group(mod, monkeypatch):
    """Not even on A's own turn in the group: B reads the reply."""
    _a_keeps_a_secret(mod, monkeypatch)
    telegram_group(monkeypatch, A_TG)
    assert A_SECRET not in _turn_context(mod, "grp", A_TG)
    telegram_group(monkeypatch, B_TG)
    assert A_SECRET not in _turn_context(mod, "grp", B_TG)


def test_a_dm_fact_reaches_a_again(mod, monkeypatch):
    """The positive half: the falsifiers above are not passing because nothing
    is ever injected."""
    _a_keeps_a_secret(mod, monkeypatch)
    telegram_dm(monkeypatch, A_TG)
    ctx = _turn_context(mod, "dm-a-2", A_TG)
    assert A_SECRET in ctx and "[private to this person]" in ctx


def test_a_dm_fact_reaches_a_by_email_too(mod, monkeypatch):
    """One person across channels: A's email turn is A's space."""
    _a_keeps_a_secret(mod, monkeypatch)
    email_turn(monkeypatch, "mail-a", A_EMAIL)
    assert A_SECRET in _turn_context(mod, "mail-a", "webhook:agentmail")


def test_b_cannot_obtain_it_by_remembering_into_a_space(mod, monkeypatch):
    """remember takes no space argument: an injected ``space`` key is ignored
    and the row lands in B's own space."""
    telegram_dm(monkeypatch, B_TG)
    _turn_context(mod, "dm-b", B_TG)
    _call(mod, "dm-b", "remember", {"fact": "B note", "space": "person:p1"})
    rows = mod._D1.query("SELECT space, peer_id FROM peer_preferences WHERE kind = 'memory'")
    assert rows == [{"space": "person:p2", "peer_id": "person:p2"}]


def test_a_cron_digest_for_b_does_not_carry_a_fact(mod, monkeypatch):
    _a_keeps_a_secret(mod, monkeypatch)
    cron_to(monkeypatch, B_TG)
    assert A_SECRET not in _turn_context(mod, "cron-b", "")
    cron_to(monkeypatch, GROUP)
    assert A_SECRET not in _turn_context(mod, "cron-shared", "")


def test_group_memory_is_shared_with_both(mod, monkeypatch):
    telegram_group(monkeypatch, B_TG)
    _turn_context(mod, "grp", B_TG)
    _call(mod, "grp", "remember", {"fact": SHARED_FACT})
    for who in (A_TG, B_TG):
        telegram_dm(monkeypatch, who)
        ctx = _turn_context(mod, f"dm-{who}", who)
        assert SHARED_FACT in ctx and "[shared]" in ctx
    telegram_group(monkeypatch, A_TG)
    assert SHARED_FACT in _turn_context(mod, "grp", A_TG)


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_unresolved_space_refuses_remember_and_writes_nothing(mod, monkeypatch):
    """FALSIFIER: drop ``if resolve(session_id) is None`` in pre_tool_block AND
    the ``space is None`` check in write."""
    telegram_group(monkeypatch, A_TG, chat=OTHER_GROUP)
    blocked = _call(mod, "other", "remember", {"fact": "x"})
    assert (
        blocked and blocked["action"] == "block" and "could not be resolved" in blocked["message"]
    )
    assert mod._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


def test_write_side_refuses_unresolved_even_if_pre_hook_did_not_run(mod, monkeypatch):
    """The second fence: post_tool_call resolves again. FALSIFIER: drop the
    ``space is None`` check in space_memory.write."""
    telegram_dm(monkeypatch, "3003")
    mod.on_post_tool_call(
        tool_name="remember", args={"fact": "x"}, session_id="s", result="{}", status="ok"
    )
    assert mod._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


def test_unresolved_space_injects_nothing(mod, monkeypatch):
    _a_keeps_a_secret(mod, monkeypatch)
    telegram_dm(monkeypatch, "3003")
    assert mod.on_pre_llm_call(session_id="x", sender_id="3003") is None


def test_a_call_another_hook_blocked_is_not_written(mod, monkeypatch):
    """FALSIFIER: drop the ``status in ("blocked", "error")`` check in write."""
    telegram_dm(monkeypatch, A_TG)
    _turn_context(mod, "dm-a", A_TG)
    mod.on_post_tool_call(
        tool_name="remember", args={"fact": "x"}, session_id="dm-a", result="{}", status="blocked"
    )
    assert mod._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


def test_tainted_turn_refuses_remember(mod, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _turn_context(mod, "dm-a", A_TG)
    SESSION_TAINT.mark("dm-a", "unknown_external")
    blocked = _call(mod, "dm-a", "remember", {"fact": "planted"})
    assert blocked and "outside the conversation" in blocked["message"]
    assert mod._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


def test_unreadable_people_block_is_fail_closed_at_register(monkeypatch, tmp_path):
    doc = couples_yaml()
    doc["people"][0]["telegram_id"] = "not-a-number"
    write_seat(monkeypatch, tmp_path, doc)
    m = load_plugin("hermes-smd-peer-memory")
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    from tests.conftest import FakePluginContext

    ctx = FakePluginContext()
    m.register(ctx)
    assert m._SPACE_MODE is True
    assert "remember" in ctx.tools
    telegram_dm(monkeypatch, A_TG)
    assert m.space_memory.resolve("s") is None


def test_legacy_rows_with_no_space_are_never_injected(mod, monkeypatch):
    """A row from before spaces belongs to no space."""
    mod.store.record_preference(
        mod._D1,
        customer_slug="captain-mark",
        peer_id="person:p2",
        persona_slug="",
        preference="legacy note",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="old",
    )
    telegram_dm(monkeypatch, B_TG)
    assert "legacy note" not in _turn_context(mod, "dm-b", B_TG)


# ---------------------------------------------------------------------------
# Preferences: one person, many channels; still space-fenced
# ---------------------------------------------------------------------------


def test_preference_follows_the_person_across_channels(mod, monkeypatch):
    """Telegram id and email unify to person:p1. FALSIFIER: return
    ``sender_id`` instead of ``space.key`` in space_memory.speaker."""
    email_turn(monkeypatch, "mail-a", A_EMAIL)
    _turn_context(mod, "mail-a", "webhook:agentmail")
    _call(
        mod, "mail-a", "record_peer_preference", {"preference": "Short replies", "source": "stated"}
    )
    telegram_dm(monkeypatch, A_TG)
    assert "Short replies" in _turn_context(mod, "dm-a", A_TG)
    rows = mod._D1.query("SELECT peer_id, space FROM peer_preferences")
    assert rows == [{"peer_id": "person:p1", "space": "person:p1"}]


def test_a_dm_preference_is_not_injected_for_a_in_the_group(mod, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    _turn_context(mod, "dm-a", A_TG)
    _call(mod, "dm-a", "record_peer_preference", {"preference": "Remind me about the surprise"})
    telegram_group(monkeypatch, A_TG)
    assert "Remind me about the surprise" not in _turn_context(mod, "grp", A_TG)


def test_b_never_sees_a_preference(mod, monkeypatch):
    telegram_group(monkeypatch, A_TG)
    _turn_context(mod, "grp", A_TG)
    _call(mod, "grp", "record_peer_preference", {"preference": "Ana wants bullet points"})
    telegram_group(monkeypatch, B_TG)
    assert "Ana wants bullet points" not in _turn_context(mod, "grp", B_TG)
    telegram_group(monkeypatch, A_TG)
    assert "Ana wants bullet points" in _turn_context(mod, "grp", A_TG)


def test_group_speaker_who_is_not_a_person_records_nothing(mod, monkeypatch):
    telegram_group(monkeypatch, "3003")
    _turn_context(mod, "grp", "3003")
    _call(mod, "grp", "record_peer_preference", {"preference": "stranger pref"})
    assert mod._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Tool surface + schema migration
# ---------------------------------------------------------------------------


def test_remember_tool_acks_and_validates(mod):
    assert json.loads(mod.space_memory.remember_tool({"fact": " x "})) == {"ok": True, "fact": "x"}
    assert json.loads(mod.space_memory.remember_tool({}))["ok"] is False


def test_remember_is_registered_only_on_a_space_mode_seat(monkeypatch, tmp_path):
    from tests.conftest import FakePluginContext

    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    write_seat(monkeypatch, tmp_path)
    m = load_plugin("hermes-smd-peer-memory")
    ctx = FakePluginContext()
    m.register(ctx)
    assert "remember" in ctx.tools and m._SPACE_MODE is True

    write_seat(monkeypatch, tmp_path, {"customer_id": "scott"})
    m2 = load_plugin("hermes-smd-peer-memory")
    ctx2 = FakePluginContext()
    m2.register(ctx2)
    assert "remember" not in ctx2.tools and m2._SPACE_MODE is False


def test_non_space_seat_pre_tool_call_is_a_no_op(monkeypatch, tmp_path):
    """REGRESSION: on a seat without people:, the new hook never blocks."""
    m = load_plugin("hermes-smd-peer-memory")
    assert m._SPACE_MODE is False
    telegram_dm(monkeypatch, "3003")
    assert m.on_pre_tool_call(tool_name="record_peer_preference", args={}, session_id="s") is None


def test_ensure_schema_migrates_a_table_created_before_spaces(tmp_path):
    """The table's migration: an old table gains space + kind; old rows read
    '' / 'preference'; a second run is a no-op."""
    m = load_plugin("hermes-smd-peer-memory")
    c = D1Client(binding_name="UNUSED", customer_slug="testco", db_path=str(tmp_path / "old.db"))
    c.execute(
        "CREATE TABLE peer_preferences (id TEXT PRIMARY KEY, customer_slug TEXT NOT NULL, "
        "peer_id TEXT NOT NULL, persona_slug TEXT NOT NULL DEFAULT '', preference TEXT NOT NULL, "
        "why TEXT, how_to_apply TEXT, source TEXT NOT NULL DEFAULT 'stated' "
        "CHECK (source IN ('stated', 'demonstrated')), session_id TEXT NOT NULL DEFAULT '', "
        "recorded_at TEXT NOT NULL DEFAULT (datetime('now')), superseded_by TEXT)"
    )
    c.execute(
        "INSERT INTO peer_preferences (id, customer_slug, peer_id, preference) "
        "VALUES ('old1', 'testco', 'x@y.com', 'old pref')"
    )
    m.store.ensure_schema(c)
    m.store.ensure_schema(c)
    cols = {r["name"] for r in c.query("PRAGMA table_info(peer_preferences)")}
    assert {"space", "kind"} <= cols
    row = c.query("SELECT space, kind FROM peer_preferences WHERE id = 'old1'")[0]
    assert row == {"space": "", "kind": "preference"}
    # The legacy read path still sees the old row.
    assert [r["preference"] for r in m.store.active_preferences(c, peer_id="x@y.com")] == [
        "old pref"
    ]


def test_restatement_supersedes_only_within_its_space(mod):
    for sp in ("person:p1", "shared", "person:p1"):
        mod.store.record_preference(
            mod._D1,
            customer_slug="captain-mark",
            peer_id="person:p1",
            persona_slug="",
            preference="Same words",
            why=None,
            how_to_apply=None,
            source="stated",
            session_id="s",
            space=sp,
            kind="memory",
        )
    live = mod._D1.query(
        "SELECT space FROM peer_preferences WHERE superseded_by IS NULL ORDER BY space"
    )
    assert [r["space"] for r in live] == ["person:p1", "shared"]


def test_session_origin_register_is_clean_between_tests():
    assert SESSION_INBOUND_ORIGIN.get("mail-a") is None
