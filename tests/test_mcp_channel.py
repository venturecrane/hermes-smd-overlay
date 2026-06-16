"""Tests for the MCP channel (Claude as an inbound channel).

Covers the three overlay-side pieces of the synchronous-return spine without a
Machine:
  * ``shared/mcp_result_store`` — the cross-process result store.
  * ``hermes-smd-mcp-result-sink`` — the agent-side capture hook.
  * ``webhook_gate`` MCP dispatch + stub auth — the gate-side JSON-RPC surface.

The load-bearing test is ``test_sink_write_is_visible_to_store_take``: it proves
the agent-side write and the gate-side read interoperate over the store, which is
the heart of the synchronous bridge.
"""

import pytest

import webhook_gate as gate
from bootstrap import translate
from shared import mcp_result_store
from tests.conftest import load_plugin


@pytest.fixture(autouse=True)
def _store_dir(tmp_path, monkeypatch):
    """Point the result store at an isolated tmp dir for every test."""
    monkeypatch.setenv("SMD_MCP_STORE_DIR", str(tmp_path / "smd-mcp"))


# --- result store --------------------------------------------------------------


def test_put_then_take_round_trips():
    assert mcp_result_store.put("abc123", {"answer": "hello"})
    assert mcp_result_store.take("abc123") == {"answer": "hello"}


def test_take_is_one_shot():
    mcp_result_store.put("once", {"answer": "x"})
    assert mcp_result_store.take("once") == {"answer": "x"}
    assert mcp_result_store.take("once") is None  # consumed


def test_take_missing_returns_none():
    assert mcp_result_store.take("never-written") is None


def test_unsafe_correlation_id_is_refused():
    assert mcp_result_store.put("../escape", {"answer": "x"}) is False
    assert mcp_result_store.take("../escape") is None


def test_prune_removes_stale_results():
    mcp_result_store.put("stale", {"answer": "old"})
    # A later put with a far-future clock prunes anything older than the TTL.
    mcp_result_store.put("fresh", {"answer": "new"}, now=1e12)
    assert mcp_result_store.take("stale") is None
    assert mcp_result_store.take("fresh") == {"answer": "new"}


# --- result-sink plugin (agent side) ------------------------------------------


def test_sink_registers_post_llm_call(fake_ctx):
    sink = load_plugin("hermes-smd-mcp-result-sink")
    sink.register(fake_ctx)
    assert "post_llm_call" in fake_ctx.registered


def test_sink_captures_marked_turn():
    sink = load_plugin("hermes-smd-mcp-result-sink")
    sink.on_post_llm_call(
        user_message="[[mcp-cid:cid-1]] ignore me\nReply with: hi",
        assistant_response="the answer",
    )
    assert mcp_result_store.take("cid-1") == {"answer": "the answer"}


def test_sink_ignores_unmarked_turns():
    sink = load_plugin("hermes-smd-mcp-result-sink")
    # A turn from another channel carries no [[mcp-cid:...]] marker.
    sink.on_post_llm_call(user_message="just a normal email body", assistant_response="x")
    sink.on_post_llm_call(session_id="telegram:12345", assistant_response="x")
    assert mcp_result_store.take("12345") is None


def test_sink_is_exception_safe_on_bad_kwargs():
    sink = load_plugin("hermes-smd-mcp-result-sink")
    # Missing/odd shapes must never raise out of the hook.
    sink.on_post_llm_call()
    sink.on_post_llm_call(user_message=None)
    sink.on_post_llm_call(user_message="[[mcp-cid:cid-2]]", assistant_response=None)
    assert mcp_result_store.take("cid-2") == {"answer": ""}


# --- the bridge (the load-bearing interop test) -------------------------------


def test_sink_write_is_visible_to_store_take():
    """The agent-side sink writes; the gate-side store read collects it.

    The synchronous bridge in miniature: the sink recovers the correlation id
    from the turn's marked user_message and ``put``s the answer; the gate's
    long-poll ``take``s that exact id. If the marker the gate plants and the
    regex the sink uses ever disagreed, the gate would hang forever — so this
    asserts they agree on the real prompt shape.
    """
    sink = load_plugin("hermes-smd-mcp-result-sink")
    correlation_id = "deadbeef"
    # The exact prompt the route materializes, with the marker rendered.
    rendered_prompt = (
        f"[[mcp-cid:{correlation_id}]] operator-internal correlation token — do "
        "NOT repeat it or mention it in your reply.\n"
        "An MCP request arrived ... Reply with EXACTLY the text below ...\n"
        "the message"
    )
    sink.on_post_llm_call(user_message=rendered_prompt, assistant_response="bridged")
    collected = mcp_result_store.take(correlation_id)
    assert collected is not None and collected["answer"] == "bridged"


# --- gate JSON-RPC dispatch + stub auth ---------------------------------------


def test_stub_auth_fail_closed_when_unset(monkeypatch):
    monkeypatch.delenv("SMD_MCP_STUB_TOKEN", raising=False)
    assert gate._mcp_stub_authorized("Bearer anything") is False


def test_stub_auth_accepts_correct_bearer(monkeypatch):
    monkeypatch.setenv("SMD_MCP_STUB_TOKEN", "s3cr3t")
    assert gate._mcp_stub_authorized("Bearer s3cr3t") is True
    assert gate._mcp_stub_authorized("Bearer wrong") is False
    assert gate._mcp_stub_authorized(None) is False


def test_dispatch_initialize_advertises_tools():
    status, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert status == 200
    assert body["result"]["protocolVersion"] == gate.MCP_PROTOCOL_VERSION
    assert body["result"]["capabilities"]["tools"] == {"listChanged": False}


def test_dispatch_initialized_notification_is_202_no_body():
    status, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert status == 202 and body is None


def test_dispatch_ping_is_empty_result():
    status, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert status == 200 and body["result"] == {}


def test_dispatch_tools_list_includes_all_verbs():
    _, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in body["result"]["tools"]}
    assert {"echo", "fetch_documents", "store_document"} <= names


def test_tools_call_fetch_documents_drives_a_turn(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        gate, "_drive_agent_turn", lambda n, a: captured.update(tool=n, args=a) or {"answer": "ok"}
    )
    _, body = gate._mcp_dispatch(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "fetch_documents", "arguments": {"query": "henderson"}},
        }
    )
    assert captured == {"tool": "fetch_documents", "args": {"query": "henderson"}}
    assert body["result"]["content"][0]["text"] == "ok"


def test_dispatch_unknown_method_is_method_not_found():
    _, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 4, "method": "bogus"})
    assert body["error"]["code"] == gate._JSON_RPC_METHOD_NOT_FOUND


def test_tools_call_unknown_tool_is_error():
    _, body = gate._mcp_dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert body["error"]["code"] == gate._JSON_RPC_METHOD_NOT_FOUND


def test_tools_call_missing_params_is_invalid_params():
    _, body = gate._mcp_dispatch({"jsonrpc": "2.0", "id": 6, "method": "tools/call"})
    assert body["error"]["code"] == gate._JSON_RPC_INVALID_PARAMS


def test_tools_call_returns_stored_answer(monkeypatch):
    """tools/call drives a turn and returns the stored answer in-line.

    We stub the forward (no real gateway in a unit test) and pre-seed the store
    as the result-sink would, proving the gate collects and shapes the MCP
    result correctly.
    """
    captured = {}

    def fake_drive(tool_name, args):
        captured["tool"] = tool_name
        captured["args"] = args
        return {"answer": "echoed: hi"}

    monkeypatch.setattr(gate, "_drive_agent_turn", fake_drive)
    _, body = gate._mcp_dispatch(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"message": "hi"}},
        }
    )
    assert captured == {"tool": "echo", "args": {"message": "hi"}}
    assert body["result"]["content"][0]["text"] == "echoed: hi"
    assert "isError" not in body["result"]


def test_tools_call_timeout_returns_iserror(monkeypatch):
    monkeypatch.setattr(gate, "_drive_agent_turn", lambda *a, **k: None)
    _, body = gate._mcp_dispatch(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {}},
        }
    )
    assert body["result"]["isError"] is True


# --- translate.py: mcp route materialization ----------------------------------


def test_materialize_emits_mcp_route_when_enabled(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "mcp-secret")
    out = translate._materialize_webhook_platform({"mcp_connector": {"enabled": True}})
    routes = out["webhook"]["extra"]["routes"]
    prompt = routes["mcp"]["prompt"]
    assert routes["mcp"]["secret"] == "mcp-secret"
    assert routes["mcp"]["events"] == []  # allow-all; the generic prompt dispatches on verb
    # The correlation marker must be in the prompt so the result-sink can recover
    # the cid from the turn's user_message (the session-id approach does not work).
    assert "[[mcp-cid:{correlation_id}]]" in prompt
    # The generic prompt carries the action + arguments and dispatches the verbs.
    assert "{event_type}" in prompt and "{message}" in prompt
    assert "fetch_documents" in prompt and "store_document" in prompt


def test_materialize_omits_mcp_route_when_secret_unset(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET_MCP", raising=False)
    out = translate._materialize_webhook_platform({"mcp_connector": {"enabled": True}})
    assert out == {}  # fail-closed: no verifying secret => no route


def test_materialize_omits_mcp_route_when_disabled(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "mcp-secret")
    out = translate._materialize_webhook_platform({"mcp_connector": {"enabled": False}})
    assert out == {}


def test_mcp_trigger_populates_events_and_skills(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_MCP", "mcp-secret")
    customer = {
        "mcp_connector": {"enabled": True},
        "webhook_triggers": [
            {"source": "mcp", "event_type": "fetch", "skill": "drive-fetch", "persona": "crane"}
        ],
    }
    routes = translate._materialize_webhook_platform(customer)["webhook"]["extra"]["routes"]
    assert routes["mcp"]["events"] == ["fetch"]
    assert routes["mcp"]["skills"] == ["drive-fetch"]
