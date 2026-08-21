"""The audit digest a firm can check against its own Sent Items copy (ss-console#2501).

WHAT WAS WRONG WITH THE OLD DIGEST, in one paragraph, because the fix only makes
sense against it. ``body_digest`` is sha256 over ``relay.draft_body``'s
``scan_text``: subject + text + html joined. That string is what the content and
fabrication floors inspect, so the digest proves the audit row describes the body
those floors saw. It proves that INSIDE this system and nowhere else -- the
subject is never transmitted on a reply (a reply threads under "Re: ..."), so
counsel holding the firm's own copy of the message cannot reconstruct the input
and cannot recompute the hash. An audit field an outsider cannot check is a field
that asks them to take SMD's word for it.

WHAT THESE PIN. ``body_digest_authored`` (and ``body_digest_authored_html``) are
sha256 over EXACTLY the bytes handed to the transport. Every test below captures
those bytes at the transport seam -- the fake broker, not the plugin's own
variables -- and recomputes the hash from what the transport received. That is
the property, stated as the test does it: if the row's digest and the wire bytes
ever disagree, these turn red.

Each equality test carries its own falsifier inline: the same assertion is
re-run against the captured bytes with ONE byte changed, and must fail. A digest
test that cannot tell a body from a nearly identical body has measured nothing.

THE RECIPE IS TRANSPORT-DEPENDENT, and that is not a hedge. AgentMail's
``send_reply`` transmits a real text/plain part, so the stored body can equal the
authored bytes. Graph's ``POST /messages/{id}/reply`` COMPOSES the stored
message -- its own HTML wrapper plus the quoted original -- so byte equality
cannot hold there by construction, for every reply the paid seat has ever sent.
The honest recipe on that transport is containment: the authored bytes appear
verbatim inside the stored body. Both halves are pinned here.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared import inbound
from tests.conftest import load_plugin

_MOD = load_plugin("hermes-smd-reply")
relay = _MOD.relay
sweeper = _MOD.sweeper
held_store = _MOD.held_store

_AGENTMAIL_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - greg@whitfield.example\n"
)
_MSGRAPH_YAML = (
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


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flip_one_byte(text: str) -> str:
    """The same body with a single character changed. The falsifier's input."""
    assert text, "cannot falsify against an empty body"
    swapped = "X" if text[0] != "X" else "Y"
    return swapped + text[1:]


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self):
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]

    def reply_sent(self) -> dict:
        return next(m for a, m in self.events() if a == "REPLY_SENT")


class _FakeGraphBroker:
    """Records exactly what Graph would have been handed."""

    def __init__(self) -> None:
        self.reply_calls: list[tuple[str, str, str]] = []

    def send_reply(self, message_id, comment, *, html="", session_id="", matter_ref=None):
        self.reply_calls.append((message_id, comment, html))
        return ""


@pytest.fixture(autouse=True)
def _clear_origin():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()


def _record_origin(message_id="msg_in"):
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin(
            sender_address="greg@whitfield.example",
            message_id=message_id,
            inbox_id="inbox_x",
        ),
    )


def _agentmail_mod(monkeypatch, tmp_path):
    """The plugin on an AgentMail seat, with the transport captured."""
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    wire: list[dict] = []

    def _fake_send(*, message_id, text, html, **_kw):
        wire.append({"message_id": message_id, "text": text, "html": html})
        return "msg_sent_1"

    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_AGENTMAIL_YAML)
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(mod.relay, "send_reply", _fake_send)
    return mod, d1, wire


def _msgraph_mod(monkeypatch, tmp_path):
    """The paid seat's shape: replies go through Graph's /reply."""
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    fake = _FakeGraphBroker()
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_MSGRAPH_YAML)
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_API_KEY", None, raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", fake.send_reply)
    return mod, d1, fake


# ---------------------------------------------------------------------------
# 1. The digest equals sha256 of the bytes the transport received
# ---------------------------------------------------------------------------


def test_authored_digest_is_sha256_of_the_bytes_agentmail_received(monkeypatch, tmp_path) -> None:
    mod, d1, wire = _agentmail_mod(monkeypatch, tmp_path)
    _record_origin()
    body = "Thanks for the intake. We have opened the matter and will confirm."
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args={"to": ["greg@whitfield.example"], "subject": "Re: New matter", "text": body},
        session_id="s1",
    )
    assert len(wire) == 1
    received = wire[0]["text"]
    meta = d1.reply_sent()
    # Recomputed from what the TRANSPORT got, not from what the test authored.
    assert meta["body_digest_authored"] == _sha256(received)
    # Falsifier: one byte different and the same assertion must fail. Without
    # this, a digest of the empty string would satisfy the line above whenever
    # the transport was handed nothing.
    assert meta["body_digest_authored"] != _sha256(_flip_one_byte(received))
    # No html was authored on this send, so no html digest is claimed. Absent,
    # not the sha256 of "" -- a reader must be able to tell those apart.
    assert "body_digest_authored_html" not in meta
    # And the body itself still never lands in the ledger.
    assert body not in json.dumps(meta)


def test_authored_digest_is_sha256_of_the_bytes_graph_received(monkeypatch, tmp_path) -> None:
    """The paid seat's path. Graph is handed a RENDERED html body (ss#2489), so
    the html digest must cover the rendered bytes -- digesting the composer's
    unrendered markdown would name something that never reached the wire."""
    _mod, d1, fake = _msgraph_mod(monkeypatch, tmp_path)
    _record_origin(message_id="graph-mid-1")
    body = "First paragraph.\n\nSecond paragraph."
    _mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": ["greg@whitfield.example"], "subject": "Re: matter", "body_text": body},
        session_id="s1",
    )
    _mid, comment, html = fake.reply_calls[0]
    meta = d1.reply_sent()
    assert meta["body_digest_authored"] == _sha256(comment)
    assert meta["body_digest_authored"] != _sha256(_flip_one_byte(comment))
    assert meta["body_digest_authored_html"] == _sha256(html)
    assert meta["body_digest_authored_html"] != _sha256(_flip_one_byte(html))
    # The html Graph received is the rendered body, not the plain text: the two
    # digests are different facts and a test that let them collapse would pass
    # against a transport that silently dropped the render.
    assert html != comment
    assert meta["body_digest_authored_html"] != meta["body_digest_authored"]


def test_a_composer_authored_html_body_is_digested_as_sent(monkeypatch, tmp_path) -> None:
    mod, d1, fake = _msgraph_mod(monkeypatch, tmp_path)
    _record_origin(message_id="graph-mid-2")
    authored_html = "<p>Confirmed. The filing went out this morning.</p>"
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "Confirmed. The filing went out this morning.",
            "html": authored_html,
        },
        session_id="s1",
    )
    _mid, _comment, html = fake.reply_calls[0]
    assert html == authored_html  # composer-authored html wins; no re-render
    assert d1.reply_sent()["body_digest_authored_html"] == _sha256(authored_html)


# ---------------------------------------------------------------------------
# 2. Containment: the recipe Graph's /reply actually permits
# ---------------------------------------------------------------------------


def test_the_authored_bytes_are_contained_in_a_graph_composed_body(monkeypatch, tmp_path) -> None:
    """Byte equality cannot hold on ``/reply`` -- Graph wraps the body and
    appends the quoted original. This simulates that composition and asserts the
    recipe the evidence packet publishes: the authored bytes appear VERBATIM
    inside the stored body, and their digest is the one on the row."""
    mod, d1, fake = _msgraph_mod(monkeypatch, tmp_path)
    _record_origin(message_id="graph-mid-3")
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={
            "to": ["greg@whitfield.example"],
            "subject": "Re: matter",
            "body_text": "We filed the response today.",
        },
        session_id="s1",
    )
    _mid, _comment, html = fake.reply_calls[0]
    # What Graph stores: its own wrapper, our body, then the quoted original.
    stored_body = (
        '<html><body><div class="elementToProof">'
        + html
        + '</div><hr id="stopSpelling"><div>From: someone</div></body></html>'
    )
    assert html in stored_body  # containment, the published recipe
    assert _sha256(stored_body) != d1.reply_sent()["body_digest_authored_html"]
    assert _sha256(html) == d1.reply_sent()["body_digest_authored_html"]


# ---------------------------------------------------------------------------
# 3. The scan digest is untouched by any of this
# ---------------------------------------------------------------------------


def test_the_scan_digest_keeps_its_name_and_its_meaning(monkeypatch, tmp_path) -> None:
    """``body_digest`` still hashes subject + text + html as ``draft_body``
    joins them. It is a different fact from the authored digest and must not
    have quietly become a synonym for it."""
    mod, d1, wire = _agentmail_mod(monkeypatch, tmp_path)
    _record_origin()
    subject, body = "Re: New matter", "We have the documents."
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args={"to": ["greg@whitfield.example"], "subject": subject, "text": body},
        session_id="s1",
    )
    meta = d1.reply_sent()
    assert meta["body_digest"] == inbound.content_digest(f"{subject}\n{body}")
    # The subject is never transmitted, which is exactly why the scan digest is
    # not checkable from the message a firm holds -- and why the two differ.
    assert meta["body_digest"] != meta["body_digest_authored"]
    assert wire[0]["text"] == body


# ---------------------------------------------------------------------------
# 4. A released reply is as checkable as a live one
# ---------------------------------------------------------------------------


def test_a_released_reply_carries_the_authored_digest(tmp_path) -> None:
    """A rate-held reply released minutes later is still a send row, so it
    carries the same checkable digest. Computed at release time from the stored
    body, through the same ``_transmitted_body`` the live path uses."""
    mod = _MOD
    store = held_store.HeldReplyStore(str(tmp_path / "held.db"))
    body = "Released after the window cleared."
    store.enqueue(
        sender="greg@whitfield.example",
        sender_class="external",
        adapter="agentmail",
        inbox_id="inbox_x",
        message_id="msg_held_1",
        send_text=body,
        send_html="",
        body_digest="scan-digest-from-the-live-turn",
        hold_reason="rate_limited_per_sender",
    )
    events: list[tuple[str, dict]] = []

    class _Limiter:
        def check(self, *_a, **_kw):
            return relay.RateDecision(True, "")

    result = sweeper.run_sweep_once(
        store=store,
        limiter=_Limiter(),
        policy=mod.send_policy.SendPolicy(
            internal_exempt=False,
            per_sender_max=3,
            per_sender_window_s=600.0,
            global_max=20,
            global_window_s=3600.0,
            backstop_max=0,
            backstop_window_s=3600.0,
            held_release_enabled=True,
            held_ttl_s=86400.0,
        ),
        send_fn=lambda _row: "sent_1",
        authored_digest_fn=mod._release_authored_digests,
        emit_fn=lambda *, action_type, metadata, **_kw: events.append((action_type, metadata)),
    )
    store.close()
    assert result.released == 1
    _action, meta = next((a, m) for a, m in events if a == "REPLY_SENT")
    assert meta["body_digest_authored"] == _sha256(body)
    assert meta["body_digest_authored"] != _sha256(_flip_one_byte(body))
    # The scan digest carried on the held row is preserved, not overwritten.
    assert meta["body_digest"] == "scan-digest-from-the-live-turn"


# ---------------------------------------------------------------------------
# 5. The helper itself
# ---------------------------------------------------------------------------


def test_an_absent_body_digests_to_nothing_not_to_the_empty_hash() -> None:
    assert relay.authored_digest("") == ""
    assert relay.authored_digests(text="", html="") == {}
    # The trap this guards: sha256("") is a real-looking 64-hex value.
    assert relay.authored_digest("x") != hashlib.sha256(b"").hexdigest()
