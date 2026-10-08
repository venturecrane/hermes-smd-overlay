"""The participant fence, overlay half: which request a send answers (the
ANCHOR) and which authored lane sends it (the LANE), decided by code.

ss-console ``workspace_broker/participant_fence.py`` enforces the rule: a firm
person gets Operator mail only if they were on the request, or the firm
authored them for the sending job's lane. The broker reads the request's people
out of the mailbox; what it needs from here is which request, and that must
never be the model's choice. Each test pins one rule and names its falsifier.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from shared import inbound, rule_dispatch, send_anchor, send_dispatch
from shared.outbound_recipient import send_recipients
from shared.pending_send import PENDING_SEND
from shared.turn_sources import TURN_SOURCES
from tests.conftest import load_plugin

ROOT = Path(__file__).resolve().parents[1]
JOB = "01M4BXJ2DYY5ARPXSMG869YMCR"


def _email_turn(session: str, message_id: str, sender: str = "christa@firm.example") -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        session,
        inbound.InboundOrigin(
            sender_address=sender, message_id=message_id, inbox_id="op@firm.example"
        ),
    )
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt(session, message_id)


@pytest.fixture
def graph_seat(monkeypatch):
    monkeypatch.setattr(send_anchor, "seat_adapter", lambda: "msgraph")


# -- every dispatch site decides both, and a site that does not fails ---------------


def _dispatch_calls() -> list[tuple[str, int, set[str]]]:
    found = []
    for path in [*ROOT.glob("plugins/*/*.py"), *ROOT.glob("shared/*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
            rel = str(path.relative_to(ROOT))
            # Direct calls, plus the two modules that receive dispatch injected
            # (rule_dispatch's ``send``, casework's ``dispatch``).
            direct = name == "dispatch" and owner.endswith("send_dispatch")
            injected = (rel == "shared/rule_dispatch.py" and name == "send" and not owner) or (
                rel == "plugins/hermes-smd-escalation/casework.py"
                and name == "dispatch"
                and not owner
            )
            if direct or injected:
                found.append((rel, node.lineno, {k.arg for k in node.keywords if k.arg}))
    return found


def test_every_send_dispatch_call_site_names_its_anchor_and_lane() -> None:
    """FALSIFIER: drop ``anchor=``/``lane=`` from any site and this names it."""
    calls = _dispatch_calls()
    # establishment x2, prerendered x3, the withheld notice, rule_dispatch x3,
    # casework x2.
    assert len(calls) >= 11, calls
    missing = [(p, n) for p, n, kws in calls if not {"anchor", "lane"} <= kws]
    assert missing == []


def test_a_dispatch_without_an_anchor_or_lane_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        send_dispatch.dispatch(to=["a@x.example"], subject="s", text="t")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        send_dispatch.dispatch(to=["a@x.example"], subject="s", text="t", anchor=None)  # type: ignore[call-arg]


def test_the_rule_loop_sends_under_its_lane_and_its_anchors() -> None:
    seen: list[dict] = []

    def sender(**kwargs):
        seen.append(kwargs)
        return send_dispatch.DispatchResult(sent=True)

    anchor = {"kind": "graph_message", "graph_message_id": "REQ"}
    rule_dispatch.notify_admins(
        proposal_id="abcd1234",
        text="Short sentences.",
        requester="dana@firm.example",
        rule_requests_to=["christa@firm.example"],
        send=sender,
        anchor=anchor,
    )
    rule_dispatch.notify_outcome(
        kind="installed",
        proposal_id="abcd1234",
        text="Short sentences.",
        requester="dana@firm.example",
        send=sender,
        anchor={"kind": "rule", "proposal_id": "abcd1234"},
    )
    assert seen[0]["lane"] == "rule_dispatch" and seen[0]["anchor"] == anchor
    assert seen[1]["lane"] is None and seen[1]["anchor"] == {
        "kind": "rule",
        "proposal_id": "abcd1234",
    }


# -- the anchor is this turn's own, never the sticky origin ----------------------------


def test_the_anchor_is_this_turns_email(graph_seat) -> None:
    _email_turn("s-email", "AAMkREQ")
    assert send_anchor.turn_anchor("s-email") == {
        "kind": "graph_message",
        "graph_message_id": "AAMkREQ",
    }


def test_a_later_turn_of_the_session_has_no_anchor(graph_seat) -> None:
    """The sticky origin still names the email; this turn did not open it.

    FALSIFIER: anchor on ``SESSION_INBOUND_ORIGIN.get`` and this returns REQ."""
    _email_turn("s-later", "AAMkREQ")
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt("s-later", "")
    assert inbound.SESSION_INBOUND_ORIGIN.get("s-later") is not None
    assert send_anchor.turn_anchor("s-later") is None


def test_a_session_handed_two_emails_anchors_on_neither(graph_seat) -> None:
    _email_turn("s-two", "AAMkONE")
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt("s-two", "AAMkTWO")
    assert send_anchor.turn_anchor("s-two") is None


def test_an_agentmail_seat_anchors_on_the_agentmail_message(monkeypatch) -> None:
    monkeypatch.setattr(send_anchor, "seat_adapter", lambda: "agentmail")
    _email_turn("s-am", "<abc@agentmail.to>")
    assert send_anchor.turn_anchor("s-am") == {
        "kind": "agentmail_message",
        "message_id": "<abc@agentmail.to>",
    }


def test_a_job_wake_anchors_on_its_own_job() -> None:
    TURN_SOURCES.note("s-wake", "webhook:handoff", f"Held: for chronology job {JOB}.")
    assert send_anchor.turn_anchor("s-wake") == {"kind": "medchron_job", "job_id": JOB}
    # A wake the model writes into a cron turn is not a wake.
    TURN_SOURCES.note("s-not", "webhook:agentmail", f"for chronology job {JOB}.")
    assert send_anchor.turn_anchor("s-not") is None


@pytest.mark.parametrize(
    ("word", "kind"),
    [
        ("demand", "demand_job"),
        ("drafting", "drafting_job"),
        ("chronology", "medchron_job"),
        ("litigation", "litigation_job"),
    ],
)
def test_every_job_lane_wake_anchors_on_its_job(word: str, kind: str) -> None:
    """Every job lane, the litigation status lane (#431) included: the broker
    resolves the job's request email and requires its From to be the job's
    requester. FALSIFIER: drop a kind from shared.turn_sources and its wake has
    no anchor."""
    TURN_SOURCES.note(f"s-{word}", "webhook:handoff", f"Done: for {word} job {JOB}.")
    assert send_anchor.turn_anchor(f"s-{word}") == {"kind": kind, "job_id": JOB}


def test_the_address_keyed_recovery_is_gone() -> None:
    assert not hasattr(inbound.SESSION_INBOUND_ORIGIN, "find_for_recipient")


# -- the gate classifies cc and bcc ---------------------------------------------------


def test_the_send_gate_classifies_cc_and_bcc() -> None:
    """FALSIFIER: classify ``to`` alone and the attorney on bcc is unseen."""
    args = {
        "to": ["Christa <christa@firm.example>"],
        "cc": ["dana@firm.example"],
        "bcc": "craig@firm.example",
    }
    assert send_recipients("smd_send_message", args, "s1") == {
        "christa@firm.example",
        "dana@firm.example",
        "craig@firm.example",
    }


# -- the model's send tool ----------------------------------------------------------------


@pytest.fixture
def trust(monkeypatch, graph_seat):
    mod = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(mod, "_authored_email_adapter", lambda: "msgraph")
    monkeypatch.setattr(mod, "_seat_email_adapter", lambda: "msgraph")
    PENDING_SEND.clear()
    yield mod
    PENDING_SEND.clear()


def test_the_send_tool_takes_its_anchor_from_the_turn_and_never_from_args(
    trust, monkeypatch
) -> None:
    seen: dict = {}

    def fake(payload, **kwargs):
        seen.update(kwargs)
        return "<sent@x>"

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", fake)
    _email_turn("s-tool", "AAMkREQ")
    out = trust._smd_send_message(
        {
            "to": ["christa@firm.example"],
            "subject": "s",
            "text": "t",
            "anchor": {"kind": "graph_message", "graph_message_id": "FORGED"},
            "lane": "escalation",
        },
        session_id="s-tool",
    )
    assert out.startswith("Sent")
    assert seen["anchor"] == {"kind": "graph_message", "graph_message_id": "AAMkREQ"}
    assert "lane" not in seen


def test_a_participant_refusal_reaches_the_model_with_its_decision(trust, monkeypatch) -> None:
    def refuse(_payload, **_kw):
        raise trust.outbound_send.MsGraphSendError(
            "broker refused the send: participant fence: 1 firm recipient(s) were not on the request"
        )

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", refuse)
    _email_turn("s-refused", "AAMkREQ")
    out = trust._smd_send_message(
        {"to": ["craig@firm.example"], "subject": "s", "text": "t"}, session_id="s-refused"
    )
    assert out.startswith("Not sent")
    assert send_anchor.FENCE_DECISION in out


def test_an_ordinary_refusal_carries_no_fence_decision(trust, monkeypatch) -> None:
    def refuse(_payload, **_kw):
        raise trust.outbound_send.MsGraphSendError("broker refused the send: not on the roster")

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", refuse)
    out = trust._smd_send_message({"to": ["x@firm.example"], "subject": "s", "text": "t"})
    assert send_anchor.FENCE_DECISION not in out


def test_a_held_send_replays_under_the_anchor_it_was_held_with(trust, monkeypatch) -> None:
    """The approval turn is another turn; its email vouches for nobody here.

    FALSIFIER: replay under ``turn_anchor`` and the approver's email is sent."""
    seen: dict = {}

    def fake(payload, **kwargs):
        seen.update(kwargs)
        return "<sent@x>"

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", fake)
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust, "_scan_approved_send", lambda *a, **k: None)
    held = {"kind": "graph_message", "graph_message_id": "AAMkHELD"}
    PENDING_SEND.capture(
        "smd_send_message",
        {"to": ["dana@firm.example"], "text": "t"},
        {"dana@firm.example"},
        anchor=held,
    )
    PENDING_SEND.mark_approved("telegram:1")
    _email_turn("s-approval", "AAMkAPPROVAL")
    out = trust._dispatch_approved_send("s-approval", "firm")
    assert out is not None and "Dispatched" in out
    assert seen["anchor"] == held


def test_the_tool_replay_uses_the_noted_held_anchor_once(trust, monkeypatch) -> None:
    seen: list[dict] = []

    def fake(payload, **kwargs):
        seen.append(kwargs)
        return "<sent@x>"

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", fake)
    held = {"kind": "graph_message", "graph_message_id": "AAMkHELD"}
    send_anchor.note_replay(held, None, {"dana@firm.example"})
    _email_turn("s-re", "AAMkAPPROVAL")
    trust._smd_send_message({"to": ["dana@firm.example"], "text": "t"}, session_id="s-re")
    trust._smd_send_message({"to": ["dana@firm.example"], "text": "t"}, session_id="s-re")
    assert seen[0]["anchor"] == held
    assert seen[1]["anchor"] == {"kind": "graph_message", "graph_message_id": "AAMkAPPROVAL"}


def test_an_out_of_turn_dispatch_forwards_its_own_anchor_and_lane(trust, monkeypatch) -> None:
    seen: dict = {}

    def fake(payload, **kwargs):
        seen.update(kwargs)
        return "<sent@x>"

    monkeypatch.setattr(trust.outbound_send, "send_via_msgraph", fake)
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    result = trust._dispatch_internal_message(
        to=["paula@firm.example"], subject="s", text="t", anchor=None, lane="escalation"
    )
    assert result.sent is True
    assert seen.get("lane") == "escalation" and "anchor" not in seen


# -- routines ride their authored lane ------------------------------------------------------


class _Cfg:
    def __init__(self, personas):
        self.personas = personas


def test_a_routine_with_its_own_recipient_rides_its_skill_lane(monkeypatch) -> None:
    from shared import customer_config

    personas = [
        {
            "skills": [
                {
                    "name": "statute-watch",
                    "enabled": True,
                    "settings": {"recipient": "christa@firm.example"},
                },
                {
                    "name": "retired",
                    "enabled": False,
                    "settings": {"recipient": "craig@firm.example"},
                },
            ]
        }
    ]
    monkeypatch.setattr(
        customer_config.CustomerConfig,
        "from_volume",
        classmethod(lambda cls, path=None: _Cfg(personas)),
    )
    assert send_anchor.routine_lane("statute-watch") == "skill:statute-watch"
    assert send_anchor.routine_lane("retired") == "escalation"
    assert send_anchor.routine_lane("deadline-miss-escalator") == "escalation"
