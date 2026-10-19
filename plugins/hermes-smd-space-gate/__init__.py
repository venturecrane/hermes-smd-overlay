"""hermes-smd-space-gate — a write from an unresolved space is refused (ADR 0091).

On a seat a couple shares (``people:`` authored), every turn belongs to a space
(``shared.space``): one partner's own chat or email, or a shared one. Reads are
fenced by space everywhere they happen (the record store, peer memory, the
calendar), so partner B never reads partner A's private space.

Writes are not held. Either partner adds to the shared space, or changes a
setting, alone (Captain decision 2026-10-19, recorded as D-006): a calendar
invite for both, a shared list entry, a shared doc, a message or an email to
the other partner all land as asked. A record written on a turn that read
outside content is stamped ``from_web: true`` by the record-store gate
(``shared.space_taint``).

What this plugin still decides: a call that can write somewhere a person reads
(:func:`is_candidate`), on a turn whose space cannot be resolved, is refused.
It cannot be shown that the write lands where it was asked for. A people block
that cannot be read counts as unresolved, so every candidate is refused.

On a seat that authors no ``people:`` the hook returns at once: this plugin
cannot change any other seat's behavior. The hook is exception-safe; a hook
that cannot decide refuses.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import space as space_mod

logger = logging.getLogger(__name__)

#: Tool names (or prefixes) that can write somewhere a person reads.
RECORD_WRITE = "record_store_write"
SEND_MESSAGE = "send_message"
CALENDAR_INVITE_PREFIX = "calendar_invite"
WORKSPACE_PREFIX = "workspace_"

#: Workspace tools that read. Every other ``workspace_*`` tool is a write.
_WORKSPACE_READS = frozenset(
    {
        "workspace_gmail_search",
        "workspace_gmail_get",
        "workspace_calendar_list",
        "workspace_calendar_get",
        "workspace_drive_list",
        "workspace_drive_get",
        "workspace_drive_export",
        "workspace_docs_get",
        "workspace_sheets_get_values",
    }
)

_UNRESOLVED = (
    "{tool} refused: this conversation's space could not be resolved, so it cannot "
    "be shown that this write lands where it was asked for. Nothing was written."
)


def _is_email_send(name: str) -> bool:
    try:
        from shared.action_classes import ActionClass, classify_tool

        cls = classify_tool(name).action_class
    except Exception:  # noqa: BLE001 — an unclassifiable tool is not a known send
        return False
    return cls in (
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_INTERNAL,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
        ActionClass.EXTERNAL_SEND_AS_STAFF,
    )


def is_candidate(tool_name: str) -> bool:
    """Whether this tool can write somewhere a person reads."""
    name = (tool_name or "").strip().lower()
    return (
        name in (RECORD_WRITE, SEND_MESSAGE)
        or name.startswith(CALENDAR_INVITE_PREFIX)
        or (name.startswith(WORKSPACE_PREFIX) and name not in _WORKSPACE_READS)
        or _is_email_send(name)
    )


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    """Refuse a write candidate on an unresolved space. Exception-safe."""
    tool_name = str(kwargs.get("tool_name") or "")
    try:
        if not is_candidate(tool_name):
            return None
        try:
            cfg = space_mod.space_config()
            if cfg is None:
                return None
            current = space_mod.resolve_space(
                str(kwargs.get("session_id") or "") or None, config=cfg
            )
        except space_mod.SpaceConfigError:
            logger.exception("hermes-smd-space-gate: people block unreadable; space unresolved")
            current = None
        if current is None:
            return {"action": "block", "message": _UNRESOLVED.format(tool=tool_name)}
        return None
    except Exception:  # noqa: BLE001 — a hook that cannot decide refuses
        logger.exception("hermes-smd-space-gate: pre_tool_call failed; refusing %s", tool_name)
        return {"action": "block", "message": _UNRESOLVED.format(tool=tool_name)}


def register(ctx: Any) -> None:
    """Register the hook. It decides space mode per call and returns at once on
    a seat that authors no ``people:``, so registering everywhere keeps the
    manifest honest (``tests/test_hook_parity.py``) without changing any other
    seat's behavior."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    logger.info("hermes-smd-space-gate registered (inert unless people: is authored)")


__all__ = ["is_candidate", "on_pre_tool_call", "register"]
