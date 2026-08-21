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

import hashlib
import itertools
import json

import pytest
import yaml

from shared import audit_contract, inbound, matter_binding
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
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()


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

    # ss#2258: send_reply no longer takes a key or an inbox — the broker pins
    # both. The fixture records exactly what the agent process can still express.
    def _fake_send(*, message_id, text, html, **_kw):
        sent.append({"message_id": message_id, "text": text, "html": html})
        return "msg_sent_1"

    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_ROSTERED_YAML)

    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", fake_d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    # One-reply-per-inbound register: process-wide in production, per-test here.
    # ``load_plugin`` hands back the process-cached module, so without a fresh
    # instance the reused ``msg_*`` ids from an earlier test would read as
    # duplicates in a later one.
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    # Held-reply store (#2070) is register-time infra; point it at a temp db so
    # the live-path hold tests exercise real persistence, not a stub.
    monkeypatch.setattr(
        mod,
        "_HELD_STORE",
        mod.held_store.HeldReplyStore(str(tmp_path / "held.db")),
        raising=False,
    )
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
    # ss#2258: the agent no longer names the inbox — the broker pins it from the
    # seat's own config, so identity is absent from what this process can express.
    assert "inbox_id" not in sent[0]
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
    # ss#2258: the agent no longer names the inbox — the broker pins it from the
    # seat's own config, so identity is absent from what this process can express.
    assert "inbox_id" not in sent[0]
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
    # ss#2258: the agent no longer names the inbox — the broker pins it from the
    # seat's own config, so identity is absent from what this process can express.
    assert "inbox_id" not in sent[0]
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


_SENSITIVE_BODY = "Our fee for this engagement is $5,000 due on signing the contract."


def test_internal_recipient_sensitive_body_sends(relay_mod) -> None:
    """ss #1932: the locked recipient is on ``scope.inbound_allow_from``, so they
    classify INTERNAL — the content floor does not apply, mirroring the send
    path's ADR 0072 carve-out (firm-internal coordination legitimately names
    deadlines, signatures, attorneys). The live failure this pins: an ack
    confirmation naming a "deadline" was held in drafts on its way back to a
    rostered colleague."""
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text=(
                "Acknowledged ACK-6WS08D. The deadline item is snoozed for 7 days; "
                "the attorney still needs to sign off in Smokeball."
            ),
        ),
        session_id="s1",
    )
    assert len(sent) == 1
    sent_meta = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert sent_meta and sent_meta[0]["recipient_class"] == "internal"
    assert sent_meta[0]["content_floor_applied"] is False


def test_non_internal_class_keeps_the_floor(relay_mod, monkeypatch) -> None:
    """The skip is CLASS-conditional, never unconditional: a locked recipient
    that does not classify INTERNAL keeps the content floor exactly as before."""
    from shared.recipient_classifier import RecipientClass

    mod, d1, sent = relay_mod
    monkeypatch.setattr(mod, "classify_recipients_typed", lambda *a, **k: RecipientClass.CLIENT)
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"], text=_SENSITIVE_BODY),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"] == "content_sensitive" for a, m in d1.events())


def test_classifier_fault_keeps_the_floor(relay_mod, monkeypatch) -> None:
    """Classification faults fail toward floored, never open."""
    mod, d1, sent = relay_mod

    def _boom(*_a, **_k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(mod, "classify_recipients_typed", _boom)
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"], text=_SENSITIVE_BODY),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"] == "content_sensitive" for a, m in d1.events())


def test_internal_recipient_fabrication_gate_still_applies(relay_mod) -> None:
    """The ADR 0072 carve-out skips ONLY the content floor. A Tier-1 fabrication
    marker holds the reply even to an INTERNAL recipient."""
    mod, d1, sent = relay_mod
    _record_origin(sender="greg@whitfield.example")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text="The client portal is coming soon; tell the team to hold questions.",
        ),
        session_id="s1",
    )
    assert sent == []
    assert any(a == "REPLY_HELD" and m["reason"].startswith("fabrication") for a, m in d1.events())


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


_burst_seq = itertools.count()


def _burst(mod, n: int, sender: str = "greg@whitfield.example") -> None:
    """n live-path replies, each a fresh session opened by the same sender.

    Ids come from a process counter so two bursts inside one test are two runs
    of DISTINCT inbound emails, which is what a real dialogue is. Replaying a
    message id would now read as the same email answered twice and hold on the
    one-reply-per-inbound guard, masking whatever the test is actually pinning.
    """
    for _ in range(n):
        i = next(_burst_seq)
        sid = f"s{i}"
        inbound.SESSION_INBOUND_ORIGIN.record(
            sid,
            inbound.InboundOrigin(sender, f"msg_{i}", inbox_id="inbox_x"),
        )
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft([sender]),
            session_id=sid,
        )


def test_per_sender_rate_limit(relay_mod) -> None:
    """The live path enforces the AUTHORED per-sender cap (send_policy is
    live-read per call — the ctor caps only govern the legacy allow() path)."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(_ROSTERED_YAML + "send_policy:\n  reply:\n    per_sender_max: 2\n")
    _burst(mod, 4)
    assert len(sent) == 2  # only the first two cleared the per-sender window
    assert any(
        a == "REPLY_HELD" and m["reason"] == "rate_limited_per_sender" for a, m in d1.events()
    )


def test_unauthored_policy_pins_current_defaults(relay_mod) -> None:
    """No send_policy block ⇒ exactly the pre-#2070 behavior: 3 per sender
    per window, granular hold reason, no exemption. The regression pin."""
    mod, d1, sent = relay_mod
    _burst(mod, 5)
    assert len(sent) == 3
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert len(held) == 2 and all(m["reason"] == "rate_limited_per_sender" for m in held)


def test_internal_exempt_sustains_dialogue(relay_mod) -> None:
    """The #2070 headline: a rostered INTERNAL sender under an authored
    exemption sustains a 10-exchange dialogue with zero rate holds, bounded
    only by the reply backstop."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML
        + "send_policy:\n"
        + "  reply:\n"
        + "    internal_exempt: true\n"
        + "    backstop_max: 60\n"
        + "    backstop_window_seconds: 3600\n"
    )
    _burst(mod, 10)
    assert len(sent) == 10
    assert not [m for a, m in d1.events() if a == "REPLY_HELD"]


def test_backstop_bounds_exempt_senders(relay_mod) -> None:
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML
        + "send_policy:\n"
        + "  reply:\n"
        + "    internal_exempt: true\n"
        + "    backstop_max: 4\n"
    )
    _burst(mod, 6)
    assert len(sent) == 4
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert len(held) == 2 and all(m["reason"] == "rate_limited_backstop" for m in held)


def test_malformed_policy_falls_back_to_defaults(relay_mod) -> None:
    """A typo in send_policy tightens back to the platform defaults — it can
    never loosen (whole-block fail-closed)."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML
        + "send_policy:\n"
        + "  reply:\n"
        + "    internal_exempt: true\n"
        + "    backstop_max: -5\n"  # malformed ⇒ ENTIRE block defaults, exemption dropped
    )
    _burst(mod, 5)
    assert len(sent) == 3  # platform default per-sender cap
    assert any(
        a == "REPLY_HELD" and m["reason"] == "rate_limited_per_sender" for a, m in d1.events()
    )


def test_classification_fault_keeps_caps_despite_exemption(relay_mod, monkeypatch) -> None:
    """If recipient classification raises, the sender is NOT internal for
    exemption purposes — the default caps still bind (fail-closed)."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML + "send_policy:\n  reply:\n    internal_exempt: true\n"
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("classifier fault")

    monkeypatch.setattr(mod, "classify_recipients_typed", _boom)
    _burst(mod, 5)
    assert len(sent) == 3  # default per-sender cap still applied
    assert any(
        a == "REPLY_HELD" and m["reason"] == "rate_limited_per_sender" for a, m in d1.events()
    )


def test_policy_live_read_mid_flight(relay_mod) -> None:
    """Authoring the exemption between calls takes effect with no restart
    (ADR 0044): capped before, flowing after."""
    mod, d1, sent = relay_mod
    _burst(mod, 4)  # default policy: 3 sent, 1 held
    assert len(sent) == 3
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML + "send_policy:\n  reply:\n    internal_exempt: true\n"
    )
    _burst(mod, 3)
    assert len(sent) == 6  # all three flowed under the authored exemption


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


def test_send_reply_delegates_to_the_broker_and_carries_no_credential() -> None:
    """ss#2258: the reply transport is a broker verb, not a REST call from here.

    What this pins is the ABSENCE of authority in this process: no api_key, no
    inbox id, no recipient. The agent supplies content and the source message id;
    everything about who may receive it is decided by the process holding the key.
    """
    mod = load_plugin("hermes-smd-reply")
    captured = {}

    def _sender(*, message_id, text, html, session_id="", matter_ref=None):
        captured.update(
            message_id=message_id,
            text=text,
            html=html,
            session_id=session_id,
            matter_ref=matter_ref,
        )
        return "msg_out"

    out = mod.relay.send_reply(
        message_id="msg_in", text="hello", html="<p>hello</p>", sender=_sender
    )
    assert out == "msg_out"
    # The audit joins (ss-console#2497) ride along and are EMPTY here, because
    # this caller supplied none — the absence is recorded as absence, never as a
    # placeholder. What stays pinned is that no recipient, key, or inbox id
    # crosses this seam.
    assert captured == {
        "message_id": "msg_in",
        "text": "hello",
        "html": "<p>hello</p>",
        "session_id": "",
        "matter_ref": None,
    }


def test_send_reply_surfaces_a_broker_refusal_as_a_send_error() -> None:
    """A refusal must reach the caller's audited REPLY_FAILED path, with its reason.

    The broker refuses when the original sender is not on inbound_allow_from —
    anyone can email a seat's inbox, so that check is the reply lane's fence.
    """
    mod = load_plugin("hermes-smd-reply")

    def _refuse(**_kw):
        raise mod.relay.agentmail_broker.BrokerError("sender is not on inbound_allow_from")

    with pytest.raises(mod.relay.RelaySendError, match="inbound_allow_from"):
        mod.relay.send_reply(message_id="m", text="t", html="", sender=_refuse)


def test_send_reply_distinguishes_an_unreachable_broker_from_a_refusal() -> None:
    """A socket failure is not a policy decision and must not read as one."""
    mod = load_plugin("hermes-smd-reply")

    def _down(**_kw):
        raise mod.relay.agentmail_broker.AgentMailBrokerUnavailable("socket missing")

    with pytest.raises(mod.relay.RelaySendError, match="unavailable"):
        mod.relay.send_reply(message_id="m", text="t", html="", sender=_down)


# ---------------------------------------------------------------------------
# 5b. Held-reply persistence + ordering guard (#2070 O2)
# ---------------------------------------------------------------------------

_RELEASE_YAML = (
    _ROSTERED_YAML
    + "send_policy:\n"
    + "  reply:\n"
    + "    per_sender_max: 2\n"
    + "  held_release:\n"
    + "    enabled: true\n"
)


def test_rate_hold_enqueues_for_release(relay_mod) -> None:
    """A rate hold now PERSISTS instead of vanishing (the burst-rehearsal fix)."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(_RELEASE_YAML)
    _burst(mod, 3)
    assert len(sent) == 2
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert len(held) == 1
    assert held[0]["reason"] == "rate_limited_per_sender"
    assert held[0]["held_for_release"] is True
    assert mod._HELD_STORE.pending_count() == 1


def test_queued_behind_held_preserves_order(relay_mod) -> None:
    """Once a sender has a reply waiting, later replies queue behind it — a
    cleared window must never let answer 5 overtake answer 4."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(_RELEASE_YAML)
    _burst(mod, 4)
    assert len(sent) == 2  # 3rd rate-held, 4th queued behind it
    reasons = [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"]
    assert reasons == ["rate_limited_per_sender", "queued_behind_held"]
    assert mod._HELD_STORE.pending_count() == 2


def test_hold_drops_when_release_unauthored(relay_mod) -> None:
    """Without held_release, a rate hold behaves exactly as before #2070."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(_ROSTERED_YAML + "send_policy:\n  reply:\n    per_sender_max: 1\n")
    _burst(mod, 3)
    assert len(sent) == 1
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert len(held) == 2
    assert all(m["held_for_release"] is False for m in held)
    assert mod._HELD_STORE.pending_count() == 0


def test_semantic_holds_never_enqueue(relay_mod) -> None:
    """Roster/recipient-lock refusals are decisions, not delays — never queued."""
    mod, d1, sent = relay_mod
    mod._YAML_PATH.write_text(
        _NO_ROSTER_YAML + "send_policy:\n  held_release:\n    enabled: true\n"
    )
    _record_origin(sender="greg@whitfield.example", session="sx")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="sx",
    )
    assert sent == []
    assert any(m["reason"] == "sender_not_on_roster" for a, m in d1.events() if a == "REPLY_HELD")
    assert mod._HELD_STORE.pending_count() == 0


# ---------------------------------------------------------------------------
# Origin resolution after #195: the bound session wins over the address guess
# ---------------------------------------------------------------------------


def test_bound_session_beats_a_fresher_address_entry(relay_mod) -> None:
    """The misattribution regression.

    Two messages from the same sender are in flight; the address index holds
    the LATER one. A session bound (by message id) to the EARLIER message must
    reply into that earlier thread, not the address index's most-recent.
    """
    mod, d1, sent = relay_mod
    reg = inbound.SESSION_INBOUND_ORIGIN
    reg.record("", inbound.InboundOrigin("greg@whitfield.example", "msg_early", "", "inbox_x"))
    reg.record("", inbound.InboundOrigin("greg@whitfield.example", "msg_late", "", "inbox_x"))
    assert reg.bind("agent-1", "msg_early") is True

    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="agent-1",
    )
    assert len(sent) == 1
    assert sent[0]["message_id"] == "msg_early"  # not the fresher msg_late
    _, meta = next((a, m) for a, m in d1.events() if a == "REPLY_SENT")
    assert meta["in_reply_to"] == "msg_early"


# ---------------------------------------------------------------------------
# 9. Draft outcome + one-reply-per-inbound (leg-1 double-send)
#
# Live defect, 2026-07-30 (vfy_01KYTG0B88R3B5K0D7FKPACRZT): the relay acted on
# the tool NAME alone. ``mcp_agentmail_create_draft`` returned "Message not
# found (HTTP 404)" — no draft existed — and a real email went out anyway; the
# agent retried, the retry succeeded, and the SAME answer was emailed a second
# time. Every later turn of the dialogue then quoted a stale reply.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "error"},
        {"error_type": "Message not found (HTTP 404)"},
        {"result": '{"error": "Message not found", "code": 404}'},
        {"result": '{"ok": false}'},
        {"result": '{"status": "failed"}'},
    ],
    ids=["status", "error_type", "result_error", "result_ok_false", "result_status"],
)
def test_failed_draft_call_never_relays(relay_mod, kw) -> None:
    """Every shape in which the tool can say "no draft" refuses the send."""
    mod, d1, sent = relay_mod
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin("greg@whitfield.example", "msg_fail", inbox_id="inbox_x"),
    )
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
        **kw,
    )
    assert sent == []
    # Not a hold either — nothing was ever draftable, so there is no reply to
    # hold. The agent's own retry is the recovery path.
    assert not [a for a, _ in d1.events() if a in {"REPLY_SENT", "REPLY_HELD"}]


def test_unknown_result_shape_still_relays(relay_mod) -> None:
    """Detection is positive-only: an unrecognised envelope must not silence
    the channel. A future Hermes result shape degrades to today's behaviour,
    with the one-reply guard as the structural backstop."""
    mod, _d1, sent = relay_mod
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin("greg@whitfield.example", "msg_odd", inbox_id="inbox_x"),
    )
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
        result="<not json at all>",
        status="ok",
    )
    assert len(sent) == 1


def test_retry_after_failed_draft_sends_exactly_once(relay_mod) -> None:
    """The live sequence: failed draft, then the agent's successful retry —
    one inbound, one email. Before the fix this produced two."""
    mod, d1, sent = relay_mod
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin("greg@whitfield.example", "msg_retry", inbox_id="inbox_x"),
    )
    args = _draft(["greg@whitfield.example"])
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=args,
        session_id="s1",
        error_type="Message not found (HTTP 404)",
    )
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft", args=args, session_id="s1", status="ok"
    )
    assert len(sent) == 1
    assert sent[0]["message_id"] == "msg_retry"
    assert len([a for a, _ in d1.events() if a == "REPLY_SENT"]) == 1


def test_second_successful_draft_for_one_inbound_is_held(relay_mod) -> None:
    """One message in, at most one reply out — even when BOTH draft calls
    succeed and no result envelope reveals the retry."""
    mod, d1, sent = relay_mod
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin("greg@whitfield.example", "msg_dup", inbox_id="inbox_x"),
    )
    for _ in range(2):
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"]),
            session_id="s1",
        )
    assert len(sent) == 1
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == ["duplicate_reply"]


def test_distinct_inbounds_each_get_a_reply(relay_mod) -> None:
    """The guard keys on the INBOUND message id, not the body — the same
    answer to two different emails still goes out twice."""
    mod, _d1, sent = relay_mod
    for i in range(2):
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
    assert [s["message_id"] for s in sent] == ["msg_0", "msg_1"]


def test_rate_held_reply_commits_once_and_releases_once(relay_mod) -> None:
    """A rate-held reply is enqueued exactly once. Without the commit-on-enqueue
    a retry in the same turn would queue a SECOND row and the sweeper would
    deliver the answer twice."""
    mod, d1, _sent = relay_mod
    mod._YAML_PATH.write_text(
        _ROSTERED_YAML + "send_policy:\n"
        "  reply:\n"
        "    per_sender_max: 0\n"
        "  held_release:\n"
        "    enabled: true\n"
    )
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin("greg@whitfield.example", "msg_held", inbox_id="inbox_x"),
    )
    for _ in range(2):
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"]),
            session_id="s1",
        )
    assert mod._HELD_STORE.pending_count() == 1
    reasons = [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"]
    assert reasons[-1] == "duplicate_reply"


# ---------------------------------------------------------------------------
# 10. Provenance captions on the reply channel
#
# Live defect, same rehearsal: the drafting path passes
# ``allowed_case_names=provenance.register_for(session).captions()``; the relay
# passed nothing. The trust gate allowed a draft naming matters read from
# Smokeball, then this gate blocked the identical body as
# fabrication:tier2_citation, and the sender got silence with no notice.
# ---------------------------------------------------------------------------


def test_caption_read_this_session_is_quotable(relay_mod) -> None:
    """A case caption the agent READ this session clears the citation tier."""
    from shared import provenance

    mod, _d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_prov"
    provenance.note_session(session_id)
    provenance.record_read(session_id, "Open matters: Alvarez v. Brightline Freight (active)")
    inbound.SESSION_INBOUND_ORIGIN.record(
        session_id,
        inbound.InboundOrigin("greg@whitfield.example", "msg_prov", inbox_id="inbox_x"),
    )
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text="One of the open matters is Alvarez v. Brightline Freight.",
        ),
        session_id=session_id,
    )
    assert len(sent) == 1
    provenance._reset_for_tests()


def test_unread_caption_still_blocks(relay_mod) -> None:
    """The exemption is provenance, not amnesty: a caption NOT read this
    session is still refused. This is the ADR 0028 invariant, unchanged."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_noprov"
    provenance.note_session(session_id)
    inbound.SESSION_INBOUND_ORIGIN.record(
        session_id,
        inbound.InboundOrigin("greg@whitfield.example", "msg_noprov", inbox_id="inbox_x"),
    )
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(
            ["greg@whitfield.example"],
            text="As held in Marbury v. Madison, the claim survives.",
        ),
        session_id=session_id,
    )
    assert sent == []
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == [
        "fabrication:tier2_citation"
    ]
    provenance._reset_for_tests()


# ---------------------------------------------------------------------------
# 7. Matter identity on the reply lane (ss#2167)
#
# These exist because the matter gate was shipped, kill-tested, and reported
# working while doing NOTHING on this lane. It lives in enforce.evaluate_tool_call
# behind `is_send`, which is true only for EXTERNAL_SEND* classes; the tool this
# lane calls is create_draft, which is INTERNAL_WRITE. So the gate never ran,
# and this function relayed the draft out as real email anyway — 86 of the
# pilot's replies, with no matter-identity check of any kind.
#
# A test of the gate's verdict logic cannot catch that, which is the whole point:
# the verdict logic was correct the entire time. Only a test of THIS path can.
#
# WHAT THESE PROVE, AND THE CHECK THAT KEEPS IT TRUE.
# Until ss#2263 they pinned logic against an UNAUTHORABLE config, and said so:
# the roster below puts one address on `scope.inbound_allow_from` AND types it in
# `scope.outbound_roster`, which both validators used to reject ("a recipient
# cannot be both internal and a typed outbound class"). A reply only fires for a
# sender on `inbound_allow_from`, so such a sender could never carry a typed
# class, and the gate's enforcing branch was unreachable in every AUTHORABLE
# config — a stronger statement than "unconfigured today".
#
# ss#2263 made exactly this config authorable, so these now exercise a reachable
# state. That claim is not left to prose: `test_client_typed_yaml_is_authorable`
# below runs the SEAT'S OWN validator over the literal yaml these tests write. If
# the schema ever re-forbids the overlap, that test fails and takes this section's
# meaning down with it, rather than letting a green run keep asserting coverage
# the config can no longer reach.
# ---------------------------------------------------------------------------

_M_A = "aaaaaaaa-1111-2222-3333-444444444444"
_NUM_A = "2026-PI-101"
_PARTY_A = "alvarez@example.com"
_SENDER = "greg@whitfield.example"


# A roster where the inbound sender is ALSO typed as a client. This is the
# configuration that makes the matter gate reachable on this lane at all: an
# inbound-roster match alone classifies INTERNAL (and is exempt), so without a
# typed CLIENT entry every one of these tests would pass against a gate that
# never ran.
_CLIENT_TYPED_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - greg@whitfield.example\n"
    "  outbound_roster:\n"
    "    - address: greg@whitfield.example\n"
    "      class: client\n"
)


def _type_sender_as_client(mod) -> None:
    mod._YAML_PATH.write_text(_CLIENT_TYPED_YAML)


def test_client_typed_yaml_is_authorable(tmp_path):
    """The falsifier for this whole section (ss#2263, ss#2271).

    Every reply-lane matter-gate test below writes ``_CLIENT_TYPED_YAML``. That
    config used to be REJECTED — one address on ``scope.inbound_allow_from`` and
    also typed in ``scope.outbound_roster`` — which made those tests pin logic no
    firm could ever author. Nothing in a passing run of them said so; only a
    comment did, and a comment cannot fail.

    So the acceptance is asserted against the seat's OWN validator, on the literal
    bytes the tests write. If the overlap is ever re-forbidden, this fails and the
    section's coverage claim fails with it, instead of quietly reverting to
    unreachable-but-green.
    """
    from bootstrap.validate import _validate_outbound_roster

    cfg = yaml.safe_load(_CLIENT_TYPED_YAML)
    errors: list[str] = []
    _validate_outbound_roster(cfg, errors)
    assert errors == []

    # And the classifier agrees with the validator: this sender is a CLIENT, not
    # firm staff — which is what makes the gate below run at all.
    from shared.customer_config import CustomerConfig
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    path = tmp_path / "customer.yaml"
    path.write_text(_CLIENT_TYPED_YAML)
    conf = CustomerConfig.from_volume(str(path))
    assert (
        classify_recipients_typed([_SENDER], conf.inbound_roster, conf.outbound_roster)
        is RecipientClass.CLIENT
    )


def _seed_closed(party: str) -> None:
    """Matter A's OWN complete party list was read this turn."""
    matter_binding._reset_for_tests()
    m = matter_binding.membership_for("s1")
    m.add(_M_A, [party], complete=True)
    m.add_alias(_NUM_A, _M_A)


def test_reply_citing_another_matter_is_held_not_relayed(relay_mod) -> None:
    mod, d1, sent = relay_mod
    # Matter A belongs to Alvarez. The inbound sender is not a party to it.
    _seed_closed(_PARTY_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_matter_1", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. The deposition is set for Tuesday."),
        session_id="s1",
    )
    assert sent == [], "a cross-matter reply reached the transport"
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert [h["reason"] for h in held] == ["matter_mismatch"]
    # The hold must name what disagreed, or a reviewer cannot action it.
    assert _NUM_A in held[0]["matters"]
    assert _SENDER in held[0]["detail"]
    matter_binding._reset_for_tests()


def test_control_reply_to_a_party_of_the_cited_matter_is_relayed(relay_mod) -> None:
    # The half that makes the test above mean something. Without it, a gate that
    # held EVERY reply would satisfy the assertion and look like a working control.
    mod, d1, sent = relay_mod
    _seed_closed(_SENDER)  # this time the sender IS a party to matter A
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_matter_2", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. The deposition is set for Tuesday."),
        session_id="s1",
    )
    assert len(sent) == 1, "a correct-pairing reply was withheld"
    assert not [m for a, m in d1.events() if a == "REPLY_HELD"]
    matter_binding._reset_for_tests()


def test_reply_citing_no_matter_is_untouched(relay_mod) -> None:
    # Scope control: the gate must not interfere with ordinary reply traffic.
    mod, d1, sent = relay_mod
    _seed_closed(_PARTY_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_matter_3", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text="Thanks, received. We'll follow up shortly."),
        session_id="s1",
    )
    assert len(sent) == 1
    matter_binding._reset_for_tests()


def test_unresolved_membership_records_but_does_not_hold(relay_mod) -> None:
    # Captain call 2026-08-11: get_matter fires on 8 of 86 reply turns, so
    # holding on unresolved would withhold correct client replies at a rate
    # nobody has measured. The row IS the measurement.
    mod, d1, sent = relay_mod
    matter_binding._reset_for_tests()
    # The matter is known by number but its party set was never closed.
    m = matter_binding.membership_for("s1")
    m.add(_M_A, [_PARTY_A], complete=False)
    m.add_alias(_NUM_A, _M_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_matter_4", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. Noted."),
        session_id="s1",
    )
    assert len(sent) == 1, "an unresolved membership withheld a reply"
    unresolved = [m for a, m in d1.events() if a == "MATTER_UNRESOLVED"]
    assert len(unresolved) == 1
    assert _NUM_A in unresolved[0]["matters"]
    matter_binding._reset_for_tests()


def test_report_mode_records_nothing_and_holds_nothing(relay_mod, monkeypatch) -> None:
    mod, d1, sent = relay_mod
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "report")
    _seed_closed(_PARTY_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_matter_5", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. The deposition is set for Tuesday."),
        session_id="s1",
    )
    assert len(sent) == 1
    assert not [m for a, m in d1.events() if a == "REPLY_HELD"]
    matter_binding._reset_for_tests()


# ---------------------------------------------------------------------------
# 15. The money register on the reply channel (ss-console#2367)
#
# The caption half above fixed one exemption on one path. ss#2258 then gave
# ``specific-dollar-amount`` a provenance-scoped exemption on the DRAFTING path
# and said, deliberately, "no change to any other output path". This is the
# consequence, one path over: on 2026-08-13 the Operator read fifteen documents
# on 2026-PI-104, filed the demand letter through the checked seam, and its
# reply naming the filing was held ``fabrication:tier1_marker`` on the Kaiser
# lien and the MedFin payoff. Both figures were read that session off the firm's
# own records; both were cited to their source in the sentence that carried
# them. The firm asked for a demand letter and got silence.
#
# Both directions are tested, because a narrowing that only proves the permit
# direction is a hole: an INVENTED figure in a reply must still block.
# ---------------------------------------------------------------------------

# The two documents the Operator actually read that session, in the shape a
# read returns them.
_KAISER_READ = (
    "Kaiser Third-Party Liability Assertion, dated 2026-07-14: total asserted lien $9,310.02."
)
_MEDFIN_READ = "MedFin Payoff Statement, dated 2026-07-29: payoff amount $12,500.00."


def _reply_with_money(mod, session_id, text, message_id, *, tool_call_id=None):
    """Run one create_draft through the relay for a body carrying figures."""
    inbound.SESSION_INBOUND_ORIGIN.record(
        session_id,
        inbound.InboundOrigin("greg@whitfield.example", message_id, inbox_id="inbox_x"),
    )
    kwargs = {
        "tool_name": "agentmail:create_draft",
        "args": _draft(["greg@whitfield.example"], text=text),
        "session_id": session_id,
    }
    if tool_call_id is not None:
        kwargs["tool_call_id"] = tool_call_id
    mod.on_post_tool_call(**kwargs)


def test_a_figure_read_this_session_is_relayable(relay_mod) -> None:
    """Direction one, and the delivery that was lost. A figure traced to a
    document read this session passes on the reply path exactly as it does on
    the drafting path."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_money_ok"
    provenance.note_session(session_id)
    provenance.record_read(session_id, _KAISER_READ)
    provenance.record_read(session_id, _MEDFIN_READ)
    _reply_with_money(
        mod,
        session_id,
        "The demand is filed on the matter. Two figures need your resolution: "
        "the Kaiser lien of $9,310.02 per the Third-Party Liability Assertion "
        "dated 2026-07-14, and the MedFin payoff of $12,500.00 per the payoff "
        "statement dated 2026-07-29.",
        "msg_money_ok",
    )
    assert len(sent) == 1, [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert not [m for a, m in d1.events() if a == "REPLY_HELD"]
    provenance._reset_for_tests()


def test_an_invented_figure_in_a_reply_still_blocks(relay_mod) -> None:
    """Direction two. Without this the narrowing is a hole: the exemption is
    provenance, not amnesty, and a figure in no source the agent read is exactly
    the fabrication the gate exists for."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_money_invented"
    provenance.note_session(session_id)
    provenance.record_read(session_id, _KAISER_READ)
    _reply_with_money(
        mod,
        session_id,
        "The demand is filed. We value this claim at $88,000.00 to resolve.",
        "msg_money_invented",
    )
    assert sent == [], "an invented figure was relayed to a client"
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == [
        "fabrication:tier1_marker"
    ]
    provenance._reset_for_tests()


def test_one_invented_figure_holds_a_reply_full_of_verified_ones(relay_mod) -> None:
    """All-or-nothing on this path too. A body with one read figure and one
    invented figure is not partly honest, and waiving the marker there would
    let the invented one ride out beside the real one."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_money_mixed"
    provenance.note_session(session_id)
    provenance.record_read(session_id, _KAISER_READ)
    _reply_with_money(
        mod,
        session_id,
        "The Kaiser lien is $9,310.02 per the assertion on the matter, and we "
        "will demand $88,000.00.",
        "msg_money_mixed",
    )
    assert sent == []
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == [
        "fabrication:tier1_marker"
    ]
    provenance._reset_for_tests()


def test_a_session_that_read_nothing_gets_no_exemption(relay_mod) -> None:
    """The fail-closed default is unchanged. An empty register exempts nothing,
    so this path degrades to exactly the behaviour that shipped before."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_money_empty"
    provenance.note_session(session_id)
    _reply_with_money(
        mod,
        session_id,
        "The Kaiser lien is $9,310.02 per the assertion on the matter.",
        "msg_money_empty",
    )
    assert sent == []
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == [
        "fabrication:tier1_marker"
    ]
    provenance._reset_for_tests()


def test_the_money_register_does_not_widen_the_other_markers(relay_mod) -> None:
    """Scope control. The exemption is one marker wide: a full money register
    does not rescue a body that trips a different Tier-1 marker."""
    from shared import provenance

    mod, d1, sent = relay_mod
    provenance._reset_for_tests()
    session_id = "s_money_other"
    provenance.note_session(session_id)
    provenance.record_read(session_id, _KAISER_READ)
    _reply_with_money(
        mod,
        session_id,
        "The lien is $9,310.02. We'll reach out to schedule kickoff.",
        "msg_money_other",
    )
    assert sent == []
    assert [m["reason"] for a, m in d1.events() if a == "REPLY_HELD"] == [
        "fabrication:tier1_marker"
    ]
    provenance._reset_for_tests()


# ---------------------------------------------------------------------------
# 16. A hold the agent can act on (ss-console#2367)
#
# ``post_tool_call`` returns are collected and ignored by the firing site
# (docs/hook-surface.md §2), so every hold this plugin made went to two places
# the model cannot read: a D1 row and a Sentry message. That is why the authored
# redraft-once recovery in demand-letter-drafter/SKILL.md never fired on
# 2026-08-13. It was not a compliance failure; the signal never arrived.
# ``transform_tool_result`` fires immediately after, for the same tool_call_id,
# and its first str return replaces the tool result. These tests pin that the
# hold reaches that surface, that a delivered reply says nothing, and that a
# reply queued for automatic release is never reported as undelivered.
# ---------------------------------------------------------------------------

_DRAFT_RESULT = '{"draft_id": "dft_123", "status": "ok"}'


def _transform(mod, tool_call_id, result=_DRAFT_RESULT, tool_name="agentmail:create_draft"):
    return mod.on_transform_tool_result(
        tool_name=tool_name,
        args={},
        result=result,
        session_id="s_notice",
        tool_call_id=tool_call_id,
    )


def test_registers_transform_tool_result(fake_ctx, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    mod = load_plugin("hermes-smd-reply")
    mod.register(fake_ctx)
    assert "transform_tool_result" in fake_ctx.registered


def test_a_held_reply_is_told_to_the_agent(relay_mod) -> None:
    """The whole issue in one assertion: the turn that held the reply now ends
    with the hold in the model's context instead of ``create_draft -> ok``."""
    from shared import provenance

    mod, _d1, sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    provenance._reset_for_tests()
    session_id = "s_notice_held"
    provenance.note_session(session_id)
    _reply_with_money(
        mod,
        session_id,
        "We value this claim at $88,000.00.",
        "msg_notice_held",
        tool_call_id="tc_held",
    )
    assert sent == []
    out = _transform(mod, "tc_held")
    assert isinstance(out, str)
    assert "WAS NOT SENT" in out
    assert "fabrication:tier1_marker" in out
    assert "greg@whitfield.example" in out
    assert "Redraft once" in out
    # The draft id survives: the agent needs it to update the draft it is about
    # to redraft, so the notice is appended, never substituted.
    assert "dft_123" in out
    provenance._reset_for_tests()


def test_the_notice_never_carries_the_draft_body(relay_mod) -> None:
    """Same rule as the audit row. The hold is reported by reason, never by
    quoting what was withheld."""
    from shared import provenance

    mod, _d1, _sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    provenance._reset_for_tests()
    body = "We value this claim at $88,000.00."
    _reply_with_money(mod, "s_notice_body", body, "msg_notice_body", tool_call_id="tc_body")
    out = _transform(mod, "tc_body")
    assert out is not None
    assert "$88,000.00" not in out
    assert body not in out
    provenance._reset_for_tests()


def test_a_delivered_reply_says_nothing(relay_mod) -> None:
    """The control that makes the test above mean something: a notice on every
    draft would satisfy "the agent is told" while telling it nothing true."""
    mod, _d1, sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    _record_origin(message_id="msg_notice_sent")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["greg@whitfield.example"]),
        session_id="s1",
        tool_call_id="tc_sent",
    )
    assert len(sent) == 1
    assert _transform(mod, "tc_sent") is None


def test_a_hold_is_told_exactly_once(relay_mod) -> None:
    """One hold, one telling. A second transform for the same call (a retried
    dispatch, another registrant) must not re-announce it."""
    from shared import provenance

    mod, _d1, _sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    provenance._reset_for_tests()
    _reply_with_money(
        mod, "s_notice_once", "We value this claim at $88,000.00.", "msg_once", tool_call_id="tc_1"
    )
    assert _transform(mod, "tc_1") is not None
    assert _transform(mod, "tc_1") is None
    provenance._reset_for_tests()


def test_a_hold_is_told_only_on_the_call_that_produced_it(relay_mod) -> None:
    """Keyed on the dispatch, not the session: an unrelated draft call in the
    same turn is not handed someone else's hold."""
    from shared import provenance

    mod, _d1, _sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    provenance._reset_for_tests()
    _reply_with_money(
        mod,
        "s_notice_key",
        "We value this claim at $88,000.00.",
        "msg_key",
        tool_call_id="tc_mine",
    )
    assert _transform(mod, "tc_other") is None
    assert _transform(mod, "tc_mine") is not None
    provenance._reset_for_tests()


def test_a_non_draft_result_is_never_touched(relay_mod) -> None:
    mod, _d1, _sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    assert _transform(mod, "tc_x", tool_name="mcp_smokeball_read_document") is None


def test_a_queued_reply_is_not_reported_as_undelivered(relay_mod) -> None:
    """A rate-held reply that WILL be released is not silence. Reporting it as
    undelivered would provoke a redraft and deliver the answer twice."""
    mod, d1, sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    mod._YAML_PATH.write_text(_RELEASE_YAML)
    for i in range(3):
        inbound.SESSION_INBOUND_ORIGIN.record(
            f"s_q{i}",
            inbound.InboundOrigin("greg@whitfield.example", f"msg_q{i}", inbox_id="inbox_x"),
        )
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"]),
            session_id=f"s_q{i}",
            tool_call_id=f"tc_q{i}",
        )
    assert len(sent) == 2
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert [h["held_for_release"] for h in held] == [True]
    assert _transform(mod, "tc_q2") is None, "a queued reply was reported as lost"


def test_a_dropped_rate_hold_is_still_told(relay_mod) -> None:
    """The other half: with no release authored the reply is genuinely dropped,
    and that IS silence the agent has to know about."""
    mod, _d1, sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    mod._YAML_PATH.write_text(_ROSTERED_YAML + "send_policy:\n  reply:\n    per_sender_max: 1\n")
    for i in range(2):
        inbound.SESSION_INBOUND_ORIGIN.record(
            f"s_d{i}",
            inbound.InboundOrigin("greg@whitfield.example", f"msg_d{i}", inbox_id="inbox_x"),
        )
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"]),
            session_id=f"s_d{i}",
            tool_call_id=f"tc_d{i}",
        )
    assert len(sent) == 1
    out = _transform(mod, "tc_d1")
    assert out is not None and "rate_limited" in out


def test_a_second_hold_for_one_message_stops_the_redraft_loop(relay_mod) -> None:
    """The authored recovery says redraft ONCE and then deliver the minimal
    factual note. A notice that said "redraft" forever would author a loop."""
    from shared import provenance

    mod, _d1, _sent = relay_mod
    mod._HOLD_NOTICES._reset_for_tests()
    provenance._reset_for_tests()
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s_loop",
        inbound.InboundOrigin("greg@whitfield.example", "msg_loop", inbox_id="inbox_x"),
    )
    for n, call in enumerate(("tc_r1", "tc_r2")):
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft(["greg@whitfield.example"], text=f"We value this claim at $88,00{n}.00."),
            session_id="s_loop",
            tool_call_id=call,
        )
    first = _transform(mod, "tc_r1")
    second = _transform(mod, "tc_r2")
    assert first is not None and "Redraft once" in first
    assert second is not None
    assert "Do NOT redraft" in second, second
    provenance._reset_for_tests()


# ---------------------------------------------------------------------------
# Audit joins on the reply lane (ss-console#2497)
#
# Measured on the live ledgers 2026-08-21 (vfy_01M0H8DR6JAPYVHFMNJZXQZ517):
# session_id was on 0 of 8 REPLY_SENT rows on ashton-price and 0 of 207 on the
# pilot; matter_ref was NULL on every send row on both. A reply row that cannot
# name the matter it was about is what a disputed-communication question falls
# into, and it is what makes the #2167 silence read as innocence.
# ---------------------------------------------------------------------------


def test_a_sent_reply_names_its_session_the_matter_and_the_person(relay_mod) -> None:
    """FALSIFIER: revert ``_emit_reply_event`` to the two-argument form and all
    three assertions fail while every other reply test stays green, which is how
    eight months of send rows shipped with none of them."""
    mod, d1, sent = relay_mod
    _seed_closed(_SENDER)  # the sender IS a party, so the reply relays
    matter_binding.membership_for("s1").note_content_read(_M_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_join_1", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. Confirmed for Tuesday."),
        session_id="s1",
    )
    assert len(sent) == 1
    row = next(c for c in d1.calls if c[1][2] == "REPLY_SENT")
    meta = json.loads(row[1][-1])
    assert meta["session_id"] == "s1"
    assert meta["sender_key"] == hashlib.sha256(_SENDER.encode()).hexdigest()
    # matter_ref is the COLUMN, not a metadata key: the portal audit record
    # filters on the column, which is the whole reason the AC says so.
    by_col = dict(zip(audit_contract.COLUMNS, row[1], strict=True))
    assert by_col["matter_ref"] == _M_A
    matter_binding._reset_for_tests()


def test_a_held_reply_names_its_session_before_any_matter_work_happens(relay_mod) -> None:
    """The early holds return long before the matter gate runs, and they are the
    rows an auditor reads first when asking why someone got no answer.

    FALSIFIER: bind ``_CURRENT_CALL.session_id`` at the send site instead of at
    hook entry and this row loses its session while the sent-reply test above
    still passes."""
    mod, d1, sent = relay_mod
    # Not on the roster: held at step (a), before recipient-lock or matter work.
    _record_origin(sender="stranger@nowhere.example", message_id="msg_join_2", inbox_id="inbox_x")
    mod.on_post_tool_call(
        tool_name="agentmail:create_draft",
        args=_draft(["stranger@nowhere.example"]),
        session_id="s_hold",
    )
    assert sent == []
    held = [c for c in d1.calls if c[1][2] == "REPLY_HELD"]
    assert len(held) == 1
    assert json.loads(held[0][1][-1])["session_id"] == "s_hold"


def test_the_broker_is_told_the_same_two_joins_the_reply_row_carries(relay_mod) -> None:
    """One reply produces TWO rows, REPLY_SENT here and CONFIRM_SEND_DISPATCHED
    in the broker, and before this change the only way to match them was their
    timestamps. FALSIFIER: drop the forwarding at the ``relay.send_reply`` call
    site and this fails while the audit row above still passes."""
    mod, d1, _sent = relay_mod
    forwarded: list[dict] = []

    def _capture(*, message_id, text, html, session_id="", matter_ref=None):
        forwarded.append({"session_id": session_id, "matter_ref": matter_ref})
        return "msg_sent_2"

    _seed_closed(_SENDER)
    matter_binding.membership_for("s1").note_content_read(_M_A)
    _type_sender_as_client(mod)
    _record_origin(sender=_SENDER, message_id="msg_join_3", inbox_id="inbox_x")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mod.relay, "send_reply", _capture)
        mod.on_post_tool_call(
            tool_name="agentmail:create_draft",
            args=_draft([_SENDER], text=f"Re: matter {_NUM_A}. Confirmed."),
            session_id="s1",
        )
    assert forwarded == [{"session_id": "s1", "matter_ref": _M_A}]
    matter_binding._reset_for_tests()


def test_a_released_hold_claims_no_session_it_does_not_have(relay_mod) -> None:
    """The sweeper runs minutes later on its own thread and the held-reply store
    never persisted either join, so a released row must carry NEITHER rather than
    the sweeper's own absent session written as an attribution.

    FALSIFIER: default ``session_id`` to something truthy in
    ``_emit_reply_event`` and this row starts claiming a session that never
    composed it."""
    mod, d1, _sent = relay_mod
    mod._emit_reply_event(
        action_type="REPLY_SENT",
        metadata={"recipient": _SENDER, "released_from_hold": True},
    )
    row = next(c for c in d1.calls if c[1][2] == "REPLY_SENT")
    by_col = dict(zip(audit_contract.COLUMNS, row[1], strict=True))
    assert by_col["matter_ref"] is None
    assert "session_id" not in json.loads(row[1][-1])
