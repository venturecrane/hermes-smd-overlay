"""Two one-shot sends a couples seat lost on 2026-10-19, through the REAL gate.

1. **15:34Z, a cron-created repeating invitation refused "(date)".** The email an
   invitation rides in states its own dates in words ("Friday, October 23,
   2026"), written by the calendar plugin's code from the event. In a cron turn
   nothing else was read, so the identifier gate in BLOCK mode refused the
   invitation's own start date. On a role that names its own calendar as a
   source (``identifier_gate.provenance_sources``), those dates verify for that
   one send; a date in the model's free text still does not.
2. **17:56Z, a routine's email refused "markup '**'".** A routine's final
   response reaches the person through ``shared.space_delivery``; one refusal
   and the message is gone. The text is now rendered to what a person reads
   before the scan, and the rendered text is what is sent.

The seat's role policy is the mirrored ss-console couples block
(``tests/contract/policy_inputs.json``), materialized as ``ROLES_DIR`` exactly as
the image carries it. The gate is never put in report mode here.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from shared import calendar_store, cron_space, provenance, send_dispatch, space, space_delivery
from tests import couples_fixtures as cf
from tests.conftest import load_plugin
from tests.test_calendar_tools import _couples
from tests.test_policy_matrix import INPUTS

COUPLES_POLICY = INPUTS["captain-mark"]["role_policy"]


def _date(offset: int) -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=offset)


def _roles(tmp_path, monkeypatch, policy: dict | None) -> None:
    root = tmp_path / "roles" / "couples"
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"role": "couples", "persona": "persona.md"}
    if policy is not None:
        manifest["policy"] = policy
    (root / "role.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setenv("ROLES_DIR", str(tmp_path / "roles"))


@pytest.fixture
def real_gate(monkeypatch, tmp_path):
    """The trust plugin's out-of-turn sender with its real scans; the broker
    records what would leave (tests/test_calendar_copy_scan.py's rig)."""
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    monkeypatch.setenv(calendar_store.DB_PATH_ENV, str(tmp_path / "calendar.db"))
    cf.write_seat(monkeypatch, tmp_path, _couples())
    _roles(tmp_path, monkeypatch, COUPLES_POLICY)
    calendar = load_plugin("hermes-smd-calendar")
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "couples-demo")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    wire: list[dict[str, Any]] = []

    def broker_send(body, **kwargs):
        wire.append(body)
        return f"msg-{len(wire)}"

    monkeypatch.setattr(trust.outbound_send.agentmail_broker, "send_message", broker_send)
    send_dispatch.set_sender(trust._dispatch_internal_message)
    provenance._reset_for_tests()
    yield calendar, wire
    send_dispatch.set_sender(None)
    provenance._reset_for_tests()
    space._reset_cache_for_tests()


def _cron_create(calendar, monkeypatch, args: dict[str, Any]) -> dict[str, Any]:
    """A cron turn: nothing read this session before the tool runs."""
    cf.cron_to(monkeypatch, cf.GROUP)
    return json.loads(calendar.tools.run("calendar_invite_create", args, "cron-s1"))


def _weekend(offset: int = 4) -> dict[str, Any]:
    start = _date(offset)
    return {
        "title": "Kids weekend",
        "start": start.isoformat(),
        "end": (start + datetime.timedelta(days=3)).isoformat(),
        "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=FR,SA,SU",
        "attendees": ["p1", "p2"],
    }


# ---------------------------------------------------------------- invitations


def test_a_cron_created_repeating_invitation_is_sent(real_gate, monkeypatch) -> None:
    calendar, wire = real_gate
    out = _cron_create(calendar, monkeypatch, _weekend())
    assert out["sent"] is True, out
    (body,) = wire
    # The body really does carry the date in words, i.e. the gate saw a DATE hit.
    assert _date(4).strftime("%B %-d, %Y") in body["text"]


def test_FALSIFIER_without_the_role_source_the_same_invitation_is_refused(
    real_gate, tmp_path, monkeypatch
) -> None:
    calendar, wire = real_gate
    no_sources = json.loads(json.dumps(COUPLES_POLICY))
    no_sources["identifier_gate"].pop("provenance_sources", None)
    _roles(tmp_path, monkeypatch, no_sources)
    out = _cron_create(calendar, monkeypatch, _weekend())
    assert out["sent"] is False
    assert "(date)" in out["not_sent_because"], out
    assert wire == []


def test_an_invented_date_in_the_description_is_still_refused(real_gate, monkeypatch) -> None:
    calendar, wire = real_gate
    trip = _date(40)
    out = _cron_create(
        calendar,
        monkeypatch,
        {**_weekend(), "description": f"Flights booked for {trip.strftime('%B %-d, %Y')}"},
    )
    assert out["sent"] is False
    assert "(date)" in out["not_sent_because"], out
    assert wire == []


def test_the_self_described_dates_end_with_the_send(real_gate, monkeypatch) -> None:
    calendar, wire = real_gate
    _cron_create(calendar, monkeypatch, _weekend())
    assert provenance.current_self_dates() == frozenset()


# ---------------------------------------------------------- routine emails


@pytest.fixture
def routine(real_gate, monkeypatch):
    _, wire = real_gate
    run = SimpleNamespace(recipients=(cf.A_EMAIL,), skill="couples-checkin", space_key="person:p1")
    monkeypatch.setattr(cron_space, "email_run", lambda *a, **k: run)
    space_delivery._reset_for_tests()
    yield wire
    space_delivery._reset_for_tests()


#: The refused shape: bold on line 5, a dash, then a plain sentence.
REFUSED_SHAPE = (
    "Your check-in for this week\n"
    "\n"
    "A few things to talk about together.\n"
    "\n"
    "**Weekend plans** — the farmers market is back.\n"
    "Tell me how the week went for you."
)


def test_a_routine_with_markdown_and_a_dash_is_delivered_rendered(routine) -> None:
    result = space_delivery.deliver("cron-r1", REFUSED_SHAPE, config=space.space_config())
    assert result is not None and result.sent, result
    (body,) = routine
    assert "**" not in body["text"] and "—" not in body["text"]
    assert "Weekend plans, the farmers market is back." in body["text"]


def test_FALSIFIER_the_unrendered_shape_is_refused(routine, monkeypatch) -> None:
    from shared import report_render

    monkeypatch.setattr(report_render, "render_message_plain", lambda text: text)
    result = space_delivery.deliver("cron-r2", REFUSED_SHAPE, config=space.space_config())
    assert result is not None and not result.sent
    assert "markup" in result.reason, result.reason
    assert routine == []


def test_a_routine_with_an_invented_dated_claim_is_still_refused(routine) -> None:
    text = REFUSED_SHAPE + f"\nYour flight leaves {_date(40).isoformat()}."
    result = space_delivery.deliver("cron-r3", text, config=space.space_config())
    assert result is not None and not result.sent
    assert "(date)" in result.reason, result.reason
    assert routine == []


def test_the_render_subtracts_markers_and_normalizes_dashes() -> None:
    from shared import report_render

    rendered = report_render.render_message_plain(
        "**Plans** — market, __then__ _lunch_ at `noon`; file_name_v2 stays."
    )
    assert rendered == "Plans, market, then lunch at noon; file_name_v2 stays."
