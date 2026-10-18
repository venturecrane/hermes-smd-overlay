"""hermes-smd-space-delivery — a couples routine reaches its people by email.

ADR 0091. Until a person's (or the shared group's) Telegram chat is bound, a
space-bound routine is materialized with Hermes ``deliver: local``, so Hermes
delivers it nowhere. This plugin delivers it by email from ``post_llm_call``,
which fires once per turn with the final response (``docs/hook-surface.md``
section 4). The work lives in :mod:`shared.space_delivery`.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import space_delivery

logger = logging.getLogger(__name__)


def on_post_llm_call(**kwargs: Any) -> None:
    """Email a cron run's final response when its space has no Telegram yet."""
    try:
        space_delivery.deliver(kwargs.get("session_id"), kwargs.get("assistant_response"))
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("space-delivery: post_llm_call failed", exc_info=True)
    return None


def register(ctx: Any) -> None:
    ctx.register_hook("post_llm_call", on_post_llm_call)
