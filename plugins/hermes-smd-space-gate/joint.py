"""The two-person approval hooks (``shared.joint_acts``), wired into the space gate.

``hold``           a joint-record write becomes a proposal: held, the proposer's
                   yes recorded, and every other partner sent the exact change by
                   the seat itself, on their authored channel: their Telegram DM
                   when they prefer Telegram and are bound, otherwise email.
``answer``         a partner's ``agree`` / ``reject`` in their OWN Telegram DM.
``answer_person``  the same answer from a verified email (``email_confirm``).

Each partner's yes counts once, whatever the channel. Both run only on a
space-mode seat; the gate's hooks call them first, before the private-to-shared
logic, so a joint change is never also held for a "share it" (two people
deciding together is the stronger confirmation).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from shared import joint_acts, joint_notice
from shared import space as space_mod

logger = logging.getLogger(__name__)

_AGREE = frozenset({"agree", "i agree", "yes agree", "agree to the change", "agreed"})
_REJECT = frozenset({"reject", "reject it", "reject the change", "decline", "i reject"})

NOTICE_LANE_SKILL = joint_notice.NOTICE_LANE_SKILL
NOTICE_SUBJECT = joint_notice.NOTICE_SUBJECT

PROPOSAL_NOTICE = joint_notice.PROPOSAL_NOTICE
APPLIED_NOTICE = "You both agreed, so this change is now in place:\n\n{payload}"
REJECTED_NOTICE = "{who} declined the change, so nothing changed:\n\n{payload}"
FAILED_NOTICE = (
    "You both agreed, but this change could not be applied ({why}), so nothing "
    "changed:\n\n{payload}"
)

_HELD = (
    "HELD for both of you: {tool} changes something the two partners decide "
    "together, so it applies only when each of them agrees. Their request counts "
    "as their yes. The seat has sent {others} the exact change; it applies when "
    'they reply "agree", and either of them can reply "reject". It expires in 7 '
    "days. Tell the person it is waiting on their partner. Do not write it again."
)
_REFUSED = "{tool} refused: {why}. Nothing changed."


def _normalize(message: str) -> str:
    s = re.sub(r"[^\w\s]", " ", message.strip().lower())
    return re.sub(r"\s+", " ", s).strip()


# The asker and the notice are shared with the couples settings tool
# (shared.joint_notice), so both name the asker and reach the partner alike.
_speaker = joint_notice.speaker
_reachable = joint_notice.reachable


def notify(person: space_mod.Person, text: str) -> None:
    """Tell one person on their authored channel (``shared.joint_notice``)."""
    joint_notice.notify(person, text)


def hold(
    tool_name: str,
    args: Any,
    current: space_mod.Space,
    cfg: space_mod.SpaceConfig,
    user_id: str,
    session_id: str = "",
) -> dict[str, Any]:
    """Hold a joint write as a proposal (or refuse it). Always blocks."""
    proposer = _speaker(cfg, current, user_id, session_id)
    if proposer is None:
        return {
            "action": "block",
            "message": _REFUSED.format(
                tool=tool_name,
                why="a change the two of you decide together is asked for by one of you, "
                "in your own chat or email or the shared group, so each of you can answer it",
            ),
        }
    others = [p for p in cfg.people if p.id != proposer.id]
    if not others or not all(_reachable(p) for p in others):
        return {
            "action": "block",
            "message": _REFUSED.format(
                tool=tool_name, why="the other partner has no chat or email the seat can ask in"
            ),
        }
    try:
        act = joint_acts.propose(dict(args), proposer.id)
    except joint_acts.JointActError as exc:
        return {"action": "block", "message": _REFUSED.format(tool=tool_name, why=str(exc))}
    except OSError:
        logger.exception("hermes-smd-space-gate: joint act could not be saved")
        return {
            "action": "block",
            "message": _REFUSED.format(tool=tool_name, why="the proposal could not be kept"),
        }
    text = PROPOSAL_NOTICE.format(
        who=proposer.name or proposer.id, payload=joint_acts.payload_text(act)
    )
    try:
        for other in others:
            notify(other, text)
    except Exception:  # noqa: BLE001 — not asked means not held
        logger.warning("hermes-smd-space-gate: joint proposal notice not sent; withdrawn")
        joint_acts.respond(proposer.id, False, cfg)
        return {
            "action": "block",
            "message": _REFUSED.format(
                tool=tool_name, why="the other partner could not be shown the change to agree to"
            ),
        }
    logger.info("hermes-smd-space-gate: joint change %s proposed by %s", act.id, proposer.id)
    return {
        "action": "block",
        "message": _HELD.format(tool=tool_name, others=", ".join(p.name or p.id for p in others)),
    }


def answer(
    message: Any, cfg: space_mod.SpaceConfig, current: space_mod.Space | None
) -> dict[str, Any] | None:
    """A partner's ``agree`` / ``reject``, accepted only in their own DM."""
    if not isinstance(message, str) or current is None:
        return None
    if current.source != space_mod.SOURCE_TELEGRAM_DM or not current.person_id:
        return None
    person = cfg.person(current.person_id)
    if person is None:
        return None
    return answer_person(_normalize(message), cfg, person)


def answer_person(
    words: str, cfg: space_mod.SpaceConfig, person: space_mod.Person
) -> dict[str, Any] | None:
    """One partner's answer, from any channel the caller has verified is theirs.
    ``words`` is already normalized. Each person's yes counts once."""
    if words not in _AGREE and words not in _REJECT:
        return None
    if joint_acts.open_act() is None:
        return None
    try:
        act, outcome = joint_acts.respond(person.id, words in _AGREE, cfg)
    except Exception:  # noqa: BLE001 — the act stays open; say so
        logger.exception("hermes-smd-space-gate: joint change could not be applied")
        return {
            "context": "The change both partners are deciding could not be applied just "
            "now. Nothing changed; it is still waiting. Say so plainly."
        }
    if act is None:
        return None
    payload = joint_acts.payload_text(act)
    if outcome == "applied":
        for p in cfg.people:
            try:
                notify(p, APPLIED_NOTICE.format(payload=payload))
            except Exception:  # noqa: BLE001 — applied regardless; the notice is a courtesy
                logger.warning("hermes-smd-space-gate: applied notice to %s not sent", p.id)
        return {
            "context": "Both partners agreed; the change is in place and both were told. "
            "Confirm in one line."
        }
    if outcome == "rejected":
        for p in cfg.people:
            if p.id == person.id:
                continue
            try:
                notify(p, REJECTED_NOTICE.format(who=person.name or person.id, payload=payload))
            except Exception:  # noqa: BLE001
                logger.warning("hermes-smd-space-gate: rejected notice to %s not sent", p.id)
        return {
            "context": "This partner declined the change; nothing changed and the other "
            "partner was told."
        }
    if outcome == "failed":
        why = act.history[-1] if act.history else "it no longer fits the current settings"
        for p in cfg.people:
            try:
                notify(p, FAILED_NOTICE.format(why=why, payload=payload))
            except Exception:  # noqa: BLE001
                logger.warning("hermes-smd-space-gate: failed notice to %s not sent", p.id)
        return {
            "context": "Both partners agreed, but the change could not be applied: "
            f"{why}. Nothing changed and both were told. Say so plainly."
        }
    if outcome == "already":
        return {
            "context": "This partner had already agreed; it is still waiting on the other partner."
        }
    return {
        "context": "This partner's yes is recorded; the change waits on the other partner's yes."
    }


__all__ = ["answer", "answer_person", "hold", "notify"]
