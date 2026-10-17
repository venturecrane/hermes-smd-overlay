"""hermes-smd-space-gate — the private-to-shared write gate (ADR 0090 R1, ADR 0091).

WHY (ss-console #3264, critique revision R1). On a seat a couple shares, every
READ is fenced by space (``shared.space``): a turn in partner A's DM sees only
A's space and the shared one. That leaves the other direction. A turn in A's DM
can still WRITE somewhere B reads: the shared store or list, the group chat, a
calendar invite B receives, a shared Google doc, an email to B. Each is a thing
A may legitimately ask for, so the write is not refused. It is HELD, and it
lands only when A, in the same chat, confirms the exact payload that will land.

Two hooks:

``pre_tool_call``  classifies the call (``shared.space_writes.crossing``). A
    crossing from a private Telegram DM is held in ``SPACE_WRITES`` bound to
    that person and chat, and the call is blocked with the exact payload for the
    model to show. When the person has confirmed, the re-invoked call is matched
    by tool and target, its arguments are REPLACED by the stored payload, the
    approval is spent, and the call proceeds through every other gate. A
    crossing from an email turn or a cron run is refused outright: there is no
    chat in which the same person could confirm it. A candidate call on an
    unresolved space is refused.

``pre_llm_call``  approves the held write when the turn's whole message is a
    bare "share it" from the person who raised it, in the chat it was raised in
    (the binding from overlay #465, ``shared.pending_send``). Only writes held
    BEFORE this message arrived can be approved by it, so a write the model
    composes after the person said "share it" is never covered by that "share it".

On a seat that authors no ``people:`` both hooks return at once: this plugin
cannot change any other seat's behavior. A people block that cannot be read
counts as space mode with no space, so every candidate call is refused.

Hooks are exception-safe; a hook that cannot decide refuses.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from collections import OrderedDict
from typing import Any

from shared import space as space_mod
from shared import space_writes, turn_principal
from shared.pending_send import _may_approve
from shared.turn_principal import Principal

logger = logging.getLogger(__name__)

#: The whole message, normalized, must be one of these. Deliberately distinct
#: from the trust gate's send approvals ("send it", "approve"), so a "yes" to a
#: held email can never also release a held write to the shared space.
_CONFIRMATIONS: frozenset[str] = frozenset(
    {"share it", "yes share it", "share it now", "yes share it now", "ok share it"}
)
_NEGATIONS: frozenset[str] = frozenset(
    {"no", "not", "dont", "don", "wait", "stop", "cancel", "hold", "nope", "never"}
)

#: When each turn (session, turn id) was first seen by this process: a
#: confirmation covers only writes held before the turn that carried it, even if
#: the hook runs again inside that turn. Bounded, FIFO.
_MAX_SEEN = 512
_first_seen: OrderedDict[tuple[str, str], float] = OrderedDict()


def _normalize(message: str) -> str:
    s = re.sub(r"[^\w\s]", " ", message.strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def is_confirmation(message: Any) -> bool:
    if not isinstance(message, str):
        return False
    words = _normalize(message)
    if not words or _NEGATIONS & set(words.split()):
        return False
    return words in _CONFIRMATIONS


def _turn_started(session_id: str, turn_id: str) -> float:
    if not turn_id:
        return time.time()
    key = (session_id, turn_id)
    if key not in _first_seen:
        _first_seen[key] = time.time()
        while len(_first_seen) > _MAX_SEEN:
            _first_seen.popitem(last=False)
    return _first_seen[key]


_UNRESOLVED = (
    "{tool} refused: this conversation's space could not be resolved, so it cannot "
    "be shown that this write stays where it was said. Nothing was written."
)
_NO_CHAT = (
    "{tool} refused: this would put what was said in a private conversation into "
    "{where}, and that needs the person to confirm the exact words in their own "
    "Telegram chat. Ask them to make the request there."
)
_TOO_LONG = (
    "{tool} refused: this would put what was said in a private conversation into "
    "{where}, and it is too long to confirm word for word in chat. Ask for a "
    "shorter version."
)
_HELD = (
    "HELD, not done: this would put what was said in this private conversation "
    "into {where}, where the other person can see it. Show the person the exact "
    'payload below, word for word, and ask them to reply "share it" here if '
    "that is what they want shared. Once they do, call {tool} again with the same "
    "arguments: exactly this payload is what will be written, whatever is passed "
    "the second time.\n\n{payload}"
)
_CONFIRMED = (
    "The person confirmed the held write to {where}. Call {tool} again now with "
    "the same arguments; exactly the payload they confirmed is what will be written."
)


def _current_space(session_id: str) -> tuple[bool, space_mod.Space | None]:
    try:
        cfg = space_mod.space_config()
        if cfg is None:
            return False, None
        return True, space_mod.resolve_space(session_id or None, config=cfg)
    except space_mod.SpaceConfigError:
        logger.exception("hermes-smd-space-gate: people block unreadable; space unresolved")
        return True, None


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    """Hold, refuse, or release a private-to-shared write. Exception-safe."""
    tool_name = str(kwargs.get("tool_name") or "")
    try:
        if not space_writes.is_candidate(tool_name):
            return None
        session_id = str(kwargs.get("session_id") or "")
        in_space_mode, current = _current_space(session_id)
        if not in_space_mode:
            return None
        if current is None:
            return {"action": "block", "message": _UNRESOLVED.format(tool=tool_name)}
        cfg = space_mod.space_config()
        if cfg is None:
            return None
        args = kwargs.get("args")
        cross = space_writes.crossing(tool_name, args, current, cfg)
        if cross is None:
            return None
        if current.source != space_mod.SOURCE_TELEGRAM_DM:
            return {
                "action": "block",
                "message": _NO_CHAT.format(tool=tool_name, where=cross.where),
            }
        turn = turn_principal.current()
        held = space_writes.SPACE_WRITES.take_for_send(tool_name, cross.targets, turn)
        if held is not None:
            if isinstance(args, dict):
                args.clear()
                args.update(copy.deepcopy(held.args))
            logger.info("hermes-smd-space-gate: confirmed write released (%s)", tool_name)
            return None
        payload = space_writes.exact_text(args)
        if len(payload) > space_writes.MAX_CONFIRM_CHARS:
            return {
                "action": "block",
                "message": _TOO_LONG.format(tool=tool_name, where=cross.where),
            }
        space_writes.SPACE_WRITES.capture(tool_name, args, cross.targets, requester=turn)
        logger.info("hermes-smd-space-gate: private-to-shared write held (%s)", tool_name)
        return {
            "action": "block",
            "message": _HELD.format(tool=tool_name, where=cross.where, payload=payload),
        }
    except Exception:  # noqa: BLE001 — a hook that cannot decide refuses
        logger.exception("hermes-smd-space-gate: pre_tool_call failed; refusing %s", tool_name)
        return {"action": "block", "message": _UNRESOLVED.format(tool=tool_name)}


def on_pre_llm_call(**kwargs: Any) -> dict[str, Any] | None:
    """Approve the held write this person raised in this chat. Exception-safe."""
    try:
        if str(kwargs.get("platform") or "").lower() != "telegram":
            return None
        message = kwargs.get("user_message")
        if not is_confirmation(message):
            return None
        session_id = str(kwargs.get("session_id") or "")
        arrived = _turn_started(session_id, str(kwargs.get("turn_id") or ""))
        turn = turn_principal.current()
        sender = str(kwargs.get("sender_id") or "").strip()
        if not turn.user_id or (sender and sender != turn.user_id):
            return None
        approver = Principal(
            platform="telegram",
            user_id=turn.user_id,
            chat_id=turn.chat_id,
            chat_type=turn.chat_type,
        )
        for rec in space_writes.SPACE_WRITES.pending():
            if rec.approved or rec.created_at >= arrived:
                continue
            if _may_approve(rec.requester, approver, sole_principal=False):
                rec.approved = True
                rec.approval_source = f"telegram:{turn.user_id}"
                logger.info("hermes-smd-space-gate: held write confirmed by its requester")
                where = _where(rec)
                return {"context": _CONFIRMED.format(where=where, tool=rec.tool_name)}
        return None
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-space-gate: pre_llm_call failed", exc_info=True)
        return None


def _where(rec: Any) -> str:
    targets = sorted(rec.recipients)
    return ", ".join(targets) if targets else "the shared space"


def register(ctx: Any) -> None:
    """Register both hooks. Each decides space mode per call and returns at once
    on a seat that authors no ``people:``, so registering everywhere keeps the
    manifest honest (``tests/test_hook_parity.py``) without changing any other
    seat's behavior."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    logger.info("hermes-smd-space-gate registered (inert unless people: is authored)")


__all__ = ["is_confirmation", "on_pre_llm_call", "on_pre_tool_call", "register"]
