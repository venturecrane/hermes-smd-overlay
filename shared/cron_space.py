"""The space a cron routine runs in, and where it delivers (ADR 0091, couples role).

On a space-mode seat (customer.yaml authors ``people:``) every routine names a
``space``: ``person:<id>`` or ``shared``. That one authored value decides two
things, and this module is the single place both are derived from it:

* **Delivery.** ``person:<id>`` delivers to that person's Telegram DM, whose chat
  id is their ``telegram_id``; ``shared`` delivers to
  ``spaces.shared_telegram_chat``. Hermes takes an explicit ``telegram:<chat_id>``
  target (``cron/scheduler_delivery.py`` ``_resolve_delivery_target``). A target
  not yet bound (an empty id during onboarding) delivers ``local`` with a
  WARNING, never to a guessed chat.
* **The run's bound space.** The space rides in the job's managed name,
  ``op-managed:<persona>:<skill>@<space>``. The model cannot write a job (the
  ``cronjob`` tool is CODE_EXECUTION, which the couples persona does not have),
  so the name is authored state. :func:`bound_cron_space` reads it back for the
  space resolver (``shared/space.py`` resolution step 1, "cron run").

Hermes clears ``HERMES_SESSION_ID`` on a cron run (``cron/scheduler.py``
``_CronRunScope``), so the job is found from the hook's ``session_id``
(``cron_<job_id>_<date>_<time>``) through :mod:`shared.cron_attribution`. With
no session id, the run's own delivery target (``HERMES_CRON_AUTO_DELIVER_*``,
published per job by the scheduler) is mapped back through ``people`` and
``spaces``. When both resolve and disagree the answer is ``None``: the resolver
fails closed on ``None``.

Seats without ``people`` never reach any of this: their names carry no suffix and
their jobs carry no ``deliver`` key, so the cron store stays byte-identical.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

SHARED = "shared"
PERSON_PREFIX = "person:"
LOCAL = "local"
TELEGRAM = "telegram"

# Separates a managed name from its space. Skill slugs never contain it.
SPACE_SEP = "@"

_PERSON_ID_RE = re.compile(r"^[a-z0-9-]+$")


class CronSpaceError(ValueError):
    """An authored cron space cannot be resolved (fail-closed)."""


def space_mode(customer: dict[str, Any]) -> bool:
    """A seat is in space mode when it authors a non-empty ``people`` list."""
    people = customer.get("people")
    return isinstance(people, list) and len(people) > 0


def _people_by_id(customer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for person in customer.get("people") or []:
        if isinstance(person, dict):
            pid = str(person.get("id") or "").strip()
            if pid:
                out[pid] = person
    return out


def _shared_chat(customer: dict[str, Any]) -> str:
    spaces = customer.get("spaces")
    if not isinstance(spaces, dict):
        return ""
    return str(spaces.get("shared_telegram_chat") or "").strip()


def normalize_space(raw: Any, customer: dict[str, Any]) -> str:
    """Return the canonical space string for an authored cron ``space``.

    Raises :class:`CronSpaceError` for anything that is not ``shared`` or
    ``person:<id>`` naming a person in ``people``. A guess is never made."""
    value = str(raw or "").strip()
    if value == SHARED:
        return SHARED
    if value.startswith(PERSON_PREFIX):
        pid = value[len(PERSON_PREFIX) :]
        if _PERSON_ID_RE.match(pid) and pid in _people_by_id(customer):
            return PERSON_PREFIX + pid
    raise CronSpaceError(
        f"cron space {value!r} is not 'shared' or 'person:<id>' naming an id in people"
    )


def delivery_for(space: str, customer: dict[str, Any]) -> str:
    """The Hermes ``deliver`` value for a canonical space.

    ``telegram:<chat_id>`` when the target is bound; ``local`` (with a WARNING)
    when the person's ``telegram_id`` or the shared chat is still empty."""
    if space == SHARED:
        chat = _shared_chat(customer)
        label = "spaces.shared_telegram_chat"
    elif space.startswith(PERSON_PREFIX):
        pid = space[len(PERSON_PREFIX) :]
        person = _people_by_id(customer).get(pid)
        if person is None:
            raise CronSpaceError(f"cron space {space!r} names no person in people")
        chat = str(person.get("telegram_id") or "").strip()
        label = f"people[{pid}].telegram_id"
    else:
        raise CronSpaceError(f"cron space {space!r} is not canonical")
    if not chat:
        logger.warning(
            "cron_space: %s is not bound yet; the %s routine delivers locally until it is",
            label,
            space,
        )
        return LOCAL
    return f"{TELEGRAM}:{chat}"


def name_with_space(base_name: str, space: str | None) -> str:
    """Append the space to a managed name; no space means the name is unchanged."""
    return f"{base_name}{SPACE_SEP}{space}" if space else base_name


def split_name(name: str) -> tuple[str, str | None]:
    """``(base_name, space)`` for a managed name; ``space`` is ``None`` when absent."""
    base, sep, space = str(name or "").partition(SPACE_SEP)
    return (base, space or None) if sep else (base, None)


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — outside the gateway, env is the source
        return str(os.environ.get(name, "") or "").strip()
    try:
        return str(get_session_env(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable context is an unknown one
        return ""


def space_for_delivery(platform: str, chat_id: str, customer: dict[str, Any]) -> str | None:
    """Map a cron run's delivery chat back to its space, or ``None``."""
    if platform.lower() != TELEGRAM or not chat_id:
        return None
    matches: set[str] = set()
    if chat_id == _shared_chat(customer):
        matches.add(SHARED)
    for pid, person in _people_by_id(customer).items():
        if chat_id == str(person.get("telegram_id") or "").strip():
            matches.add(PERSON_PREFIX + pid)
    return matches.pop() if len(matches) == 1 else None


def _load_customer() -> dict[str, Any] | None:
    try:
        from shared.customer_config import CustomerConfig

        data = CustomerConfig.from_volume()._data
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — no config is no space
        logger.warning("cron_space: customer config unreadable", exc_info=True)
        return None


def bound_cron_space(
    session_id: str | None = None,
    *,
    customer: dict[str, Any] | None = None,
    hermes_home: str | None = None,
) -> str | None:
    """The space this cron run is bound to, or ``None``. Never raises.

    ``None`` for a non-cron turn, a job with no authored space, a seat not in
    space mode, or two evidence paths that disagree."""
    try:
        if customer is None:
            customer = _load_customer()
        if not customer or not space_mode(customer):
            return None

        from_name: str | None = None
        named = False
        if session_id:
            from shared import cron_attribution

            if cron_attribution.parse_cron_session(session_id) is not None:
                named = True
                routine = cron_attribution.resolve_routine(session_id, hermes_home=hermes_home)
                if routine is None or not routine.space:
                    return None
                from_name = normalize_space(routine.space, customer)

        from_delivery: str | None = None
        if _session_env("HERMES_CRON_SESSION") == "1":
            from_delivery = space_for_delivery(
                _session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM"),
                _session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID"),
                customer,
            )

        if named:
            if from_delivery is not None and from_delivery != from_name:
                logger.warning(
                    "cron_space: job space %s disagrees with delivery space %s; unbound",
                    from_name,
                    from_delivery,
                )
                return None
            return from_name
        return from_delivery
    except Exception:  # noqa: BLE001 — an unresolved space fails closed downstream
        logger.warning("cron_space: bound space unresolved", exc_info=True)
        return None
