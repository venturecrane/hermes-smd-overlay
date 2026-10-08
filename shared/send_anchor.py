"""The participant fence's ANCHOR and LANE, decided by code (ss-console
``workspace_broker/participant_fence.py``).

THE RULE THE BROKER ENFORCES (2026-10-07, a law-firm seat). A firm person gets Operator
mail only if they were From, To or Cc on the request the send answers, or the
firm authored them for the job sending it (a LANE), or SMD originated the work
and they are an admin. Outside recipients are unchanged. The broker reads the
request's participants out of the mailbox itself; what it needs from this
process is WHICH request, and that is the part the model must never choose.

WHERE AN ANCHOR COMES FROM, and nowhere else:

* this turn's prompt-carried email (``SESSION_INBOUND_ORIGIN.bound_this_turn``),
  never the sticky session origin and never an address-keyed guess;
* a job's completion wake: the job id the gateway's wake named
  (``shared.turn_sources``); the broker resolves the job's request email and
  requires its From to be the job's requester;
* a rule row: ``{"kind": "rule", "proposal_id": ...}``, resolved by the broker to
  the origin it recorded when the row was created;
* a held send: the anchor captured when it was held (``shared.pending_send``),
  re-checked by the broker at replay.

A cron or connector turn has none, and sends only through a lane, to SMD, or to
outside recipients.

WHERE A LANE COMES FROM. Only the code path that owns it names it, through the
``lane=`` keyword of ``shared.send_dispatch.dispatch``; the model's send tool
never forwards one. Each lane maps broker-side to exactly one authored key.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from shared import inbound, provenance
from shared.turn_sources import TURN_SOURCES

logger = logging.getLogger(__name__)

GRAPH_MESSAGE = "graph_message"
AGENTMAIL_MESSAGE = "agentmail_message"
#: The lanes, by the names the broker knows (participant_fence.LANE_*).
LANE_RULE_DISPATCH = "rule_dispatch"
LANE_ESCALATION = "escalation"
LANE_SKILL_PREFIX = "skill:"
#: The inbox marker a verified reply binding's origin carries (reply binding.py).
_BOUND_INBOX = "verified-binding"
_TRUSTED_MODES = frozenset({provenance.MODE_KEYED, provenance.MODE_THREAD, provenance.MODE_PROCESS})

Anchor = dict[str, str]


def seat_adapter() -> str:
    """The seat's authored Email adapter (``agentmail`` when unauthored), the
    same default the trust and reply plugins share."""
    from shared.customer_config import CustomerConfig  # local import (enforce.py idiom)

    try:
        record = CustomerConfig.from_volume().connectors.get("Email")
    except Exception:  # noqa: BLE001 - an unreadable config takes the shared default
        return "agentmail"
    adapter = record.get("adapter") if isinstance(record, dict) else None
    return adapter.strip().lower() if isinstance(adapter, str) and adapter.strip() else "agentmail"


def message_anchor(message_id: str, adapter: str | None = None) -> Anchor | None:
    """The anchor for one received email on this seat's channel."""
    mid = str(message_id or "").strip()
    if not mid:
        return None
    if (adapter or seat_adapter()) == "msgraph":
        return {"kind": GRAPH_MESSAGE, "graph_message_id": mid}
    return {"kind": AGENTMAIL_MESSAGE, "message_id": mid}


def origin_anchor(
    origin: inbound.InboundOrigin | None, adapter: str | None = None
) -> Anchor | None:
    """The anchor for a verified inbound origin (a bound reply's is Graph)."""
    if origin is None:
        return None
    if origin.inbox_id == _BOUND_INBOX:
        return message_anchor(origin.message_id, "msgraph")
    return message_anchor(origin.message_id, adapter)


def turn_anchor(session_id: str | None = None) -> Anchor | None:
    """The anchor THIS turn may send under, or None. Never a sticky origin."""
    try:
        resolved, mode = provenance.resolve_session_with_mode(session_id or None)
    except Exception:  # noqa: BLE001 - no resolution is no anchor
        return None
    if not resolved or mode not in _TRUSTED_MODES:
        return None
    origin = inbound.SESSION_INBOUND_ORIGIN.bound_this_turn(resolved)
    if origin is not None:
        return origin_anchor(origin)
    wake = TURN_SOURCES.wake_job(resolved)
    if wake is not None:
        return {"kind": wake[0], "job_id": wake[1]}
    return None


def message_origin(session_id: str | None = None) -> Anchor | None:
    """This turn's own EMAIL as an anchor, or None (a job wake is not one).

    What a broker row records as the request it was created from (a rule, an
    operations request, an act), so a later letter about it anchors there."""
    anchor = turn_anchor(session_id)
    if anchor and anchor.get("kind") in (GRAPH_MESSAGE, AGENTMAIL_MESSAGE):
        return anchor
    return None


def skill_lane(skill: str) -> str:
    return LANE_SKILL_PREFIX + skill


def routine_lane(skill: str) -> str:
    """The lane a routine's code-rendered send rides: ``skill:<name>`` when the
    seat authors that skill its own ``settings.recipient`` (statute-watch's one
    reader), otherwise ``escalation`` (the case-alert lists). The routine is the
    scheduler's fact (``cron_attribution``), never the model's; the broker maps
    either lane to its authored key and to nobody else."""
    from shared.customer_config import CustomerConfig  # local import (enforce.py idiom)

    try:
        personas = CustomerConfig.from_volume().personas
    except Exception:  # noqa: BLE001 - an unreadable config falls to the narrower lane
        return LANE_ESCALATION
    for persona in personas:
        for entry in persona.get("skills") or [] if isinstance(persona, dict) else []:
            settings = entry.get("settings") if isinstance(entry, dict) else None
            if (
                isinstance(settings, dict)
                and entry.get("name") == skill
                and entry.get("enabled") is True
                and settings.get("recipient")
            ):
                return skill_lane(skill)
    return LANE_ESCALATION


# An out-of-turn dispatch runs the gate on this thread; a send the gate holds
# for approval is captured with the dispatch's own anchor and lane, not the
# turn's. Set only by the trust plugin's internal sender.
_SCOPE = threading.local()


@contextmanager
def dispatch_scope(anchor: Anchor | None, lane: str | None) -> Iterator[None]:
    previous = getattr(_SCOPE, "value", None)
    _SCOPE.value = (anchor, lane)
    try:
        yield
    finally:
        _SCOPE.value = previous


def capture_context(session_id: str | None) -> tuple[Anchor | None, str | None]:
    """(anchor, lane) a held send is captured with: the out-of-turn dispatch's
    when one is running on this thread, else this turn's anchor and no lane."""
    scoped = getattr(_SCOPE, "value", None)
    if scoped is not None:
        return scoped
    return turn_anchor(session_id), None


def note_replay(anchor: Anchor | None, lane: str | None, recipients: Any) -> None:
    """An approved held send is about to re-run as a tool call on this thread:
    the handler sends it under the anchor it was HELD with, not this turn's."""
    _SCOPE.replay = (frozenset(recipients or ()), anchor, lane)


def take_replay(recipients: Any) -> tuple[Anchor | None, str | None] | None:
    """The noted replay's (anchor, lane), once, when its recipients match."""
    noted = getattr(_SCOPE, "replay", None)
    _SCOPE.replay = None
    if noted is None or noted[0] != frozenset(recipients or ()):
        return None
    return noted[1], noted[2]


#: Appended by the trust plugin when the broker's participant fence refused a
#: send, so the Operator acts on the refusal instead of retrying it.
REFUSAL_MARKER = "participant fence:"
FENCE_DECISION = (
    "This email was not sent: someone at the firm on it was not on the request it "
    "answers. Reply to the person who asked if you still can, otherwise write only to "
    "them, and say who should see it. Do not send it to anyone else. SMD has been told."
)


def refusal_note(reason: str) -> str:
    """``FENCE_DECISION`` when ``reason`` is a participant-fence refusal, else ""."""
    return FENCE_DECISION if REFUSAL_MARKER in str(reason or "") else ""


def envelope_fields(anchor: Anchor | None, lane: str | None) -> dict[str, Any]:
    """The two broker request fields, present only when set."""
    return {**({"anchor": dict(anchor)} if anchor else {}), **({"lane": lane} if lane else {})}


__all__ = [
    "message_origin",
    "routine_lane",
    "AGENTMAIL_MESSAGE",
    "FENCE_DECISION",
    "GRAPH_MESSAGE",
    "REFUSAL_MARKER",
    "note_replay",
    "refusal_note",
    "take_replay",
    "LANE_ESCALATION",
    "LANE_RULE_DISPATCH",
    "LANE_SKILL_PREFIX",
    "Anchor",
    "capture_context",
    "dispatch_scope",
    "envelope_fields",
    "message_anchor",
    "origin_anchor",
    "seat_adapter",
    "skill_lane",
    "turn_anchor",
]
