"""hermes-smd-calendar: the seat's own calendar (ss-console#3264).

The guarantees, each with a test that fails if it regresses:

* a seat that does not author the calendar skill on the roster lane is
  unchanged: no tools, no hooks;
* every invitation leaves through the seat's ONE audited out-of-turn sender
  (``shared.send_dispatch``), named on the ``skill:calendar`` lane, carrying
  the invitation as ``calendar`` and nothing else; with no sender wired, nothing
  is sent and the tool says so;
* the recipient lock: an address that is not one of the seat's people refuses
  the whole invitation before anything is stored or sent;
* UID stable, SEQUENCE only up, across create, update and cancel;
* space mode: a private event stays private, a crossing to the partner is the
  R1 hook's refusal, an unresolved conversation can do nothing;
* the trust plugin carries the invitation to the broker and strips a model's
  attempt to name one;
* an invitation a person sends the seat is kept, and a forwarded third-party
  one is not.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from shared import calendar_store, send_dispatch, space
from shared.send_dispatch import CALENDAR_ARG, DispatchResult
from tests import couples_fixtures as cf
from tests.conftest import FakePluginContext, load_plugin

SMD_ADDRESS = "scott@smd.services"

CALENDAR_PERSONA = {
    "slug": "couples",
    "skills": [{"name": "calendar", "enabled": True, "settings": {"recipients": "roster"}}],
}


def _couples(**overrides: Any) -> dict[str, Any]:
    doc = cf.couples_yaml(
        personas=[CALENDAR_PERSONA],
        # SMD's own address is on the roster, as on every seat: the roster lane
        # admits it at the broker, so only the tool's lock keeps it off an invite.
        scope={"inbound_allow_from": [cf.A_EMAIL, cf.B_EMAIL, SMD_ADDRESS]},
        connectors={
            "Email": {"adapter": "agentmail", "inbox_address": "couples-demo@agentmail.to"}
        },
    )
    doc.update(overrides)
    return doc


@pytest.fixture
def cal(monkeypatch, tmp_path):
    """The plugin, a couples seat on disk, a fresh store, and a recording sender."""
    monkeypatch.setenv(calendar_store.DB_PATH_ENV, str(tmp_path / "calendar.db"))
    cf.write_seat(monkeypatch, tmp_path, _couples())
    plugin = load_plugin("hermes-smd-calendar")
    sent: list[dict[str, Any]] = []

    def sender(**kwargs: Any) -> DispatchResult:
        sent.append(kwargs)
        return DispatchResult(
            sent=True, message_id=f"msg-{len(sent)}", recipients=tuple(kwargs["to"])
        )

    send_dispatch.set_sender(sender)
    yield plugin, sent
    send_dispatch.set_sender(None)
    space._reset_cache_for_tests()


def _call(plugin, tool: str, args: dict[str, Any], session_id: str = "s1") -> dict[str, Any]:
    return json.loads(plugin.tools.run(tool, args, session_id))


DATE_NIGHT = {
    "title": "Date night",
    "start": "2026-10-24T18:30",
    "timezone": "America/Phoenix",
}


# -- registration -------------------------------------------------------------


def test_a_calendar_seat_registers_four_tools_and_two_hooks(cal) -> None:
    plugin, _ = cal
    ctx = FakePluginContext()
    plugin.register(ctx)
    assert set(ctx.tools) == {
        "calendar_invite_create",
        "calendar_invite_update",
        "calendar_invite_cancel",
        "calendar_invite_list",
    }
    assert set(ctx.registered) == {"pre_tool_call", "pre_llm_call"}


@pytest.mark.parametrize(
    "doc",
    [
        cf.couples_yaml(),  # no calendar skill at all
        cf.couples_yaml(
            personas=[{"slug": "c", "skills": [{"name": "calendar", "enabled": True}]}]
        ),
        cf.couples_yaml(
            personas=[
                {
                    "slug": "c",
                    "skills": [
                        {"name": "calendar", "enabled": False, "settings": {"recipients": "roster"}}
                    ],
                }
            ]
        ),
        cf.couples_yaml(personas=[CALENDAR_PERSONA], connectors={"Email": {"adapter": "msgraph"}}),
    ],
    ids=["no-skill", "no-roster-lane", "disabled", "msgraph-seat"],
)
def test_a_seat_without_the_authored_calendar_is_unchanged(monkeypatch, tmp_path, doc) -> None:
    cf.write_seat(monkeypatch, tmp_path, doc)
    ctx = FakePluginContext()
    load_plugin("hermes-smd-calendar").register(ctx)
    assert ctx.tools == {} and ctx.registered == {}


def test_a_seat_with_no_customer_yaml_registers_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "absent.yaml"))
    ctx = FakePluginContext()
    load_plugin("hermes-smd-calendar").register(ctx)
    assert ctx.tools == {} and ctx.registered == {}


# -- the send path ------------------------------------------------------------


def test_create_sends_through_the_one_audited_sender_on_the_calendar_lane(cal, monkeypatch) -> None:
    plugin, sent = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    out = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    assert out["sent"] is True and out["sequence"] == 0
    (call,) = sent
    assert call["to"] == [cf.A_EMAIL]
    assert call["lane"] == "skill:calendar"
    assert call["templated"] is False, "the event's words are the model's, never a template"
    assert set(call["calendar"]) == {"method", "ics"} and call["calendar"]["method"] == "REQUEST"
    ics = call["calendar"]["ics"].replace("\r\n ", "")
    assert f"UID:{out['uid']}\r\n" in ics and "SEQUENCE:0\r\n" in ics
    assert f"mailto:{cf.A_EMAIL}" in ics and cf.B_EMAIL not in ics
    assert "ORGANIZER:mailto:couples-demo@agentmail.to\r\n" in ics
    assert call["subject"] == "Invitation: Date night"
    stored = calendar_store.STORE.get(out["uid"])
    assert stored is not None and stored.space == "person:p1" and stored.delivery == "sent:msg-1"


def test_no_module_of_the_plugin_can_reach_the_vendor_on_its_own() -> None:
    """Falsifier for "the send goes through the audited path": the plugin's
    source names no transmit function, no HTTP client and no key. The only
    agentmail_broker use is the inbox-scoped attachment READ for ingest."""
    root = Path(__file__).parent.parent / "plugins" / "hermes-smd-calendar"
    for path in root.glob("*.py"):
        source = path.read_text()
        for forbidden in (
            "send_message",
            "send_reply",
            "urllib",
            "requests",
            "http.client",
            "AGENTMAIL_API_KEY",
            "outbound_send",
        ):
            assert forbidden not in source, (path.name, forbidden)
    tools = load_plugin("hermes-smd-calendar").tools
    assert "send_dispatch.dispatch(" in inspect.getsource(tools._send)


def test_with_no_sender_wired_nothing_is_sent_and_the_tool_says_so(cal, monkeypatch) -> None:
    plugin, _ = cal
    send_dispatch.set_sender(None)
    cf.telegram_group(monkeypatch, cf.A_TG)
    out = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    assert out["sent"] is False
    assert "no send path" in out["not_sent_because"]
    assert calendar_store.STORE.get(out["uid"]).delivery.startswith("not_sent:")


def test_a_refused_send_is_reported_as_not_sent(cal, monkeypatch) -> None:
    plugin, _ = cal
    send_dispatch.set_sender(
        lambda **k: DispatchResult(
            sent=False, reason="broker refused the send: calendar refused: x"
        )
    )
    cf.telegram_group(monkeypatch, cf.A_TG)
    out = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    assert out["sent"] is False and "calendar refused" in out["not_sent_because"]
    listed = _call(plugin, "calendar_invite_list", {})["events"]
    assert listed[0]["invitation_delivered"] is False


# -- the recipient lock --------------------------------------------------------


@pytest.mark.parametrize(
    "stranger",
    [
        SMD_ADDRESS,  # on inbound_allow_from, so the broker's roster lane would admit it
        cf.B_EMAIL,  # a person's own address: on a shared seat people are named by id
        "stranger@example.com",
        "p3",
        "@example.com",
        "",
    ],
)
def test_anything_but_a_person_id_refuses_the_whole_invitation_before_the_broker(
    cal, monkeypatch, stranger
) -> None:
    """The lock is in tool code (the Brokery rehearsal lesson, 2026-10-16): a
    model-named address never reaches the sender, even one the broker would pass."""
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    with pytest.raises(RuntimeError, match="not sent to anyone"):
        plugin.tools.run(
            "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", stranger]}, "s1"
        )
    assert sent == [] and calendar_store.STORE.list() == []


def test_the_recipient_lock_maps_person_ids_to_their_authored_addresses(cal, monkeypatch) -> None:
    """Falsifier for the lock above: the same call naming only people goes, to
    exactly their authored addresses."""
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "P2"]})
    assert sent[0]["to"] == [cf.A_EMAIL, cf.B_EMAIL]
    assert SMD_ADDRESS not in sent[0]["calendar"]["ics"]


def test_a_roster_seat_without_people_invites_its_exact_roster_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(calendar_store.DB_PATH_ENV, str(tmp_path / "calendar.db"))
    doc = {
        "customer_id": "solo",
        "personas": [CALENDAR_PERSONA],
        "scope": {"inbound_allow_from": ["owner@firm.example", "@firm.example"]},
    }
    cf.write_seat(monkeypatch, tmp_path, doc)
    plugin = load_plugin("hermes-smd-calendar")
    sent: list[dict[str, Any]] = []
    send_dispatch.set_sender(
        lambda **k: sent.append(k) or DispatchResult(sent=True, message_id="m")
    )
    try:
        cf.clear_turn(monkeypatch)
        out = _call(
            plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["owner@firm.example"]}
        )
        assert out["sent"] and calendar_store.STORE.get(out["uid"]).space == ""
        assert "ORGANIZER:mailto:solo@agentmail.to" in sent[0]["calendar"]["ics"]
        with pytest.raises(RuntimeError, match="not sent to anyone"):
            plugin.tools.run(
                "calendar_invite_create", {**DATE_NIGHT, "attendees": ["other@firm.example"]}, "s1"
            )
    finally:
        send_dispatch.set_sender(None)


# -- UID and SEQUENCE ------------------------------------------------------------


def test_update_and_cancel_keep_the_uid_and_raise_the_sequence(cal, monkeypatch) -> None:
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    moved = _call(
        plugin, "calendar_invite_update", {"uid": made["uid"], "start": "2026-10-24T19:30"}
    )
    gone = _call(plugin, "calendar_invite_cancel", {"uid": made["uid"]})
    assert [made["sequence"], moved["sequence"], gone["sequence"]] == [0, 1, 2]
    assert [c["calendar"]["method"] for c in sent] == ["REQUEST", "REQUEST", "CANCEL"]
    for call in sent:
        assert f"UID:{made['uid']}\r\n" in call["calendar"]["ics"]
    assert "DTSTART;TZID=America/Phoenix:20261024T193000\r\n" in sent[1]["calendar"]["ics"]
    # A moved start keeps the event's length (one hour by default).
    assert "DTEND;TZID=America/Phoenix:20261024T203000\r\n" in sent[1]["calendar"]["ics"]
    assert sent[1]["subject"] == "Updated invitation: Date night"
    assert sent[2]["subject"] == "Canceled: Date night"
    with pytest.raises(RuntimeError, match="already canceled"):
        plugin.tools.run("calendar_invite_update", {"uid": made["uid"], "title": "x"}, "s1")


def test_the_store_refuses_a_sequence_that_goes_down(cal, monkeypatch) -> None:
    plugin, _ = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    _call(plugin, "calendar_invite_update", {"uid": made["uid"], "title": "Dinner"})
    event = calendar_store.STORE.get(made["uid"])
    event.sequence = 0
    with pytest.raises(calendar_store.CalendarStoreError, match="SEQUENCE 1"):
        calendar_store.STORE.put(event)


def test_who_is_invited_cannot_change_on_an_update(cal, monkeypatch) -> None:
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    with pytest.raises(RuntimeError, match="cannot change"):
        plugin.tools.run("calendar_invite_update", {"uid": made["uid"], "attendees": ["p2"]}, "s1")
    assert len(sent) == 1


# -- spaces ------------------------------------------------------------------------


def test_a_private_event_stays_in_its_persons_space(cal, monkeypatch) -> None:
    plugin, _ = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    assert [e["uid"] for e in _call(plugin, "calendar_invite_list", {})["events"]] == [made["uid"]]
    cf.telegram_dm(monkeypatch, cf.B_TG)
    assert _call(plugin, "calendar_invite_list", {})["events"] == []
    with pytest.raises(RuntimeError, match="no event with that uid"):
        plugin.tools.run("calendar_invite_cancel", {"uid": made["uid"]}, "s1")
    cf.telegram_group(monkeypatch, cf.B_TG)
    assert _call(plugin, "calendar_invite_list", {})["events"] == []


@pytest.mark.parametrize("attendees", [["p2"], ["p1", "p2"]])
def test_inviting_the_partner_from_a_private_chat_is_the_r1_hooks_refusal(
    cal, monkeypatch, attendees
) -> None:
    plugin, sent = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    with pytest.raises(RuntimeError, match="private conversation"):
        plugin.tools.run("calendar_invite_create", {**DATE_NIGHT, "attendees": attendees}, "s1")
    assert sent == [] and calendar_store.STORE.list() == []


def test_changing_a_shared_event_from_a_private_chat_is_refused_too(cal, monkeypatch) -> None:
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    cf.telegram_dm(monkeypatch, cf.A_TG)
    assert [e["uid"] for e in _call(plugin, "calendar_invite_list", {})["events"]] == [made["uid"]]
    with pytest.raises(RuntimeError, match="private conversation"):
        plugin.tools.run("calendar_invite_update", {"uid": made["uid"], "title": "Moved"}, "s1")
    assert len(sent) == 1


def _space_gate(monkeypatch, *, armed: bool, sees_crossing: bool) -> list[tuple]:
    """Stand in for workstream A's ``shared.space_writes`` (overlay#476): its
    pre_tool_call hook holds a crossing until "share it", so a crossing call that
    reaches the handler is one it released."""
    import sys
    import types

    seen: list[tuple] = []
    fake = types.ModuleType("shared.space_writes")
    fake.gate_armed = lambda: armed

    def crossing(tool_name, args, current, cfg):
        seen.append((tool_name, dict(args), current.key))
        return object() if sees_crossing else None

    fake.crossing = crossing
    monkeypatch.setitem(sys.modules, "shared.space_writes", fake)
    import shared

    monkeypatch.setattr(shared, "space_writes", fake, raising=False)
    return seen


def test_a_crossing_released_by_the_armed_space_gate_is_sent(cal, monkeypatch) -> None:
    plugin, sent = cal
    seen = _space_gate(monkeypatch, armed=True, sees_crossing=True)
    cf.telegram_dm(monkeypatch, cf.A_TG)
    args = {**DATE_NIGHT, "attendees": ["p1", "p2"]}
    out = _call(plugin, "calendar_invite_create", args)
    assert out["sent"] and sent[0]["to"] == [cf.A_EMAIL, cf.B_EMAIL]
    assert seen == [("calendar_invite_create", args, "person:p1")]
    assert calendar_store.STORE.get(out["uid"]).space == "shared"


@pytest.mark.parametrize(
    ("armed", "sees"), [(False, True), (True, False)], ids=["not-armed", "gate-disagrees"]
)
def test_a_crossing_the_space_gate_did_not_hold_is_refused(cal, monkeypatch, armed, sees) -> None:
    """Not armed, or armed but blind to this crossing: either way the gate did
    not hold it for the person's confirmation, so tool code refuses."""
    plugin, sent = cal
    _space_gate(monkeypatch, armed=armed, sees_crossing=sees)
    cf.telegram_dm(monkeypatch, cf.A_TG)
    with pytest.raises(RuntimeError, match="private conversation"):
        plugin.tools.run("calendar_invite_create", {**DATE_NIGHT, "attendees": ["p2"]}, "s1")
    assert sent == []


def test_calendar_reach_answers_the_way_the_tool_resolves(cal, monkeypatch) -> None:
    from shared import calendar_reach

    plugin, _ = cal
    reach = calendar_reach.invite_reach
    assert reach("calendar_invite_create", {"attendees": ["p1", "P2"]}) == {cf.A_EMAIL, cf.B_EMAIL}
    assert reach("calendar_invite_list", {}) == frozenset()
    for bad in ({"attendees": [SMD_ADDRESS]}, {"attendees": ["p9"]}, {"attendees": "p1"}):
        with pytest.raises(calendar_reach.ReachUnknown):
            reach("calendar_invite_create", bad)
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    for tool in ("calendar_invite_update", "calendar_invite_cancel"):
        assert reach(tool, {"uid": made["uid"]}) == {cf.A_EMAIL, cf.B_EMAIL}
        with pytest.raises(calendar_reach.ReachUnknown):
            reach(tool, {"uid": "nope"})


@pytest.mark.parametrize(
    "turn",
    [
        lambda m: cf.telegram_dm(m, "9999"),
        lambda m: cf.telegram_group(m, cf.A_TG, chat=cf.OTHER_GROUP),
        cf.clear_turn,
    ],
    ids=["unknown-dm", "other-group", "no-context"],
)
def test_a_conversation_whose_space_cannot_be_resolved_does_nothing(cal, monkeypatch, turn) -> None:
    plugin, sent = cal
    turn(monkeypatch)
    for tool, args in (
        ("calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]}),
        ("calendar_invite_list", {}),
    ):
        with pytest.raises(RuntimeError, match="no calendar change can be made"):
            plugin.tools.run(tool, args, "s9")
    assert sent == []


# -- the hooks ---------------------------------------------------------------------


def test_a_calendar_change_on_a_tainted_turn_is_refused(cal, monkeypatch) -> None:
    plugin, _ = cal
    monkeypatch.setattr(plugin.SESSION_TAINT, "is_tainted", lambda _s: True)
    for tool in ("calendar_invite_create", "calendar_invite_update", "calendar_invite_cancel"):
        verdict = plugin.on_pre_tool_call(tool_name=tool, args={}, session_id="s1")
        assert (
            verdict
            and verdict["action"] == "block"
            and "outside the seat's people" in verdict["message"]
        )
    assert (
        plugin.on_pre_tool_call(tool_name="calendar_invite_list", args={}, session_id="s1") is None
    )


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"path": "{db}"}),
        ("read_file", {"path": "{db}-journal"}),
        ("write_file", {"path": "{db}", "content": "x"}),
        ("search_files", {"path": "{dir}", "pattern": "uid"}),
        ("patch", {"mode": "patch", "patch": "*** Update File: {db}"}),
    ],
)
def test_the_file_tools_cannot_reach_the_calendar(cal, tool, args) -> None:
    plugin, _ = cal
    db = str(calendar_store.db_path())
    filled = {k: v.format(db=db, dir=str(Path(db).parent)) for k, v in args.items()}
    verdict = plugin.on_pre_tool_call(tool_name=tool, args=filled, session_id="s1")
    assert verdict and verdict["action"] == "block"


def test_the_file_fence_is_about_the_calendar_only(cal, tmp_path) -> None:
    """Falsifier for the fence above: an unrelated path is not this hook's business."""
    plugin, _ = cal
    other = tmp_path / "elsewhere" / "notes.md"
    assert (
        plugin.on_pre_tool_call(tool_name="read_file", args={"path": str(other)}, session_id="s1")
        is None
    )


# -- the trust plugin carries it -------------------------------------------------


def test_the_trust_sender_carries_the_invitation_to_the_broker(monkeypatch) -> None:
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "couples-demo")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    wire: list[dict[str, Any]] = []

    def broker_send(body, **kwargs):
        wire.append({"body": body, **kwargs})
        return "msg-9"

    monkeypatch.setattr(trust.outbound_send.agentmail_broker, "send_message", broker_send)
    invitation = {"method": "REQUEST", "ics": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"}
    result = trust._dispatch_internal_message(
        to=[cf.A_EMAIL],
        subject="Invitation: x",
        text="x",
        session_id="s1",
        templated=False,
        calendar=invitation,
        lane="skill:calendar",
    )
    assert result.sent
    (sent,) = wire
    assert sent["body"]["calendar"] == invitation
    assert CALENDAR_ARG not in sent["body"]
    assert sent["lane"] == "skill:calendar"


def test_a_held_send_keeps_its_invitation_for_the_approved_replay(monkeypatch) -> None:
    """The gate captures the PAYLOAD it holds, so the invitation must ride it."""
    trust = load_plugin("hermes-smd-trust")
    seen: dict[str, Any] = {}

    def hold(tool_name, args, slug, session_id=""):
        seen.update(args)
        return {"action": "block", "message": "held for approval"}

    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", hold)
    invitation = {"method": "CANCEL", "ics": "x"}
    result = trust._dispatch_internal_message(
        to=[cf.A_EMAIL], subject="s", text="t", session_id="s1", calendar=invitation
    )
    assert result.sent is False
    assert seen[CALENDAR_ARG] == invitation
    body = trust.outbound_send._send_body(dict(seen))
    assert body["calendar"] == invitation


def test_a_model_cannot_name_an_invitation_of_its_own(monkeypatch) -> None:
    trust = load_plugin("hermes-smd-trust")
    args = {"to": [cf.A_EMAIL], "text": "hi", CALENDAR_ARG: {"method": "REQUEST", "ics": "x"}}
    trust.on_pre_tool_call(tool_name="smd_send_message", args=args, session_id="s1")
    assert CALENDAR_ARG not in args


def test_a_graph_seat_refuses_an_invitation_rather_than_sending_without_it() -> None:
    trust = load_plugin("hermes-smd-trust")
    with pytest.raises(trust.outbound_send.MsGraphSendError, match="no calendar invitation"):
        trust.outbound_send.send_via_msgraph(
            {"to": [cf.A_EMAIL], "text": "x", CALENDAR_ARG: {"method": "REQUEST", "ics": "x"}},
            sender=lambda *a, **k: "never",
        )


def test_a_malformed_invitation_never_reaches_the_broker() -> None:
    trust = load_plugin("hermes-smd-trust")
    with pytest.raises(trust.outbound_send.AgentMailSendError, match="malformed"):
        trust.outbound_send._send_body({"to": ["a@b.c"], CALENDAR_ARG: {"method": "REQUEST"}})


# -- ingest ------------------------------------------------------------------------


INBOUND = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\nUID:g-77@google.com\r\n"
    "SEQUENCE:{seq}\r\nDTSTART:20261101T170000Z\r\nDTEND:20261101T180000Z\r\nSUMMARY:Brunch\r\n"
    "ORGANIZER:mailto:{organizer}\r\nATTENDEE:mailto:couples-demo@agentmail.to\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _mail(monkeypatch, plugin, sender: str, ics: str, session: str = "mail-1") -> None:
    cf.email_turn(monkeypatch, session, sender)
    from shared.inbound import SESSION_INBOUND_ORIGIN

    monkeypatch.setattr(
        SESSION_INBOUND_ORIGIN, "bound_this_turn", lambda s: SESSION_INBOUND_ORIGIN.get(s)
    )
    broker = plugin.ingest.agentmail_broker
    monkeypatch.setattr(
        broker,
        "list_attachments",
        lambda _m: [
            {
                "attachment_id": "a1",
                "filename": "invite.ics",
                "content_type": "text/calendar; method=REQUEST",
            }
        ],
    )
    monkeypatch.setattr(broker, "read_calendar_attachment", lambda _m, _a: ics)


def test_an_invitation_a_person_sends_is_kept_in_their_space(cal, monkeypatch) -> None:
    plugin, sent = cal
    _mail(
        monkeypatch,
        plugin,
        cf.A_EMAIL,
        INBOUND.format(method="REQUEST", seq=0, organizer=cf.A_EMAIL),
    )
    note = plugin.on_pre_llm_call(session_id="mail-1")
    assert note and "Brunch" in note["context"]
    kept = calendar_store.STORE.get("g-77@google.com")
    assert kept.origin == "inbound" and kept.space == "person:p1" and kept.organizer == cf.A_EMAIL
    assert sent == [], "keeping an invitation answers nobody"
    cf.telegram_dm(monkeypatch, cf.B_TG)
    assert _call(plugin, "calendar_invite_list", {})["events"] == []
    cf.telegram_dm(monkeypatch, cf.A_TG)
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["came_from"] == f"an invitation from {cf.A_EMAIL}"
    with pytest.raises(RuntimeError, match="only its organizer"):
        plugin.tools.run("calendar_invite_cancel", {"uid": "g-77@google.com"}, "s1")


def test_a_later_cancel_from_the_same_person_cancels_it(cal, monkeypatch) -> None:
    plugin, _ = cal
    _mail(
        monkeypatch,
        plugin,
        cf.A_EMAIL,
        INBOUND.format(method="REQUEST", seq=0, organizer=cf.A_EMAIL),
    )
    plugin.on_pre_llm_call(session_id="mail-1")
    _mail(
        monkeypatch,
        plugin,
        cf.A_EMAIL,
        INBOUND.format(method="CANCEL", seq=1, organizer=cf.A_EMAIL),
        "mail-2",
    )
    plugin.on_pre_llm_call(session_id="mail-2")
    assert calendar_store.STORE.get("g-77@google.com").status == "CANCELLED"


@pytest.mark.parametrize(
    ("sender", "organizer"),
    [
        (cf.A_EMAIL, "dentist@clinic.example"),  # forwarded third-party invite
        ("stranger@example.com", "stranger@example.com"),  # not one of the people
        (cf.B_EMAIL, cf.A_EMAIL),  # one person forwarding the other's
    ],
)
def test_an_invitation_that_is_not_the_senders_own_is_not_kept(
    cal, monkeypatch, sender, organizer
) -> None:
    plugin, _ = cal
    _mail(monkeypatch, plugin, sender, INBOUND.format(method="REQUEST", seq=0, organizer=organizer))
    plugin.on_pre_llm_call(session_id="mail-1")
    assert calendar_store.STORE.list() == []


def test_an_inbound_part_never_rewrites_an_event_this_seat_sent(cal, monkeypatch) -> None:
    plugin, _ = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    forged = INBOUND.format(method="CANCEL", seq=9, organizer=cf.A_EMAIL).replace(
        "g-77@google.com", made["uid"]
    )
    _mail(monkeypatch, plugin, cf.A_EMAIL, forged)
    plugin.on_pre_llm_call(session_id="mail-1")
    assert calendar_store.STORE.get(made["uid"]).status == "CONFIRMED"


def test_ingest_never_raises_out_of_the_hook(cal, monkeypatch) -> None:
    plugin, _ = cal
    _mail(monkeypatch, plugin, cf.A_EMAIL, "x")

    def boom(_m):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(plugin.ingest.agentmail_broker, "list_attachments", boom)
    assert plugin.on_pre_llm_call(session_id="mail-1") is None
