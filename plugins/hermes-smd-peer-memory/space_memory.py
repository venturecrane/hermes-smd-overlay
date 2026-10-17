"""Space memory: the per-peer lane on a seat several people share (ADR 0091).

WHY (ss-console #3264). A couple shares one hosted agent. Hermes' native memory
is one MEMORY.md per profile that every turn reads, so it is switched off on
such a seat (``bootstrap.translate``). This module is what replaces it: every
row carries the SPACE it was said in (``person:<id>`` or ``shared``, resolved by
``shared.space``, never named by the model), and a turn is shown only rows from
the spaces it may read. Partner B's turn, in B's DM or in the group, never sees
a row partner A wrote in A's DM.

One person, one id. A partner's Telegram id and email address both resolve to
the same ``person:<id>`` (``people:`` in customer.yaml), so their preferences
follow them across channels instead of splitting into two peers.

Fail closed. A turn whose space cannot be resolved injects nothing, and a write
on it is refused before it runs (``pre_tool_call``) and again where it would be
written (``post_tool_call``, which also skips any call another hook blocked).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared import space as space_mod
from shared.inbound import SESSION_TAINT

from . import store

logger = logging.getLogger(__name__)

REMEMBER_TOOL = "remember"

REMEMBER_DESCRIPTION = (
    "Remember something for later. It is kept in the space this conversation is "
    "in, never anywhere else: said in one person's private chat, only that person "
    "will ever see it again; said in the shared group, both people will. Use it "
    "for facts and plans you were asked to keep (a date, a gift idea, a place they "
    "loved). You do not choose where it is kept, and you cannot use it to pass "
    "something from one person to the other."
)

REMEMBER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fact": {
            "type": "string",
            "description": "What to remember, in one or two plain sentences.",
        }
    },
    "required": ["fact"],
    "additionalProperties": False,
}

_UNRESOLVED = (
    "{tool} refused: this conversation's space could not be resolved, so there is "
    "no space it would be safe to keep this in. Nothing was kept."
)
_TAINTED = (
    "{tool} refused: this turn read content from outside the conversation, and "
    "memory keeps only what the people here said. Nothing was kept."
)


def resolve(session_id: str) -> space_mod.Space | None:
    """The turn's space, or None (unresolved, or the block could not be read)."""
    try:
        return space_mod.resolve_space(session_id or None)
    except space_mod.SpaceConfigError:
        logger.exception("hermes-smd-peer-memory: people block unreadable; space unresolved")
        return None


def speaker(space: space_mod.Space | None, sender_id: str) -> str | None:
    """The ``person:<id>`` speaking on this turn, or None.

    In a person's space the speaker is that person (whichever channel they used).
    In the shared group it is the authored person whose Telegram id sent the
    message. A cron run has no speaker."""
    if space is None:
        return None
    if space.private:
        return space.key
    if space.source == space_mod.SOURCE_TELEGRAM_GROUP:
        try:
            person = space_mod.person_for_sender(sender_id)
        except space_mod.SpaceConfigError:
            return None
        return space_mod.person_key(person.id) if person is not None else None
    return None


def context_block(
    client: Any, space: space_mod.Space | None, peer: str | None, persona: str
) -> str | None:
    """The block to inject on this turn, or None (unresolved: nothing at all)."""
    if space is None:
        return None
    memories, preferences = store.space_rows(
        client,
        readable=space_mod.readable_spaces(space),
        speaker=peer,
        persona_slug=persona or None,
    )
    return store.render_space_block(memories, preferences, private=space.private)


def pre_tool_block(tool_name: str, session_id: str) -> dict[str, str] | None:
    """Refuse a memory write the model can see refused: an unresolved space, or
    a tainted turn. ``None`` lets the call run."""
    if session_id:
        try:
            tainted = SESSION_TAINT.is_tainted(session_id)
        except Exception:  # noqa: BLE001 — an unreadable taint state refuses
            tainted = True
        if tainted:
            return {"action": "block", "message": _TAINTED.format(tool=tool_name)}
    if resolve(session_id) is None:
        return {"action": "block", "message": _UNRESOLVED.format(tool=tool_name)}
    return None


def write(
    client: Any,
    *,
    customer_slug: str,
    tool_name: str,
    args: dict[str, Any],
    session_id: str,
    stashed_peer: str | None,
    persona: str,
    status: Any,
) -> str | None:
    """Persist a ``remember`` or a preference into the turn's space; return the
    row id, or None when nothing was written (and why is logged)."""
    if status in ("blocked", "error"):
        logger.info("hermes-smd-peer-memory: %s not written (call %s)", tool_name, status)
        return None
    if session_id and SESSION_TAINT.is_tainted(session_id):
        logger.warning("hermes-smd-peer-memory: %s refused on tainted session", tool_name)
        return None
    space = resolve(session_id)
    if space is None:
        logger.warning("hermes-smd-peer-memory: %s refused, space unresolved", tool_name)
        return None
    if tool_name == REMEMBER_TOOL:
        fact, error = store.parse_remember_args(args)
        if error or fact is None:
            logger.warning("hermes-smd-peer-memory: remember rejected: %s", error)
            return None
        return store.record_preference(
            client,
            customer_slug=customer_slug,
            peer_id=stashed_peer or "",
            persona_slug=persona,
            preference=fact,
            why=None,
            how_to_apply=None,
            source="stated",
            session_id=session_id,
            space=space.key,
            kind="memory",
        )
    if not stashed_peer:
        logger.warning("hermes-smd-peer-memory: preference skipped, no speaker on this turn")
        return None
    clean, error = store.parse_capture_args(args)
    if error or clean is None:
        logger.warning("hermes-smd-peer-memory: capture rejected: %s", error)
        return None
    return store.record_preference(
        client,
        customer_slug=customer_slug,
        peer_id=stashed_peer,
        persona_slug=persona,
        preference=clean["preference"],
        why=clean["why"],
        how_to_apply=clean["how_to_apply"],
        source=clean["source"],
        session_id=session_id,
        space=space.key,
        kind="preference",
    )


def remember_tool(args: dict[str, Any], **_: Any) -> str:
    """Validate and acknowledge; the write happens in ``post_tool_call``, which
    holds the session id the space is resolved from."""
    fact, error = store.parse_remember_args(args if isinstance(args, dict) else {})
    if error:
        return json.dumps({"ok": False, "error": error})
    return json.dumps({"ok": True, "fact": fact})


__all__ = [
    "REMEMBER_DESCRIPTION",
    "REMEMBER_SCHEMA",
    "REMEMBER_TOOL",
    "context_block",
    "pre_tool_block",
    "remember_tool",
    "resolve",
    "speaker",
    "write",
]
