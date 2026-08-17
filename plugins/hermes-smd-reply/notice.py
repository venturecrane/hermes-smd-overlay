"""A held reply the agent is TOLD about (ss-console#2367).

WHY THIS EXISTS
---------------
The relay runs on ``post_tool_call``, whose return value the firing site
collects and ignores (``docs/hook-surface.md`` §2, "Observer only"). So every
hold this plugin makes was, until now, written to two places the agent cannot
read: a D1 audit row (``REPLY_HELD``) and a Sentry message. The model's turn
saw ``create_draft -> ok`` and nothing else.

That is the ss#2367 shape end to end: on 2026-08-13 the Operator read fifteen
documents on 2026-PI-104, filed a demand letter through the checked seam, drafted
a reply naming it, and the reply was held on a Tier-1 marker. The turn ended 23
seconds later with no redraft and no minimal note, and ``demand-letter-drafter``'s
authored recovery ("do not retry the same content, and do not drop the work.
Redraft once... If refused twice, deliver the minimal factual note") could not
have fired: the authored recovery was never delivered to the surface the model
reads. Authored is not delivered.

WHAT THIS DOES
--------------
Records each hold against its ``tool_call_id`` and renders it as text the reply
plugin appends to the draft tool's own result at ``transform_tool_result`` --
the hook that fires immediately after ``post_tool_call`` for the SAME
``tool_call_id`` and whose first ``str`` return replaces the tool result
(``plugins/hermes-smd-hook-probe/README.md:67``, ``model_tools.py:847-857``).
So the hold lands in the model's context in the same turn, in-band, attached to
the very call that produced it. Nothing here decides anything: the hold already
happened, this is only the telling.

WHAT IT DOES NOT DO
-------------------
* It never carries the draft body, and never any inbound text. The only
  non-constant strings are the closed-vocabulary hold reason and the verified
  inbound sender's address (sanitized here anyway).
* It says nothing when the reply is durably queued for automatic release: a
  rate-held reply that WILL go out on its own is not silence, and telling the
  agent "not delivered" there would provoke a redraft that duplicates it.
* It is not a gate. A failure to record or render a notice leaves the hold
  exactly as effective as it was.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# Hold reasons whose recovery is "redraft once, strip only the flagged class"
# -- the content classes ``demand-letter-drafter``'s SKILL.md prescribes for.
# Everything else gets the structural wording (a redraft of the same body
# would be held identically, so asking for one would be a loop).
_CONTENT_REASON_PREFIXES = ("fabrication:", "content_sensitive")

_REDRAFT_GUIDANCE = (
    "Redraft once. Keep every captured fact (what you did, where the artifact "
    "lives, what is reserved, what the record does not establish) and strip "
    "ONLY the flagged content class. If a redraft is held again, deliver the "
    "minimal factual note so a person still learns that the work happened and "
    "what is waiting on them."
)

_SECOND_HOLD_GUIDANCE = (
    "This is hold {n} for this inbound message. Do NOT redraft the same content "
    "again. Deliver the minimal factual note now (what happened, where the work "
    "lives, what awaits a person), or escalate."
)

_REASON_HINTS: dict[str, str] = {
    "matter_mismatch": (
        "The reply cites a matter this recipient is not a party to. Do not "
        "restate it: reply about matters this person is on, or escalate."
    ),
    "duplicate_reply": (
        "A reply to this inbound message was already delivered. This second "
        "draft was not transmitted. Do not redraft; the person has been told."
    ),
    "recipient_mismatch": (
        "The draft addressed someone other than the person who wrote in. A "
        "reply can only go back to that sender, and only to that sender."
    ),
    "sender_not_on_roster": (
        "This sender is not on the organization roster, so no reply is sent "
        "autonomously. The draft stands; a person has to take it from here."
    ),
    "empty_body": "The draft carried no body to relay. A subject alone is not a reply.",
    "no_inbox_id": "No inbox was recorded for the original message, so there is nothing to thread into.",
}

_GENERIC_HINT = (
    "The draft was not transmitted, and no automatic release is queued for it. "
    "Do not assume the person has been told."
)

_MAX_REASON_LEN = 120
_MAX_RECIPIENT_LEN = 254


def _sanitize(value: str, limit: int) -> str:
    """One line, bounded. The reason is ours and the address is a verified
    inbound sender, but this text is injected into the model's context, so it
    is normalized rather than trusted."""
    if not isinstance(value, str):
        return ""
    flat = " ".join(value.split())
    return flat[:limit]


@dataclass(frozen=True)
class HoldNotice:
    """One held reply, as the agent will be told about it."""

    reason: str
    recipient: str
    message_id: str
    attempt: int = 1


def render(notice: HoldNotice) -> str:
    """The model-facing text for one hold. No em dashes (house style)."""
    reason = _sanitize(notice.reason, _MAX_REASON_LEN) or "unspecified"
    recipient = _sanitize(notice.recipient, _MAX_RECIPIENT_LEN) or "the inbound sender"
    if notice.attempt >= 2:
        guidance = _SECOND_HOLD_GUIDANCE.format(n=notice.attempt)
    elif reason.startswith(_CONTENT_REASON_PREFIXES):
        guidance = _REDRAFT_GUIDANCE
    else:
        guidance = _REASON_HINTS.get(reason, _GENERIC_HINT)
    return (
        "[reply-channel] YOUR REPLY WAS NOT SENT. The draft was created, but the "
        f"reply channel held it: {reason}. Nobody has been told, and {recipient} "
        f"is still waiting. {guidance}"
    )


def append_notice(result: str, notice: HoldNotice) -> str:
    """The draft tool's own result with the hold appended.

    Appended rather than substituted: the draft id in the result is real and
    the agent may need it to update the draft it is about to redraft.
    """
    base = result if isinstance(result, str) else ""
    text = render(notice)
    return f"{base}\n\n{text}" if base else text


class HoldNoticeStore:
    """Per-``tool_call_id`` holds, taken exactly once, with a per-message count.

    Bounded and thread-safe. ``take`` is destructive so one hold is told once:
    a second ``transform_tool_result`` for the same call (a retried dispatch,
    a second registrant) does not re-announce it. The attempt counter is keyed
    by the INBOUND message id, not the call, because "refused twice" in the
    authored recovery means twice for the same person's question.
    """

    def __init__(self, max_entries: int = 64, max_counts: int = 256) -> None:
        self._max = max(1, int(max_entries))
        self._max_counts = max(1, int(max_counts))
        self._pending: dict[str, HoldNotice] = {}
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, *, tool_call_id: str, reason: str, recipient: str, message_id: str) -> None:
        key = tool_call_id if isinstance(tool_call_id, str) else ""
        msg = message_id if isinstance(message_id, str) else ""
        with self._lock:
            if len(self._counts) >= self._max_counts:
                self._counts.clear()
            attempt = self._counts.get(msg, 0) + 1 if msg else 1
            if msg:
                self._counts[msg] = attempt
            if len(self._pending) >= self._max:
                self._pending.clear()
            self._pending[key] = HoldNotice(
                reason=reason or "",
                recipient=recipient or "",
                message_id=msg,
                attempt=attempt,
            )

    def take(self, tool_call_id: str) -> HoldNotice | None:
        key = tool_call_id if isinstance(tool_call_id, str) else ""
        with self._lock:
            return self._pending.pop(key, None)

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._pending.clear()
            self._counts.clear()


__all__ = [
    "HoldNotice",
    "HoldNoticeStore",
    "append_notice",
    "render",
]
