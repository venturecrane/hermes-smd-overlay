"""The space gate over EMAIL: "share it" and the two-person answers by reply.

WHY (ss-console #3264, Captain directive): the couples product must work fully
over email, Telegram later. Until now a private-to-shared write from an email
turn was refused outright, because there was no chat for the same person to
confirm it in. An email thread is that chat.

HOLD. A crossing from a person's email turn is held in ``SPACE_WRITES`` bound
to a principal made of verified fields only: the Svix-verified sender of THIS
turn's email (``SESSION_INBOUND_ORIGIN.bound_this_turn``) and that email's
thread (``conversation_id``, the provider's thread id). The seat itself replies
in that thread, through the broker's audited ``agentmail_reply`` verb, so the
confirmation goes to that sender only (the recipient is structural, never
named): the exact held payload and "Reply 'share it' to send". If the reply
cannot be sent, nothing is held.

CONFIRM. A later email turn approves the held write only when every one holds:
* it is bound to its own verified origin (the turn's own message id);
* its sender is the address that raised the hold, and it is on the same thread;
* the sender's OWN new words (``reply_text``: the provider's quoted-history-
  stripped text) are a bare "share it", nothing else;
* it is not a forward (subject ``Fwd:`` / ``Fw:``) and not auto-submitted.
The other partner's "share it", a forward of the person's old reply, and a
"share it" quoted inside a longer message release nothing. Approval is single
use and keeps the register's expiry; the turn is then told the exact call to
repeat, and the re-invoked call's arguments are replaced by the stored payload.

TWO-PERSON ANSWERS. An email whose own words are a bare ``agree`` / ``reject``
from a person's verified address counts as that person's answer to the open
joint change (``joint``), once per person whatever the channel.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from shared import inbound, space_writes
from shared import space as space_mod
from shared.turn_principal import Principal

logger = logging.getLogger(__name__)

EMAIL = "email"
_FORWARD_RE = re.compile(r"^\s*(fwd?|fw)\s*:", re.IGNORECASE)

_HELD = (
    "HELD, not done: this would put what was said in this private conversation "
    "into {where}, where the other person can see it. The seat has just replied "
    "to the person in this email thread with the exact words and asked them to "
    'reply "share it". Do not restate or reword them. Nothing else is needed '
    "from you on this turn."
)
_NOT_SHOWN = (
    "{tool} refused: this would put what was said in a private email into "
    "{where}, and the exact words could not be sent to the person to confirm. "
    "Nothing was written."
)
_RELEASED = (
    "The person confirmed, by email, the held write to {where}. Call {tool} now "
    "with exactly these arguments; the confirmed payload is what will be written:\n"
    "{args}"
)

NOTICE_SUBJECT_TAIL = "Reply 'share it' to send."


def this_turn_origin(session_id: str, user_message: Any = None) -> inbound.InboundOrigin | None:
    """The verified origin of THIS turn's email, or None. Binds first when the
    prompt is at hand (this plugin's pre_llm_call runs before the inbound
    plugin's)."""
    if user_message is not None:
        try:
            inbound.bind_origin_from_prompt(session_id, user_message)
        except Exception:  # noqa: BLE001 — binding is best-effort; unbound is "no"
            logger.debug("hermes-smd-space-gate: origin bind failed", exc_info=True)
    return inbound.SESSION_INBOUND_ORIGIN.bound_this_turn(session_id)


def principal(origin: inbound.InboundOrigin | None) -> Principal | None:
    """The email requester: verified sender + thread. None without a thread."""
    if origin is None or not origin.sender_address or not origin.conversation_id:
        return None
    return Principal(
        platform=EMAIL,
        user_id=origin.sender_address.strip().lower(),
        chat_id=f"thread:{origin.conversation_id}",
        chat_type=EMAIL,
    )


def own_words(origin: inbound.InboundOrigin) -> str | None:
    """The sender's own new words, normalized, or None when this message cannot
    speak for them (a forward, an auto-reply, no extracted text)."""
    if origin.auto_submitted or _FORWARD_RE.match(origin.subject or ""):
        return None
    text = origin.reply_text or ""
    if not text.strip():
        return None
    s = re.sub(r"[^\w\s]", " ", text.strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def send_confirmation(origin: inbound.InboundOrigin, session_id: str, text: str) -> None:
    """Reply in the thread, to the sender only, through the broker. Raises."""
    from shared import agentmail_broker, send_anchor

    agentmail_broker.send_reply(
        origin.message_id,
        text=text,
        session_id=session_id,
        anchor=send_anchor.message_origin(session_id),
    )


def hold_or_release(
    tool_name: str,
    args: Any,
    cross: space_writes.Crossing,
    current: space_mod.Space,
    session_id: str,
    payload_for: Any,
) -> dict[str, Any] | None:
    """The email half of the gate's pre_tool_call. ``payload_for`` renders the
    exact payload (the gate's own stamping and length rules)."""
    origin = this_turn_origin(session_id)
    who = principal(origin)
    if who is None or origin is None:
        return {
            "action": "block",
            "message": f"{tool_name} refused: this email cannot be tied to its own thread, "
            "so the person could not confirm it there. Nothing was written.",
        }
    held = space_writes.SPACE_WRITES.take_for_send(tool_name, cross.targets, who)
    if held is not None:
        if isinstance(args, dict):
            args.clear()
            args.update(json.loads(json.dumps(held.args)))
        space_writes.note_release(tool_name, who, current)
        logger.info("hermes-smd-space-gate: email-confirmed write released (%s)", tool_name)
        return None
    payload, refusal = payload_for(args)
    if refusal is not None:
        return refusal
    try:
        send_confirmation(origin, session_id, space_writes.notice_text(payload))
    except Exception:  # noqa: BLE001 — not shown means not held
        logger.warning("hermes-smd-space-gate: email confirmation not sent; write refused")
        return {"action": "block", "message": _NOT_SHOWN.format(tool=tool_name, where=cross.where)}
    space_writes.SPACE_WRITES.capture(tool_name, args, cross.targets, requester=who)
    logger.info("hermes-smd-space-gate: private-to-shared write held by email (%s)", tool_name)
    return {"action": "block", "message": _HELD.format(where=cross.where)}


def confirm_from_email(origin: inbound.InboundOrigin, words: str) -> dict[str, Any] | None:
    """Approve the write this sender held on this thread, when ``words`` is a
    bare "share it". Returns the context telling the turn what to call."""
    from . import is_confirmation

    if not is_confirmation(words):
        return None
    who = principal(origin)
    if who is None:
        return None
    now = time.time()
    for rec in space_writes.SPACE_WRITES.pending():
        if rec.approved or rec.created_at >= now:
            continue
        req = rec.requester
        if req.platform != EMAIL or req.user_id != who.user_id or req.chat_id != who.chat_id:
            continue
        rec.approved = True
        rec.approval_source = f"email:{who.user_id}"
        logger.info("hermes-smd-space-gate: held write confirmed by email")
        return {
            "context": _RELEASED.format(
                where=", ".join(sorted(rec.recipients)),
                tool=rec.tool_name,
                args=json.dumps(rec.args, ensure_ascii=False, sort_keys=True),
            )
        }
    return None


__all__ = ["confirm_from_email", "hold_or_release", "own_words", "principal", "this_turn_origin"]
