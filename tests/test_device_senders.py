"""Device senders: a reply to an office scanner goes to the person it names.

A seat's office scanner is a mailbox on the firm's own domain that emails scans
to the Operator. The reply lane used to answer the sender, which for a scanner
is a mailbox nobody reads: the firm got silence. ``scope.device_senders`` maps
such a device to the person who answers for it, and these tests pin that the
redirect is exactly that and nothing wider:

* the authored key validates as a closed shape (exact addresses, device on the
  roster, target on the admins, no duplicates);
* the reply lane sends to the authored person, with the lock, the floors, the
  matter gate and the rate limiter all keyed on that person;
* a draft to anybody else is still held, and a transport that cannot redirect
  holds rather than answering the device;
* held replies release to the person, and rows written before the column
  existed still release to their sender;
* the trust gate does not refuse the redirected draft on a tainted turn,
  because it is the same draft tool the lane has always used.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from textwrap import dedent

import pytest

from bootstrap.validate import validate_customer_yaml
from shared import inbound, msgraph_broker
from shared.customer_config import CustomerConfig
from shared.inbound import SESSION_TAINT, TRUST_CLASS_UNKNOWN_EXTERNAL
from shared.send_policy import SendPolicy
from tests.conftest import load_plugin

DEVICE = "scanner@firm.example"
PERSON = "office@firm.example"
OTHER = "someone@firm.example"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_BASE = dedent(
    """\
    schema_version: 1
    customer_id: acme
    customer_name: Acme Corp
    vertical: law-firm
    fly_region: iad
    model: claude-opus-4-7
    hermes_ref: v2026.5.16-smd.0
    personas:
      - slug: marcus
        status: active
        name: Marcus
        title: AI Associate
        entitlements:
          exposure:
            internal_write: autonomous
    scope:
      inbound_allow_from:
        - '@firm.example'
      admins:
        - office@firm.example
    """
)


def _validate(tmp_path: Path, device_block: str) -> list[str]:
    path = tmp_path / "customer.yaml"
    path.write_text(_BASE + device_block)
    return [e for e in validate_customer_yaml(path) if "device_senders" in e]


def test_a_mapped_device_validates(tmp_path: Path) -> None:
    block = "  device_senders:\n    - address: Scanner@Firm.example\n      replies_to: office@firm.example\n"
    assert _validate(tmp_path, block) == []


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        ("  device_senders: scanner@firm.example\n", "must be a list"),
        ("  device_senders:\n    - scanner@firm.example\n", "must be a mapping"),
        (
            "  device_senders:\n    - address: '@firm.example'\n      replies_to: office@firm.example\n",
            "exact address",
        ),
        (
            "  device_senders:\n    - address: scanner@firm.example\n      replies_to: '@firm.example'\n",
            "exact address",
        ),
        (
            "  device_senders:\n    - address: scanner@firm.example\n"
            "      replies_to: office@firm.example\n      cc: boss@firm.example\n",
            "unknown key",
        ),
        (
            "  device_senders:\n    - address: fax@elsewhere.example\n      replies_to: office@firm.example\n",
            "not covered by scope.inbound_allow_from",
        ),
        (
            "  device_senders:\n    - address: scanner@firm.example\n      replies_to: someone@firm.example\n",
            "not on scope.admins",
        ),
        (
            "  device_senders:\n    - address: scanner@firm.example\n      replies_to: office@firm.example\n"
            "    - address: SCANNER@firm.example\n      replies_to: office@firm.example\n",
            "duplicate device address",
        ),
    ],
)
def test_the_redirect_cannot_be_authored_wider(tmp_path: Path, block: str, fragment: str) -> None:
    errors = _validate(tmp_path, block)
    assert errors, "expected a device_senders error"
    assert any(fragment in e for e in errors), errors


def test_the_accessor_matches_one_device_case_insensitively() -> None:
    cfg = CustomerConfig(
        {
            "scope": {
                "admins": [PERSON],
                "device_senders": [
                    {"address": "Scanner@Firm.Example", "replies_to": "Office@firm.example"},
                    # Not an admin: dropped at runtime even if it slipped past review.
                    {"address": "copier@firm.example", "replies_to": OTHER},
                ],
            }
        }
    )
    assert cfg.device_reply_target("SCANNER@firm.example") == PERSON
    assert cfg.device_reply_target("copier@firm.example") is None
    # Exact match only: a domain is not a device.
    assert cfg.device_reply_target("other-scanner@firm.example") is None
    assert cfg.device_reply_target(None) is None
    assert CustomerConfig({"scope": {}}).device_reply_target(DEVICE) is None


# ---------------------------------------------------------------------------
# The recipient lock
# ---------------------------------------------------------------------------


def test_the_lock_admits_the_device_the_person_or_both_and_nobody_else() -> None:
    relay = load_plugin("hermes-smd-reply").relay
    assert relay.recipient_locked({"to": [DEVICE]}, DEVICE, PERSON)
    assert relay.recipient_locked({"to": [PERSON]}, DEVICE, PERSON)
    assert relay.recipient_locked({"to": [PERSON, DEVICE]}, DEVICE, PERSON)
    assert not relay.recipient_locked({"to": [PERSON, OTHER]}, DEVICE, PERSON)
    assert not relay.recipient_locked({"to": [OTHER]}, DEVICE, PERSON)
    assert not relay.recipient_locked({"to": []}, DEVICE, PERSON)
    # Unmapped: exactly the sender, as before.
    assert relay.recipient_locked({"to": [DEVICE]}, DEVICE)
    assert not relay.recipient_locked({"to": [PERSON]}, DEVICE)
    assert not relay.recipient_locked({"to": [PERSON]}, DEVICE, DEVICE)


# ---------------------------------------------------------------------------
# The reply lane
# ---------------------------------------------------------------------------

_SEAT_YAML = (
    "customer_id: acme\n"
    "vertical: law-firm\n"
    "connectors:\n"
    "  Email:\n"
    "    adapter: {adapter}\n"
    "    enabled: true\n"
    "scope:\n"
    "  inbound_allow_from:\n"
    "    - '@firm.example'\n"
    "  admins:\n"
    f"    - {PERSON}\n"
    "  device_senders:\n"
    f"    - address: {DEVICE}\n"
    f"      replies_to: {PERSON}\n"
)


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self) -> list[tuple[str, dict]]:
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


class _FakeGraphBroker:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send_reply(self, message_id, comment, *, html="", session_id="", matter_ref=None, **kw):
        self.calls.append({"message_id": message_id, "comment": comment, **kw})
        return "<sent@firm.example>"


@pytest.fixture(autouse=True)
def _clean_registers():
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    SESSION_TAINT._tainted.clear()
    yield
    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_message.clear()
    SESSION_TAINT._tainted.clear()


@pytest.fixture
def lane(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    graph = _FakeGraphBroker()
    agentmail: list[dict] = []
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(_SEAT_YAML.format(adapter="msgraph"))
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(
        mod, "_HELD_STORE", mod.held_store.HeldReplyStore(str(tmp_path / "held.db")), raising=False
    )
    monkeypatch.setattr(mod.msgraph_broker, "send_reply", graph.send_reply)
    monkeypatch.setattr(
        mod.relay, "send_reply", lambda **kw: agentmail.append(kw) or "am-1", raising=True
    )
    return mod, d1, graph, agentmail, yaml_path


def _inbound(sender: str = DEVICE, message_id: str = "graph-mid-1", session: str = "s1") -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        session,
        inbound.InboundOrigin(
            sender_address=sender, message_id=message_id, inbox_id="op@x.example"
        ),
    )


def _draft(mod, to, session: str = "s1", call: str = "c1") -> None:
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": to, "subject": "Re: scan", "body_text": "Filed the scan to the matter."},
        session_id=session,
        tool_call_id=call,
    )


@pytest.mark.parametrize("to", [[PERSON], [DEVICE], [PERSON, DEVICE]])
def test_a_scan_reply_goes_to_the_authored_person(lane, to) -> None:
    mod, d1, graph, _agentmail, _ = lane
    _inbound()
    _draft(mod, to)
    assert len(graph.calls) == 1
    assert graph.calls[0]["message_id"] == "graph-mid-1"
    assert graph.calls[0]["to"] == PERSON
    sent = [m for a, m in d1.events() if a == "REPLY_SENT"]
    assert len(sent) == 1
    assert sent[0]["recipient"] == PERSON
    assert sent[0]["device_sender"] == DEVICE
    assert sent[0]["redirected_from_device"] is True
    # The redirect is audited by address and flag only; never the body.
    assert "Filed the scan" not in json.dumps(sent[0])


def test_an_ordinary_sender_is_answered_exactly_as_before(lane) -> None:
    """Control: the same seat, a person who is not a device, no ``to`` at all."""
    mod, d1, graph, _agentmail, _ = lane
    _inbound(sender=OTHER)
    _draft(mod, [OTHER])
    assert len(graph.calls) == 1
    assert "to" not in graph.calls[0]
    sent = next(m for a, m in d1.events() if a == "REPLY_SENT")
    assert sent["recipient"] == OTHER
    assert "device_sender" not in sent


def test_a_draft_to_anybody_else_is_still_held(lane) -> None:
    mod, d1, graph, _agentmail, _ = lane
    _inbound()
    _draft(mod, [OTHER])
    assert graph.calls == []
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert held and held[0]["reason"] == "recipient_mismatch"


def test_a_transport_that_cannot_redirect_holds_instead_of_answering_the_device(lane) -> None:
    mod, d1, graph, agentmail, yaml_path = lane
    yaml_path.write_text(_SEAT_YAML.format(adapter="agentmail"))
    _inbound()
    _draft(mod, [PERSON])
    assert graph.calls == [] and agentmail == []
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert held[0]["reason"] == "device_redirect_unsupported"
    assert held[0]["recipient"] == PERSON
    assert held[0]["device_sender"] == DEVICE


def test_the_floors_classify_the_person_not_the_device(lane, monkeypatch) -> None:
    mod, _d1, _graph, _agentmail, _ = lane
    seen: list[list[str]] = []
    real = mod.classify_recipients_typed

    def _spy(recipients, *a, **k):
        seen.append(list(recipients))
        return real(recipients, *a, **k)

    monkeypatch.setattr(mod, "classify_recipients_typed", _spy)
    gated: list[set[str]] = []
    real_gate = mod.matter_gate.evaluate

    def _gate_spy(**kw):
        gated.append(set(kw["recipients"]))
        return real_gate(**kw)

    monkeypatch.setattr(mod.matter_gate, "evaluate", _gate_spy)
    _inbound()
    _draft(mod, [PERSON])
    assert seen == [[PERSON]]
    assert gated == [{PERSON}]


def test_the_rate_limit_counts_against_the_person(lane) -> None:
    mod, d1, graph, _agentmail, _ = lane
    for i in range(4):
        _inbound(message_id=f"graph-mid-{i}", session=f"s{i}")
        _draft(mod, [PERSON], session=f"s{i}", call=f"c{i}")
    # Default per-recipient cap is 3; the fourth scan's reply is rate-held,
    # and it is the PERSON's window that filled.
    assert len(graph.calls) == 3
    held = [m for a, m in d1.events() if a == "REPLY_HELD"]
    assert held[0]["reason"] == "rate_limited_per_sender"
    assert held[0]["recipient"] == PERSON


def test_a_held_redirect_is_stored_with_its_person(lane) -> None:
    mod, _d1, _graph, _agentmail, yaml_path = lane
    yaml_path.write_text(
        _SEAT_YAML.format(adapter="msgraph")
        + "send_policy:\n  reply:\n    per_sender_max: 1\n  held_release:\n    enabled: true\n"
    )
    for i in range(2):
        _inbound(message_id=f"graph-mid-{i}", session=f"s{i}")
        _draft(mod, [PERSON], session=f"s{i}", call=f"c{i}")
    rows = mod._HELD_STORE.iter_held()
    assert len(rows) == 1
    assert rows[0].sender == DEVICE
    assert rows[0].reply_to == PERSON
    assert rows[0].recipient == PERSON
    assert mod._HELD_STORE.has_pending(PERSON)
    assert not mod._HELD_STORE.has_pending(DEVICE)


def test_the_release_path_sends_to_the_stored_person(lane) -> None:
    mod, _d1, graph, _agentmail, _ = lane
    store = mod._HELD_STORE
    row_id = store.enqueue(
        sender=DEVICE,
        sender_class="internal",
        adapter="msgraph",
        inbox_id="op@x.example",
        message_id="graph-mid-9",
        send_text="Filed.",
        send_html="",
        body_digest="d",
        hold_reason="rate_limited_per_sender",
        reply_to=PERSON,
    )
    row = next(r for r in store.iter_held() if r.id == row_id)
    mod._release_send(row)
    assert graph.calls[-1]["to"] == PERSON
    assert graph.calls[-1]["message_id"] == "graph-mid-9"


# ---------------------------------------------------------------------------
# Held-reply store + sweeper
# ---------------------------------------------------------------------------

_RELEASE_ON = SendPolicy(
    internal_exempt=False,
    per_sender_max=3,
    per_sender_window_s=600.0,
    global_max=20,
    global_window_s=3600.0,
    backstop_max=0,
    backstop_window_s=3600.0,
    held_release_enabled=True,
    held_ttl_s=86400.0,
)

_OLD_SCHEMA = """
CREATE TABLE held_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  sender TEXT NOT NULL,
  sender_class TEXT NOT NULL DEFAULT '',
  adapter TEXT NOT NULL,
  inbox_id TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL,
  send_text TEXT,
  send_html TEXT,
  body_digest TEXT NOT NULL DEFAULT '',
  hold_reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'held',
  released_at REAL,
  last_error TEXT
)
"""


def _sweep(mod, store, sent: list, emitted: list):
    return mod.sweeper.run_sweep_once(
        store=store,
        limiter=mod.relay.RateLimiter(),
        policy=_RELEASE_ON,
        send_fn=lambda row: sent.append(row) or "sent-1",
        emit_fn=lambda **kw: emitted.append(kw),
        now=None,
    )


def test_a_volume_from_before_the_column_still_releases_to_its_sender(tmp_path: Path) -> None:
    """The old-row path: a seat's held_replies.db predates ``reply_to``."""
    mod = load_plugin("hermes-smd-reply")
    db = tmp_path / "held.db"
    conn = sqlite3.connect(db)
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO held_replies (created_at, sender, adapter, message_id, send_text, "
        "hold_reason) VALUES (strftime('%s','now'), ?, 'msgraph', 'mid-old', 'x', 'rate')",
        (OTHER,),
    )
    conn.commit()
    conn.close()
    store = mod.held_store.HeldReplyStore(str(db))
    (row,) = store.iter_held()
    assert row.reply_to == "" and row.recipient == OTHER
    sent: list = []
    emitted: list = []
    result = _sweep(mod, store, sent, emitted)
    assert result.released == 1
    assert emitted[0]["metadata"]["recipient"] == OTHER
    assert "device_sender" not in emitted[0]["metadata"]
    # And the migrated volume accepts a new redirect row.
    store.enqueue(
        sender=DEVICE,
        sender_class="",
        adapter="msgraph",
        inbox_id="",
        message_id="mid-new",
        send_text="y",
        send_html="",
        body_digest="",
        hold_reason="rate",
        reply_to=PERSON,
    )
    assert store.iter_held()[0].recipient == PERSON
    store.close()


def test_the_sweeper_releases_a_redirect_to_its_person(tmp_path: Path) -> None:
    mod = load_plugin("hermes-smd-reply")
    store = mod.held_store.HeldReplyStore(str(tmp_path / "held.db"))
    store.enqueue(
        sender=DEVICE,
        sender_class="internal",
        adapter="msgraph",
        inbox_id="",
        message_id="mid-1",
        send_text="Filed.",
        send_html="",
        body_digest="d",
        hold_reason="rate_limited_per_sender",
        reply_to=PERSON,
    )
    sent: list = []
    emitted: list = []
    assert _sweep(mod, store, sent, emitted).released == 1
    assert sent[0].reply_to == PERSON
    meta = emitted[0]["metadata"]
    assert meta["recipient"] == PERSON
    assert meta["device_sender"] == DEVICE
    store.close()


def test_an_ordinary_row_stores_no_reply_to(tmp_path: Path) -> None:
    mod = load_plugin("hermes-smd-reply")
    store = mod.held_store.HeldReplyStore(str(tmp_path / "held.db"))
    row_id = store.enqueue(
        sender=OTHER,
        sender_class="",
        adapter="msgraph",
        inbox_id="",
        message_id="mid-1",
        send_text="x",
        send_html="",
        body_digest="",
        hold_reason="rate",
        reply_to=OTHER,
    )
    assert store.get(row_id)["reply_to"] == ""
    store.close()


# ---------------------------------------------------------------------------
# The broker client
# ---------------------------------------------------------------------------


def test_the_broker_client_carries_to_only_when_asked(monkeypatch) -> None:
    payloads: list[dict] = []
    monkeypatch.setattr(
        msgraph_broker, "_call", lambda verb, payload, **kw: payloads.append(payload) or {}
    )
    msgraph_broker.send_reply("mid", "hello", to=PERSON)
    msgraph_broker.send_reply("mid", "hello")
    assert payloads[0]["to"] == PERSON
    assert "to" not in payloads[1]


# ---------------------------------------------------------------------------
# The trust gate: the redirected draft is the same draft tool
# ---------------------------------------------------------------------------


def test_the_trust_gate_allows_the_redirected_draft_on_a_tainted_turn(monkeypatch) -> None:
    """Reading a scan taints the turn, and a tainted turn refuses every send
    class. The redirect must therefore live in the reply lane, and the draft it
    rides on must still pass: this pins that a draft addressed to the person,
    not the device, is allowed on exactly such a turn."""
    trust = load_plugin("hermes-smd-trust")
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {
            enforce.ActionClass.INTERNAL_WRITE: enforce.Ceiling.AUTONOMOUS,
            enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS,
        },
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: ["@firm.example"])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    SESSION_TAINT.mark("s-scan", TRUST_CLASS_UNKNOWN_EXTERNAL)
    draft = enforce.evaluate_tool_call(
        "mcp_msgraph_mail_create_draft",
        {"to": [PERSON], "subject": "Re: scan", "body_text": "Filed the scan."},
        "acme",
        session_id="s-scan",
    )
    assert draft is None, draft
    # Control: the taint is live on this session, so an actual send is refused.
    send = enforce.evaluate_tool_call(
        "mcp_agentmail_send_message",
        {"to": [PERSON], "subject": "S", "text": "Filed the scan."},
        "acme",
        session_id="s-scan",
    )
    assert isinstance(send, dict) and send.get("action") == "block"
    assert "tainted" in send["message"], send
