"""Where a space-bound cron routine delivers (ADR 0091, couples role).

On a space-mode seat (customer.yaml authors ``people:``) every routine names a
``space``: ``person:<id>`` or ``shared``.

**Telegram, when bound.** The materializer turns the space into the job's Hermes
``deliver`` target, and that target IS the run's space: Hermes publishes
``deliver`` per job as ``HERMES_CRON_AUTO_DELIVER_PLATFORM`` / ``_CHAT_ID``
(``cron/scheduler.py`` ``_reload_dotenv_and_publish_delivery_target``), and
``shared.space.resolve_space`` maps that chat back to the space (resolution
step 1). ``person:<id>`` delivers to ``telegram:<telegram_id>`` (a person's DM
chat id is their user id); ``shared`` to ``telegram:<spaces.shared_telegram_chat>``.
The chat comes from ``SpaceConfig.chat_for_space``, the resolver's own map.

**Email, until then.** A target not bound yet delivers ``local`` in Hermes,
never to a guessed chat, and the run's final response goes by EMAIL instead
(``hermes-smd-space-delivery``): ``person:<id>`` to that person's
``people[].email``, ``shared`` as ONE email to every person. Hermes at the pinned
ref has no email delivery this seat could use (its ``email`` platform is SMTP,
which no seat configures; seats mail through the AgentMail broker), so the send
is the overlay's, through ``shared.send_dispatch`` on the broker's ``space``
lane, which admits exactly ``people[].email``.

**The space of an email-delivered run.** With ``deliver: local`` Hermes
publishes no target, so the delivery target cannot carry the space. The job's
managed name does: ``op-managed:<persona>:<skill>@<space>``, authored by the
materializer and unwritable by the model (the ``cronjob`` tool is
CODE_EXECUTION, which the couples persona does not have).
:func:`routine_space_key` reads it back from the run's ``cron_<job_id>_...``
session id; the resolver falls back to it when no Telegram target was published.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    else ``local`` (the run then goes by email, :func:`email_recipients`)."""
    resolved = cfg.space_for_key(str(raw_space or "").strip(), source="cron")
    if resolved is None:
        raise CronSpaceError(
            f"cron space {raw_space!r} is not 'shared' or 'person:<id>' naming an id in people"
        )
    chat = cfg.chat_for_space(resolved)
    if not chat:
        logger.warning(
            "cron_space: the %s chat is not bound yet; its routine delivers by email until it is",
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


def email_recipients(space_key: str, cfg: _space.SpaceConfig) -> tuple[str, ...]:
    """Who an email-delivered run in ``space_key`` reaches: that person's
    ``people[].email``, or every person's for ``shared``. ``()`` for a key that
    names nobody authored. Code decides this; nothing a model wrote does."""
    resolved = cfg.space_for_key(space_key, source="cron")
    if resolved is None:
        return ()
    if resolved.kind == "shared":
        return tuple(p.email for p in cfg.people if p.email)
    person = cfg.person(resolved.person_id or "")
    return (person.email,) if person is not None and person.email else ()


@dataclass(frozen=True)
class EmailRun:
    """A cron run whose final response goes by email (Telegram unbound)."""

    space_key: str
    skill: str
    recipients: tuple[str, ...]


def routine_space_key(session_id: Any, *, hermes_home: str | None = None) -> str | None:
    """The space a cron run's job was materialized for, from its managed name.
    ``None`` for a non-cron session, an unmanaged job, or a job with no space.
    Never raises."""
    try:
        from shared import cron_attribution

        routine = cron_attribution.resolve_routine(str(session_id or ""), hermes_home=hermes_home)
        if routine is None or not routine.job_name.startswith("op-managed:"):
            return None
        return split_name(routine.job_name)[1]
    except Exception:  # noqa: BLE001 - no space is the fail-closed answer
        logger.warning("cron_space: routine space unresolved", exc_info=True)
        return None


def email_run(
    session_id: Any, cfg: _space.SpaceConfig, *, hermes_home: str | None = None
) -> EmailRun | None:
    """The email delivery this cron run owes, or ``None``.

    Owed only when every one holds: the session is a managed cron run with a
    space, the job's Hermes ``deliver`` is ``local`` (Telegram not bound when it
    was materialized; a Telegram job is Hermes' to deliver, never twice), and the
    space names at least one authored address. Never raises."""
    try:
        from shared import cron_attribution

        routine = cron_attribution.resolve_routine(str(session_id or ""), hermes_home=hermes_home)
        if routine is None or not routine.job_name.startswith("op-managed:") or not routine.skill:
            return None
        key = split_name(routine.job_name)[1]
        if not key or (routine.deliver or LOCAL) != LOCAL:
            return None
        recipients = email_recipients(key, cfg)
        if not recipients:
            logger.warning("cron_space: %s routine has no authored email to deliver to", key)
            return None
        return EmailRun(space_key=key, skill=routine.skill, recipients=recipients)
    except Exception:  # noqa: BLE001 - an undecidable delivery is no delivery
        logger.warning("cron_space: email delivery unresolved", exc_info=True)
        return None
