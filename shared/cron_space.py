"""Where a space-bound cron routine delivers (ADR 0091, couples role).

On a space-mode seat (customer.yaml authors ``people:``) every routine names a
``space``: ``person:<id>`` or ``shared``. The materializer turns it into the job's
Hermes ``deliver`` target, and that target IS the run's space: Hermes publishes
``deliver`` per job as ``HERMES_CRON_AUTO_DELIVER_PLATFORM`` / ``_CHAT_ID``
(``cron/scheduler.py`` ``_reload_dotenv_and_publish_delivery_target``), and
``shared.space.resolve_space`` maps that chat back to the space (resolution
step 1). There is no second marker to keep in step.

* ``person:<id>`` delivers to ``telegram:<telegram_id>`` (a person's DM chat id
  is their user id); ``shared`` to ``telegram:<spaces.shared_telegram_chat>``.
  The chat comes from ``SpaceConfig.chat_for_space``, the resolver's own map.
* A target not bound yet (an empty id during onboarding) delivers ``local`` with
  a WARNING, never to a guessed chat. Its run then resolves to no space and
  every space fence fails closed.

The space is also appended to the job's managed name
(``op-managed:<persona>:<skill>@<space>``) so the same routine can run once per
partner. That suffix is for uniqueness only; nothing reads a space from it.
"""

from __future__ import annotations

import logging
from typing import Any

from shared import space as _space

logger = logging.getLogger(__name__)

LOCAL = "local"
TELEGRAM = "telegram"

# Separates a managed name from its space. Skill slugs never contain it.
SPACE_SEP = "@"


class CronSpaceError(ValueError):
    """An authored cron space cannot be resolved (fail-closed)."""


def space_config(customer: dict[str, Any]) -> _space.SpaceConfig | None:
    """The seat's space config, or ``None`` when it authors no ``people``."""
    try:
        return _space.parse_space_config(customer)
    except _space.SpaceConfigError as exc:
        raise CronSpaceError(f"people/spaces are malformed: {exc}") from exc


def delivery_for(raw_space: Any, cfg: _space.SpaceConfig) -> tuple[str, str]:
    """``(space_key, deliver)`` for an authored cron ``space``.

    Raises :class:`CronSpaceError` unless it is ``shared`` or ``person:<id>``
    naming an authored person. ``deliver`` is ``telegram:<chat>`` when bound,
    else ``local`` with a WARNING."""
    resolved = cfg.space_for_key(str(raw_space or "").strip(), source="cron")
    if resolved is None:
        raise CronSpaceError(
            f"cron space {raw_space!r} is not 'shared' or 'person:<id>' naming an id in people"
        )
    chat = cfg.chat_for_space(resolved)
    if not chat:
        logger.warning(
            "cron_space: the %s chat is not bound yet; its routine delivers locally until it is",
            resolved.key,
        )
        return resolved.key, LOCAL
    return resolved.key, f"{TELEGRAM}:{chat}"


def name_with_space(base_name: str, space_key: str | None) -> str:
    """Append the space to a managed name; no space means the name is unchanged."""
    return f"{base_name}{SPACE_SEP}{space_key}" if space_key else base_name


def split_name(name: str) -> tuple[str, str | None]:
    """``(base_name, space_key)`` for a managed name; ``space_key`` is ``None`` when absent."""
    base, sep, key = str(name or "").partition(SPACE_SEP)
    return (base, key or None) if sep else (base, None)
