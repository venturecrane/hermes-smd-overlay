"""A send whose answer was lost must never be offered for a retry.

Until this test existed, every broker send failure reached the agent as the same
sentence: "it was not sent. You can ask me to retry." That is true of a refusal
the broker makes before calling the mail vendor, and of a broker that was never
reached. It is false when the request was written and the answer was lost (a
socket timeout or reset after the write, an empty or unreadable reply, or the
broker reporting that the vendor call itself failed in transit). There the
message may already be out, and the retry sends it twice.

Three outcomes, three answers, each pinned end to end through a real Unix socket
standing in for the broker:

1. A definite refusal or vendor "no" in the broker's reply: not sent, retry fine.
2. Unknown: do NOT resend; SMD checks (the broker wrote its intent row first, so
   the console's send reconciler flags an intent with no result).
3. Connect-time failure, nothing delivered: not sent, retry fine.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from collections.abc import Callable, Iterator

import pytest

from shared import agentmail_broker, msgraph_broker, workspace_broker
from shared.inbound import SESSION_TAINT
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin

ALLOWED = "7367659986"
TO = "client@example.com"
_AGENTMAIL_SEAT_YAML = (
    "customer_id: acme\nvertical: law-firm\nconnectors:\n  Email:\n"
    "    adapter: agentmail\n    backend: mcp:agentmail\n    enabled: true\n"
)


# ---------------------------------------------------------------------------
# A real broker socket, so the transport faults are the real ones.
# ---------------------------------------------------------------------------


def _reply(obj: dict) -> Callable[[socket.socket], None]:
    def handle(conn: socket.socket) -> None:
        conn.sendall(json.dumps(obj).encode() + b"\n")

    return handle


def _hang_up(conn: socket.socket) -> None:
    """Read the request, then close without answering: the broker died mid-send."""


TRANSPORT_ERROR = _reply(
    {"ok": False, "error": "AgentMailTransportError", "message": "agentmail send failed: timed out"}
)
GRAPH_TRANSPORT_ERROR = _reply(
    {"ok": False, "error": "MsGraphTransportError", "message": "graph sendMail failed: HTTP 503"}
)
REFUSED = _reply(
    {
        "ok": False,
        "error": "AgentMailRefused",
        "message": "client@example.com is not on this seat's authored surface",
    }
)
INTENT_NOT_RECORDED = _reply(
    {
        "ok": False,
        "error": "AgentMailRefused",
        "message": "not sent: the audit ledger could not record this send before it was made, "
        "so it was not attempted",
    }
)
SENT_CONFIRM_ROW_SHORT = _reply({"ok": True, "message_id": "msg-77", "confirm_row_written": False})


@pytest.fixture
def broker(monkeypatch) -> Iterator[Callable[[Callable[[socket.socket], None]], str]]:
    """Start a one-shot broker on a short socket path; returns the path."""
    # AF_UNIX paths are capped near 104 bytes on macOS; pytest's tmp_path can
    # exceed that, so the directory is made short on purpose.
    root = tempfile.mkdtemp(prefix="sou")
    servers: list[socket.socket] = []

    def start(handler: Callable[[socket.socket], None]) -> str:
        path = os.path.join(root, f"b{len(servers)}.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        servers.append(server)

        def serve() -> None:
            conn, _ = server.accept()
            with conn:
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65_536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                handler(conn)

        threading.Thread(target=serve, daemon=True).start()
        monkeypatch.setenv(workspace_broker.SOCKET_ENV, path)
        return path

    yield start
    for server in servers:
        server.close()


def _no_broker(monkeypatch) -> None:
    """A socket path with nothing behind it: connect() fails, nothing is written."""
    monkeypatch.setenv(
        workspace_broker.SOCKET_ENV, os.path.join(tempfile.mkdtemp(prefix="sou"), "absent.sock")
    )


# ---------------------------------------------------------------------------
# The client: which faults mean "nothing was delivered".
# ---------------------------------------------------------------------------


def test_connect_failure_is_not_reached(monkeypatch):
    _no_broker(monkeypatch)
    with pytest.raises(workspace_broker.BrokerNotReached):
        workspace_broker.request({"action": "x"})


def test_a_hang_up_after_the_write_is_unreadable_not_not_reached(broker):
    broker(_hang_up)
    with pytest.raises(workspace_broker.BrokerReplyUnreadable):
        workspace_broker.request({"action": "x"})


def test_the_reply_error_code_is_kept(broker):
    broker(TRANSPORT_ERROR)
    with pytest.raises(workspace_broker.BrokerError) as caught:
        workspace_broker.request({"action": "x"})
    assert caught.value.code == "AgentMailTransportError"
    assert workspace_broker.send_outcome_unknown(caught.value)


@pytest.mark.parametrize(
    "code, unknown",
    [
        ("AgentMailTransportError", True),
        ("MsGraphTransportError", True),
        ("internal_error", True),
        ("invalid_response", True),
        ("AgentMailRefused", False),
        ("MsGraphRefused", False),
        ("PermissionError", False),
        ("ValueError", False),
        ("request_too_large", False),
        ("", False),
    ],
)
def test_which_reply_codes_leave_a_send_unknown(code, unknown):
    assert (
        workspace_broker.send_outcome_unknown(workspace_broker.BrokerError("x", code=code))
        is unknown
    )


# ---------------------------------------------------------------------------
# The transports: the typed error each fault becomes.
# ---------------------------------------------------------------------------


def _outbound_send():
    return load_plugin("hermes-smd-trust").outbound_send


@pytest.mark.parametrize("handler", [TRANSPORT_ERROR, _hang_up])
def test_agentmail_unknown_outcomes_raise_send_outcome_unknown(broker, handler):
    out = _outbound_send()
    broker(handler)
    with pytest.raises(out.SendOutcomeUnknown) as caught:
        out.send_message(payload={"to": [TO], "subject": "S", "text": "B"})
    # Still the channel's own error, so callers catching it are unchanged.
    assert isinstance(caught.value, out.AgentMailSendError)
    assert str(caught.value).startswith(out.OUTCOME_UNKNOWN_PREFIX)


@pytest.mark.parametrize("handler", [REFUSED, INTENT_NOT_RECORDED])
def test_agentmail_definite_refusals_are_not_unknown(broker, handler):
    out = _outbound_send()
    broker(handler)
    with pytest.raises(out.AgentMailSendError) as caught:
        out.send_message(payload={"to": [TO], "subject": "S", "text": "B"})
    assert not isinstance(caught.value, out.SendOutcomeUnknown)


def test_agentmail_connect_failure_is_not_unknown(monkeypatch):
    out = _outbound_send()
    _no_broker(monkeypatch)
    with pytest.raises(out.AgentMailSendError) as caught:
        out.send_message(payload={"to": [TO], "subject": "S", "text": "B"})
    assert not isinstance(caught.value, out.SendOutcomeUnknown)
    assert "nothing was sent" in str(caught.value)


def test_a_sent_message_whose_confirm_row_failed_is_still_sent(broker):
    """ss-console#3346: ``ok: true, confirm_row_written: false`` is a success."""
    out = _outbound_send()
    broker(SENT_CONFIRM_ROW_SHORT)
    assert out.send_message(payload={"to": [TO], "subject": "S", "text": "B"}) == "msg-77"


def test_graph_lost_answer_on_an_attachment_send_is_unknown_not_attachment_refused(broker):
    """Checked before the attachment test: retrying without the attachment
    would be the duplicate."""
    out = _outbound_send()
    broker(
        _reply(
            {
                "ok": False,
                "error": "MsGraphTransportError",
                "message": "attachment upload timed out",
            }
        )
    )
    from tests.test_attachment_scan import descriptor

    with pytest.raises(out.SendOutcomeUnknown) as caught:
        out.send_via_msgraph(
            {"to": [TO], "subject": "S", "body_text": "B"}, attachments=[descriptor()]
        )
    assert not isinstance(caught.value, out.AttachmentSendError)


def test_graph_connect_failure_is_not_unknown(monkeypatch):
    out = _outbound_send()
    _no_broker(monkeypatch)
    with pytest.raises(out.MsGraphSendError) as caught:
        out.send_via_msgraph({"to": [TO], "subject": "S", "body_text": "B"})
    assert not isinstance(caught.value, out.SendOutcomeUnknown)


def test_graph_transport_error_is_unknown(broker):
    out = _outbound_send()
    broker(GRAPH_TRANSPORT_ERROR)
    with pytest.raises(out.MsGraphOutcomeUnknown):
        out.send_via_msgraph({"to": [TO], "subject": "S", "body_text": "B"})


def test_the_broker_clients_keep_their_old_unavailable_type():
    """A caller catching the old type still catches the new, narrower one."""
    assert issubclass(
        agentmail_broker.AgentMailBrokerNotReached, agentmail_broker.AgentMailBrokerUnavailable
    )
    assert issubclass(
        msgraph_broker.MsGraphBrokerNotReached, msgraph_broker.MsGraphBrokerUnavailable
    )
    assert issubclass(workspace_broker.BrokerNotReached, OSError)
    assert issubclass(workspace_broker.BrokerReplyUnreadable, ValueError)


# ---------------------------------------------------------------------------
# What the agent is told: the approved send, end to end.
# ---------------------------------------------------------------------------


@pytest.fixture
def trust(monkeypatch):
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ALLOWED)
    mod = load_plugin("hermes-smd-trust")
    enforce = mod.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["scott@smd.services"])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")
    monkeypatch.setattr(mod, "get_secret", lambda k: "am_key")
    yield mod
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _approve_and_dispatch(trust) -> str:
    trust.enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": [TO], "subject": "Report", "text": "the reviewed body"},
        "scott",
        session_id="s1",
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{ALLOWED}")
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None
    return ctx


def _says_do_not_resend(ctx: str) -> None:
    lowered = ctx.lower()
    assert "do not send it again" in lowered
    assert "retry" not in lowered
    assert "not sent" not in lowered and "was not sent" not in lowered
    assert "checking" in lowered


@pytest.mark.parametrize(
    "handler", [TRANSPORT_ERROR, _hang_up], ids=["vendor_transport", "hang_up"]
)
def test_approved_send_with_unknown_outcome_is_never_offered_a_retry(trust, broker, handler):
    """THE DEFECT. On origin/main both cases told the agent "it was not sent.
    You can ask me to retry" (the vendor-transport case) or raised out of the
    dispatch entirely (the hang-up, an unparsed empty reply)."""
    broker(handler)
    _says_do_not_resend(_approve_and_dispatch(trust))


def test_approved_send_definite_refusal_still_offers_a_retry(trust, broker):
    broker(INTENT_NOT_RECORDED)
    ctx = _approve_and_dispatch(trust)
    assert "not sent" in ctx.lower() and "retry" in ctx.lower()


def test_approved_send_connect_failure_still_offers_a_retry(trust, monkeypatch):
    _no_broker(monkeypatch)
    ctx = _approve_and_dispatch(trust)
    assert "not sent" in ctx.lower() and "retry" in ctx.lower()


# ---------------------------------------------------------------------------
# The model's own send tool, and the out-of-turn sender.
# ---------------------------------------------------------------------------


def _agentmail_seat(monkeypatch, tmp_path) -> None:
    path = tmp_path / "customer.yaml"
    path.write_text(_AGENTMAIL_SEAT_YAML)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))


def test_send_tool_unknown_outcome_says_do_not_resend(monkeypatch, tmp_path, broker):
    trust = load_plugin("hermes-smd-trust")
    _agentmail_seat(monkeypatch, tmp_path)
    broker(TRANSPORT_ERROR)
    result = trust._smd_send_message({"to": [TO], "subject": "S", "text": "B"})
    _says_do_not_resend(result)


def test_send_tool_connect_failure_still_says_not_sent(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    _agentmail_seat(monkeypatch, tmp_path)
    _no_broker(monkeypatch)
    result = trust._smd_send_message({"to": [TO], "subject": "S", "text": "B"})
    assert result.startswith("Not sent:")


def test_dispatch_result_carries_outcome_unknown():
    """The out-of-turn result names the third state, defaulting to False so
    every existing constructor is unchanged."""
    assert DispatchResult(sent=False).outcome_unknown is False
    assert DispatchResult(sent=False, outcome_unknown=True).outcome_unknown is True
