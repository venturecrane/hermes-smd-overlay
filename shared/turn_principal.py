"""Who is speaking on this turn, and from which chat (ADR 0071 / #1806 binding).

A withheld send is bound to the person who raised it and the chat they raised
it in, and an approval is accepted only from that same person in that same chat.
Before this binding the register held one send for the whole Machine and any
allowlisted Telegram sender's bare "send it" released it: on a seat with two
allowlisted principals (a couple), one partner could approve the other's send,
and from a group chat.

The identity comes from Hermes' per-turn session context, never from the model:
the gateway binds ``HERMES_SESSION_*`` as task-local ContextVars for every
inbound message (``gateway/session_context.py`` ``set_session_vars``, bound in
``gateway/run.py`` ``_set_session_env``), and they propagate into tool worker
threads (``tools.thread_context.propagate_context_to_thread``). Outside the
gateway (unit tests, a CLI) ``get_session_env`` is unimportable or unbound and
the same names are read from ``os.environ``, which is also its own fallback.

Three shapes, decided where the send is HELD:

* a human Telegram turn binds ``user_id`` + ``chat_id`` (+ ``chat_type``);
* a cron turn that delivers to a Telegram chat binds that chat only — the
  routine raised the send, and the notice that it is held lands in that chat;
* anything else (no session context, an email-webhook turn) binds nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TELEGRAM = "telegram"


@dataclass(frozen=True)
class Principal:
    """A turn's speaker and chat. Empty strings mean "not known on this turn"."""

    platform: str = ""
    user_id: str = ""
    chat_id: str = ""
    chat_type: str = ""

    @property
    def bound(self) -> bool:
        return bool(self.user_id or self.chat_id)


UNBOUND = Principal()


def _session_env(name: str) -> str:
    """One ``HERMES_SESSION_*`` value for this turn: the gateway ContextVar when
    Hermes is importable, else ``os.environ``. Never raises."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — outside the gateway, env is the source
        return str(os.environ.get(name, "") or "").strip()
    try:
        return str(get_session_env(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable context is an unknown one
        return ""


def current() -> Principal:
    """The speaker and chat of the turn running now (approval time)."""
    return Principal(
        platform=_session_env("HERMES_SESSION_PLATFORM").lower(),
        user_id=_session_env("HERMES_SESSION_USER_ID"),
        chat_id=_session_env("HERMES_SESSION_CHAT_ID"),
        chat_type=_session_env("HERMES_SESSION_CHAT_TYPE").lower(),
    )


def requester() -> Principal:
    """Who a send held on THIS turn is bound to (capture time). See module doc."""
    turn = current()
    if turn.platform == _TELEGRAM and turn.user_id:
        return turn
    if _session_env("HERMES_CRON_SESSION") == "1":
        platform = _session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM").lower()
        chat_id = _session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID")
        if platform == _TELEGRAM and chat_id:
            return Principal(platform=_TELEGRAM, chat_id=chat_id)
    return UNBOUND
