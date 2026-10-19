"""hermes-smd-couples-settings — a couple sets their schedule by talking (ss-console #3264).

Two tools, registered only on a seat that authors ``people:`` (ADR 0091):

``couples_settings_read``    what is set, as THIS space may see it: the
    timezone, the check-in topics, this person's own settings (in their private
    space), and the routines visible here (shared ones, check-ins, and this
    person's own; never the other partner's private routines).
``couples_settings_change``  one to six changes, applied together: timezone,
    check-in topics, a person's quiet hours, dates lead time or skipped topics,
    and routines (add, change, pause, resume, remove).

WHY A PURPOSE-MADE TOOL. The couples persona has no shell, file, code or
delegation tools (ADR 0090 R2), and Hermes' ``cronjob`` is CODE_EXECUTION
(``shared.action_classes``), so it is not offered. This tool cannot write a cron
job, a path, or an address: it writes one validated JSON document
(``shared.couples_settings``) that the seat's fixed slot jobs read every quarter
hour (``shared.couples_gate``). Where a routine is delivered is a person id or
``shared`` from the seat's ``people:`` block, resolved to an address in code.

WHO DECIDES. Either partner, alone: every change applies at once (Captain,
2026-10-19). The asker is named from verified fields only (:func:`_speaker`);
a scheduled run has no asker and is refused. A turn that read outside content
is refused too, the same injection defense the record-store write follows.

The handlers decide everything themselves; there is no hook whose order
matters. They raise nothing to Hermes: every refusal is a sentence for the
model, and nothing is changed by one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared import couples_gate
from shared import couples_settings as cs
from shared import couples_settings_text as cst
from shared import space as space_mod
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

TOOL_READ = "couples_settings_read"
TOOL_CHANGE = "couples_settings_change"

_CHANGE_ITEM = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": list(cs.OPS)},
        "timezone": {"type": "string", "description": "IANA name, e.g. America/Los_Angeles."},
        "topics": {"type": "array", "items": {"type": "string"}, "maxItems": cs.MAX_TOPICS},
        "person": {"type": "string", "description": "A person id from people (never an address)."},
        "start": {"type": "string", "description": "HH:MM, 24-hour."},
        "end": {"type": "string", "description": "HH:MM, 24-hour."},
        "lead_days": {"type": "integer", "minimum": 0, "maximum": cs.MAX_LEAD_DAYS},
        "kind": {"type": "string", "enum": list(cs.KINDS)},
        "for": {
            "type": "string",
            "description": "A person id from people, or 'shared' for a routine both get.",
        },
        "days": {
            "type": "array",
            "items": {"type": "string", "enum": list(couples_gate.DAYS)},
            "minItems": 1,
        },
        "time": {"type": "string", "description": "HH:MM, 24-hour, on the quarter hour."},
        "routine": {"type": "string", "description": "A routine id from couples_settings_read."},
        "until": {"type": "string", "description": "YYYY-MM-DD, paused through this date."},
    },
    "required": ["op"],
    "additionalProperties": False,
}

TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    TOOL_READ: (
        "What this couple has set up: their timezone, their check-in topics, your own "
        "settings (in a private conversation), the routines you can see (each with an "
        "id and a plain description). "
        "The people list gives each partner's id for couples_settings_change.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    TOOL_CHANGE: (
        "Change the couple's schedule or settings. Ops: set_timezone {timezone}; "
        "set_checkin_topics {topics}; set_quiet_hours {person,start,end}; "
        "clear_quiet_hours {person}; set_dates_lead_days {person,lead_days}; "
        "set_paused_topics {person,topics}; add_routine {kind,for,days,time}; "
        "change_routine {routine,days?,time?}; pause_routine {routine,until}; "
        "resume_routine {routine}; remove_routine {routine}. Either partner may make "
        "any change; it applies now. Times are on the quarter hour. The result gives "
        "the exact lines to restate to the person.",
        {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "items": _CHANGE_ITEM,
                    "minItems": 1,
                    "maxItems": cs.MAX_CHANGES,
                }
            },
            "required": ["changes"],
            "additionalProperties": False,
        },
    ),
}

_REFUSED = "{tool} refused: {why}. Nothing changed."
_DONE = (
    "DONE: this applies now. Tell the person, restating exactly these lines and "
    "adding nothing about times or days they do not say:\n{summary}{notes}"
)


def _turn(session_id: str) -> tuple[space_mod.SpaceConfig, space_mod.Space] | str:
    try:
        cfg = space_mod.space_config()
    except space_mod.SpaceConfigError:
        return "this seat's people could not be read"
    if cfg is None:
        return "this seat is not shared by people"
    current = space_mod.resolve_space(session_id or None, config=cfg)
    if current is None:
        return "this conversation's space could not be resolved"
    return cfg, current


def _customer() -> dict[str, Any]:
    from shared.customer_config import CustomerConfig

    return CustomerConfig.from_volume(space_mod._config_path()).raw


def _read_handler(args: dict[str, Any], **kwargs: Any) -> str:
    turn = _turn(str(kwargs.get("session_id") or ""))
    if isinstance(turn, str):
        return _REFUSED.format(tool=TOOL_READ, why=turn)
    cfg, current = turn
    try:
        doc = cs.load()
    except cs.SettingsError as exc:
        return _REFUSED.format(tool=TOOL_READ, why=str(exc))
    out = cst.view(doc, current, cfg)
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def _tainted(session_id: str) -> bool:
    try:
        from shared.inbound import SESSION_TAINT

        return SESSION_TAINT.is_tainted(session_id)
    except Exception:  # noqa: BLE001 — unknown taint reads as tainted
        return True


def _notes(doc: dict[str, Any], cfg: space_mod.SpaceConfig, current: space_mod.Space) -> str:
    lines = []
    for routine, pid in cst.quiet_conflicts(doc, [p.id for p in cfg.people]):
        if not cs.routine_visible(routine, current):
            continue
        person = cfg.person(pid)
        who = (person.name or person.id) if person is not None else pid
        lines.append(
            f"Note: {cst.routine_text(routine, cfg, doc.get('timezone'))} falls inside "
            f"{who}'s quiet hours, so it is not sent while those apply."
        )
    return ("\n" + "\n".join(lines)) if lines else ""


def _speaker(
    cfg: space_mod.SpaceConfig, current: space_mod.Space, user_id: str, session_id: str
) -> space_mod.Person | None:
    """The person making the request: the DM's or the private email's person,
    the authored person whose Telegram id spoke in the group, or the verified
    sender of a joint email thread. ``None`` for a cron run or anything else."""
    if current.source in (space_mod.SOURCE_TELEGRAM_DM, space_mod.SOURCE_EMAIL):
        return cfg.person(current.person_id or "")
    if current.source == space_mod.SOURCE_TELEGRAM_GROUP:
        return cfg.person_for_telegram(user_id)
    if current.source == space_mod.SOURCE_EMAIL_JOINT:
        from shared.inbound import SESSION_INBOUND_ORIGIN

        origin = SESSION_INBOUND_ORIGIN.get(session_id)
        return cfg.person_for_email(origin.sender_address) if origin is not None else None
    return None


def _change_handler(args: dict[str, Any], **kwargs: Any) -> str:
    session_id = str(kwargs.get("session_id") or "")
    turn = _turn(session_id)
    if isinstance(turn, str):
        return _REFUSED.format(tool=TOOL_CHANGE, why=turn)
    cfg, current = turn
    from shared import turn_principal

    speaker = _speaker(cfg, current, turn_principal.current().user_id, session_id)
    if speaker is None:
        return _REFUSED.format(
            tool=TOOL_CHANGE,
            why="a change is asked for by one of the two of you, in your own conversation "
            "or a shared one; a scheduled run cannot ask",
        )
    if _tainted(session_id):
        return _REFUSED.format(
            tool=TOOL_CHANGE,
            why="this conversation read outside content, so a settings change is not taken "
            "from it; ask the person to send the change in a new message",
        )
    raw = args.get("changes") if isinstance(args, dict) else None
    try:
        kinds = cs.available_kinds(_customer())
        changes = cs.parse_changes(raw, cfg)
        doc = cs.load()
        after = cs.simulate(changes, doc, kinds, current)
        summary = cst.describe(changes, doc, cfg)
    except cs.SettingsError as exc:
        return _REFUSED.format(tool=TOOL_CHANGE, why=str(exc))
    except Exception as exc:  # noqa: BLE001 — the config itself is unreadable
        logger.warning("hermes-smd-couples-settings: change could not be checked (%s)", exc)
        return _REFUSED.format(tool=TOOL_CHANGE, why="the seat's settings could not be checked")
    try:
        after = cs.apply_changes(
            changes, kinds=kinds, changed_by=speaker.id, summary=summary, space=current
        )
    except cs.SettingsError as exc:
        return _REFUSED.format(tool=TOOL_CHANGE, why=str(exc))
    except OSError:
        logger.exception("hermes-smd-couples-settings: settings could not be saved")
        return _REFUSED.format(tool=TOOL_CHANGE, why="the settings could not be saved")
    logger.info("hermes-smd-couples-settings: %d change(s) by %s applied", len(changes), speaker.id)
    return _DONE.format(summary=summary, notes=_notes(after, cfg, current))


_HANDLERS = {TOOL_READ: _read_handler, TOOL_CHANGE: _change_handler}


def register(ctx: Any) -> None:
    """Register both tools on a seat that authors ``people:``; nothing elsewhere."""
    try:
        cfg = space_mod.space_config()
    except space_mod.SpaceConfigError:
        logger.warning("hermes-smd-couples-settings: people unreadable; no tools")
        return
    if cfg is None:
        logger.info("hermes-smd-couples-settings: no people on this seat; no tools")
        return
    for name, (description, schema) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="couples_settings",
            schema=schema,
            handler=_HANDLERS[name],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-couples-settings registered %d tools", len(TOOLS))


__all__ = ["TOOLS", "TOOL_CHANGE", "TOOL_READ", "register"]
