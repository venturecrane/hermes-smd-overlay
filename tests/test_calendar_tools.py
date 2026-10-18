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


@pytest.fixture(autouse=True)
def _fresh_calendar_inbox():
    """The consumed-email register is process-wide; no test inherits a mark."""
    from shared import calendar_inbox

    calendar_inbox._reset_for_tests()
    yield
    calendar_inbox._reset_for_tests()


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
    assert set(ctx.registered) == {"pre_tool_call", "pre_gateway_dispatch"}


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
    fake.take_release = lambda tool_name, principal: True
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


def _through_the_real_gate(plugin, gate, tool: str, args: dict[str, Any]) -> dict | None:
    """Run the call the way the gateway does: the space gate's pre_tool_call
    first, then (when it does not block) the handler, on the args it left."""
    blocked = gate.on_pre_tool_call(tool_name=tool, args=args, session_id="s")
    if blocked:
        return blocked
    return {"ran": _call(plugin, tool, args, "s")}


@pytest.fixture
def real_gate(monkeypatch):
    from shared import space_writes

    space_writes.SPACE_WRITES.clear()
    space_writes._releases.clear()
    # The gate posts the exact words through the Bot API; record instead.
    monkeypatch.setattr(space_writes, "post_notice", lambda chat, text: None)
    gate = load_plugin("hermes-smd-space-gate")
    gate.register(FakePluginContext())  # arms space_writes
    yield gate
    space_writes.SPACE_WRITES.clear()
    monkeypatch.setattr(space_writes, "_ARMED", False)


@pytest.mark.parametrize("tool", ["create", "update", "cancel"])
def test_the_real_space_gate_holds_an_invite_to_the_partner_until_share_it(
    cal, real_gate, monkeypatch, tool
) -> None:
    """End to end with workstream A's gate (overlay#476): from Ana's DM, an
    invitation that reaches Ben, including an update or cancel of a shared event
    whose arguments name only its uid, is held; nothing is sent; Ana's "share it"
    in her DM releases exactly that call, and only then does it send."""
    plugin, sent = cal
    if tool == "create":
        args: dict[str, Any] = {**DATE_NIGHT, "attendees": ["p1", "p2"]}
    else:
        cf.telegram_group(monkeypatch, cf.A_TG)
        made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
        sent.clear()
        args = {"uid": made["uid"], **({"title": "Moved"} if tool == "update" else {})}
    name = f"calendar_invite_{tool}"
    cf.telegram_dm(monkeypatch, cf.A_TG)
    held = _through_the_real_gate(plugin, real_gate, name, dict(args))
    assert held and held["action"] == "block" and held["message"].startswith("HELD")
    assert sent == []
    # Only Ana, in her own DM, releases it: Ben's "share it" in his DM, and
    # Ana's in the group, release nothing and the call stays held.
    for elsewhere, user in (
        (lambda: cf.telegram_dm(monkeypatch, cf.B_TG), cf.B_TG),
        (lambda: cf.telegram_group(monkeypatch, cf.A_TG), cf.A_TG),
    ):
        elsewhere()
        assert (
            real_gate.on_pre_llm_call(
                session_id="s",
                turn_id=f"x{user}",
                user_message="share it",
                platform="telegram",
                sender_id=user,
            )
            is None
        )
    cf.telegram_dm(monkeypatch, cf.A_TG)
    still = _through_the_real_gate(plugin, real_gate, name, dict(args))
    assert still and still.get("action") == "block" and sent == []
    monkeypatch.setenv("HERMES_SESSION_ID", "s")
    released = real_gate.on_pre_llm_call(
        session_id="s",
        turn_id="t2",
        user_message="share it",
        platform="telegram",
        sender_id=cf.A_TG,
    )
    assert released and "confirmed" in released["context"]
    out = _through_the_real_gate(plugin, real_gate, name, dict(args))
    assert out and out["ran"]["sent"] is True
    assert sent[0]["to"] == [cf.A_EMAIL, cf.B_EMAIL]
    # One confirmation, one send: the same call again is held, and a handler
    # reached without the gate's release refuses.
    sent.clear()
    again = _through_the_real_gate(plugin, real_gate, name, dict(args))
    assert again and again["message"].startswith("HELD") and sent == []


def test_a_crossing_that_skips_the_gate_is_refused_even_when_armed(
    cal, real_gate, monkeypatch
) -> None:
    """The gate is armed and sees the crossing, but it never released this call
    (no "share it", so no release noted): the handler's backstop refuses.
    FALSIFIER: drop the take_release requirement in seat._released_by_the_space_gate."""
    plugin, sent = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    with pytest.raises(RuntimeError, match="private conversation"):
        plugin.tools.run("calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]}, "s")
    assert sent == []


def test_the_real_space_gate_lets_a_private_invite_through_unheld(
    cal, real_gate, monkeypatch
) -> None:
    """Falsifier for the hold above: an invite only to the requester is not a crossing."""
    plugin, sent = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    out = _through_the_real_gate(
        plugin, real_gate, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]}
    )
    assert out and out["ran"]["sent"] is True and sent[0]["to"] == [cf.A_EMAIL]


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


# -- ingest (pre_gateway_dispatch) -------------------------------------------------


INBOUND = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:{method}\r\nBEGIN:VEVENT\r\nUID:g-77@google.com\r\n"
    "SEQUENCE:{seq}\r\nDTSTART:20261101T170000Z\r\nDTEND:20261101T180000Z\r\nSUMMARY:Brunch\r\n"
    "ORGANIZER:mailto:{organizer}\r\nATTENDEE:mailto:couples-demo@agentmail.to\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)

#: What Google and Apple send the organizer when an invitee answers.
REPLY = (
    "BEGIN:VCALENDAR\r\nPRODID:-//Google Inc//Google Calendar 70.9054//EN\r\nVERSION:2.0\r\n"
    "METHOD:REPLY\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nSEQUENCE:0\r\nDTSTAMP:20261018T010000Z\r\n"
    "DTSTART;TZID=America/Phoenix:20261024T183000\r\nSUMMARY:Date night\r\n"
    "ORGANIZER:mailto:couples-demo@agentmail.to\r\n"
    "ATTENDEE;CN=Ana;PARTSTAT={partstat}:mailto:{attendee}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _payload(sender: str, message_id: str = "msg-in-1") -> dict[str, Any]:
    return {
        "event_type": "message.received",
        "message": {
            "from": f"Someone <{sender}>",
            # Authenticated: a calendar part from an unauthenticated From files
            # nothing (shared/sender_auth.py); that case has its own test.
            "authentication_results": {
                "dmarc": "pass",
                "dmarc_domain": sender.rsplit("@", 1)[-1].lower(),
            },
            "message_id": message_id,
            "inbox_id": "couples-demo@agentmail.to",
            "subject": "Accepted: Date night",
            "text": "Ana has accepted this invitation.",
        },
    }


def _attach(monkeypatch, plugin, *texts: str) -> None:
    broker = plugin.ingest.agentmail_broker
    parts = [
        {"attachment_id": f"a{i}", "filename": "invite.ics", "content_type": "text/calendar"}
        for i in range(len(texts))
    ]
    monkeypatch.setattr(broker, "list_attachments", lambda _m: parts)
    monkeypatch.setattr(broker, "read_calendar_attachment", lambda _m, a: texts[int(a[1:])])


def _dispatch(plugin, sender: str, message_id: str = "msg-in-1") -> dict | None:
    return plugin.on_pre_gateway_dispatch(payload=_payload(sender, message_id))


@pytest.fixture
def audited(cal, monkeypatch):
    plugin, _ = cal
    rows: list[tuple] = []

    class Client:
        def execute(self, sql, *params):
            rows.append(params)

    monkeypatch.setattr(plugin, "_AUDIT", Client())
    return rows


def _row_metadata(row: tuple) -> dict:
    for value in row:
        if isinstance(value, str) and value.startswith("{") and "calendar_reply" in value:
            return json.loads(value)
    raise AssertionError(f"no calendar_reply metadata in {row}")


@pytest.mark.parametrize("partstat", ["ACCEPTED", "DECLINED", "TENTATIVE"])
def test_a_persons_response_updates_their_status_and_wakes_no_turn(
    cal, audited, monkeypatch, partstat
) -> None:
    plugin, sent = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    sent.clear()
    _attach(
        monkeypatch, plugin, REPLY.format(uid=made["uid"], partstat=partstat, attendee=cf.A_EMAIL)
    )
    verdict = _dispatch(plugin, cf.A_EMAIL)
    assert verdict == {"action": "skip", "reason": "calendar_reply"}, "no agent turn"
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["responses"] == {cf.A_EMAIL: partstat, cf.B_EMAIL: "no answer yet"}
    assert row["sequence"] == 0, "a response is not a change to the event"
    assert sent == [], "nothing answers a calendar robot"
    (audit,) = audited
    assert _row_metadata(audit)["replies"] == [f"recorded:{partstat}"]
    assert "WEBHOOK_SUPPRESSED" in audit


def test_a_forged_from_files_no_response_and_the_email_goes_on(cal, audited, monkeypatch) -> None:
    """An RSVP whose From is not authenticated is only a claim to be that
    attendee (shared/sender_auth.py): it marks nobody, and the email is not
    swallowed, so it reaches the router as ordinary (external) mail.

    FALSIFIER: delete the sender_auth check in ingest.process and the status
    flips to ACCEPTED and the dispatch is skipped."""
    plugin, _ = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1", "p2"]})
    _attach(
        monkeypatch, plugin, REPLY.format(uid=made["uid"], partstat="ACCEPTED", attendee=cf.A_EMAIL)
    )

    def unreadable(_message_id):
        raise plugin.ingest.agentmail_broker.AgentMailReadError("no key in tests")

    monkeypatch.setattr(plugin.ingest.agentmail_broker, "message_authentication", unreadable)
    payload = _payload(cf.A_EMAIL)
    payload["message"].pop("authentication_results")
    assert plugin.on_pre_gateway_dispatch(payload=payload) is None
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["responses"][cf.A_EMAIL] == "no answer yet"
    assert audited == []


def test_the_router_leaves_a_consumed_response_alone(cal, audited, monkeypatch) -> None:
    """Hermes runs every pre_gateway_dispatch callback before reading results, so
    the router still sees the email; it must write no origin, card or row."""
    plugin, _ = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    _attach(
        monkeypatch, plugin, REPLY.format(uid=made["uid"], partstat="ACCEPTED", attendee=cf.A_EMAIL)
    )
    router = load_plugin("hermes-smd-webhook-router")
    monkeypatch.setattr(
        router.router,
        "build_routing_table",
        lambda _p: (_ for _ in ()).throw(AssertionError("the router routed a consumed email")),
    )
    assert _dispatch(plugin, cf.A_EMAIL)["action"] == "skip"
    assert router.on_pre_gateway_dispatch(payload=_payload(cf.A_EMAIL)) is None
    # One-shot: the same id is not swallowed twice.
    from shared import calendar_inbox

    assert calendar_inbox.take("msg-in-1") is False


@pytest.mark.parametrize(
    ("who", "uid", "attendee", "why"),
    [
        (cf.A_EMAIL, "unknown@elsewhere", cf.A_EMAIL, "not an event this seat organized"),
        ("stranger@example.com", None, "stranger@example.com", "not from one of the seat's people"),
        (SMD_ADDRESS, None, SMD_ADDRESS, "not from one of the seat's people"),
        (cf.B_EMAIL, None, cf.A_EMAIL, "no response from its sender"),  # Ben answering as Ana
        (cf.B_EMAIL, None, cf.B_EMAIL, "was not invited"),
    ],
)
def test_any_other_response_is_dropped_from_the_turn_and_changes_nothing(
    cal, audited, monkeypatch, who, uid, attendee, why
) -> None:
    plugin, _ = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    _attach(
        monkeypatch,
        plugin,
        REPLY.format(uid=uid or made["uid"], partstat="ACCEPTED", attendee=attendee),
    )
    assert _dispatch(plugin, who) == {"action": "skip", "reason": "calendar_reply"}
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["responses"] == {cf.A_EMAIL: "no answer yet"}
    (reply,) = _row_metadata(audited[0])["replies"]
    assert reply.startswith("dropped:") and why in reply


def test_an_email_with_no_calendar_part_dispatches_as_before(cal, audited, monkeypatch) -> None:
    """Falsifier for the skip: an ordinary email from a person wakes its turn."""
    plugin, _ = cal
    _attach(monkeypatch, plugin)
    assert _dispatch(plugin, cf.A_EMAIL) is None
    assert audited == []


def test_an_invitation_a_person_sends_is_kept_in_their_space_and_the_turn_goes_on(
    cal, audited, monkeypatch
) -> None:
    plugin, sent = cal
    _attach(monkeypatch, plugin, INBOUND.format(method="REQUEST", seq=0, organizer=cf.A_EMAIL))
    assert _dispatch(plugin, cf.A_EMAIL) is None, (
        "an invitation is not a response; the email is read"
    )
    kept = calendar_store.STORE.get("g-77@google.com")
    assert kept.origin == "inbound" and kept.space == "person:p1" and kept.organizer == cf.A_EMAIL
    assert sent == [] and audited == []
    cf.telegram_dm(monkeypatch, cf.B_TG)
    assert _call(plugin, "calendar_invite_list", {})["events"] == []
    cf.telegram_dm(monkeypatch, cf.A_TG)
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["came_from"] == f"an invitation from {cf.A_EMAIL}"
    with pytest.raises(RuntimeError, match="only its organizer"):
        plugin.tools.run("calendar_invite_cancel", {"uid": "g-77@google.com"}, "s1")


def test_a_later_cancel_from_the_same_person_cancels_it(cal, monkeypatch) -> None:
    plugin, _ = cal
    _attach(monkeypatch, plugin, INBOUND.format(method="REQUEST", seq=0, organizer=cf.A_EMAIL))
    _dispatch(plugin, cf.A_EMAIL)
    _attach(monkeypatch, plugin, INBOUND.format(method="CANCEL", seq=1, organizer=cf.A_EMAIL))
    _dispatch(plugin, cf.A_EMAIL, "msg-in-2")
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
    _attach(monkeypatch, plugin, INBOUND.format(method="REQUEST", seq=0, organizer=organizer))
    assert _dispatch(plugin, sender) is None
    assert calendar_store.STORE.list() == []


def test_an_inbound_part_never_rewrites_an_event_this_seat_sent(cal, monkeypatch) -> None:
    plugin, _ = cal
    cf.telegram_dm(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    forged = INBOUND.format(method="CANCEL", seq=9, organizer=cf.A_EMAIL).replace(
        "g-77@google.com", made["uid"]
    )
    _attach(monkeypatch, plugin, forged)
    _dispatch(plugin, cf.A_EMAIL)
    assert calendar_store.STORE.get(made["uid"]).status == "CONFIRMED"


def test_ingest_never_raises_out_of_the_hook(cal, monkeypatch) -> None:
    plugin, _ = cal

    def boom(_m):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(plugin.ingest.agentmail_broker, "list_attachments", boom)
    assert _dispatch(plugin, cf.A_EMAIL) is None


# -- seats without the calendar (A&P and every other seat today) -------------------


def test_a_seat_without_the_calendar_never_reads_or_swallows_inbound_mail(
    monkeypatch, tmp_path
) -> None:
    """The A&P shape: no calendar skill. register() returns before wiring any
    hook (plugins/hermes-smd-calendar/__init__.py register), so Hermes never
    calls the calendar's pre_gateway_dispatch, the vendor is never asked for
    attachments, and the router's consumed-check (the only calendar code on the
    router path) answers False for every email, so it routes as before."""
    cf.write_seat(
        monkeypatch,
        tmp_path,
        {
            "customer_id": "firm",
            "scope": {"inbound_allow_from": ["@firm.example"]},
            "personas": [{"slug": "ops", "skills": [{"name": "deadline-watch", "enabled": True}]}],
        },
    )
    plugin = load_plugin("hermes-smd-calendar")
    ctx = FakePluginContext()
    plugin.register(ctx)
    assert ctx.registered == {} and ctx.tools == {}

    def never(*_a, **_k):
        raise AssertionError("a seat without a calendar read the mailbox")

    monkeypatch.setattr(plugin.ingest.agentmail_broker, "list_attachments", never)
    router = load_plugin("hermes-smd-webhook-router")
    assert router._consumed_by_calendar(_payload("paralegal@firm.example")) is False


def test_an_answer_with_a_note_is_recorded_and_still_reaches_its_turn(cal, audited, monkeypatch):
    """Over-broad skip guard: "Accepted, but can we start at 7?" is a message."""
    plugin, _ = cal
    cf.telegram_group(monkeypatch, cf.A_TG)
    made = _call(plugin, "calendar_invite_create", {**DATE_NIGHT, "attendees": ["p1"]})
    noted = REPLY.format(uid=made["uid"], partstat="ACCEPTED", attendee=cf.A_EMAIL).replace(
        "END:VEVENT", "COMMENT:can we start at 7?\r\nEND:VEVENT"
    )
    _attach(monkeypatch, plugin, noted)
    assert _dispatch(plugin, cf.A_EMAIL) is None
    (row,) = _call(plugin, "calendar_invite_list", {})["events"]
    assert row["responses"] == {cf.A_EMAIL: "ACCEPTED"}
    assert audited == []


def test_a_stale_mark_never_swallows_an_email() -> None:
    from shared import calendar_inbox

    calendar_inbox.mark("m-old", now=100.0)
    assert calendar_inbox.take("m-old", now=100.0 + calendar_inbox.TTL_SECONDS + 1) is False
    calendar_inbox.mark("m-new", now=100.0)
    assert calendar_inbox.take("m-new", now=101.0) is True
    assert calendar_inbox.take("m-new", now=101.0) is False


@pytest.mark.parametrize("payload", [None, "not json", {"message": "x"}, {"data": 7}, []])
def test_the_routers_calendar_check_fails_open_on_any_payload(payload) -> None:
    """Fail OPEN: a payload the check cannot read is routed exactly as before."""
    router = load_plugin("hermes-smd-webhook-router")
    assert router._consumed_by_calendar(payload) is False


def test_a_broken_calendar_read_dispatches_the_email_normally(cal, monkeypatch) -> None:
    """Fail OPEN on a calendar seat too: a parse, store or config failure is
    never a skip; the email reaches its turn."""
    plugin, _ = cal
    _attach(monkeypatch, plugin, "BEGIN:VCALENDAR\r\nMETHOD:REPLY\r\n")  # truncated part
    assert _dispatch(plugin, cf.A_EMAIL) is None

    def broken(*_a, **_k):
        raise RuntimeError("broken")

    _attach(
        monkeypatch,
        plugin,
        REPLY.format(uid="u-1", partstat="ACCEPTED", attendee=cf.A_EMAIL),
    )
    monkeypatch.setattr(plugin.ingest.calendar_store.STORE, "get", broken)
    assert _dispatch(plugin, cf.A_EMAIL) is None
    monkeypatch.setattr(plugin.seat_mod, "load_seat", broken)
    assert _dispatch(plugin, cf.A_EMAIL) is None
