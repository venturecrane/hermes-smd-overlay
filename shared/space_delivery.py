"""Email delivery for a space-bound cron run while Telegram is unbound (ADR 0091).

A couples seat must work fully over email before anyone binds Telegram. A
routine whose space has no Telegram chat yet is materialized with Hermes
``deliver: local`` (``shared.cron_space``), and its final response comes here:

* ``person:<id>``: one email to that person's ``people[].email``;
* ``shared``: ONE email to every person.

WHO IS DECIDED BY CODE, TWICE. :func:`shared.cron_space.email_run` derives the
recipients from the job's managed name and the seat's ``people:``; the model's
text is the body and nothing else. The send rides the broker's ``space`` lane,
which admits exactly ``people[].email`` (ss-console ``space_people.py``), so even
a caller here that named someone else would be refused by the process holding
the key. Everything else about the send (ceilings, taint, content floor,
fabrication scan, the audit row) is the seat's ordinary gated path,
``shared.send_dispatch``.

Never raises: a failed delivery is logged with its reason, and the run's output
is still saved locally by Hermes.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from typing import Any

from shared import cron_space, send_anchor, send_dispatch, silence
from shared import space as _space

logger = logging.getLogger(__name__)

#: Fixed frame used only when the response has no usable first line.
FALLBACK_SUBJECT = "A note from your assistant"
_SUBJECT_MAX = 78

_lock = threading.Lock()
_delivered: OrderedDict[str, None] = OrderedDict()
_MAX_REMEMBERED = 256

#: One silence rule for every final-response consumer (``shared.silence``).
_is_silent = silence.is_silent


def subject_for(text: str) -> str:
    """The first non-empty line, stripped of markdown markers, cut to 78 chars."""
    for line in text.splitlines():
        cleaned = re.sub(r"^[#>*\-\s]+", "", line).replace("**", "").strip()
        if cleaned:
            return cleaned if len(cleaned) <= _SUBJECT_MAX else cleaned[: _SUBJECT_MAX - 3] + "..."
    return FALLBACK_SUBJECT


def _claim(session_id: str) -> bool:
    """True the first time a session is delivered in this process."""
    with _lock:
        if session_id in _delivered:
            return False
        _delivered[session_id] = None
        while len(_delivered) > _MAX_REMEMBERED:
            _delivered.popitem(last=False)
        return True


def deliver(
    session_id: Any,
    response: Any,
    *,
    config: _space.SpaceConfig | None = None,
    hermes_home: str | None = None,
) -> send_dispatch.DispatchResult | None:
    """Email this cron run's final response when it owes one. ``None`` when it
    owes none (not a space-mode seat, not an email-delivered run, silence, or
    already delivered). Never raises."""
    try:
        sid = str(session_id or "")
        text = response if isinstance(response, str) else ""
        cfg = config if config is not None else _space.space_config()
        if cfg is None or not sid:
            return None
        run = cron_space.email_run(sid, cfg, hermes_home=hermes_home)
        if run is None or not text.strip() or _is_silent(text):
            return None
        if not _claim(sid):
            return None
        result = send_dispatch.dispatch(
            to=list(run.recipients),
            subject=subject_for(text),
            text=text,
            # A routine answers no email; the lane is what admits the people,
            # named here and nowhere else.
            anchor=None,
            lane=send_anchor.LANE_SPACE,
            session_id=sid,
            templated=False,
            audit_extra={"skill_name": run.skill},
        )
        if result.sent:
            logger.info("space_delivery: %s routine delivered by email", run.space_key)
        else:
            logger.warning(
                "space_delivery: %s routine NOT delivered by email: %s",
                run.space_key,
                result.reason,
            )
        return result
    except Exception:  # noqa: BLE001 - delivery never raises into a hook
        logger.warning("space_delivery: delivery failed", exc_info=True)
        return None


def _reset_for_tests() -> None:
    with _lock:
        _delivered.clear()
