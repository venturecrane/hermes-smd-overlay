"""Outbound recipient resolution for the proactive send gate.

The trust gate (``hermes-smd-trust``) needs the *recipient* of a proactive send
to decide whether it is an internal (rostered staff) send — governed by the
``external_send_internal`` ceiling — or an outside send (``external_send``). The
recipient is not always in the send call's args:

* ``send_message`` / ``forward_message`` carry ``to`` directly.
* ``send_draft`` carries only a ``draft_id``; the recipient was named at
  ``create_draft`` time. So we record ``draft_id → recipients`` at
  ``create_draft`` / ``update_draft`` (post_tool_call, where the created id is in
  the result) into a per-session registry, and look it up at ``send_draft`` time.
* ``reply_to_message`` is NOT handled here — a reply's recipient is the verified
  inbound sender, owned by the ``hermes-smd-reply`` recipient-locked path.

Fail-closed: if the recipient of a send cannot be resolved (``send_draft`` of a
draft this session never observed, or an empty/malformed ``to``), the recipient
set is empty and the caller routes the send to OUTSIDE (draft) — **never**
INTERNAL. A send is never promoted to autonomous on an unresolved recipient.

Address extraction uses ``email.utils.parseaddr`` so a ``"Name <addr>"`` form
yields the bracketed routable address (defeating a display-name spoof) before it
reaches ``recipient_classifier`` (which then applies strict canonicalization).
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

# Live runtime tool names (mcp_<server>_<tool>) — the only form the agent emits.
# Proactive sends whose recipient is in the call's own ``to`` arg:
DIRECT_TO_SEND_TOOLS: frozenset[str] = frozenset(
    {"mcp_agentmail_send_message", "mcp_agentmail_forward_message"}
)
# Proactive send that resolves its recipient from a recorded draft_id:
DRAFT_SEND_TOOLS: frozenset[str] = frozenset({"mcp_agentmail_send_draft"})
# Draft authoring tools whose (result id, args ``to``) we record for later sends:
DRAFT_RECORD_TOOLS: frozenset[str] = frozenset(
    {"mcp_agentmail_create_draft", "mcp_agentmail_update_draft"}
)
# Every proactive send this module classifies. reply_to_message is deliberately
# absent — the reply plugin owns the recipient-locked reply-to-sender path.
CLASSIFIED_SEND_TOOLS: frozenset[str] = DIRECT_TO_SEND_TOOLS | DRAFT_SEND_TOOLS


def _normalize_addr(value: Any) -> str:
    """Bare, lower-cased routable address from a ``"Name <addr>"`` or bare string.

    ``parseaddr`` takes the bracketed address, so a display-name spoof
    (``"scott@smd.services <attacker@evil.com>"``) yields the real routable
    address (``attacker@evil.com``), never the display text. Empty/unparseable → "".
    """
    if not isinstance(value, str):
        return ""
    return parseaddr(value)[1].strip().lower()


def extract_to_recipients(args: Any) -> set[str]:
    """Normalized recipient set from a ``to`` argument (list or single string)."""
    if not isinstance(args, dict):
        return set()
    raw = args.get("to")
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return set()
    return {addr for addr in (_normalize_addr(x) for x in items) if addr}


def _extract_draft_id_from_result(result: Any) -> str:
    """Best-effort draft id from a ``create_draft`` / ``update_draft`` result.

    Handles a dict, a JSON string, or an MCP text-content wrapper. Returns "" if
    no id is found — the send then fails closed (unresolved → OUTSIDE/draft).
    """
    obj: Any = result
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except (ValueError, TypeError):
            return ""
    if isinstance(obj, dict):
        for key in ("draft_id", "id"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                return v
        # AgentMail sometimes nests the object under "draft".
        nested = obj.get("draft")
        if isinstance(nested, dict):
            for key in ("draft_id", "id"):
                v = nested.get(key)
                if isinstance(v, str) and v:
                    return v
    return ""


@dataclass
class DraftRecipientRegistry:
    """Per-session ``draft_id → recipient set`` map, populated at draft creation.

    Bounded (FIFO eviction) so a long-lived single-tenant Machine cannot leak
    unboundedly across sessions/drafts. Mirrors :class:`shared.inbound.SessionTaint`.
    Keyed by ``(session_id, draft_id)`` flattened to one bounded store.
    """

    max_entries: int = 2048
    _by_key: OrderedDict[str, set[str]] = field(default_factory=OrderedDict)

    @staticmethod
    def _key(session_id: str, draft_id: str) -> str:
        return f"{session_id or ''}\x1f{draft_id}"

    def record(self, session_id: str, draft_id: str, recipients: set[str]) -> None:
        """Record a draft's recipients. No-op without a draft id or recipients."""
        if not draft_id or not recipients:
            return
        key = self._key(session_id, draft_id)
        self._by_key[key] = set(recipients)
        self._by_key.move_to_end(key)
        while len(self._by_key) > self.max_entries:
            self._by_key.popitem(last=False)

    def lookup(self, session_id: str, draft_id: str) -> set[str] | None:
        """Recipients recorded for this draft, or ``None`` if never observed."""
        if not draft_id:
            return None
        return self._by_key.get(self._key(session_id, draft_id))


# Module-level singleton — one tenant per Machine (AGENTS.md #5).
DRAFT_RECIPIENTS = DraftRecipientRegistry()


def record_draft_from_post_tool_call(
    tool_name: str, args: Any, result: Any, session_id: str
) -> None:
    """post_tool_call hook helper: record a created/updated draft's recipients.

    Best-effort and exception-free at the call site (the hook wraps it). Only
    fires for the draft-authoring tools; extracts the created id from the result
    and the ``to`` from the args.
    """
    if tool_name not in DRAFT_RECORD_TOOLS:
        return
    recipients = extract_to_recipients(args)
    draft_id = _extract_draft_id_from_result(result)
    DRAFT_RECIPIENTS.record(session_id, draft_id, recipients)


def send_recipients(tool_name: str, args: Any, session_id: str) -> set[str] | None:
    """Recipient set for a proactive send, or ``None`` if unresolvable.

    ``None`` / empty → the caller routes the send to OUTSIDE (draft), never
    INTERNAL. Only classifies the proactive send tools; any other tool returns
    ``None`` (the caller leaves its base action class untouched).
    """
    if tool_name in DIRECT_TO_SEND_TOOLS:
        recips = extract_to_recipients(args)
        return recips or None
    if tool_name in DRAFT_SEND_TOOLS:
        draft_id = args.get("draft_id") if isinstance(args, dict) else None
        if not isinstance(draft_id, str) or not draft_id:
            return None
        return DRAFT_RECIPIENTS.lookup(session_id, draft_id)
    return None
