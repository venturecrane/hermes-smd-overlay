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

from shared import joint_acts, space_writes
from shared import space as space_mod

logger = logging.getLogger(__name__)

_AGREE = frozenset({"agree", "i agree", "yes agree", "agree to the change", "agreed"})
_REJECT = frozenset({"reject", "reject it", "reject the change", "decline", "i reject"})

#: The lane a joint-change notice email rides. The broker admits the seat's
#: people on it only when the couples-checkin skill authors
#: ``settings.recipients: roster`` (ss-console participant fence).
NOTICE_LANE_SKILL = "couples-checkin"
NOTICE_SUBJECT = "A change for the two of you to decide"

PROPOSAL_NOTICE = (
    "{who} would like to change something you two decide together:\n\n"
    "{payload}\n\n"
    "Reply 'agree' to approve it or 'reject' to decline. "
    "It applies only when you both agree, and it expires in 7 days."
)
APPLIED_NOTICE = "You both agreed, so this change is now in place:\n\n{payload}"
REJECTED_NOTICE = "{who} declined the change, so nothing changed:\n\n{payload}"

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


def _speaker(
    cfg: space_mod.SpaceConfig, current: space_mod.Space, user_id: str, session_id: str
) -> space_mod.Person | None:
    """The person making the request: the DM's or the private email's person,
    the authored person whose Telegram id spoke in the group, or the verified
    sender of a joint email thread."""
    if current.source in (space_mod.SOURCE_TELEGRAM_DM, space_mod.SOURCE_EMAIL):
        return cfg.person(current.person_id or "")
    if current.source == space_mod.SOURCE_TELEGRAM_GROUP:
        return cfg.person_for_telegram(user_id)
    if current.source == space_mod.SOURCE_EMAIL_JOINT:
        from shared.inbound import SESSION_INBOUND_ORIGIN

        origin = SESSION_INBOUND_ORIGIN.get(session_id)
        return cfg.person_for_email(origin.sender_address) if origin is not None else None
    return None


def _reachable(person: space_mod.Person) -> bool:
    return bool(person.telegram_id or person.email)


def notify(person: space_mod.Person, text: str) -> None:
    """Tell one person, by code, on their authored channel (``people[].channel``,
    email by default): their Telegram DM only when they prefer Telegram and are
    bound, else their email. Raises when it could not be sent."""
    if person.reached_by_telegram:
        space_writes.post_notice(person.telegram_id, text)
        return
    if not person.email:
        raise RuntimeError(f"{person.id} has no channel the seat can reach")
    from shared import send_anchor, send_dispatch

    result = send_dispatch.dispatch(
        to=[person.email],
        subject=NOTICE_SUBJECT,
        text=text,
        anchor=None,
        lane=send_anchor.skill_lane(NOTICE_LANE_SKILL),
        templated=True,
    )
    if not result.sent:
        raise RuntimeError(result.reason or "the notice was not sent")


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
    if outcome == "already":
        return {
            "context": "This partner had already agreed; it is still waiting on the other partner."
        }
    return {
        "context": "This partner's yes is recorded; the change waits on the other partner's yes."
    }


__all__ = ["answer", "answer_person", "hold", "notify"]
