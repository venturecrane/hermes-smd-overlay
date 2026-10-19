"""The words a couple reads about their settings (ss-console #3264).

Every sentence the seat shows a person about a setting is rendered HERE, in
code, from validated values: the confirmation the agent restates and the
"what do you have set up?" answer. Nothing is composed by the model, so the
text a partner reads is exactly the setting that applies, and it carries none
of the conversation that led to it (a partner's private words are never
relayed; the setting is).
"""

from __future__ import annotations

from typing import Any

from shared import couples_settings as cs
from shared import space as space_mod

_DAY_NAMES = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
_KIND_NAMES = {
    "couples-checkin": "check-in",
    "gratitude-prompts": "gratitude prompt",
    "dates-to-remember": "dates-to-remember reminder",
    "weekly-checkin-agenda": "weekly check-in agenda",
    "travel": "travel price check",
}


def clock(hhmm: str) -> str:
    """``20:00`` -> ``8:00 PM``."""
    hour, minute = int(hhmm[:2]), hhmm[3:]
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:{minute} {suffix}"


def days_text(days: list[str]) -> str:
    if list(days) == list(cs.couples_gate.DAYS):
        return "every day"
    names = [_DAY_NAMES[d] + "s" for d in days]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _who(space_key: str, cfg: space_mod.SpaceConfig) -> str:
    if space_key == space_mod.SHARED:
        return "both of you, in one message"
    person = cfg.person(space_key.split(":", 1)[-1])
    return person.name or person.id if person is not None else space_key


def _name(pid: str, cfg: space_mod.SpaceConfig) -> str:
    person = cfg.person(pid)
    return (person.name or person.id) if person is not None else pid


def routine_text(routine: dict[str, Any], cfg: space_mod.SpaceConfig, tz: str | None) -> str:
    kind = _KIND_NAMES.get(str(routine.get("kind")), str(routine.get("kind")))
    when = f"{days_text(routine['days'])} at {clock(routine['time'])}"
    if tz:
        when += f" ({tz})"
    line = f"{kind} for {_who(str(routine.get('space')), cfg)}, {when}"
    if routine.get("paused_until"):
        line += f", paused through {routine['paused_until']}"
    return line


def describe(changes: list[cs.Change], before: dict[str, Any], cfg: space_mod.SpaceConfig) -> str:
    """One line per change, from the validated values."""
    after = cs.simulate(changes, before, frozenset(cs.KINDS), None)
    tz = after.get("timezone")
    by_id = {r["id"]: r for r in before.get("routines") or []}
    lines: list[str] = []
    for change in changes:
        f = change.fields
        op = change.op
        if op == "set_timezone":
            lines.append(f"Your timezone: {f['timezone']}")
        elif op == "set_checkin_topics":
            topics = "; ".join(f["topics"]) or "none"
            lines.append(f"Check-in topics: {topics}")
        elif op == "set_quiet_hours":
            lines.append(
                f"Quiet hours for {_name(f['person'], cfg)}: no scheduled messages from "
                f"{clock(f['start'])} to {clock(f['end'])}"
            )
        elif op == "clear_quiet_hours":
            lines.append(f"Quiet hours for {_name(f['person'], cfg)}: none")
        elif op == "set_dates_lead_days":
            lines.append(
                f"Dates-to-remember reminders for {_name(f['person'], cfg)}: "
                f"{f['lead_days']} days ahead"
            )
        elif op == "set_paused_topics":
            topics = "; ".join(f["topics"]) or "none"
            lines.append(f"Check-in topics skipped for {_name(f['person'], cfg)}: {topics}")
        elif op == "add_routine":
            target = space_mod.SHARED if f["for"] == space_mod.SHARED else f"person:{f['for']}"
            new = {"kind": f["kind"], "space": target, "days": f["days"], "time": f["time"]}
            lines.append("New: " + routine_text(new, cfg, tz))
        else:
            old = by_id.get(f["routine"])
            if old is None:
                lines.append(f"Routine {f['routine']}")
                continue
            if op == "change_routine":
                new = {**old, **{k: f[k] for k in ("days", "time") if k in f}}
                lines.append("Changed: " + routine_text(new, cfg, tz))
            elif op == "pause_routine":
                lines.append(f"Paused through {f['until']}: " + routine_text(old, cfg, tz))
            elif op == "resume_routine":
                lines.append("Resumed: " + routine_text({**old, "paused_until": None}, cfg, tz))
            else:
                lines.append("Removed: " + routine_text(old, cfg, tz))
    return "\n".join(f"- {line}" for line in lines)


def view(
    doc: dict[str, Any], current: space_mod.Space, cfg: space_mod.SpaceConfig
) -> dict[str, Any]:
    """What this space may see: the couple's shared settings, this person's own,
    and the routines visible here (``couples_settings.routine_visible``)."""
    tz = doc.get("timezone")
    out: dict[str, Any] = {
        "people": [{"id": p.id, "name": p.name} for p in cfg.people],
        "timezone": tz,
        "checkin_topics": list(doc.get("checkin_topics") or []),
        "routines": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "space": r["space"],
                "description": routine_text(r, cfg, tz),
            }
            for r in doc.get("routines") or []
            if cs.routine_visible(r, current)
        ],
    }
    if current.private and current.person_id:
        mine = (doc.get("people") or {}).get(current.person_id) or {}
        out["your_settings"] = {
            "quiet_hours": mine.get("quiet_hours"),
            "dates_lead_days": mine.get("dates_lead_days"),
            "paused_topics": mine.get("paused_topics") or [],
        }
    return out


def quiet_conflicts(doc: dict[str, Any], people: list[str]) -> list[tuple[dict[str, Any], str]]:
    """``(routine, person id)`` for every routine whose time falls in the quiet
    hours of someone it reaches. The gate keeps it silent then; the asker
    should hear that now rather than wonder later."""
    gate = cs.couples_gate
    out: list[tuple[dict[str, Any], str]] = []
    settings = doc.get("people") or {}
    for routine in doc.get("routines") or []:
        minutes = gate._minutes(routine.get("time"))
        if minutes is None:
            continue
        for pid in gate.recipients(str(routine.get("space")), people):
            if gate.in_quiet_hours((settings.get(pid) or {}).get("quiet_hours"), minutes):
                out.append((routine, pid))
    return out


__all__ = ["clock", "days_text", "describe", "quiet_conflicts", "routine_text", "view"]
