"""What a tainted turn may still keep on a seat a couple shares (ss-console #3283).

A web search taints the session. On a couples seat a tainted turn may still
write into its OWN private space, and a record into the shared one (each stamped
from_web: either partner adds to the shared space alone, Captain decision
2026-10-19); a scheduled run into its own space; a seat without people: keeps
the old rule. Each falsifier names the line whose removal
makes it fail.
"""

from __future__ import annotations

import pytest

from shared import record_store, space, space_taint
from shared.d1_client import D1Client
from shared.inbound import SESSION_TAINT
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_TG,
    GROUP,
    clear_turn,
    cron_to,
    telegram_dm,
    telegram_group,
    write_seat,
)

PRICE = "Flight PHX-LIS on Oct 30 is $612 (web)"


def _rec(owner: str, body: str = PRICE) -> str:
    return f"---\nowner: {owner}\nkind: price-watch\n---\n{body}\n"


@pytest.fixture
def seat(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    roots = {"trips": tmp_path / "trips", "lists": tmp_path / "lists"}
    policies = {
        "trips": record_store.StorePolicy(root=roots["trips"], owner_field="owner"),
        "lists": record_store.StorePolicy(root=roots["lists"]),
    }
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: policies)
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: roots)
    SESSION_TAINT._tainted.clear()
    SESSION_TAINT.mark("s", "unknown_external")  # a web_search on this session
    yield {
        "records": load_plugin("hermes-smd-record-store"),
        "gate": load_plugin("hermes-smd-space-gate"),
    }
    SESSION_TAINT._tainted.clear()
    space._reset_cache_for_tests()


def _write(seat, store: str, content: str, name: str = "watch.md") -> tuple[dict, list]:
    """Run the write the way the gateway does: every pre_tool_call hook, space
    gate first (plugin.yaml order); a block from any of them blocks."""
    args = {"store": store, "name": name, "content": content}
    blocks = []
    for plugin in (seat["gate"], seat["records"]):
        out = plugin.on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s")
        if out:
            blocks.append(out)
    return args, blocks


def test_tainted_dm_write_to_own_space_is_allowed_and_stamped(seat, monkeypatch):
    """FALSIFIER: return False from the first branch of tainted_write_allowed."""
    telegram_dm(monkeypatch, A_TG)
    args, blocks = _write(seat, "trips", _rec("p1"))
    assert blocks == []
    assert "from_web: true" in args["content"] and "owner: p1" in args["content"]


def test_tainted_dm_record_write_to_shared_lands_stamped(seat, monkeypatch):
    """Either partner adds to the shared space alone; a record from a turn that
    read the web says so. FALSIFIER: drop the shared branch of
    tainted_write_allowed, or the stamp in the record-store hook."""
    telegram_dm(monkeypatch, A_TG)
    args, blocks = _write(seat, "trips", _rec("shared"))
    assert blocks == []
    assert PRICE in args["content"] and args["content"].count("from_web: true") == 1
    args, blocks = _write(seat, "lists", "milk")
    assert blocks == [] and args["content"].startswith("---\nfrom_web: true\n---")


def test_tainted_group_write_is_refused(seat, monkeypatch):
    """Only a PRIVATE space is narrowed: the group writes nothing when tainted."""
    telegram_group(monkeypatch, B_TG)
    _, blocks = _write(seat, "lists", "milk")
    assert any("outside the firm" in b["message"] for b in blocks)


def test_tainted_cron_writes_only_its_own_space(seat, monkeypatch):
    cron_to(monkeypatch, GROUP)
    _, blocks = _write(seat, "lists", "price dropped")
    assert blocks == []
    cron_to(monkeypatch, A_TG)
    _, blocks = _write(seat, "trips", _rec("p1"))
    assert blocks == []
    _, blocks = _write(seat, "trips", _rec("shared"))
    assert any("outside the firm" in b["message"] for b in blocks)


def test_tainted_send_is_not_narrowed(seat, monkeypatch):
    """Only a record write is narrowed into shared; a send on a tainted turn is
    still refused by the trust gate
    (tests/test_brief_taint_exemption.py::test_the_model_callable_send_is_still_refused_on_a_tainted_turn,
    untouched here). FALSIFIER: drop the ``tool_name == RECORD_WRITE`` term."""
    telegram_dm(monkeypatch, A_TG)
    assert not space_taint.tainted_write_allowed("mcp_agentmail_send_message", "s", "shared")
    assert space_taint.tainted_write_allowed("record_store_write", "s", "shared")


def test_seat_without_people_keeps_the_old_rule(monkeypatch, tmp_path):
    """REGRESSION: on a brokery-shaped seat a tainted write is refused, exactly
    as before (the resolver never resolves there, and the record-store
    narrowing also checks space mode). FALSIFIER: drop the tainted-write
    refusal in the record-store hook."""
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path, {"customer_id": "brokery"})
    root = tmp_path / "ohv"
    policy = record_store.StorePolicy(root=root, owner_field="agent")
    monkeypatch.setattr(record_store, "authored_policies", lambda *_, **__: {"ohv": policy})
    monkeypatch.setattr(record_store, "authored_stores", lambda *_, **__: {"ohv": root})
    SESSION_TAINT._tainted.clear()
    SESSION_TAINT.mark("s", "unknown_external")
    plugin = load_plugin("hermes-smd-record-store")
    telegram_dm(monkeypatch, A_TG)
    args = {"store": "ohv", "name": "a.md", "content": f"---\nagent: {A_EMAIL}\n---\nn"}
    out = plugin.on_pre_tool_call(tool_name="record_store_write", args=args, session_id="s")
    assert out and "outside the firm" in out["message"]
    assert "from_web" not in args["content"]
    SESSION_TAINT._tainted.clear()


def test_stamp_is_idempotent_and_adds_frontmatter_when_absent():
    once = space_taint.stamp_from_web("---\nowner: p1\n---\nbody")
    assert once == "---\nowner: p1\nfrom_web: true\n---\nbody"
    assert space_taint.stamp_from_web(once) == once
    assert space_taint.stamp_from_web("milk") == "---\nfrom_web: true\n---\nmilk"


# ---------------------------------------------------------------------------
# remember on a tainted turn
# ---------------------------------------------------------------------------


@pytest.fixture
def mem(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path)
    SESSION_TAINT._tainted.clear()
    SESSION_TAINT.mark("s", "unknown_external")
    m = load_plugin("hermes-smd-peer-memory")
    client = D1Client(
        binding_name="UNUSED", customer_slug="captain-mark", db_path=str(tmp_path / "m.db")
    )
    m.store.ensure_schema(client)
    m.bind_runtime(customer_slug="captain-mark", client=client, space_mode=True)
    yield m
    SESSION_TAINT._tainted.clear()
    space._reset_cache_for_tests()


def _remember(m, tool: str = "remember", args: dict | None = None):
    args = args or {"fact": PRICE}
    m.on_pre_llm_call(
        session_id="s", sender_id=__import__("os").environ.get("HERMES_SESSION_USER_ID", "")
    )
    blocked = m.on_pre_tool_call(tool_name=tool, args=args, session_id="s")
    m.on_post_tool_call(
        tool_name=tool,
        args=args,
        session_id="s",
        result="{}",
        status="blocked" if blocked else "ok",
    )
    return blocked


def test_tainted_remember_in_own_dm_is_kept_marked_from_web(mem, monkeypatch):
    """FALSIFIER: drop _tainted_remember_allowed from space_memory.write."""
    telegram_dm(monkeypatch, A_TG)
    assert _remember(mem) is None
    rows = mem._D1.query("SELECT space, from_web FROM peer_preferences")
    assert rows == [{"space": "person:p1", "from_web": 1}]
    ctx = mem.on_pre_llm_call(session_id="s2", sender_id=A_TG)["context"]
    assert "from a web search" in ctx


def test_tainted_remember_in_the_group_is_refused(mem, monkeypatch):
    telegram_group(monkeypatch, A_TG)
    assert _remember(mem)
    assert mem._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0


def test_a_preference_is_never_captured_on_a_tainted_turn(mem, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    assert _remember(mem, "record_peer_preference", {"preference": "Always book window seats"})
    assert mem._D1.query("SELECT COUNT(*) AS n FROM peer_preferences")[0]["n"] == 0
