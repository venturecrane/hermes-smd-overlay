"""hermes-smd-space-delivery — a couples routine reaches its people by email.

ADR 0091. Until a person's (or the shared group's) Telegram chat is bound, a
space-bound routine is materialized with Hermes ``deliver: local``, so Hermes
delivers it nowhere. This plugin delivers it by email. The work lives in
:mod:`shared.space_delivery`.

Both callbacks only HAND OFF to that module's worker and return at once.
Hermes skips a bounded hook callback that is still running from ANOTHER
session (``hermes_cli/plugins_dispatch.py`` ``_run_hook_callback_bounded``,
keyed per callback for the whole process), so a callback that sent the email
itself silently dropped the second of two routines due the same minute
(hermes-captain-mark, 2026-10-19). ``on_session_end`` (fired after
``post_llm_call`` in the same turn) lets the worker make a routine that owed
an email and sent none LOUD.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import space_delivery

logger = logging.getLogger(__name__)


def on_post_llm_call(**kwargs: Any) -> None:
    """Hand a run's final response to the delivery worker (returns at once)."""
    try:
        space_delivery.note_response(kwargs.get("session_id"), kwargs.get("assistant_response"))
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.error("space-delivery: post_llm_call hand-off failed", exc_info=True)
    return None


def on_session_end(**kwargs: Any) -> None:
    """Hand the turn's end to the worker, which makes an undelivered routine loud."""
    try:
        space_delivery.note_turn_end(
            kwargs.get("session_id"),
            completed=kwargs.get("completed"),
            failed=kwargs.get("failed"),
            interrupted=kwargs.get("interrupted"),
        )
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.error("space-delivery: on_session_end hand-off failed", exc_info=True)
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
