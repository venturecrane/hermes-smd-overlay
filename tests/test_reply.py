"""Tests for the hermes-smd-reply plugin (the Operator reply channel).

The reply channel sends the agent's GOVERNED draft back to the verified inbound
sender, OUTSIDE the model's tool path, without weakening any agent floor — and
only when that sender is on the organization roster (``scope.inbound_allow_from``,
ADR 0055). The security crux is the recipient-lock + the roster authorization;
these tests pin both, plus the re-applied content/fabrication floors, the
rate-limit, and the guarantee that an unauthored roster never sends.

Covers:
  1. recipient-lock cannot be redirected by an injected/substituted recipient;
  2. roster authorization: empty/unauthored roster holds; a sender not on the
     roster is held; authoring the roster enables the reply live (no restart);
  3. the content floor re-check holds a sensitive body before send;
  4. the fabrication gate re-check holds a body with a fabricated citation;
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


# A customer.yaml that authors the test sender onto the organization roster.
_ROSTERED_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - greg@whitfield.example\n"
)
# Same customer with NO roster authored — fail-closed: the Operator drafts but
# never autonomously replies.
_NO_ROSTER_YAML = "customer_id: acme\nvertical: law-firm\n"


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()


@pytest.fixture
def relay_mod(monkeypatch, tmp_path):
    """Load the plugin and put it in the infra-ready + rostered state.

    Infra readiness (``_INFRA_READY`` + key + limiter + audit) is register-time
    state, monkeypatched directly. Authorization is read LIVE per call (ADR 0044),
    so the relay is enabled by pointing ``_YAML_PATH`` at a customer.yaml that
    authors the test sender onto ``scope.inbound_allow_from`` (and
    ``vertical: law-firm``, which drives the content floor). Tests that want the
    held path rewrite that file to drop the roster, proving the live-read holds
    mid-flight with no restart. ``send_reply`` is replaced (via monkeypatch so it
    auto-restores — ``load_plugin`` returns the process-cached module) with a
    capturing stub so no network call happens.
    """
    mod = load_plugin("hermes-smd-reply")
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

    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_ROSTERED_YAML)

    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", fake_d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
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
    # No customer.yaml on the default path ⇒ no roster, but the hook still wires.
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    mod = load_plugin("hermes-smd-reply")
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
    assert any(a == "REPLY_SENT" for a, _ in events)
    _, meta = next((a, m) for a, m in events if a == "REPLY_SENT")
    assert meta["recipient"] == "greg@whitfield.example"
    assert "body_digest" in meta
    assert "Thanks for reaching out" not in json.dumps(meta)


def test_runtime_mcp_tool_name_fires_relay(relay_mod) -> None:
    """Regression for the 2026-06-12 inbound live test: the relay hooked the
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
    the relay reads under the AGENT session_id. When they differ (the 2026-06-12
    live bug — draft created, no reply sent), the relay recovers the verified
    origin by matching the draft's recipient against the address index."""
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
    assert any(a == "REPLY_HELD" and m["reason"] == "recipient_mismatch" for a, m in d1.events())


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
    assert any(a == "REPLY_HELD" and m["reason"] == "no_inbox_id" for a, m in d1.events())


# ---------------------------------------------------------------------------
# 2. Roster authorization — the org roster is what permits an autonomous reply
# ---------------------------------------------------------------------------


def test_empty_roster_holds(relay_mod) -> None:
    mod, d1, sent = relay_mod
    # Unauthored roster: scope.inbound_allow_from absent. Read LIVE, so rewriting
    # the customer.yaml to drop it holds the reply on the very next call — no
    # restart (ADR 0044). A verified sender still gets a HELD audit row (the
    # employee drafted, did not send), reason=sender_not_on_roster.
    mod._YAML_PATH.write_text(_NO_ROSTER_YAML)
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"] == "sender_not_on_roster" for a, m in d1.events())


def test_sender_not_on_roster_holds(relay_mod) -> None:
    mod, d1, sent = relay_mod
    # The roster authors greg; a DIFFERENT verified inbound sender is held —
    # reaching/replying outside the roster needs explicit authorization.
    _record_origin(sender="stranger@elsewhere.test", message_id="msg_in", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["stranger@elsewhere.test"]),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"] == "sender_not_on_roster" for a, m in d1.events())


def test_domain_roster_entry_matches(relay_mod) -> None:
    mod, _d1, sent = relay_mod
    # An "@domain" roster entry authorizes any sender at that domain.
    mod._YAML_PATH.write_text(
        "customer_id: acme\nvertical: law-firm\nscope:\n"
        "  inbound_allow_from:\n    - '@whitfield.example'\n"
    )
    _record_origin(sender="anyone@whitfield.example", message_id="msg_in", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["anyone@whitfield.example"]),
        session_id="s1",
    )
    assert len(sent) == 1


def test_live_roster_enables_mid_flight(relay_mod) -> None:
    """The positive ADR 0044 guarantee: adding a sender to the roster applies on
    the next call with no restart. Start with NO roster (held), author the sender
    onto scope.inbound_allow_from, and the very next identical call replies."""
    mod, _d1, sent = relay_mod
    mod._YAML_PATH.write_text(_NO_ROSTER_YAML)  # no roster
    _record_origin(sender="greg@whitfield.example", message_id="msg_in", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert sent == []
    # Author the sender onto the roster — no re-register, no restart.
    mod._YAML_PATH.write_text(_ROSTERED_YAML)
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert len(sent) == 1


def test_unreadable_yaml_fails_closed(relay_mod) -> None:
    """If customer.yaml is missing/unreadable at decision time, the relay cannot
    confirm the roster, so it fails closed and never sends."""
    mod, _d1, sent = relay_mod
    mod._YAML_PATH.unlink()  # remove the authored file out from under the live read
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
    )
    assert sent == []


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


def test_sensitive_body_held(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # A money/contract body trips the content floor — hold (do not relay).
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text="Our fee for this engagement is $5,000 due on signing the contract.",
        ),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"] == "content_sensitive" for a, m in d1.events())


def test_empty_body_held(relay_mod) -> None:
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    # Subject-only draft: nothing to transmit ⇒ held (content floor fails closed
    # on a bodyless send; the empty-body guard backstops it too).
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
    assert any(a == "REPLY_HELD" and m["reason"] == "rate_limited" for a, m in d1.events())


def test_rate_limiter_window_eviction() -> None:
    mod = load_plugin("hermes-smd-reply")
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
    mod = load_plugin("hermes-smd-reply")
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
    assert any(a == "REPLY_FAILED" for a, _ in d1.events())


# ---------------------------------------------------------------------------
# Pure-logic units (relay.py)
# ---------------------------------------------------------------------------


def test_recipient_locked_pure() -> None:
    mod = load_plugin("hermes-smd-reply")
    r = mod.relay
    assert r.recipient_locked({"to": ["a@x.test"]}, "a@x.test") is True
    assert r.recipient_locked({"to": ["A@X.test"]}, "a@x.test") is True
    assert r.recipient_locked({"to": ["a@x.test", "b@x.test"]}, "a@x.test") is False
    assert r.recipient_locked({"to": ["b@x.test"]}, "a@x.test") is False
    assert r.recipient_locked({"to": []}, "a@x.test") is False
    assert r.recipient_locked({}, "a@x.test") is False
    assert r.recipient_locked({"to": ["a@x.test"]}, "") is False


def test_send_reply_builds_request() -> None:
    mod = load_plugin("hermes-smd-reply")
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
