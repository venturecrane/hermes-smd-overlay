"""The relay's working drafts on a Graph seat: discarded once the reply is out.

WHY. The relay never sends the draft ``create_draft`` made. It transmits the
reply as a NEW message through the broker (``msgraph_reply`` /
``msgraph_reply_bound``), because that path is the recipient-locked, fenced and
audited one. The draft was only ever the agent's working copy, and nothing
removed it: on ashton-price the operator mailbox's Drafts folder held 50+ unsent
drafts to firm staff by 2026-10-12, each one a reply that had in fact gone out
(one reply at 21:46:10 left three drafts, 21:45:56, 21:46:02 and 21:46:07,
because two were held and redrafted in the same turn).

THE RULE. A draft is tracked against the reply it is a copy of (the relay's
``_REPLIED`` key in this session) and every tracked draft for that reply is
discarded once the reply is TRANSMITTED, including the held drafts that the
sent one superseded. A draft the relay holds and never sends is left alone, so
a draft deliberately kept for a person to review stays where they can see it:

* ``sender_not_on_roster`` keeps its draft for staff and is never tracked;
* a turn whose reply is held and never sent discards nothing;
* a reply durably queued for release discards nothing now (the release sends
  from the queued text later, and the draft is the only copy staff can read
  until then).

A draft that arrives after its reply already went out (``duplicate_reply``) is
discarded at once: it is a copy of an answer the person already has.

Deletion is the broker's (``msgraph_discard_draft``), which deletes only a
message Graph still reports as a draft. Best effort: a failure is logged and
never undoes, delays or fails the reply.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from shared.result_unwrap import parse_tool_result

logger = logging.getLogger(__name__)


def draft_id_of(result: Any) -> str:
    """The Graph id of the draft a ``create_draft`` result reports, or ``""``.

    The connector returns the created message resource. Read only an ``id`` on a
    message that does not say it is NOT a draft; anything else is ``""`` and
    nothing is ever discarded on its strength.
    """
    parsed = parse_tool_result(result)
    if not isinstance(parsed, dict) or parsed.get("isDraft") is False:
        return ""
    draft_id = parsed.get("id")
    return draft_id.strip() if isinstance(draft_id, str) else ""


class WorkingDrafts:
    """(session, reply key) -> draft ids awaiting their reply. Bounded, thread-safe."""

    def __init__(self, max_keys: int = 512) -> None:
        self._max = max(1, int(max_keys))
        self._by_key: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        self._lock = threading.Lock()

    def track(self, session_id: str, reply_key: str, draft_id: str) -> None:
        if not draft_id or not reply_key:
            return
        key = (session_id or "", reply_key)
        with self._lock:
            ids = self._by_key.setdefault(key, [])
            if draft_id not in ids:
                ids.append(draft_id)
            self._by_key.move_to_end(key)
            while len(self._by_key) > self._max:
                self._by_key.popitem(last=False)

    def take(self, session_id: str, reply_key: str) -> list[str]:
        with self._lock:
            return self._by_key.pop((session_id or "", reply_key), [])

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._by_key.clear()


def discard(draft_ids: list[str], discard_one: Callable[[str], str]) -> None:
    """Discard each draft; never raises."""
    for draft_id in draft_ids:
        if not draft_id:
            continue
        try:
            outcome = discard_one(draft_id)
            logger.info("hermes-smd-reply: working draft %s after its reply was sent", outcome)
        except Exception as exc:  # noqa: BLE001 - the reply is out; a leftover draft is not its failure
            logger.warning("hermes-smd-reply: working draft not discarded (%s)", exc)


__all__ = ["WorkingDrafts", "discard", "draft_id_of"]
