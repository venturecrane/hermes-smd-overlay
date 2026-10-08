"""A withheld notice inherits the code-fixed-recipients exemption, and only then.

The date-prep brief is model-composed, goes out on a turn tainted by reading the
matter's documents, and is sent only because its recipients are code-fixed
(``shared.send_dispatch.CodeFixedRecipients``). It is also the message most
likely to carry a bad clock. If its withheld notice met the taint gate without
the capability, the person would hear nothing (the 2026-08-19 failure). These
tests drive the REAL trust gate and the REAL outbound scans; only the transport
and the spec floors are stubbed, so a refusal here is the gate's own.
"""

from __future__ import annotations

import pytest

from shared import inbound, output_checklist, provenance, send_dispatch, spec_gate
from shared.inbound import SESSION_TAINT, TRUST_CLASS_UNKNOWN_EXTERNAL
from tests.conftest import load_plugin

trust = load_plugin("hermes-smd-trust")
enforce = trust.enforce

ATTORNEY = "atty@firm.example"
PARALEGAL = "para@firm.example"
SESSION = "cron_date_prep_brief_20260929"
BODY = "Hearing prep for Garcia.\nThe hearing is at 16:30 in Dept 31."


@pytest.fixture(autouse=True)
def seat(monkeypatch):
    monkeypatch.setattr(inbound, "SESSION_INBOUND_ORIGIN", inbound.SessionInboundOrigin())
    SESSION_TAINT._tainted.clear()
    provenance._reset_for_tests()
    monkeypatch.setenv("SMD_VERTICAL", "law-firm")
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {
            enforce.ActionClass.EXTERNAL_SEND_INTERNAL: enforce.Ceiling.AUTONOMOUS,
            enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS,
        },
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: [ATTORNEY, PARALEGAL])
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: [])
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setattr(enforce, "_resolve_active_persona", lambda: "agent-case")
    monkeypatch.setattr(spec_gate, "check_structure_floor", lambda **_k: None)
    monkeypatch.setattr(spec_gate, "check_spec_gate", lambda **_k: None)
    monkeypatch.setattr(spec_gate, "_AUDIT_WIRED", True)
    monkeypatch.setattr(spec_gate, "_AUDIT_CLIENT", None)
    monkeypatch.setattr(trust, "get_secret", lambda _k: "pilot-smokeball")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    sent: list[dict] = []

    def transport(*, payload, session_id="", matter_ref=None, audit_extra=None, **_):
        sent.append({"payload": dict(payload), "audit_extra": dict(audit_extra or {})})
        return f"msg-{len(sent)}"

    monkeypatch.setattr(trust.outbound_send, "send_message", transport)
    send_dispatch.set_sender(trust._dispatch_internal_message)
    yield sent
    send_dispatch.set_sender(None)
    SESSION_TAINT._tainted.clear()


def _taint() -> None:
    SESSION_TAINT.mark(SESSION, TRUST_CLASS_UNKNOWN_EXTERNAL)


def _brief(**extra) -> send_dispatch.DispatchResult:
    return send_dispatch.dispatch(
        to=[ATTORNEY],
        cc=[PARALEGAL],
        subject="Garcia hearing prep",
        text=BODY,
        anchor=None,
        lane="escalation",
        session_id=SESSION,
        templated=False,
        **extra,
    )


def test_an_exhausted_code_fixed_brief_on_a_tainted_turn_still_tells_its_people(seat):
    _taint()
    capability = send_dispatch.CodeFixedRecipients(to=(ATTORNEY,), cc=(PARALEGAL,), source="brief")
    results = [_brief(code_fixed_recipients=capability) for _ in range(3)]
    assert [r.sent for r in results] == [False, False, False]
    assert results[0].reason.startswith("Refused:")
    assert results[2].reason.startswith("Withheld:")
    assert "notice that a message was withheld went to" in results[2].reason
    [notice] = seat
    assert notice["payload"]["to"] == [ATTORNEY]
    assert notice["payload"]["cc"] == [PARALEGAL]
    assert notice["payload"]["subject"] == "Withheld: Garcia hearing prep"
    assert "16:30" not in notice["payload"]["text"]
    assert notice["audit_extra"]["taint_exempt"] == "code_fixed_recipients"
    assert output_checklist.check(notice["payload"]["text"], output_checklist.STAFF_SEND) == []


def test_without_the_capability_a_tainted_send_gains_no_exemption(seat):
    """The same body on the same tainted turn with no capability: the brief
    itself is refused by the taint gate, and nothing opens the exemption."""
    _taint()
    results = [_brief() for _ in range(3)]
    assert not any(r.sent for r in results)
    assert seat == []


def test_an_in_turn_send_never_carries_the_capability(seat, monkeypatch):
    """The hook path has no capability to inherit: an exhausted in-turn staff
    send's notice is dispatched without one."""
    calls: list[dict] = []

    def recorder(**kwargs):
        calls.append(kwargs)
        return send_dispatch.DispatchResult(sent=True, recipients=tuple(kwargs["to"]))

    monkeypatch.setattr(send_dispatch, "_SENDER", recorder)
    args = {"to": [ATTORNEY], "subject": "Garcia", "text": BODY}
    for _ in range(3):
        trust.outbound.check_outbound_send(
            tool_name="smd_send_message", args=dict(args), session_id="s-in-turn", tool_call_id="c"
        )
    [notice] = calls
    assert "code_fixed_recipients" not in notice
