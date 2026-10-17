"""Warn the shared group once when people outside ``people:`` can read it (ADR 0091, R7).

The couples role answers only the people it serves. Hermes already drops a group
message from anyone outside ``telegram.group_allow_from`` (verified against the
pinned adapter, ``vfy_01M55VAXT7KCFM46758VTYHE07``). Dropping a message does not
stop someone from reading the group, though: a third member sees everything the
agent posts to the shared space. This module tells the group, once.

Mechanism:

* On a turn in the shared space (``shared.space.resolve_space`` returns kind
  ``shared`` from the Telegram path; cron runs never trigger it), at most once
  per :data:`CHECK_INTERVAL_S` per process, ask the Bot API
  ``getChatMemberCount`` for the shared chat.
* The expected count is ``len(people) + 1`` (the bot). Above that, post one
  plain notice with ``sendMessage``.
* The count it warned at is recorded in a state file under ``HERMES_HOME``. The
  notice is not repeated for the same count; it goes again only if the count
  rises above the last one warned at (a new outsider joined).

The notice is fixed text, never model output, and goes only to the authored
shared chat. API and file errors are logged and swallowed: this is a courtesy
notice, and it must never fail or delay a turn (the caller runs it off-thread).

Uses ``urllib`` rather than ``httpx`` deliberately: httpx logs every request
URL at INFO, and a Bot API URL carries the token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shared import space as _space
from shared.secrets import get_secret

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 600.0
_TIMEOUT_S = 10.0
_API = "https://api.telegram.org"
_METHODS = frozenset({"getChatMemberCount", "sendMessage"})
_STATE_FILE = "smd-state/group-audience.json"

NOTICE = (
    "A note for this group: this assistant answers only the people it works for. "
    "Anyone else in this group can still read everything shared here."
)

# One process, one tenant (AGENTS.md hard rule #5). Guards the check interval.
_lock = threading.Lock()
_last_check: dict[str, float] = {}

BotCall = Callable[[str, dict[str, str]], Any]


def _bot_call(method: str, params: dict[str, str]) -> Any:
    """POST one Bot API method and return its ``result``. Raises on any failure.

    The token never reaches a log: errors are re-raised without the URL."""
    token = get_secret("TELEGRAM_BOT_TOKEN").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    if method not in _METHODS:
        raise ValueError(f"Bot API method {method!r} is not one this module calls")
    data = urllib.parse.urlencode(params).encode()
    # The scheme and host are the constant _API; only the token and an allowlisted
    # method name vary, so no caller can steer this to file:// or another host.
    req = urllib.request.Request(f"{_API}/bot{token}/{method}", data=data, method="POST")
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — strip the URL (it holds the token)
        raise RuntimeError(f"Bot API {method} failed: {type(exc).__name__}") from None
    if not isinstance(body, dict) or not body.get("ok"):
        desc = body.get("description") if isinstance(body, dict) else None
        raise RuntimeError(f"Bot API {method} returned not ok: {desc}")
    return body.get("result")


def _state_path(hermes_home: str | None = None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or "/opt/data"
    return Path(home) / _STATE_FILE


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("group_audience: state file unreadable; treating as empty")
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def check_and_warn(
    cfg: _space.SpaceConfig,
    *,
    bot_call: BotCall = _bot_call,
    hermes_home: str | None = None,
) -> bool:
    """Count the shared group's members and warn once if outsiders are present.

    Returns True when a notice was sent. Never raises."""
    chat = cfg.shared_telegram_chat
    if not chat:
        return False
    try:
        count = int(bot_call("getChatMemberCount", {"chat_id": chat}))
    except Exception as exc:  # noqa: BLE001 — courtesy notice, never a failure
        logger.warning("group_audience: member count unavailable: %s", exc)
        return False
    expected = len(cfg.people) + 1
    if count <= expected:
        return False
    path = _state_path(hermes_home)
    state = _read_state(path)
    prior = (
        state.get(chat, {}).get("warned_at_count") if isinstance(state.get(chat), dict) else None
    )
    if isinstance(prior, int) and count <= prior:
        return False
    try:
        bot_call("sendMessage", {"chat_id": chat, "text": NOTICE})
    except Exception as exc:  # noqa: BLE001
        logger.warning("group_audience: notice not sent: %s", exc)
        return False
    state[chat] = {"warned_at_count": count, "expected": expected, "warned_at": time.time()}
    try:
        _write_state(path, state)
    except OSError as exc:
        logger.warning("group_audience: notice sent but not recorded: %s", exc)
    logger.info(
        "group_audience: shared group has %d members, %d expected; notice sent", count, expected
    )
    return True


def _due(chat: str, now: float) -> bool:
    with _lock:
        last = _last_check.get(chat)
        if last is not None and now - last < CHECK_INTERVAL_S:
            return False
        _last_check[chat] = now
        return True


def on_turn(
    session_id: Any = None,
    *,
    run: Callable[[Callable[[], Any]], None] | None = None,
    bot_call: BotCall = _bot_call,
    hermes_home: str | None = None,
) -> bool:
    """Hook entry: schedule a check when this turn is in the shared Telegram space.

    Returns True when a check was scheduled. Seats without ``people`` return
    False before anything else happens. Never raises."""
    try:
        cfg = _space.space_config()
        if cfg is None or not cfg.shared_telegram_chat:
            return False
        resolved = _space.resolve_space(session_id, config=cfg)
        # The group path resolves only the authored shared chat (any other group
        # is None), so a group-sourced space is the shared one. Cron runs that
        # deliver to the group resolve with SOURCE_CRON and are excluded here.
        if resolved is None or resolved.source != _space.SOURCE_TELEGRAM_GROUP:
            return False
        if not _due(cfg.shared_telegram_chat, time.monotonic()):
            return False

        def job() -> None:
            check_and_warn(cfg, bot_call=bot_call, hermes_home=hermes_home)

        if run is None:
            threading.Thread(target=job, name="smd-group-audience", daemon=True).start()
        else:
            run(job)
        return True
    except Exception:  # noqa: BLE001 — never fail a turn over a courtesy notice
        logger.warning("group_audience: check not scheduled", exc_info=True)
        return False


def _reset_for_tests() -> None:
    with _lock:
        _last_check.clear()
