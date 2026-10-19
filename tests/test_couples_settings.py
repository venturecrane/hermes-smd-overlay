"""A couple sets its schedule and settings by talking (ss-console #3264).

The tool (``plugins/hermes-smd-couples-settings``), the document it keeps
(``shared.couples_settings``) and the quarter-hour gate that runs the routines
(``shared.couples_gate``). Either partner changes any setting alone, and it
applies at once (Captain decision 2026-10-19). Each falsifier names the line
whose removal makes the test fail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

from shared import (
    couples_gate,
    couples_settings,
    couples_slots,
    cron_space,
    inbound,
    send_dispatch,
    space,
)
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT, InboundOrigin
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin
from tests.couples_fixtures import (
    A_EMAIL,
    A_TG,
    B_TG,
    clear_turn,
    couples_yaml,
    cron_to,
    telegram_dm,
    write_seat,
)

SCHEDULED = ("couples-checkin", "gratitude-prompts", "dates-to-remember", "weekly-checkin-agenda")


def _skill(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": True,
        "initiation": {"manual": True, "scheduled": True, "webhook": False},
    }


def seat_doc(**overrides: Any) -> dict[str, Any]:
    doc = couples_yaml(**overrides)
    doc["personas"] = [{"slug": "mark", "name": "Mark", "skills": [_skill(s) for s in SCHEDULED]}]
    return doc


MAILED: list[dict[str, Any]] = []


@pytest.fixture
def seat(monkeypatch, tmp_path):
    clear_turn(monkeypatch)
    write_seat(monkeypatch, tmp_path, seat_doc())
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    monkeypatch.setenv("SMD_COUPLES_SETTINGS_PATH", str(tmp_path / "state" / "settings.json"))
    MAILED.clear()

    def sender(**kw: Any) -> DispatchResult:
        MAILED.append(kw)
        return DispatchResult(sent=True, message_id=f"m{len(MAILED)}", recipients=tuple(kw["to"]))

    send_dispatch.set_sender(sender)
    plugin = load_plugin("hermes-smd-couples-settings")
    yield {"tool": plugin, "tmp": tmp_path}
    send_dispatch.set_sender(None)
    SESSION_INBOUND_ORIGIN._reset_for_tests()
    space._reset_cache_for_tests()


def change(seat, changes: list[dict[str, Any]], sid: str = "s") -> str:
    return seat["tool"]._change_handler({"changes": changes}, session_id=sid)


def read(seat, sid: str = "s") -> dict[str, Any]:
    return json.loads(seat["tool"]._read_handler({}, session_id=sid))


def stored() -> dict[str, Any]:
    return couples_settings.load()


TZ = {"op": "set_timezone", "timezone": "America/Los_Angeles"}


def set_timezone(seat, monkeypatch) -> None:
    telegram_dm(monkeypatch, A_TG)
    assert change(seat, [TZ]).startswith("DONE")


# ---------------------------------------------------------------------------
# 1. Either partner, alone, applied at once
# ---------------------------------------------------------------------------


def test_a_persons_own_quiet_hours_apply_at_once(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    out = change(
        seat, [{"op": "set_quiet_hours", "person": "p1", "start": "21:00", "end": "08:00"}]
    )
    assert out.startswith("DONE"), out
    assert "Quiet hours for Ana: no scheduled messages from 9:00 PM to 8:00 AM" in out
    assert stored()["people"]["p1"]["quiet_hours"] == {"start": "21:00", "end": "08:00"}
    assert not MAILED


def test_the_timezone_applies_at_once_and_nobody_is_asked(seat, monkeypatch):
    """FALSIFIER: hold any change for the other partner's yes."""
    telegram_dm(monkeypatch, A_TG)
    out = change(seat, [TZ])
    assert out.startswith("DONE"), out
    assert stored()["timezone"] == "America/Los_Angeles"
    assert stored()["log"][-1]["by"] == "p1"
    assert not MAILED


def test_a_persons_own_gratitude_routine_applies_at_once(seat, monkeypatch):
    set_timezone(seat, monkeypatch)
    telegram_dm(monkeypatch, A_TG)
    add = {
        "op": "add_routine",
        "kind": "gratitude-prompts",
        "for": "p1",
        "days": ["sun"],
        "time": "09:00",
    }
    out = change(seat, [add])
    assert out.startswith("DONE"), out
    assert "gratitude prompt for Ana, Sundays at 9:00 AM (America/Los_Angeles)" in out
    (routine,) = stored()["routines"]
    assert routine["space"] == "person:p1" and routine["days"] == ["sun"]


def test_a_checkin_applies_at_once(seat, monkeypatch):
    set_timezone(seat, monkeypatch)
    telegram_dm(monkeypatch, A_TG)
    add = {
        "op": "add_routine",
        "kind": "couples-checkin",
        "for": "p1",
        "days": ["tue", "fri"],
        "time": "20:00",
    }
    out = change(seat, [add])
    assert out.startswith("DONE"), out
    assert "check-in for Ana, Tuesdays and Fridays at 8:00 PM (America/Los_Angeles)" in out
    (routine,) = stored()["routines"]
    assert routine["kind"] == "couples-checkin" and routine["space"] == "person:p1"


def test_either_partner_changes_the_other_partners_quiet_hours(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    out = change(
        seat, [{"op": "set_quiet_hours", "person": "p2", "start": "22:00", "end": "07:00"}]
    )
    assert out.startswith("DONE"), out
    assert stored()["people"]["p2"]["quiet_hours"] == {"start": "22:00", "end": "07:00"}


def test_a_partner_changes_the_others_checkin_alone(seat, monkeypatch):
    """A check-in is visible to both, so either may change it."""
    set_timezone(seat, monkeypatch)
    telegram_dm(monkeypatch, B_TG)
    add = {
        "op": "add_routine",
        "kind": "couples-checkin",
        "for": "p2",
        "days": ["mon"],
        "time": "20:00",
    }
    assert change(seat, [add]).startswith("DONE")
    rid = stored()["routines"][0]["id"]
    telegram_dm(monkeypatch, A_TG)
    out = change(seat, [{"op": "change_routine", "routine": rid, "time": "21:00"}])
    assert out.startswith("DONE"), out
    assert stored()["routines"][0]["time"] == "21:00"


def test_a_mixed_bundle_applies_together(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    mine = {"op": "set_quiet_hours", "person": "p1", "start": "21:00", "end": "08:00"}
    assert change(seat, [mine, TZ]).startswith("DONE")
    assert stored()["timezone"] == "America/Los_Angeles"
    assert stored()["people"]["p1"]["quiet_hours"] == {"start": "21:00", "end": "08:00"}


def _email(monkeypatch, sid: str, sender: str) -> None:
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    origin = InboundOrigin(
        sender_address=sender,
        message_id=f"<{sid}@mail>",
        inbox_id="couples@agentmail.to",
        conversation_id=f"thr-{sid}",
    )
    SESSION_INBOUND_ORIGIN.record("", origin)
    inbound.bind_origin_from_prompt(
        sid, f"from: x\nmessage_id: <{sid}@mail>\n{inbound.UNTRUSTED_EMAIL_DELIMITER}\nbody"
    )


def test_over_email_a_change_applies_at_once(seat, monkeypatch, tmp_path):
    """No Telegram at all (email is the default channel)."""
    clear_turn(monkeypatch)
    doc = seat_doc()
    for person in doc["people"]:
        person.update({"telegram_id": "", "channel": "email"})
    write_seat(monkeypatch, tmp_path, doc)
    _email(monkeypatch, "a", A_EMAIL)
    out = seat["tool"]._change_handler({"changes": [TZ]}, session_id="a")
    assert out.startswith("DONE"), out
    assert couples_settings.load()["timezone"] == "America/Los_Angeles"
    assert not MAILED


def test_a_scheduled_run_cannot_ask_for_a_change(seat, monkeypatch):
    """FALSIFIER: drop the ``speaker is None`` refusal."""
    cron_to(monkeypatch, A_TG)
    out = change(
        seat, [{"op": "set_quiet_hours", "person": "p1", "start": "21:00", "end": "08:00"}]
    )
    assert "refused" in out and "scheduled run" in out
    assert stored()["people"] == {}


def test_a_tainted_turn_changes_nothing(seat, monkeypatch):
    """FALSIFIER: drop the ``_tainted`` refusal."""
    telegram_dm(monkeypatch, A_TG)
    monkeypatch.setattr(SESSION_TAINT, "is_tainted", lambda sid: True)
    out = change(
        seat, [{"op": "set_quiet_hours", "person": "p1", "start": "21:00", "end": "08:00"}]
    )
    assert "refused" in out and stored()["people"] == {}


# ---------------------------------------------------------------------------
# 3. Invalid input is refused, nothing changes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "set_timezone", "timezone": "Mars/Olympus_Mons"},
        {"op": "set_timezone", "timezone": "PST"},
        {"op": "set_quiet_hours", "person": "p1", "start": "99:00", "end": "08:00"},
        {"op": "set_quiet_hours", "person": "p1", "start": "21:00", "end": "21:00"},
        {"op": "set_quiet_hours", "person": "ana@example.com", "start": "21:00", "end": "08:00"},
        {
            "op": "add_routine",
            "kind": "gratitude-prompts",
            "for": "ana@example.com",
            "days": ["sun"],
            "time": "09:00",
        },
        {
            "op": "add_routine",
            "kind": "gratitude-prompts",
            "for": "p1",
            "days": ["funday"],
            "time": "09:00",
        },
        {
            "op": "add_routine",
            "kind": "gratitude-prompts",
            "for": "p1",
            "days": ["sun"],
            "time": "09:10",
        },
        {
            "op": "add_routine",
            "kind": "gratitude-prompts",
            "for": "shared",
            "days": ["sun"],
            "time": "09:00",
        },
        {"op": "add_routine", "kind": "cronjob", "for": "p1", "days": ["sun"], "time": "09:00"},
        {
            "op": "add_routine",
            "kind": "gratitude-prompts",
            "for": "p1",
            "days": ["sun"],
            "time": "09:00",
            "deliver": "x@y.z",
        },
        {"op": "set_checkin_topics", "topics": ["a" * 61]},
        {"op": "set_checkin_topics", "topics": [f"t{i}" for i in range(9)]},
        {"op": "set_dates_lead_days", "person": "p1", "lead_days": 400},
        {"op": "set_dates_lead_days", "person": "p1", "lead_days": "7"},
        {"op": "pause_routine", "routine": "r00000000", "until": "2026-13-40"},
    ],
)
def test_invalid_input_is_refused(seat, monkeypatch, bad):
    """FALSIFIER: any one of parse_changes' checks."""
    set_timezone(seat, monkeypatch)
    before = stored()
    telegram_dm(monkeypatch, A_TG)
    out = change(seat, [bad])
    assert "refused" in out, out
    assert stored() == before


def test_a_routine_needs_the_timezone_first(seat, monkeypatch):
    telegram_dm(monkeypatch, A_TG)
    add = {
        "op": "add_routine",
        "kind": "gratitude-prompts",
        "for": "p1",
        "days": ["sun"],
        "time": "09:00",
    }
    out = change(seat, [add])
    assert "refused" in out and "timezone" in out


def test_the_seat_keeps_at_most_twenty_routines(seat, monkeypatch):
    """50 asked for, 20 kept. FALSIFIER: drop the MAX_ROUTINES check."""
    set_timezone(seat, monkeypatch)
    times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)][:50]
    added = 0
    for i, at in enumerate(times):
        telegram_dm(monkeypatch, A_TG)
        out = change(
            seat,
            [
                {
                    "op": "add_routine",
                    "kind": "gratitude-prompts",
                    "for": "p1",
                    "days": ["mon"],
                    "time": at,
                }
            ],
            sid=f"s{i}",
        )
        added += out.startswith("DONE")
    assert added == couples_settings.MAX_ROUTINES == len(stored()["routines"])


# ---------------------------------------------------------------------------
# 4. Privacy: what a space sees
# ---------------------------------------------------------------------------


def test_a_partner_does_not_see_the_other_partners_private_routines(seat, monkeypatch):
    """FALSIFIER: drop routine_visible's filter in view()."""
    set_timezone(seat, monkeypatch)
    telegram_dm(monkeypatch, B_TG)
    add = {
        "op": "add_routine",
        "kind": "dates-to-remember",
        "for": "p2",
        "days": ["mon"],
        "time": "09:00",
    }
    assert change(seat, [add]).startswith("DONE")
    rid = stored()["routines"][0]["id"]
    telegram_dm(monkeypatch, A_TG)
    assert read(seat)["routines"] == []
    telegram_dm(monkeypatch, A_TG)
    out = change(seat, [{"op": "remove_routine", "routine": rid}])
    assert "refused" in out and "no routine" in out
    telegram_dm(monkeypatch, B_TG)
    assert [r["id"] for r in read(seat)["routines"]] == [rid]


# ---------------------------------------------------------------------------
# 5. The gate: right person, right time, quiet hours, pause
# ---------------------------------------------------------------------------


def _state(**people: Any) -> dict[str, Any]:
    doc = couples_settings.empty()
    doc["timezone"] = "America/Los_Angeles"
    doc["checkin_topics"] = ["how work is going"]
    doc["people"] = people
    doc["routines"] = [
        {
            "id": "r00000001",
            "kind": "couples-checkin",
            "space": "person:p1",
            "days": ["tue", "fri"],
            "time": "20:00",
            "paused_until": None,
        },
    ]
    return doc


# Tuesday 2026-10-20 20:00 in Los Angeles (PDT, UTC-7) is 03:00 UTC on the 21st.
TUE_8PM = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)


def test_a_routine_is_due_for_exactly_one_partner(seat):
    """FALSIFIER: match routines on kind alone, not kind and space."""
    state = _state()
    due_p1, _ = couples_gate.due(state, "couples-checkin", "person:p1", ["p1", "p2"], TUE_8PM)
    due_p2, _ = couples_gate.due(state, "couples-checkin", "person:p2", ["p1", "p2"], TUE_8PM)
    assert [r["id"] for r in due_p1] == ["r00000001"] and due_p2 == []
    cfg = space.space_config()
    assert cron_space.email_recipients("person:p1", cfg) == (A_EMAIL,)


def test_the_gate_reads_the_couples_wall_clock_across_daylight_saving(seat):
    """8 PM Pacific is 03:00 UTC in October and 04:00 UTC in December."""
    state = _state()
    dec_tue_8pm = datetime(2026, 12, 2, 4, 0, tzinfo=timezone.utc)
    assert couples_gate.due(state, "couples-checkin", "person:p1", ["p1"], dec_tue_8pm)[0]
    assert not couples_gate.due(
        state, "couples-checkin", "person:p1", ["p1"], TUE_8PM.replace(hour=4)
    )[0]


def test_quiet_hours_suppress(seat):
    """FALSIFIER: drop the quiet-hours loop in due()."""
    state = _state(p1={"quiet_hours": {"start": "19:30", "end": "07:00"}})
    routines, reason = couples_gate.due(
        state, "couples-checkin", "person:p1", ["p1", "p2"], TUE_8PM
    )
    assert routines == [] and reason == "quiet hours"


def test_a_shared_routine_waits_when_either_partner_is_quiet(seat):
    state = _state(p2={"quiet_hours": {"start": "19:00", "end": "23:00"}})
    state["routines"] = [
        {
            "id": "r00000002",
            "kind": "weekly-checkin-agenda",
            "space": "shared",
            "days": ["tue"],
            "time": "20:00",
            "paused_until": None,
        }
    ]
    assert (
        couples_gate.due(state, "weekly-checkin-agenda", "shared", ["p1", "p2"], TUE_8PM)[1]
        == "quiet hours"
    )


def test_a_paused_routine_is_silent_through_its_date(seat):
    state = _state()
    state["routines"][0]["paused_until"] = "2026-10-20"
    assert couples_gate.due(state, "couples-checkin", "person:p1", ["p1"], TUE_8PM)[0] == []
    state["routines"][0]["paused_until"] = "2026-10-19"
    assert couples_gate.due(state, "couples-checkin", "person:p1", ["p1"], TUE_8PM)[0]


def test_the_context_carries_only_this_partners_settings(seat):
    state = _state(p1={"paused_topics": ["money"]}, p2={"paused_topics": ["secret plans"]})
    routines, _ = couples_gate.due(state, "couples-checkin", "person:p1", ["p1", "p2"], TUE_8PM)
    block = couples_gate.context_for(state, "couples-checkin", "person:p1", routines, TUE_8PM)
    text = json.dumps(block)
    assert "money" in text and "secret plans" not in text


def _run_stub(tmp_path, state: Any, skill: str, space_key: str) -> list[dict[str, Any]]:
    profile = tmp_path / "profile"
    state_path = tmp_path / "settings.json"
    if state is not None:
        state_path.write_text(state if isinstance(state, str) else json.dumps(state))
    ref = couples_slots.stage_gate(profile, skill, space_key, ["p1", "p2"])
    stub = profile / "scripts" / ref
    stub.write_text(
        stub.read_text().replace(repr(str(couples_settings.settings_path())), repr(str(state_path)))
    )
    out = subprocess.run(
        [sys.executable, "-I", str(stub)], capture_output=True, text=True, check=True
    )
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_the_staged_stub_runs_alone_and_fails_closed(seat, tmp_path):
    """The stub is run as Hermes runs it: its own python process, stdlib only."""
    assert _run_stub(tmp_path, None, "couples-checkin", "person:p1")[-1]["wakeAgent"] is False
    assert (
        _run_stub(tmp_path, "{not json", "couples-checkin", "person:p1")[-1]["wakeAgent"] is False
    )
    # A real state: whatever the wall clock says, the last line is the gate.
    lines = _run_stub(tmp_path, _state(), "couples-checkin", "person:p1")
    assert isinstance(lines[-1]["wakeAgent"], bool)


def test_the_gate_wakes_with_its_context_when_due(seat, capsys, monkeypatch, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(_state()))

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return TUE_8PM

    monkeypatch.setattr(couples_gate, "datetime", _Now)
    couples_gate.main(str(path), "couples-checkin", "person:p1", ["p1", "p2"])
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {"wakeAgent": True}
    assert lines[0][couples_gate.CONTEXT_KEY]["checkin_topics"] == ["how work is going"]


# ---------------------------------------------------------------------------
# 6. A reprovision keeps what was said
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.n = 0

    def list_jobs(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        return list(self.jobs)

    def create_job(self, **kw: Any) -> dict[str, Any]:
        self.n += 1
        job = {"id": f"j{self.n}", **kw}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True


def test_a_reprovision_rematerializes_the_slots_and_keeps_the_couples_routines(
    seat, monkeypatch, tmp_path
):
    """Boot twice: the same slots, each gated, and the settings untouched.
    FALSIFIER: drop ``entries.extend(_slot_jobs(...))`` in cron_materialize."""
    from bootstrap.cron_materialize import materialize_cron

    set_timezone(seat, monkeypatch)
    telegram_dm(monkeypatch, A_TG)
    add = {
        "op": "add_routine",
        "kind": "gratitude-prompts",
        "for": "p1",
        "days": ["sun"],
        "time": "09:00",
    }
    change(seat, [add])
    before = stored()
    store = _Store()
    staged: list[tuple[str, str, str]] = []

    def stage(persona: str, skill: str, space_key: str) -> str:
        staged.append((persona, skill, space_key))
        return f".smd-couples/{couples_slots.stub_name(skill, space_key)}"

    customer = seat_doc()
    first = materialize_cron(customer, lambda slug: store, stage_slot_gate=stage)
    second = materialize_cron(customer, lambda slug: store, stage_slot_gate=stage)
    assert first == second
    assert "op-managed:mark:gratitude-prompts@person:p1" in second
    assert "op-managed:mark:weekly-checkin-agenda@shared" in second
    assert len(store.jobs) == len(second) == 7  # 3 per-person skills x 2 + 1 shared
    assert all(
        j["script"].startswith(".smd-couples/") and j["schedule"] == "*/15 * * * *"
        for j in store.jobs
    )
    assert stored() == before


def test_an_authored_cron_entry_for_a_conversational_skill_is_refused(seat):
    from bootstrap import validate
    from bootstrap.cron_materialize import CronMaterializeError, materialize_cron

    customer = seat_doc()
    customer["personas"][0]["cron"] = [
        {"skill": "gratitude-prompts", "schedule": "0 9 * * 0", "space": "person:p1"}
    ]
    with pytest.raises(CronMaterializeError, match="set by the couple in conversation"):
        materialize_cron(customer, lambda slug: _Store(), stage_slot_gate=lambda *a: "x")
    errors: list[str] = []
    validate._validate_couples_cron(customer, errors)
    assert errors and "conversation" in errors[0]


def test_a_seat_without_people_gets_no_slots(seat):
    from bootstrap.cron_materialize import materialize_cron

    customer = seat_doc()
    customer.pop("people")
    customer["seat"]["role"] = "hosted"
    assert materialize_cron(customer, lambda slug: _Store()) == []


def test_the_tools_register_only_on_a_seat_with_people(monkeypatch, tmp_path):
    class Ctx:
        def __init__(self) -> None:
            self.tools: list[str] = []

        def register_tool(self, **kw: Any) -> None:
            self.tools.append(kw["name"])

    doc = seat_doc()
    doc.pop("people")
    doc.pop("spaces")
    write_seat(monkeypatch, tmp_path, doc)
    ctx = Ctx()
    load_plugin("hermes-smd-couples-settings").register(ctx)
    assert ctx.tools == []
    write_seat(monkeypatch, tmp_path, seat_doc())
    ctx = Ctx()
    load_plugin("hermes-smd-couples-settings").register(ctx)
    assert sorted(ctx.tools) == ["couples_settings_change", "couples_settings_read"]
    space._reset_cache_for_tests()
