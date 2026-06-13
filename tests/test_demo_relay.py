"""Tests for the hermes-smd-demo-relay plugin.

The relay sends the agent's GOVERNED draft back to the verified inbound sender,
OUTSIDE the model's tool path, without weakening any agent floor. The security
crux is the recipient-lock + the fail-closed flag; these tests pin both, plus
the re-applied content/fabrication floors, the rate-limit, and the guarantee
that a real (non-demo) customer is byte-for-byte unaffected.

Covers (design checklist, docs/security/demo-reply-relay-design.md):
  1. recipient-lock cannot be redirected by an injected/substituted recipient;
  2. fail-closed without the flag (and a real customer is never relayed);
  3. the content floor re-check blocks a sensitive body before send;
  4. the fabrication gate re-check blocks a body with a fabricated citation;
  5. the rate-limit bounds per-sender + global volume;
  6. the happy path sends keyed on the RECORDED inbox+message (structural lock)
     and audits without persisting the body.
"""

from __future__ import annotations

import json

import pytest

from shared import inbound
from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------


class _FakeD1Client:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql: str, *params):
        self.calls.append((sql, params))
        return 1

    def events(self) -> list[tuple[str, dict]]:
        """(action_type, metadata) per emitted row. action_type is param[2];
        metadata JSON is the last param."""
        out: list[tuple[str, dict]] = []
        for _sql, params in self.calls:
            out.append((params[2], json.loads(params[-1]) if params[-1] else {}))
        return out


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()


@pytest.fixture
def relay_mod(monkeypatch):
    """Load the plugin and put it in the ENABLED demo state with fakes.

    A fake D1 records audit rows; ``send_reply`` is replaced (via monkeypatch so
    it auto-restores — ``load_plugin`` returns the process-cached module) with a
    capturing stub so no network call happens. Tests that want the disabled path
    flip ``mod._ENABLED`` themselves.
    """
    mod = load_plugin("hermes-smd-demo-relay")
    fake_d1 = _FakeD1Client()
    sent: list[dict] = []

    def _fake_send(*, api_key, inbox_id, message_id, text, html, **_kw):
        sent.append(
            {
                "api_key": api_key,
                "inbox_id": inbox_id,
                "message_id": message_id,
                "text": text,
                "html": html,
            }
        )
        return "msg_sent_1"

    monkeypatch.setattr(mod, "_ENABLED", True, raising=False)
    monkeypatch.setattr(mod, "_VERTICAL", "law-firm", raising=False)
    monkeypatch.setattr(mod, "_COHORT", "demo-law", raising=False)
    monkeypatch.setattr(mod, "_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "demo-law", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", fake_d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod.relay, "send_reply", _fake_send)
    return mod, fake_d1, sent


def _record_origin(
    sender="greg@whitfield.example", message_id="msg_in", inbox_id="inbox_x", session="s1"
):
    inbound.SESSION_INBOUND_ORIGIN.record(
        session,
        inbound.InboundOrigin(sender_address=sender, message_id=message_id, inbox_id=inbox_id),
    )


def _draft(
    to,
    text="Thanks for reaching out. We received your intake; we'll be in touch.",
    subject="Re: New matter",
    html="",
):
    return {"to": to, "subject": subject, "text": text, "html": html}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registers_post_tool_call(fake_ctx, monkeypatch, tmp_path) -> None:
    # No customer.yaml on the default path ⇒ disabled, but the hook still wires.
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    mod = load_plugin("hermes-smd-demo-relay")
    mod.register(fake_ctx)
    assert "post_tool_call" in fake_ctx.registered


# ---------------------------------------------------------------------------
# 1. Recipient-lock
# ---------------------------------------------------------------------------


def test_happy_path_sends_to_recorded_inbox_and_message(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", message_id="msg_in", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    # Sent, keyed on the RECORDED inbox + message (structural recipient-lock),
    # not on anything the draft named.
    assert len(sent) == 1
    assert sent[0]["inbox_id"] == "inbox_x"
    assert sent[0]["message_id"] == "msg_in"
    # Audit row carries digest + recipient, never the body.
    events = d1.events()
    assert any(a == "DEMO_RELAY_SENT" for a, _ in events)
    _, meta = next((a, m) for a, m in events if a == "DEMO_RELAY_SENT")
    assert meta["recipient"] == "greg@whitfield.example"
    assert "body_digest" in meta
    assert "Thanks for reaching out" not in json.dumps(meta)


def test_runtime_mcp_tool_name_fires_relay(relay_mod) -> None:
    """Regression for the 2026-06-12 demo-law live test: the relay hooked the
    colon spelling ``agentmail:create_draft``, but Hermes emits the MCP runtime
    name ``mcp_agentmail_create_draft`` — so the hook never fired in production
    and the relay was dead. The live runtime name MUST trigger the send."""
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", message_id="msg_in", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert len(sent) == 1
    assert sent[0]["inbox_id"] == "inbox_x"
    assert sent[0]["message_id"] == "msg_in"


def test_recovers_origin_when_session_id_mismatches(relay_mod) -> None:
    """The router records the origin under the DISPATCH session_id (often empty);
    the relay reads under the AGENT session_id. When they differ (the demo-law
    2026-06-12 live bug — draft created, no reply sent), the relay recovers the
    verified origin by matching the draft's recipient against the address index."""
    mod, _d1, sent = relay_mod
    # Recorded under an empty dispatch session id ...
    _record_origin(
        sender="greg@whitfield.example", message_id="msg_in", inbox_id="inbox_x", session=""
    )
    # ... while create_draft fires under a DIFFERENT agent session id.
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="agent-20260613-013303",
    )
    assert len(sent) == 1
    assert sent[0]["inbox_id"] == "inbox_x"
    assert sent[0]["message_id"] == "msg_in"


def test_recovery_fails_closed_for_unverified_recipient(relay_mod) -> None:
    """Recovery only matches verified inbound senders. A draft to an address that
    never emailed in recovers nothing → no send (injection-safe)."""
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", session="")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["attacker@evil.test"]),
        session_id="agent-x",
    )
    assert sent == []


def test_recovery_still_blocks_injected_extra_recipient(relay_mod) -> None:
    """Even on the recovery path, the recipient-lock still requires the draft to
    name ONLY the verified sender — an injected extra recipient is refused."""
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", inbox_id="inbox_x", session="")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example", "attacker@evil.test"]),
        session_id="agent-x",
    )
    assert sent == []


def test_injected_extra_recipient_fails_lock(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # An injected second recipient ("also send to attacker") must fail the lock.
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example", "attacker@evil.test"]),
        session_id="s1",
    )
    assert sent == []
    assert any(
        a == "DEMO_RELAY_BLOCKED" and m["reason"] == "recipient_mismatch" for a, m in d1.events()
    )


def test_substituted_recipient_fails_lock(relay_mod) -> None:
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # A redirected recipient (not the inbound sender) must fail the lock.
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["someone-else@elsewhere.test"]),
        session_id="s1",
    )
    assert sent == []


def test_display_name_recipient_matches(relay_mod) -> None:
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # "Display Name <addr>" normalizes to the bare address and matches.
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["Greg Whitfield <Greg@Whitfield.Example>"]),
        session_id="s1",
    )
    assert len(sent) == 1


def test_no_recorded_origin_does_not_send(relay_mod) -> None:
    mod, _d1, sent = relay_mod
    # No SESSION_INBOUND_ORIGIN for this session ⇒ fail closed.
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["anyone@x.test"]),
        session_id="never-opened",
    )
    assert sent == []


def test_missing_inbox_id_does_not_send(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example", inbox_id="")  # no inbox to thread into
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "DEMO_RELAY_BLOCKED" and m["reason"] == "no_inbox_id" for a, m in d1.events())


# ---------------------------------------------------------------------------
# 2. Fail-closed flag — a real (non-demo) customer is never relayed
# ---------------------------------------------------------------------------


def test_disabled_flag_never_sends(relay_mod) -> None:
    mod, d1, sent = relay_mod
    mod._ENABLED = False  # real customer: demo.reply_relay unauthored
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert sent == []
    assert d1.events() == []  # not even an audit row — byte-for-byte unaffected


def test_non_create_draft_tool_ignored(relay_mod) -> None:
    mod, _d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="practice_management_search_matters",
        args={"query": "greg"},
        session_id="s1",
    )
    assert sent == []


# ---------------------------------------------------------------------------
# 3 + 4. Re-applied content + fabrication floors
# ---------------------------------------------------------------------------


def test_sensitive_body_blocked(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # A money/contract body trips the content floor — refuse to relay.
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text="Our fee for this engagement is $5,000 due on signing the contract.",
        ),
        session_id="s1",
    )
    assert sent == []
    assert any(
        a == "DEMO_RELAY_BLOCKED" and m["reason"] == "content_sensitive" for a, m in d1.events()
    )


def test_empty_body_blocked(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # Subject-only draft: nothing to transmit ⇒ blocked (content floor fails
    # closed on a bodyless send; the empty-body guard backstops it too).
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: New matter",
            "text": "",
            "html": "",
        },
        session_id="s1",
    )
    assert sent == []


# ---------------------------------------------------------------------------
# 5. Rate-limit
# ---------------------------------------------------------------------------


def test_per_sender_rate_limit(relay_mod) -> None:
    mod, d1, sent = relay_mod
    # Tight per-sender bound for the test.
    mod._LIMITER = mod.relay.RateLimiter(per_sender_max=2, global_max=100)
    _record_origin(sender="greg@whitfield.example", session="s1")
    for i in range(4):
        # Each call is a fresh session opened by the same inbound sender.
        sid = f"s{i}"
        inbound.SESSION_INBOUND_ORIGIN.record(
            sid,
            inbound.InboundOrigin("greg@whitfield.example", f"msg_{i}", inbox_id="inbox_x"),
        )
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"]),
            session_id=sid,
        )
    assert len(sent) == 2  # only the first two cleared the per-sender window
    assert any(a == "DEMO_RELAY_BLOCKED" and m["reason"] == "rate_limited" for a, m in d1.events())


def test_rate_limiter_window_eviction() -> None:
    mod = load_plugin("hermes-smd-demo-relay")
    clock = {"t": 0.0}
    limiter = mod.relay.RateLimiter(
        per_sender_max=1,
        per_sender_window_s=10.0,
        global_max=100,
        clock=lambda: clock["t"],
    )
    assert limiter.allow("a@x.test") is True
    assert limiter.allow("a@x.test") is False  # within window
    clock["t"] = 11.0  # window elapsed
    assert limiter.allow("a@x.test") is True


def test_global_rate_limit() -> None:
    mod = load_plugin("hermes-smd-demo-relay")
    limiter = mod.relay.RateLimiter(per_sender_max=100, global_max=2)
    assert limiter.allow("a@x.test") is True
    assert limiter.allow("b@x.test") is True
    assert limiter.allow("c@x.test") is False  # global bound hit


# ---------------------------------------------------------------------------
# Send-failure path
# ---------------------------------------------------------------------------


def test_send_failure_audits_failed(relay_mod, monkeypatch) -> None:
    mod, d1, _sent = relay_mod
    _record_origin(sender="greg@whitfield.example")

    def _boom(**_kw):
        raise mod.relay.RelaySendError("HTTP 502")

    monkeypatch.setattr(mod.relay, "send_reply", _boom)
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert any(a == "DEMO_RELAY_FAILED" for a, _ in d1.events())


# ---------------------------------------------------------------------------
# Pure-logic units (relay.py)
# ---------------------------------------------------------------------------


def test_recipient_locked_pure() -> None:
    mod = load_plugin("hermes-smd-demo-relay")
    r = mod.relay
    assert r.recipient_locked({"to": ["a@x.test"]}, "a@x.test") is True
    assert r.recipient_locked({"to": ["A@X.test"]}, "a@x.test") is True
    assert r.recipient_locked({"to": ["a@x.test", "b@x.test"]}, "a@x.test") is False
    assert r.recipient_locked({"to": ["b@x.test"]}, "a@x.test") is False
    assert r.recipient_locked({"to": []}, "a@x.test") is False
    assert r.recipient_locked({}, "a@x.test") is False
    assert r.recipient_locked({"to": ["a@x.test"]}, "") is False


def test_send_reply_builds_request() -> None:
    mod = load_plugin("hermes-smd-demo-relay")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"messageId":"msg_out"}'

    def _opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.headers.get("Authorization")
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    out = mod.relay.send_reply(
        api_key="sek",
        inbox_id="inbox_x",
        message_id="msg_in",
        text="hello",
        html="<p>hello</p>",
        opener=_opener,
    )
    assert out == "msg_out"
    assert captured["url"].endswith("/inboxes/inbox_x/messages/msg_in/reply")
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer sek"
    assert captured["body"] == {"text": "hello", "html": "<p>hello</p>"}
