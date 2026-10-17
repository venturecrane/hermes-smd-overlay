"""hermes-smd-group-audience — tell the shared group when outsiders can read it.

ADR 0091 (privacy by space), plan revision R7: the group answers only the
people the seat serves, and warns when other members are present. Hermes'
``group_allow_from`` already keeps outsiders from getting an answer; this
plugin covers the other half, that an outsider can still READ the group.

The work lives in :mod:`shared.group_audience`. The hook only decides whether
this turn is a shared-space Telegram turn and hands the check to a background
thread, so a slow or failing Bot API call never delays or fails the turn.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import group_audience

logger = logging.getLogger(__name__)


def on_pre_llm_call(**kwargs: Any) -> None:
    """Schedule the audience check on a shared-group turn. Injects nothing."""
    try:
        group_audience.on_turn(kwargs.get("session_id"))
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("group-audience: pre_llm_call failed", exc_info=True)
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
