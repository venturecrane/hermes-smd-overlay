"""Who is asking, and how the seat tells a partner, for two-person changes.

Shared by the two callers that put a change in front of the other partner: the
space gate's held joint-record write (``plugins/hermes-smd-space-gate/joint.py``)
and the couples settings tool (``plugins/hermes-smd-couples-settings``). One
definition, so both name the asker from the same verified fields and reach the
other partner the same way (ADR 0090/0091, ss-console #3264, #3283).
"""

from __future__ import annotations

from shared import space as space_mod
from shared import space_writes

#: The lane a two-person notice email rides. The broker admits the seat's
#: people on it only when the couples-checkin skill authors
#: ``settings.recipients: roster`` (ss-console participant fence).
NOTICE_LANE_SKILL = "couples-checkin"
NOTICE_SUBJECT = "A change for the two of you to decide"

#: What the other partner is sent. ``payload`` is the exact change: the record
#: text, or a settings change's code-rendered lines. Never the conversation.
PROPOSAL_NOTICE = (
    "{who} would like to change something you two decide together:\n\n"
    "{payload}\n\n"
    "Reply 'agree' to approve it or 'reject' to decline. "
    "It applies only when you both agree, and it expires in 7 days."
)


def speaker(
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


def reachable(person: space_mod.Person) -> bool:
    return bool(person.telegram_id or person.email)


def notify(person: space_mod.Person, text: str, subject: str = NOTICE_SUBJECT) -> None:
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
        subject=subject,
        text=text,
        anchor=None,
        lane=send_anchor.skill_lane(NOTICE_LANE_SKILL),
        templated=True,
    )
    if not result.sent:
        raise RuntimeError(result.reason or "the notice was not sent")


__all__ = [
    "NOTICE_LANE_SKILL",
    "NOTICE_SUBJECT",
    "PROPOSAL_NOTICE",
    "notify",
    "reachable",
    "speaker",
]
