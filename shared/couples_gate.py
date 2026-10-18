"""The wake gate for a couple's conversation-set routines (ss-console #3264).

WHY. A couple sets their schedule by talking to the agent ("check in with me
Tuesdays and Fridays at 8pm", "no messages after 9pm", "pause the check-ins for
two weeks"). Those settings live in one JSON file on the seat's volume
(``shared.couples_settings``), written only by the ``couples_settings_change``
tool. Nothing about them is written into the Hermes cron store.

Instead the seat materializes, at boot, one fixed job per (skill, space) slot
(``op-managed:<persona>:<skill>@<space>``, ``*/15 * * * *``) whose pre-run
script is this gate. Every quarter hour the gate reads the settings file and
decides, in code, whether a routine in its slot is due NOW in the couple's own
timezone. So:

* a change takes effect on the next tick, with no cron write and no reprovision;
* daylight saving and a change of timezone need nothing re-materialized, because
  the gate computes the couple's wall clock itself;
* pause and quiet hours are enforced here, structurally, before any model runs:
  a quiet or paused slot is ``{"wakeAgent": false}``, which Hermes turns into a
  silent run with no LLM call and no delivery;
* where it is delivered is the slot's own space (``person:<id>`` or ``shared``),
  fixed by the job name the materializer authored. Nothing in the settings file
  names an address.

When a routine is due the gate prints one JSON line carrying what the run needs
(the routine, the couple's timezone, and the settings THIS space may read: a
person's run sees that person's settings and the couple's shared ones, never the
other partner's), then the wake line. Hermes injects that stdout into the run as
context.

STDLIB ONLY. Hermes runs a pre-run script as a separate ``python`` process with
the script's own directory first on ``sys.path``; the boot stager copies this
file next to each slot's stub (``scripts/.smd-couples/``). It must not import
the overlay.

FAIL CLOSED. Any error at all (no file, unreadable JSON, unknown timezone) prints
``{"wakeAgent": false}`` and exits 0. Hermes treats a FAILED script as a wake, so
this function never lets an exception escape.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

SCHEMA = "smd.couples_settings/1"

#: Python ``weekday()`` order.
DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: The slot cadence. Routine times are quarter hours, so a tick at :00/:15/:30/:45
#: UTC lands on every quarter hour of every real timezone offset.
SLOT_MINUTES = 15

CONTEXT_KEY = "smd_couples_routine"

SHARED = "shared"
_PERSON_PREFIX = "person:"


def _zone(name: Any):
    if not isinstance(name, str) or not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone is no zone
        return None


def _minutes(hhmm: Any) -> int | None:
    if not isinstance(hhmm, str) or len(hhmm) != 5 or hhmm[2] != ":":
        return None
    hh, mm = hhmm[:2], hhmm[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def in_quiet_hours(quiet: Any, local_minutes: int) -> bool:
    """Whether a local time (minutes after midnight) is inside a person's quiet
    hours. ``quiet`` is ``{"start": "HH:MM", "end": "HH:MM"}``; a window may run
    overnight (21:00 to 08:00). Anything unreadable is treated as quiet: a
    message suppressed by doubt is the safe error."""
    if quiet is None:
        return False
    if not isinstance(quiet, dict):
        return True
    start, end = _minutes(quiet.get("start")), _minutes(quiet.get("end"))
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= local_minutes < end
    return local_minutes >= start or local_minutes < end


def paused_on(routine: dict[str, Any], today: date) -> bool:
    """A routine is paused through the date it names (inclusive)."""
    until = routine.get("paused_until")
    if until is None:
        return False
    try:
        return today <= date.fromisoformat(str(until))
    except ValueError:
        return True


def recipients(space_key: str, people: list[str]) -> list[str]:
    if space_key == SHARED:
        return list(people)
    if space_key.startswith(_PERSON_PREFIX):
        return [space_key[len(_PERSON_PREFIX) :]]
    return []


def due(
    state: dict[str, Any], skill: str, space_key: str, people: list[str], now: datetime
) -> tuple[list[dict[str, Any]], str]:
    """``(routines due now in this slot, reason)``. ``now`` is aware (any zone).

    A routine is due when its skill and space are this slot's, today (in the
    couple's timezone) is one of its days, the current quarter hour is its time,
    it is not paused, and nobody it reaches is in quiet hours. ``reason`` says
    why nothing is due, for the silent line."""
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        return [], "no settings"
    zone = _zone(state.get("timezone"))
    if zone is None:
        return [], "no timezone"
    local = now.astimezone(zone)
    quarter = (local.hour * 60 + local.minute) // SLOT_MINUTES * SLOT_MINUTES
    day = DAYS[local.weekday()]
    matches: list[dict[str, Any]] = []
    for routine in state.get("routines") or []:
        if not isinstance(routine, dict):
            continue
        if routine.get("kind") != skill or routine.get("space") != space_key:
            continue
        if day not in (routine.get("days") or []):
            continue
        if _minutes(routine.get("time")) != quarter:
            continue
        if paused_on(routine, local.date()):
            continue
        matches.append(routine)
    if not matches:
        return [], "nothing due"
    settings = state.get("people") if isinstance(state.get("people"), dict) else {}
    for pid in recipients(space_key, people):
        person = settings.get(pid) if isinstance(settings.get(pid), dict) else {}
        if in_quiet_hours(person.get("quiet_hours"), local.hour * 60 + local.minute):
            return [], "quiet hours"
    return matches, ""


def context_for(
    state: dict[str, Any], skill: str, space_key: str, routines: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """What the run is told. Only what this space may read."""
    zone = _zone(state.get("timezone"))
    local = now.astimezone(zone) if zone is not None else now
    block: dict[str, Any] = {
        "skill": skill,
        "space": space_key,
        "timezone": state.get("timezone"),
        "local_time": local.strftime("%Y-%m-%d %H:%M"),
        "routines": [
            {"id": r.get("id"), "days": r.get("days"), "time": r.get("time")} for r in routines
        ],
        "checkin_topics": list(state.get("checkin_topics") or []),
    }
    if space_key.startswith(_PERSON_PREFIX):
        pid = space_key[len(_PERSON_PREFIX) :]
        people = state.get("people") if isinstance(state.get("people"), dict) else {}
        mine = people.get(pid) if isinstance(people.get(pid), dict) else {}
        block["person"] = pid
        block["paused_topics"] = list(mine.get("paused_topics") or [])
        block["dates_lead_days"] = mine.get("dates_lead_days")
    return {CONTEXT_KEY: block}


def _emit(wake: bool, extra: dict[str, Any] | None = None, reason: str = "") -> None:
    if extra is not None:
        print(json.dumps(extra, sort_keys=True))
    gate: dict[str, Any] = {"wakeAgent": wake}
    if reason:
        gate["reason"] = reason
    print(json.dumps(gate))


def main(state_path: str, skill: str, space_key: str, people: list[str]) -> None:
    """The pre-run entry point. Never raises; always ends with the gate line."""
    try:
        try:
            with open(state_path, encoding="utf-8") as handle:
                state = json.load(handle)
        except FileNotFoundError:
            _emit(False, reason="no settings")
            return
        now = datetime.now(timezone.utc)
        routines, reason = due(state, skill, space_key, list(people), now)
        if not routines:
            _emit(False, reason=reason)
            return
        _emit(True, context_for(state, skill, space_key, routines, now))
    except Exception:  # noqa: BLE001 — a gate that cannot decide stays shut
        try:
            _emit(False, reason="gate error")
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "CONTEXT_KEY",
    "DAYS",
    "SCHEMA",
    "SLOT_MINUTES",
    "context_for",
    "due",
    "in_quiet_hours",
    "main",
    "paused_on",
]
