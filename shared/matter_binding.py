"""Per-session matter membership — who is a party to which matter (ss#2167).

The outbound matter-identity gate answers one question: *is this recipient a
party to the matter this letter is about?* It cannot ask the vendor at send time
(the plugin runs in the gateway process and cannot synchronously call an MCP
connector from a ``pre_tool_call`` hook), so membership must be captured from
reads the agent already performs and held for the turn.

Mirrors ``shared.provenance``: an ``OrderedDict`` keyed by resolved session id,
LRU-evicted at a cap, and an evicted session simply yields empty — which the
gate reads as *unresolved*, the fail-safe direction.

TWO READ DIRECTIONS, PROVING DIFFERENT THINGS
---------------------------------------------
This is the load-bearing distinction in this module, and collapsing it would
turn the gate into a generator of confident wrong verdicts.

* ``get_matter`` → the matter's OWN party list. When the connector marks it
  ``parties_complete``, this is a **closed set**: it can prove membership AND
  non-membership.
* a contact-filtered ``list_matters`` → "these are the matters this ONE person
  is party to". It proves that person IS a party to each. It says nothing about
  who else is, so it can **never** prove non-membership.

Hence ``complete`` is per-matter and is only ever set by the first direction. A
recipient absent from an incomplete set is *unresolved*, never *not a party*.

Why the second direction exists at all: measured on the pilot 2026-08-10
(vfy_01KZQ200CB8XE84E1M38PQ5WGB), ``get_matter`` does not fire on reply turns —
4 of 77 replies against a 3 of 77 control — and replies are ~74% of all sends.
Without the contact-keyed direction the gate would be blind on the busiest lane.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Same ceiling and reasoning as shared.provenance._MAX_SESSIONS.
_MAX_SESSIONS = 256

# Bounds a single session's map so a long sweep cannot grow it without limit.
_MAX_MATTERS_PER_SESSION = 512
_MAX_EMAILS_PER_MATTER = 64


class MatterMembership:
    """``matter_id -> (party emails, closed?)`` for one session."""

    __slots__ = ("_by_matter", "_complete")

    def __init__(self) -> None:
        self._by_matter: dict[str, set[str]] = {}
        self._complete: set[str] = set()

    def add(self, matter_id: str, emails: Iterable[str], *, complete: bool) -> None:
        if not matter_id:
            return
        if matter_id not in self._by_matter and len(self._by_matter) >= _MAX_MATTERS_PER_SESSION:
            return
        bucket = self._by_matter.setdefault(matter_id, set())
        for raw in emails:
            addr = _norm(raw)
            if addr and len(bucket) < _MAX_EMAILS_PER_MATTER:
                bucket.add(addr)
        # complete is monotonic UP only: once a closed party set has been read,
        # a later contact-keyed addition (which is open by nature) must not
        # downgrade it, and an open read must never upgrade a set to closed.
        if complete:
            self._complete.add(matter_id)

    def parties(self, matter_id: str) -> set[str]:
        return set(self._by_matter.get(matter_id, ()))

    def is_closed(self, matter_id: str) -> bool:
        """True only when the matter's OWN complete party list was read."""
        return matter_id in self._complete

    def known_matters(self) -> set[str]:
        return set(self._by_matter)

    def matters_for(self, email: str) -> set[str]:
        """Every matter this address is known to be a party to."""
        addr = _norm(email)
        if not addr:
            return set()
        return {m for m, emails in self._by_matter.items() if addr in emails}


_sessions: "OrderedDict[str, MatterMembership]" = OrderedDict()


def _norm(value: Any) -> str:
    """Bare lower-cased address, matching shared.outbound_recipient normalization
    so a recipient and a party compare equal."""
    if not isinstance(value, str):
        return ""
    addr = value.strip().lower()
    if "<" in addr and ">" in addr:
        addr = addr[addr.rfind("<") + 1 : addr.rfind(">")].strip()
    return addr if "@" in addr else ""


def membership_for(session_id: str) -> MatterMembership:
    """The session's membership map, creating it on first use (LRU-refreshed)."""
    existing = _sessions.get(session_id)
    if existing is None:
        existing = MatterMembership()
        _sessions[session_id] = existing
        _evict_if_needed()
    else:
        _sessions.move_to_end(session_id)
    return existing


def drop(session_id: str) -> None:
    _sessions.pop(session_id, None)


def _evict_if_needed() -> None:
    while len(_sessions) > _MAX_SESSIONS:
        evicted, _ = _sessions.popitem(last=False)
        logger.debug("matter_binding: evicted oldest session %s (cap %d)", evicted, _MAX_SESSIONS)


def _as_payload(result: Any) -> Any:
    """Tool results arrive as a dict OR as its string repr depending on the fire
    site. Parse strings best-effort; never raise into a hook."""
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        text = result.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return None
    return None


def _iter_dicts(node: Any, depth: int = 0):
    """Walk a payload yielding dicts. Tool results wrap their content at varying
    depths (envelope / content / value), so the shape is discovered, not assumed."""
    if depth > 6:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node[:200]:
            yield from _iter_dicts(item, depth + 1)


def record_from_read(session_id: str, result: Any) -> None:
    """Capture membership from one read-class tool result. Best-effort by
    contract — a hook must never raise, and a miss costs a withheld send
    (recoverable), while a wrong capture costs a wrong verdict (not)."""
    try:
        payload = _as_payload(result)
        if payload is None:
            return
        m = membership_for(session_id)
        for node in _iter_dicts(payload):
            # Direction 0 — a contact record. Remembered because the two reads
            # the binding needs (who this person is, which matters they are on)
            # are SEPARATE tool calls, so the address must survive between them.
            node_id = node.get("id")
            if node_id and ("person" in node or "company" in node):
                addr = _contact_email_in(node, str(node_id))
                if addr:
                    record_contact(session_id, str(node_id), addr)
            # Direction 1 — a matter's OWN party list (closed when complete).
            parties = node.get("parties")
            matter_id = node_id or node.get("matterId") or node.get("matter_id")
            if isinstance(parties, list) and matter_id:
                emails = [p.get("email") for p in parties if isinstance(p, dict)]
                m.add(
                    str(matter_id),
                    [e for e in emails if e],
                    complete=bool(node.get("parties_complete")),
                )
            # Direction 2 — "the matters THIS person is party to" (never closed).
            contact_id = node.get("matters_for_contact")
            if contact_id:
                # Same payload first, then the address this session read earlier.
                email = _contact_email_in(payload, str(contact_id)) or contact_email(
                    session_id, str(contact_id)
                )
                if email:
                    for item in _iter_dicts(node.get("value")):
                        mid = item.get("id")
                        if mid:
                            m.add(str(mid), [email], complete=False)
    except Exception:  # noqa: BLE001 — capture must never perturb the tool path
        logger.debug("matter_binding: read capture failed", exc_info=True)


def _contact_email_in(payload: Any, contact_id: str) -> str:
    """The address for ``contact_id`` if this session already read that contact.

    The contact-filtered listing names the contact by id, not by address, so the
    binding needs the address the router's own ``get_contacts`` read supplied.
    Returns "" when unknown — which yields no capture, i.e. unresolved."""
    for node in _iter_dicts(payload):
        if str(node.get("id") or "") != contact_id:
            continue
        for holder in (node.get("person"), node.get("company"), node):
            if isinstance(holder, dict):
                addr = _norm(holder.get("email"))
                if addr:
                    return addr
    return ""


def record_contact(session_id: str, contact_id: str, email: str) -> None:
    """Remember one contact's address so a later contact-filtered listing can be
    bound to it (the two reads are separate tool calls)."""
    try:
        addr = _norm(email)
        if contact_id and addr:
            _contacts_for(session_id)[str(contact_id)] = addr
    except Exception:  # noqa: BLE001
        logger.debug("matter_binding: contact record failed", exc_info=True)


_contacts: "OrderedDict[str, dict[str, str]]" = OrderedDict()


def _contacts_for(session_id: str) -> dict[str, str]:
    existing = _contacts.get(session_id)
    if existing is None:
        existing = {}
        _contacts[session_id] = existing
        while len(_contacts) > _MAX_SESSIONS:
            _contacts.popitem(last=False)
    else:
        _contacts.move_to_end(session_id)
    return existing


def contact_email(session_id: str, contact_id: str) -> str:
    return _contacts_for(session_id).get(str(contact_id), "")


def _reset_for_tests() -> None:
    _sessions.clear()
    _contacts.clear()


__all__ = [
    "MatterMembership",
    "membership_for",
    "record_from_read",
    "record_contact",
    "contact_email",
    "drop",
]
