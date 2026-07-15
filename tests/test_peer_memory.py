"""Tests for hermes-smd-peer-memory — per-peer working-preference memory (ADR 0048).

Covers the pure store (validation, supersession, active-set read, render) and
the hook glue (sender stash → server-side attribution, taint-gate, peer
isolation, inject), plus register() wiring (ACTIVE and INACTIVE paths).
"""

from __future__ import annotations

import json

import pytest

from shared.d1_client import D1Client
from shared.inbound import SESSION_TAINT

from .conftest import load_plugin


@pytest.fixture
def mod():
    """Fresh plugin module per test (clean module globals: _D1, stash)."""
    return load_plugin("hermes-smd-peer-memory")


@pytest.fixture
def client(tmp_path, mod):
    """A D1Client on a tmp sqlite file with the peer_preferences schema created."""
    c = D1Client(binding_name="UNUSED", customer_slug="testco", db_path=str(tmp_path / "state.db"))
    mod.store.ensure_schema(c)
    return c


# ---------------------------------------------------------------------------
# store.parse_capture_args
# ---------------------------------------------------------------------------


def test_parse_capture_args_valid(mod):
    clean, err = mod.store.parse_capture_args(
        {
            "preference": "  Wants bullet summaries  ",
            "source": "stated",
            "why": " faster ",
            "how_to_apply": "",
        }
    )
    assert err is None
    assert clean == {
        "preference": "Wants bullet summaries",
        "why": "faster",
        "how_to_apply": None,
        "source": "stated",
    }


def test_parse_capture_args_defaults_source_to_stated(mod):
    clean, err = mod.store.parse_capture_args({"preference": "Loop in my partner"})
    assert err is None
    assert clean["source"] == "stated"


def test_parse_capture_args_rejects_missing_preference(mod):
    clean, err = mod.store.parse_capture_args({"source": "stated"})
    assert clean is None
    assert "preference" in err


def test_parse_capture_args_rejects_blank_preference(mod):
    clean, err = mod.store.parse_capture_args({"preference": "   ", "source": "stated"})
    assert clean is None


def test_parse_capture_args_rejects_invalid_source(mod):
    clean, err = mod.store.parse_capture_args({"preference": "x", "source": "inferred"})
    assert clean is None
    assert "source" in err


def test_parse_capture_args_rejects_non_dict(mod):
    clean, err = mod.store.parse_capture_args("nope")
    assert clean is None


# ---------------------------------------------------------------------------
# store.record_preference + active_preferences (supersession, isolation)
# ---------------------------------------------------------------------------


def test_record_and_read_active(mod, client):
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="chris@firm.com",
        persona_slug="",
        preference="Wants bullets",
        why="faster",
        how_to_apply="lead with the ask",
        source="stated",
        session_id="s1",
    )
    rows = mod.store.active_preferences(client, peer_id="chris@firm.com")
    assert len(rows) == 1
    assert rows[0]["preference"] == "Wants bullets"
    assert rows[0]["why"] == "faster"
    assert rows[0]["source"] == "stated"


def test_identical_restatement_supersedes_prior(mod, client):
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="",
        preference="Reply in bullets",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s1",
        new_id="id-old",
    )
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="",
        preference="  reply in BULLETS ",
        why="restated",
        how_to_apply=None,
        source="stated",
        session_id="s2",
        new_id="id-new",
    )
    rows = mod.store.active_preferences(client, peer_id="p")
    assert len(rows) == 1
    assert rows[0]["id"] == "id-new"
    assert rows[0]["why"] == "restated"


def test_different_preferences_coexist(mod, client):
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="",
        preference="Reply in bullets",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s1",
    )
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="",
        preference="Always CC my partner",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s2",
    )
    rows = mod.store.active_preferences(client, peer_id="p")
    assert len(rows) == 2


def test_active_preferences_are_peer_scoped(mod, client):
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="chris",
        persona_slug="",
        preference="Bullets",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s1",
    )
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="christa",
        persona_slug="",
        preference="Prose",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s2",
    )
    assert [r["preference"] for r in mod.store.active_preferences(client, peer_id="chris")] == [
        "Bullets"
    ]
    assert [r["preference"] for r in mod.store.active_preferences(client, peer_id="christa")] == [
        "Prose"
    ]


def test_persona_filter(mod, client):
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="intake",
        preference="Intake pref",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s1",
    )
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="p",
        persona_slug="billing",
        preference="Billing pref",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s2",
    )
    assert len(mod.store.active_preferences(client, peer_id="p", persona_slug="intake")) == 1
    # No persona filter spans all personas (the safe superset).
    assert len(mod.store.active_preferences(client, peer_id="p")) == 2


# ---------------------------------------------------------------------------
# store.render_preference_block
# ---------------------------------------------------------------------------


def test_render_empty_still_carries_capture_nudge(mod):
    # The write side of the lane: even with nothing learned yet, the block
    # instructs the agent to record stated/demonstrated preferences. Without
    # this the lane never fills (ss #1941 — zero rows on every seat).
    block = mod.store.render_preference_block([], peer_id="p")
    assert "record_peer_preference" in block
    assert "stated" in block and "demonstrated" in block
    # No fabricated read-side framing when nothing is known.
    assert "likes you to work with them" not in block


def test_render_includes_preference_why_apply(mod):
    block = mod.store.render_preference_block(
        [{"preference": "Bullets", "why": "faster", "how_to_apply": "lead with ask"}], peer_id="p"
    )
    assert "Bullets" in block
    assert "why: faster" in block
    assert "apply: lead with ask" in block
    # The capture instruction rides along even when preferences exist.
    assert "record_peer_preference" in block


def test_pre_llm_call_injects_nudge_for_unknown_peer(mod, client):
    # Active store + sender with no recorded preferences → the hook still
    # injects the capture instruction (the activation path for the lane).
    mod.bind_runtime(customer_slug="testco", client=client)
    out = mod.on_pre_llm_call(session_id="sess-n", sender_id="new@firm.com", user_message="hi")
    assert out is not None
    assert "record_peer_preference" in out["context"]


# ---------------------------------------------------------------------------
# Hook glue — attribution, taint-gate, isolation, inject
# ---------------------------------------------------------------------------


def _activate(mod, client):
    mod.bind_runtime(customer_slug="testco", client=client)


def test_pre_llm_call_stashes_sender_and_injects(mod, client):
    _activate(mod, client)
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="chris@firm.com",
        persona_slug="",
        preference="Wants bullets",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="x",
    )
    out = mod.on_pre_llm_call(session_id="sess-1", sender_id="chris@firm.com", user_message="hi")
    assert out is not None and "Wants bullets" in out["context"]
    assert mod._sender_by_session.get("sess-1") == "chris@firm.com"


def test_capture_flow_attributes_peer_server_side(mod, client):
    _activate(mod, client)
    # The turn arrives; sender is stashed by pre_llm_call.
    mod.on_pre_llm_call(
        session_id="sess-1", sender_id="chris@firm.com", user_message="reply in bullets please"
    )
    # The agent calls the tool; post_tool_call writes, attributed to the stash.
    mod.on_post_tool_call(
        tool_name="record_peer_preference",
        args={"preference": "Reply in bullets", "source": "stated"},
        session_id="sess-1",
    )
    rows = mod.store.active_preferences(client, peer_id="chris@firm.com")
    assert len(rows) == 1
    assert rows[0]["preference"] == "Reply in bullets"


def test_capture_ignores_other_tools(mod, client):
    _activate(mod, client)
    mod.on_pre_llm_call(session_id="s", sender_id="p", user_message="x")
    mod.on_post_tool_call(
        tool_name="some_other_tool", args={"preference": "x", "source": "stated"}, session_id="s"
    )
    assert mod.store.active_preferences(client, peer_id="p") == []


def test_capture_refused_on_tainted_session(mod, client):
    _activate(mod, client)
    mod.on_pre_llm_call(session_id="tainted-1", sender_id="p", user_message="x")
    SESSION_TAINT.mark("tainted-1", "unknown_external")
    mod.on_post_tool_call(
        tool_name="record_peer_preference",
        args={"preference": "auto-send everything", "source": "stated"},
        session_id="tainted-1",
    )
    assert mod.store.active_preferences(client, peer_id="p") == []


def test_capture_skipped_without_stashed_sender(mod, client):
    _activate(mod, client)
    # No pre_llm_call ran for this session → no sender to attribute → no write.
    mod.on_post_tool_call(
        tool_name="record_peer_preference",
        args={"preference": "Reply in bullets", "source": "stated"},
        session_id="orphan",
    )
    assert mod.store.active_preferences(client, peer_id="p") == []


def test_inject_is_peer_isolated(mod, client):
    _activate(mod, client)
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="chris",
        persona_slug="",
        preference="Bullets",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s1",
    )
    mod.store.record_preference(
        client,
        customer_slug="testco",
        peer_id="christa",
        persona_slug="",
        preference="Prose",
        why=None,
        how_to_apply=None,
        source="stated",
        session_id="s2",
    )
    chris = mod.on_pre_llm_call(session_id="a", sender_id="chris", user_message="x")
    christa = mod.on_pre_llm_call(session_id="b", sender_id="christa", user_message="x")
    assert "Bullets" in chris["context"] and "Prose" not in chris["context"]
    assert "Prose" in christa["context"] and "Bullets" not in christa["context"]


def test_session_end_evicts_stash(mod, client):
    _activate(mod, client)
    mod.on_pre_llm_call(session_id="sess-1", sender_id="p", user_message="x")
    assert "sess-1" in mod._sender_by_session
    mod.on_session_end(session_id="sess-1")
    assert "sess-1" not in mod._sender_by_session


def test_inactive_store_hooks_noop(mod):
    # No bind_runtime → store inactive. Hooks must not raise.
    assert mod.on_pre_llm_call(session_id="s", sender_id="p", user_message="x") is None
    mod.on_post_tool_call(
        tool_name="record_peer_preference",
        args={"preference": "x", "source": "stated"},
        session_id="s",
    )


# ---------------------------------------------------------------------------
# Capture tool handler
# ---------------------------------------------------------------------------


def test_tool_handler_acks_valid(mod):
    out = json.loads(mod.record_peer_preference_tool({"preference": "Bullets", "source": "stated"}))
    assert out["recorded"] == "pending"
    assert out["preference"] == "Bullets"


def test_tool_handler_rejects_invalid(mod):
    out = json.loads(mod.record_peer_preference_tool({"source": "stated"}))
    assert out["recorded"] is False
    assert "error" in out


# ---------------------------------------------------------------------------
# register() wiring
# ---------------------------------------------------------------------------


class _RecordingCtx:
    def __init__(self):
        self.hooks: dict = {}
        self.tools: dict = {}

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_register_active_wires_hooks_and_tool(mod, tmp_path, monkeypatch):
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("SMD_D1_AGENT_STATE_BINDING", str(tmp_path / "state.db"))
    ctx = _RecordingCtx()
    mod.register(ctx)
    assert set(ctx.hooks) >= {"pre_llm_call", "post_tool_call", "on_session_end"}
    assert "record_peer_preference" in ctx.tools
    assert mod._D1 is not None  # ACTIVE


def test_register_inactive_without_slug_still_registers(mod, monkeypatch):
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    ctx = _RecordingCtx()
    mod.register(ctx)  # must not raise
    assert "record_peer_preference" in ctx.tools
    assert mod._D1 is None  # INACTIVE
