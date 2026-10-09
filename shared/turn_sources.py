"""Which job's completion wake opened a turn, recorded from the gateway's own facts.

Moved here from ``plugins/hermes-smd-reply/binding.py`` (ss-console participant
fence, 2026-10-07) so every plugin can ask it: the reply binding uses it to pin a
wake to its own job, and the trust plugin's send paths use it as the wake's
participant-fence ANCHOR (``shared.send_anchor``). Plugin directories are not
importable, ``shared`` is; nothing about the record changed in the move.

A job runner's wake names its job in its first line: "... for demand job
<ULID>." (demand-letter-drafter), "... for drafting job <ULID>." (document-
drafter), "... for chronology job <ULID>." (medical-chronology-maintainer) or
"... for litigation job <ULID>." (the litigation-status skill) or "... for
negotiation job <ULID>." (the negotiation-watch skill; the id is a notice's,
one wake per new offer, or a failed job's). The earliest
mention decides the kind. It is recorded at ``pre_llm_call`` from the gateway's
sender id (``webhook:handoff``), never from anything the model writes.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any

from shared.cron_attribution import parse_cron_session

#: The sender id the gateway gives a ``/webhooks/handoff`` turn (the route, as
#: ``webhook:agentmail`` is for mail): a job's completion wake.
HANDOFF_SENDER = "webhook:handoff"
WAKE_JOB = re.compile(
    r"\b(demand|drafting|chronology|litigation|negotiation) job ([0-9A-HJKMNP-TV-Z]{26})\b"
)
#: The wake word -> the broker's job kind (ss-console reply_binding.KINDS, and
#: the participant fence's job anchor kinds).
JOB_KIND = {
    "demand": "demand_job",
    "drafting": "drafting_job",
    "chronology": "medchron_job",
    "litigation": "litigation_job",
    "negotiation": "negotiation_job",
}
JOB_KINDS = frozenset(JOB_KIND.values())
#: The kinds whose wake may be a SCHEDULED run's (no request email).
SCHEDULED_KINDS = frozenset({"litigation_job", "negotiation_job"})
#: A litigation or negotiation wake's trigger line (ss-console
#: litigation_lane.py / negotiation_lane.py, code-authored): "Trigger:
#: scheduled." Read only from a wake the gateway delivered on the handoff
#: route, like the job id itself. A negotiation wake is always scheduled.
WAKE_TRIGGER = re.compile(r"^Trigger: (request|scheduled)\.?\s*$", re.MULTILINE)


class TurnSources:
    """session id -> the handoff wake that opened it, with the job it names: the
    kind (``demand_job`` / ``drafting_job`` / ``medchron_job`` /
    ``litigation_job``) and id, or ("", ""). Cron turns need no record: the
    scheduler stamps them in the session id itself."""

    def __init__(self, max_sessions: int = 256) -> None:
        self._max = max_sessions
        self._handoff: OrderedDict[str, tuple[str, str]] = OrderedDict()
        #: Sessions opened by a SCHEDULED litigation job's wake.
        self._scheduled: set[str] = set()
        self._lock = threading.Lock()

    def note(self, session_id: str, sender_id: Any, user_message: Any) -> None:
        if not session_id or sender_id != HANDOFF_SENDER:
            return
        text = user_message if isinstance(user_message, str) else ""
        found = WAKE_JOB.search(text)
        kind = JOB_KIND[found.group(1)] if found else ""
        trigger = WAKE_TRIGGER.search(text) if kind in SCHEDULED_KINDS else None
        scheduled = bool(trigger and trigger.group(1) == "scheduled")
        with self._lock:
            if session_id in self._handoff:
                return
            self._handoff[session_id] = (kind, found.group(2)) if found else ("", "")
            if scheduled:
                self._scheduled.add(session_id)
            while len(self._handoff) > self._max:
                gone, _ = self._handoff.popitem(last=False)
                self._scheduled.discard(gone)

    def scheduled(self, session_id: str) -> bool:
        """True in a scheduled litigation job's wake."""
        with self._lock:
            return session_id in self._scheduled

    def source(self, session_id: str) -> tuple[str, str] | None:
        """("handoff", pinned job id or ""), ("cron", ""), or None."""
        with self._lock:
            if session_id in self._handoff:
                return ("handoff", self._handoff[session_id][1])
        if parse_cron_session(session_id):
            return ("cron", "")
        return None

    def kind_of_job(self, job_id: str) -> str:
        """The kind a noted wake gave ``job_id``, or ""."""
        with self._lock:
            for kind, jid in self._handoff.values():
                if jid and jid == job_id:
                    return kind
        return ""

    def job_kind(self, session_id: str) -> str:
        """The pinned job's kind in a job's wake, or ""."""
        with self._lock:
            have = self._handoff.get(session_id)
        return have[0] if have else ""

    def wake_job(self, session_id: str) -> tuple[str, str] | None:
        """(kind, job id) when a job's wake opened this session, else None."""
        with self._lock:
            have = self._handoff.get(session_id)
        return have if have and have[0] and have[1] else None

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._handoff.clear()
            self._scheduled.clear()


TURN_SOURCES = TurnSources()

__all__ = [
    "HANDOFF_SENDER",
    "JOB_KIND",
    "JOB_KINDS",
    "SCHEDULED_KINDS",
    "TURN_SOURCES",
    "WAKE_JOB",
    "WAKE_TRIGGER",
    "TurnSources",
]
