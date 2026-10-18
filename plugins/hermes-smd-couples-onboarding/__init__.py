"""hermes-smd-couples-onboarding — the seat half of couples onboarding.

ss-console ADR 0090 decision 4 and ADR 0091 s5 (plan R4). Two jobs, both on a
seat that authors ``people:`` (space mode) and nowhere else:

1. TELEGRAM BINDING, at ``pre_gateway_dispatch`` (Hermes runs it BEFORE
   authorization, ``gateway/run_inbound.py`` ``_hm_admit_event``). A Telegram DM
   from a sender who is not an authored person:

   * ``/start <token>``: the token is spooled for the gate to redeem at
     ss-console (``shared.telegram_bind``), the sender gets one fixed reply
     saying the link was received, and the message is dropped;
   * anything else: dropped silently.

   Either way the message never reaches a turn, and nothing here admits the
   sender: the allowlist is authored config, changed only by ss-console's
   generator through a reviewed PR and a restart. Dropping every unknown DM is
   also what keeps Hermes' pairing flow from ever answering one: translate
   authors ``unauthorized_dm_behavior: pair`` on a space-mode seat so the
   Telegram adapter forwards unknown DMs to this hook at all
   (``_is_user_authorized_from_message``), and this hook's ``skip`` comes before
   the runner's pairing offer.

2. ONBOARDING RECORDS, at register (boot): the joint answers both partners
   confirmed are seeded into the records the couples skills read
   (``shared.couples_seed``).

Hook callbacks are exception-safe per AGENTS.md hard rule #3. On an internal
error the hook returns ``None``: the runner's own allowlist still refuses the
sender.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared import couples_seed, space, telegram_bind
from shared.customer_config import CustomerConfig, CustomerConfigError

logger = logging.getLogger(__name__)

_SKIP_REASON = "couples-onboarding: not an authored person"
#: Strong references to in-flight reply tasks so they are not collected early.
_REPLIES: set[asyncio.Task[Any]] = set()


def _platform(source: Any) -> str:
    platform = getattr(source, "platform", None)
    return str(getattr(platform, "value", platform) or "")


def _reply(gateway: Any, source: Any, text: str) -> None:
    """Send the fixed reply without blocking dispatch. Best effort."""
    try:
        adapter = gateway._adapter_for_source(source)
        if adapter is None:
            return
        task = asyncio.get_running_loop().create_task(adapter.send(source.chat_id, text))
        _REPLIES.add(task)
        task.add_done_callback(_REPLIES.discard)
    except Exception:  # noqa: BLE001 — the bind is spooled either way
        logger.warning("couples-onboarding: confirmation reply not sent", exc_info=True)


def on_pre_gateway_dispatch(**kwargs: Any) -> dict[str, Any] | None:
    try:
        event = kwargs.get("event")
        source = getattr(event, "source", None)
        if source is None or _platform(source) != "telegram":
            return None
        if getattr(source, "chat_type", "") != "dm":
            return None  # groups: the adapter's group_allow_from fence decides
        cfg = space.space_config()
        if cfg is None:
            return None
        user_id = str(getattr(source, "user_id", "") or "").strip()
        if cfg.person_for_telegram(user_id) is not None:
            return None  # an authored person: normal dispatch
        token = telegram_bind.start_token(getattr(event, "text", None))
        if token and telegram_bind.spool_start(token, user_id):
            _reply(kwargs.get("gateway"), source, telegram_bind.CONFIRMATION_TEXT)
        return {"action": "skip", "reason": _SKIP_REASON}
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("couples-onboarding: dispatch check failed", exc_info=True)
        return None


def register(ctx: Any) -> None:
    """Register on a space-mode seat only; seed the onboarding records."""
    try:
        config = CustomerConfig.from_volume()
    except CustomerConfigError as exc:
        logger.info("couples-onboarding: customer.yaml unreadable (%s); nothing registered", exc)
        return
    try:
        if not space.space_mode(config):
            return
    except space.SpaceConfigError:
        logger.warning("couples-onboarding: people/spaces unreadable; nothing registered")
        return
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    written = couples_seed.seed(config.raw)
    logger.info("couples-onboarding registered; seeded %d onboarding record(s)", len(written))


__all__ = ["on_pre_gateway_dispatch", "register"]
