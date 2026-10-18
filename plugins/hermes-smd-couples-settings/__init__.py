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

WHO DECIDES. A change that affects only the asker applies at once. Any other
change (timezone, topics, any check-in, the other partner's settings or
routines, a shared routine) is held in the two-person register
(``shared.joint_acts``): the seat sends the other partner the exact change,
rendered in code (``shared.couples_settings_text``), and it applies only when
both have said ``agree`` (answers are taken by the space gate,
``plugins/hermes-smd-space-gate``). The asker is named from verified fields
only (``shared.joint_notice.speaker``); a scheduled run has no asker and is
refused. A turn that read outside content is refused too, the same rule the
record-store write follows.

The handlers decide everything themselves; there is no hook whose order
matters. They raise nothing to Hermes: every refusal is a sentence for the
model, and nothing is changed by one.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from shared import couples_gate, joint_acts, joint_notice
from shared import couples_settings as cs
from shared import couples_settings_text as cst
from shared import space as space_mod
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

TOOL_READ = "couples_settings_read"
TOOL_CHANGE = joint_acts.SETTINGS_TOOL

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
        "id and a plain description), and any change still waiting for both partners. "
        "The people list gives each partner's id for couples_settings_change.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    TOOL_CHANGE: (
        "Change the couple's schedule or settings. Ops: set_timezone {timezone}; "
        "set_checkin_topics {topics}; set_quiet_hours {person,start,end}; "
        "clear_quiet_hours {person}; set_dates_lead_days {person,lead_days}; "
        "set_paused_topics {person,topics}; add_routine {kind,for,days,time}; "
        "change_routine {routine,days?,time?}; pause_routine {routine,until}; "
        "resume_routine {routine}; remove_routine {routine}. A change that affects "
        "only the person asking applies now; anything else (timezone, topics, any "
        "check-in, the other partner's settings or routines, a shared routine) is sent "
        "to the other partner and applies only when both agree. Send personal changes "
        "separately from joint ones. Times are on the quarter hour. The result gives "
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
_HELD = (
    "HELD for both of you: this changes something the two partners decide together, "
    "so it applies only when each agrees. The person's request counts as their yes. "
    "The seat has sent {others} exactly these lines:\n{summary}\n"
    'It applies when {others} replies "agree"; either can reply "reject"; it expires '
    "in 7 days. Tell the person, restating those lines, that it is waiting on "
    "{others}. Do not call this again for it."
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
    act = joint_acts.open_act()
    if act is not None and act.tool == TOOL_CHANGE:
        waiting = [p.name or p.id for p in cfg.people if p.id not in act.approvals]
        out["waiting_for_both"] = {"change": act.summary, "waiting_on": waiting}
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


def _change_handler(args: dict[str, Any], **kwargs: Any) -> str:
    session_id = str(kwargs.get("session_id") or "")
    turn = _turn(session_id)
    if isinstance(turn, str):
        return _REFUSED.format(tool=TOOL_CHANGE, why=turn)
    cfg, current = turn
    from shared import turn_principal

    speaker = joint_notice.speaker(cfg, current, turn_principal.current().user_id, session_id)
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
    if not cs.needs_both(changes, doc, speaker.id):
        try:
            after = cs.apply_changes(
                changes, kinds=kinds, agreed_by=[speaker.id], summary=summary, space=current
            )
        except cs.SettingsError as exc:
            return _REFUSED.format(tool=TOOL_CHANGE, why=str(exc))
        except OSError:
            logger.exception("hermes-smd-couples-settings: settings could not be saved")
            return _REFUSED.format(tool=TOOL_CHANGE, why="the settings could not be saved")
        logger.info(
            "hermes-smd-couples-settings: %d change(s) by %s applied", len(changes), speaker.id
        )
        return _DONE.format(summary=summary, notes=_notes(after, cfg, current))
    return _hold(raw, summary, speaker, cfg)


def _hold(raw: Any, summary: str, speaker: space_mod.Person, cfg: space_mod.SpaceConfig) -> str:
    others = [p for p in cfg.people if p.id != speaker.id]
    if not others or not all(joint_notice.reachable(p) for p in others):
        return _REFUSED.format(
            tool=TOOL_CHANGE, why="the other partner has no chat or email the seat can ask in"
        )
    try:
        act = joint_acts.propose(
            {"changes": copy.deepcopy(raw)}, speaker.id, tool=TOOL_CHANGE, summary=summary
        )
    except joint_acts.JointActError as exc:
        return _REFUSED.format(tool=TOOL_CHANGE, why=str(exc))
    except OSError:
        logger.exception("hermes-smd-couples-settings: proposal could not be saved")
        return _REFUSED.format(tool=TOOL_CHANGE, why="the proposal could not be kept")
    text = joint_notice.PROPOSAL_NOTICE.format(who=speaker.name or speaker.id, payload=act.summary)
    try:
        for other in others:
            joint_notice.notify(other, text)
    except Exception:  # noqa: BLE001 — not asked means not held
        logger.warning("hermes-smd-couples-settings: proposal notice not sent; withdrawn")
        joint_acts.respond(speaker.id, False, cfg)
        return _REFUSED.format(
            tool=TOOL_CHANGE, why="the other partner could not be shown the change to agree to"
        )
    logger.info("hermes-smd-couples-settings: joint change %s proposed by %s", act.id, speaker.id)
    return _HELD.format(others=", ".join(p.name or p.id for p in others), summary=act.summary)


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
