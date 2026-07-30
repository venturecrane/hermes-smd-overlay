"""Per-person token metering (ss-console #2070 O4).

The meter answers "whose usage is this seat's spend?" — the question the
sustained-dialogue program raises and the nightly workspace-level cost plane
cannot. These tests pin the aggregation, the attribution split (inbound sender
vs system fallback), and the hard rule that metering can never fail a turn.
"""

from __future__ import annotations

import pytest

from shared import inbound
from tests.conftest import load_plugin

_MOD = load_plugin("hermes-smd-usage")
usage_store = _MOD.usage_store


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    inbound.SESSION_INBOUND_ORIGIN._unbound.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    inbound.SESSION_INBOUND_ORIGIN._unbound.clear()


@pytest.fixture
def meter(monkeypatch, tmp_path):
    """The plugin wired to a temp meter db."""
    store = usage_store.UsageStore(str(tmp_path / "agent_state.db"))
    monkeypatch.setattr(_MOD, "_STORE", store, raising=False)
    yield _MOD, store
    store.close()


_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_tokens": 50,
    "cache_write_tokens": 10,
    "reasoning_tokens": 5,
    "request_count": 1,
}


def test_attributes_to_the_inbound_sender(meter) -> None:
    mod, store = meter
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1", inbound.InboundOrigin("Greg@Whitfield.example", "m1", "", "inbox_x")
    )
    mod.on_post_api_request(
        session_id="s1", platform="webhook", model="claude-opus-5", usage=_USAGE
    )
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0]["attributed_to"] == "greg@whitfield.example"  # normalized
    assert rows[0]["attribution_source"] == "inbound_origin"
    assert rows[0]["input_tokens"] == 1000 and rows[0]["output_tokens"] == 200
    assert rows[0]["requests"] == 1


def test_falls_back_to_system_platform(meter) -> None:
    """Cron, skills, delegated sub-agents: no inbound origin, so the honest
    answer is the platform, not a guessed person."""
    mod, store = meter
    mod.on_post_api_request(
        session_id="cron-session", platform="cron", model="claude-sonnet-5", usage=_USAGE
    )
    rows = store.rows()
    assert rows[0]["attributed_to"] == "system:cron"
    assert rows[0]["attribution_source"] == "fallback"


def test_missing_platform_still_attributes(meter) -> None:
    mod, store = meter
    mod.on_post_api_request(session_id="", platform="", model="m", usage=_USAGE)
    assert store.rows()[0]["attributed_to"] == "system:unknown"


def test_aggregates_by_day_person_and_model(meter) -> None:
    mod, store = meter
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1", inbound.InboundOrigin("greg@x.test", "m1", "", "inbox_x")
    )
    for _ in range(3):
        mod.on_post_api_request(
            session_id="s1", platform="webhook", model="claude-opus-5", usage=_USAGE
        )
    # A different model on the same day is its own row.
    mod.on_post_api_request(
        session_id="s1", platform="webhook", model="claude-sonnet-5", usage=_USAGE
    )
    rows = {r["model"]: r for r in store.rows()}
    assert rows["claude-opus-5"]["requests"] == 3
    assert rows["claude-opus-5"]["input_tokens"] == 3000
    assert rows["claude-opus-5"]["output_tokens"] == 600
    assert rows["claude-sonnet-5"]["requests"] == 1


def test_partial_usage_buckets_are_zero_not_poison(meter) -> None:
    """Providers report different buckets; a missing or junk one must not
    discard the request."""
    mod, store = meter
    mod.on_post_api_request(
        session_id="",
        platform="cron",
        model="m",
        usage={"input_tokens": 10, "output_tokens": None, "cache_read_tokens": "junk"},
    )
    row = store.rows()[0]
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 0 and row["cache_read_tokens"] == 0
    assert row["requests"] == 1


def test_malformed_usage_is_a_no_op(meter) -> None:
    mod, store = meter
    mod.on_post_api_request(session_id="s1", platform="cron", model="m", usage="not-a-dict")
    mod.on_post_api_request(session_id="s1", platform="cron", model="m")
    assert store.rows() == []


def test_metering_never_raises_into_the_hook(meter, monkeypatch) -> None:
    """Metering observes; it must never be able to fail a turn."""
    mod, store = meter

    def _boom(**_kw):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(store, "record", _boom)
    mod.on_post_api_request(session_id="s1", platform="cron", model="m", usage=_USAGE)  # no raise


def test_no_store_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr(_MOD, "_STORE", None, raising=False)
    _MOD.on_post_api_request(session_id="s1", platform="cron", model="m", usage=_USAGE)


def test_register_wires_the_hook(fake_ctx, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMD_D1_AGENT_STATE_BINDING", str(tmp_path / "agent_state.db"))
    _MOD.register(fake_ctx)
    assert "post_api_request" in fake_ctx.registered


def test_register_without_a_binding_still_wires_but_disables(fake_ctx, monkeypatch) -> None:
    """A seat with no agent-state binding gets no meter — never a broken turn."""
    monkeypatch.delenv("SMD_D1_AGENT_STATE_BINDING", raising=False)
    monkeypatch.delenv("SMD_D1_AUDIT_BINDING", raising=False)
    _MOD.register(fake_ctx)
    assert "post_api_request" in fake_ctx.registered
    assert _MOD._STORE is None
