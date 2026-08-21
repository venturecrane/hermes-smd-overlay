"""Graph transport tests for the reply relay + confirm dispatch (ADR 0078, piece 3).

The seam is provider-neutral above the transport: roster / recipient-lock / floors
/ rate-limit are shared, and only the wire call differs. These pin the msgraph
branch of both out-of-band paths, plus the shared send tool:

  * the reply relay answers in-thread on the recorded inbound message id, and
    REFUSES fail-closed (audited REPLY_FAILED) when there is no transmit path —
    never falling back to AgentMail;
  * the confirm dispatch sends an approved payload with the flat
    to/cc/subject/body_text shape, and raises MsGraphSendError (→
    CONFIRM_SEND_FAILED) when it cannot;
  * `smd_send_message` routes by the adapter the SEAT authors, so the one tool
    serves both channels without either seat reaching the other's transport.

ss#2258: all three go through the workspace broker now. What used to be asserted
against a fake Graph client is asserted against a fake broker, and the difference
is the point — the fake cannot name a mailbox, cannot name a reply's recipient,
and holds no credential, because none of those are the agent's to decide any more.

The limit this file cannot test, stated so nobody reads its green as more than it
is: the agent still holds MSGRAPH_* for reads, so a path that ignores these
functions entirely can still reach Graph. Only a second, read-only app
registration closes that, and it lives in the customer's tenant.
"""

from __future__ import annotations

import json

import pytest

from shared import inbound, msgraph_broker
from shared.inbound import SESSION_TAINT
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

_MSGRAPH_ROSTERED_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "connectors:\n"
    "  Email:\n"
    "    adapter: msgraph\n"
    "    backend: mcp:msgraph-mail\n"
    "    enabled: true\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - greg@whitfield.example\n"
)


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self):
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


class _FakeGraphBroker:
    """Stands in for the broker's msgraph verbs (ss#2258).

    Note what it CANNOT express, because that is the change these tests exist to
    pin: no mailbox, no recipient for a reply, no credential. The broker takes
    the first two from the seat's own customer.yaml and holds the third, so the
    agent-side surface has nothing left to get wrong.
    """

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        #: (message_id, comment, html) — ss#2489 added the html half, and it is
        #: recorded separately so a test can assert on the RENDERED body without
        #: the plain-text assertions above losing their meaning.
        self.reply_calls: list[tuple[str, str, str]] = []
        self.sends: list[dict] = []
        #: The audit joins each call carried (ss-console#2497). Recorded so a
        #: test can assert the broker was TOLD the session and the matter — the
        #: broker writes the CONFIRM_SEND_* row and cannot learn either itself.
        self.joins: list[tuple[str, str | None]] = []
        #: What the broker resolved for this message (ss-console#2499). Empty is
        #: the DEFAULT, deliberately: it is what a pre-#2499 broker returns and
        #: what a post-#2499 broker returns when its Sent Items lookup could not
        #: find the message, so every existing assertion here keeps describing
        #: the harder of the two cases.
        self.vendor_id: str = ""

    def send_reply(self, message_id, comment, *, html="", session_id="", matter_ref=None):
        self.replies.append((message_id, comment))
        self.reply_calls.append((message_id, comment, html))
        self.joins.append((session_id, matter_ref))
        return self.vendor_id

    def send_message(self, payload, *, session_id="", matter_ref=None):
        self.sends.append(dict(payload))
        self.joins.append((session_id, matter_ref))
        return self.vendor_id


# ---------------------------------------------------------------------------
# Reply relay — msgraph provider dispatch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()


def _reply_mod(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_MSGRAPH_ROSTERED_YAML)
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", None, raising=False)  # msgraph seat: no AgentMail key
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    return mod, d1


def _record_origin(
    sender="greg@whitfield.example", message_id="graph-mid-1", mailbox="op@client.example"
):
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin(sender_address=sender, message_id=message_id, inbox_id=mailbox),
    )


def test_msgraph_reply_dispatches_via_graph_reply(monkeypatch, tmp_path):
    mod, d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "Thanks, will do.",
        },
        session_id="s1",
    )
    # Replied in-thread on the recorded Graph message id (structural lock).
    assert fake.replies == [("graph-mid-1", "Thanks, will do.")]
    events = d1.events()
    assert any(a == "REPLY_SENT" for a, _ in events)
    _, meta = next((a, m) for a, m in events if a == "REPLY_SENT")
    assert meta["adapter"] == "msgraph"
    assert "Thanks, will do." not in json.dumps(meta)  # body never persisted


# ---------------------------------------------------------------------------
# ss#2489 — the reply carries a rendered body, because Graph's /reply composes
# in HTML and collapses a plain-text comment's newlines. Four replies reached
# hermes-ashton-price's principal as one unbroken block on 2026-08-20.
# ---------------------------------------------------------------------------


def test_msgraph_reply_renders_the_body_so_line_structure_survives(monkeypatch, tmp_path):
    """The wall, pinned at the seam that produced it.

    The assertion is on STRUCTURE rather than an exact string: what failed live
    was that every block ran together, so what has to hold is that the two
    blocks arrive as two blocks.
    """
    mod, _d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "First paragraph.\n\nSecond paragraph.",
        },
        session_id="s1",
    )
    _mid, comment, html = fake.reply_calls[0]
    assert html.count("<p") == 2, html
    assert "First paragraph." in html and "Second paragraph." in html
    # The plain half still rides along: the broker keeps it as the fallback and
    # the audit digest is taken over the words, not the markup.
    assert comment == "First paragraph.\n\nSecond paragraph."


def test_msgraph_reply_renders_report_structure_as_structure(monkeypatch, tmp_path):
    mod, _d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "## What I did\n\n- Read the file\n- Logged the call",
        },
        session_id="s1",
    )
    _mid, _comment, html = fake.reply_calls[0]
    assert "<h2" in html
    assert html.count("<li") == 2


def test_msgraph_reply_escapes_model_authored_markup(monkeypatch, tmp_path):
    """Escape-by-default is the property that makes rendering safe on this path;
    it is inherited from report_render, and inheriting it silently is how it gets
    lost in a later refactor."""
    mod, _d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "<script>alert(1)</script>",
        },
        session_id="s1",
    )
    _mid, _comment, html = fake.reply_calls[0]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_composer_authored_html_body_wins(monkeypatch, tmp_path):
    mod, _d1 = _reply_mod(monkeypatch, tmp_path)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "plain",
            "html": "<p>mine</p>",
        },
        session_id="s1",
    )
    _mid, _comment, html = fake.reply_calls[0]
    assert html == "<p>mine</p>"


def test_msgraph_reply_fails_closed_when_broker_unreachable(monkeypatch, tmp_path):
    mod, d1 = _reply_mod(monkeypatch, tmp_path)

    # No broker transmit path → refuse, do NOT fall back to AgentMail and do NOT
    # fall back to a direct Graph call. "Unavailable" must never become "allowed".
    def _unavailable(*_a, **_k):
        raise mod.msgraph_broker.MsGraphBrokerUnavailable("no socket")

    monkeypatch.setattr(mod.msgraph_broker, "send_reply", _unavailable)
    _record_origin()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": ["greg@whitfield.example"], "subject": "Re: matter", "body_text": "Thanks."},
        session_id="s1",
    )
    events = d1.events()
    assert any(a == "REPLY_FAILED" for a, _ in events)
    assert not any(a == "REPLY_SENT" for a, _ in events)


# ---------------------------------------------------------------------------
# Confirm dispatch — msgraph /sendMail transport
# ---------------------------------------------------------------------------

_ALLOWED = "7367659986"
_TO = "client@example.com"
_MSGRAPH_SEND = "mcp_msgraph_mail_send_message"


@pytest.fixture(autouse=True)
def _clean_pending(monkeypatch):
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", _ALLOWED)
    yield
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _arm_msgraph(monkeypatch, trust):
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["scott@smd.services"])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")


def _capture_and_approve_msgraph(trust, body="the reviewed body", session="s1"):
    trust.enforce.evaluate_tool_call(
        _MSGRAPH_SEND,
        {"to": [_TO], "subject": "Report", "body_text": body},
        "scott",
        session_id=session,
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{_ALLOWED}")


def test_confirm_dispatch_routes_msgraph_send_via_broker(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx and _TO in ctx
    # The STORED flat payload went to the Graph broker verb, not the AgentMail one.
    assert len(fake.sends) == 1
    assert fake.sends[0]["to"] == [_TO] and fake.sends[0]["body_text"] == "the reviewed body"
    assert PENDING_SEND.peek() is None  # single-use


def test_confirm_dispatch_msgraph_never_falls_back_to_agentmail(monkeypatch):
    """A Graph seat whose broker refuses must NOT reach the AgentMail verb.

    The falsifier this exists for: route by the wrong signal — a tool name, a
    default — and an msgraph seat quietly transmits through the other channel,
    from an inbox it does not own, past a fence built for a different roster.
    """
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    agentmail_calls: list[dict] = []

    def _refuse(_payload, **_kwargs):
        raise trust.outbound_send.msgraph_broker.BrokerError("recipient not authored")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _refuse)
    monkeypatch.setattr(
        trust.outbound_send,
        "send_message",
        lambda **kw: agentmail_calls.append(kw) or "am-1",
    )
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "not sent" in ctx.lower()
    assert agentmail_calls == []


def test_confirm_dispatch_msgraph_fails_closed_without_broker(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)

    def _unavailable(_payload, **_kwargs):
        raise trust.outbound_send.msgraph_broker.MsGraphBrokerUnavailable("no socket")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _unavailable)
    _capture_and_approve_msgraph(trust)
    ctx = trust._dispatch_approved_send("s1", "scott")
    # Not delivered; the caller reports the failure plainly.
    assert ctx is not None and "not sent" in ctx.lower()


def test_send_via_msgraph_forwards_a_closed_allowlist(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    trust.outbound_send.send_via_msgraph(
        {
            "to": ["a@x.example"],
            "cc": ["b@x.example"],
            "bcc": ["c@x.example"],
            "subject": "S",
            "body_text": "B",
            "extra": "drop",
            "grant": "drop",
        }
    )
    assert fake.sends == [
        {
            "to": ["a@x.example"],
            "cc": ["b@x.example"],
            "bcc": ["c@x.example"],
            "subject": "S",
            "body_text": "B",
        }
    ]


def test_send_via_msgraph_refuses_without_a_recipient(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    with pytest.raises(trust.outbound_send.MsGraphSendError):
        trust.outbound_send.send_via_msgraph({"body_text": "B"})
    assert fake.sends == []


# ---------------------------------------------------------------------------
# smd_send_message — ONE tool, routed by what the SEAT authors
# ---------------------------------------------------------------------------
#
# These are the tests for the branch that replaced tool-name routing. With the
# msgraph connector's own send tool off the menu, `smd_send_message` is the only
# send an msgraph seat has, and if it routed by tool name it would take the
# AgentMail path on every one of them.

_AGENTMAIL_SEAT_YAML = (
    "customer_id: acme\nvertical: law-firm\nconnectors:\n  Email:\n"
    "    adapter: agentmail\n    backend: mcp:agentmail\n    enabled: true\n"
)


def _seat_yaml(monkeypatch, tmp_path, body: str):
    path = tmp_path / "customer.yaml"
    path.write_text(body)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    return path


def _both_transports(monkeypatch, trust):
    """Stub BOTH transports so a mis-route is visible as the wrong list filling."""
    graph: list[dict] = []
    agentmail: list[dict] = []
    monkeypatch.setattr(
        trust.outbound_send.msgraph_broker,
        "send_message",
        lambda payload, **_kw: graph.append(dict(payload)) or "",
    )
    monkeypatch.setattr(
        trust.outbound_send,
        "send_message",
        lambda **kw: agentmail.append(kw) or "am-1",
    )
    return graph, agentmail


# Every call below passes the payload as ONE POSITIONAL DICT, which is how Hermes
# dispatches (`entry.handler(args, **kwargs)`). These tests previously called the
# handler by keyword — the shape the handler was mistakenly written for — so they
# passed while every real invocation raised TypeError (SMD-OPERATOR-1B). Calling
# it any other way here would restore that blind spot.
_SEND_ARGS = {"to": [_TO], "subject": "S", "text": "B"}


def test_send_tool_routes_by_authored_adapter_msgraph(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, _MSGRAPH_ROSTERED_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust._smd_send_message(dict(_SEND_ARGS))
    assert len(graph) == 1 and agentmail == []
    # `text` reaches the Graph verb under its own name; the broker accepts either
    # spelling, so nothing is silently dropped on the way.
    assert graph[0]["text"] == "B"


def test_send_tool_routes_by_authored_adapter_agentmail(monkeypatch, tmp_path):
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, _AGENTMAIL_SEAT_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust._smd_send_message(dict(_SEND_ARGS))
    assert len(agentmail) == 1 and graph == []
    # THE BODY, not just the call count. A handler that took the positional dict
    # but still read its payload from **kwargs would send an EMPTY message and
    # report "Sent" — invisible to a length assertion. Its msgraph twin above has
    # always checked this; the agentmail side had not, which is the gap that let
    # `payload = dict(kwargs)` ship.
    assert agentmail[0]["payload"]["to"] == [_TO]
    assert agentmail[0]["payload"]["subject"] == "S"
    assert agentmail[0]["payload"]["text"] == "B"


def test_send_tool_refuses_when_the_seat_authors_no_email_connector(monkeypatch, tmp_path):
    """No Email connector authored => refuse by name, never fall back to agentmail.

    `_seat_email_adapter()` defaults to agentmail when nothing is authored, which
    is right for its other callers but wrong here: on such a seat that default
    means a broker call for a mailbox and credential that were never provisioned.
    Before the SMD-OPERATOR-1B fix this was masked by the TypeError; afterwards it
    would have become a soft "Not sent" the agent reads as a mild failure. Both
    ashton-price and smd author no Email connector today, so this is the live
    shape, not a hypothetical.
    """
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, "customer_id: acme\nvertical: law-firm\nconnectors: {}\n")
    graph, agentmail = _both_transports(monkeypatch, trust)

    result = trust._smd_send_message(dict(_SEND_ARGS))

    assert graph == [] and agentmail == [], "an unauthored seat must reach NEITHER transport"
    assert result.startswith("Not sent:")
    assert "no Email connector" in result


def test_send_tool_still_sends_when_the_config_cannot_be_read(monkeypatch, tmp_path):
    """An UNREADABLE config must not start refusing sends on an authored seat.

    The refusal above keys on "read fine, authors nothing". A transient read
    fault is a different condition and keeps the pre-existing agentmail default,
    so a config blip on a healthy seat does not silently stop its mail.
    """
    trust = load_plugin("hermes-smd-trust")
    _seat_yaml(monkeypatch, tmp_path, _AGENTMAIL_SEAT_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)

    def _boom():
        raise RuntimeError("volume unavailable")

    # Patch the REAL module the handler imports (the import is local to
    # `_authored_email_adapter`, so a `trust.CustomerConfig` attribute would not
    # be the object under test — it would make this test pass without exercising
    # the branch at all).
    from shared.customer_config import CustomerConfig

    monkeypatch.setattr(CustomerConfig, "from_volume", staticmethod(_boom))

    # Guard the guard: prove the patch actually reaches the handler's read path.
    assert trust._authored_email_adapter() == trust._ADAPTER_UNREADABLE

    trust._smd_send_message(dict(_SEND_ARGS))

    assert len(agentmail) == 1 and graph == []


def test_confirm_dispatch_routes_by_adapter_when_the_tool_is_the_shared_one(monkeypatch, tmp_path):
    """The withheld-then-approved path on an msgraph seat, via `smd_send_message`.

    This is the exact combination the old tool-name check got wrong: the pending
    record carries the shared tool name, so `rec.tool_name == _MSGRAPH_SEND_TOOL`
    is False, and without the adapter read the approved send would have gone out
    through AgentMail — a different inbox, a different fence.
    """
    trust = load_plugin("hermes-smd-trust")
    _arm_msgraph(monkeypatch, trust)
    _seat_yaml(monkeypatch, tmp_path, _MSGRAPH_ROSTERED_YAML)
    graph, agentmail = _both_transports(monkeypatch, trust)
    trust.enforce.evaluate_tool_call(
        "smd_send_message",
        {"to": [_TO], "subject": "Report", "text": "the reviewed body"},
        "scott",
        session_id="s1",
    )
    assert PENDING_SEND.peek() is not None
    PENDING_SEND.mark_approved(f"telegram:{_ALLOWED}")
    ctx = trust._dispatch_approved_send("s1", "scott")
    assert ctx is not None and "Dispatched" in ctx
    assert len(graph) == 1 and agentmail == []


def test_send_via_msgraph_surfaces_a_broker_refusal(monkeypatch):
    trust = load_plugin("hermes-smd-trust")

    def _refuse(_payload, **_kwargs):
        raise trust.outbound_send.msgraph_broker.BrokerError("not on the authored surface")

    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", _refuse)
    with pytest.raises(trust.outbound_send.MsGraphSendError) as excinfo:
        trust.outbound_send.send_via_msgraph({"to": ["a@x.example"], "body_text": "B"})
    # The broker's REASON reaches the operator, not a generic delivery failure.
    assert "not on the authored surface" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ss-console#2499 — the audit row can name the message
#
# Graph answers both verbs 202 with no body, so every REPLY_SENT row on the live
# A&P ledger read "(sent via msgraph, id unavailable)" (8 of 8) and could not be
# joined to the mailbox at all. The broker now stamps an X-SMD-Audit-Row header
# and resolves the message's RFC2822 id out of Sent Items; these pin that the id
# reaches the rows this repo writes, and that the placeholder survives ONLY for
# the case where it genuinely could not be resolved.
# ---------------------------------------------------------------------------


def test_a_send_row_names_the_message_the_broker_resolved(monkeypatch):
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    fake.vendor_id = "<abc@firm.example>"
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    out = trust.outbound_send.send_via_msgraph({"to": ["a@x.example"], "body_text": "B"})
    assert out == "<abc@firm.example>"


def test_a_send_the_broker_could_not_locate_still_says_so(monkeypatch):
    """The placeholder is honest HERE and only here. The broker's own row records
    why the lookup failed; manufacturing an id would name a message the mailbox
    does not contain, which reads as an answer and is not one."""
    trust = load_plugin("hermes-smd-trust")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(trust.outbound_send.msgraph_broker, "send_message", fake.send_message)
    out = trust.outbound_send.send_via_msgraph({"to": ["a@x.example"], "body_text": "B"})
    assert out == "(sent via msgraph, id unavailable)"


def test_a_reply_row_names_the_message_the_broker_resolved(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    fake = _FakeGraphBroker()
    fake.vendor_id = "<reply@firm.example>"
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    assert mod._send_msgraph_reply("AAMk1", "sure") == "<reply@firm.example>"


def test_a_reply_the_broker_could_not_locate_still_says_so(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    fake = _FakeGraphBroker()
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    assert mod._send_msgraph_reply("AAMk1", "sure") == "(sent via msgraph, id unavailable)"


def test_the_broker_client_prefers_the_resolved_id_over_the_empty_call_result():
    """``message_id`` is what the vendor CALL returned, which on Graph is always
    empty; ``vendor_message_id`` is what the broker went and looked up. Preferring
    the second with the first behind it is what lets a seat run either side of
    the ss-console change with no deployment ordering.

    FALSIFIER: read ``message_id`` first and the resolved id is discarded on
    every real send, which is the state before this change."""
    assert (
        msgraph_broker._vendor_id({"message_id": "", "vendor_message_id": "<x@firm.example>"})
        == "<x@firm.example>"
    )
    assert msgraph_broker._vendor_id({"message_id": "<legacy@firm.example>"}) == (
        "<legacy@firm.example>"
    )
    # Both present and different is the case that actually pins the ORDER: with
    # only an empty message_id in play, either ordering returns the same answer
    # and the assertion measures nothing.
    assert (
        msgraph_broker._vendor_id(
            {
                "message_id": "<from-the-call@firm.example>",
                "vendor_message_id": "<looked-up@firm.example>",
            }
        )
        == "<looked-up@firm.example>"
    )
    assert msgraph_broker._vendor_id({}) == ""
    # A non-string is not an id. The broker is trusted, the wire is not.
    assert msgraph_broker._vendor_id({"vendor_message_id": 7}) == ""
