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

A third source ADDS parties and closes nothing: ``get_roles_on_matter`` /
``get_relationships_on_matter``, the reads ADR 0086 names canonical for "who is
on this matter". A role record is where opposing counsel and adjusters attach —
parties on neither ``clientIds`` nor ``otherSideIds``, so invisible to the first
direction, so liable to be called outsiders on a matter whose party list had
closed. Add-only is the whole safety argument: it can make an address a proven
party, never a proven non-party.

Why the second direction exists at all: measured on the pilot 2026-08-10
(vfy_01KZQ200CB8XE84E1M38PQ5WGB), ``get_matter`` does not fire on reply turns —
4 of 77 replies against a 3 of 77 control — and replies are ~74% of all sends.
Without the contact-keyed direction the gate would be blind on the busiest lane.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# Same ceiling and reasoning as shared.provenance._MAX_SESSIONS.
_MAX_SESSIONS = 256

# Bounds a single session's map so a long sweep cannot grow it without limit.
_MAX_MATTERS_PER_SESSION = 512
_MAX_EMAILS_PER_MATTER = 64


class MatterMembership:
    """``matter_id -> (party emails, closed?)`` for one session."""

    __slots__ = (
        "_by_matter",
        "_by_matter_folded",
        "_ambiguous_ids",
        "_complete",
        "_contact_complete",
        "_alias",
        "_ambiguous",
    )

    def __init__(self) -> None:
        self._by_matter: dict[str, set[str]] = {}
        # Case-folded id -> the id as the connector spelled it. The extractor
        # (matter_gate._MATTER_ID_RE) carries IGNORECASE and returns the match
        # verbatim, so a body citing an uppercased GUID produced a token that
        # `in self._by_matter` could never match, and the gate reported
        # *unresolved* against a party set it had actually read (ss#2290). The
        # number path was already folded by _norm_matter; this is the id path's
        # missing half. Lookup only — nothing stored or displayed is rewritten.
        self._by_matter_folded: dict[str, str] = {}
        # Folded keys claimed by two distinct ids, kept for the same reason
        # _ambiguous is: without it the loser's next read re-adds the key and
        # the binding flip-flops.
        self._ambiguous_ids: set[str] = set()
        self._complete: set[str] = set()
        # ss#2264 — the CONTACT axis. `_complete` closes a MATTER's own party
        # list; this closes the other direction: the addresses whose FULL set of
        # matters was read this session. Both prove non-membership, and only this
        # one is keyed off the read the reply lane actually performs (list_matters
        # fires on 34 of 86 reply turns against get_matter's 8), which is why the
        # gate could previously only ever return *unresolved* there.
        self._contact_complete: set[str] = set()
        # A matter's human-readable number ("2026-PI-101") -> its connector id.
        # Correspondence cites the number; the connector keys everything by the
        # id, and without this join a real letter's citation resolved to nothing
        # and the gate returned *unresolved* on a body it could have checked.
        self._alias: dict[str, str] = {}
        # Numbers seen pointing at two different matters. Kept so the collision
        # is remembered after the alias is withdrawn — otherwise the loser's next
        # read would simply re-add it and the ambiguity would flip-flop.
        self._ambiguous: set[str] = set()

    def add(self, matter_id: str, emails: Iterable[str], *, complete: bool) -> None:
        if not matter_id:
            return
        if matter_id not in self._by_matter and len(self._by_matter) >= _MAX_MATTERS_PER_SESSION:
            return
        bucket = self._by_matter.setdefault(matter_id, set())
        self._index_folded(matter_id)
        for raw in emails:
            addr = _norm(raw)
            if addr and len(bucket) < _MAX_EMAILS_PER_MATTER:
                bucket.add(addr)
        # complete is monotonic UP only: once a closed party set has been read,
        # a later contact-keyed addition (which is open by nature) must not
        # downgrade it, and an open read must never upgrade a set to closed.
        if complete:
            self._complete.add(matter_id)

    def _index_folded(self, matter_id: str) -> None:
        """Make ``matter_id`` findable by a differently-cased citation.

        Ambiguity is withdrawn, never guessed — the same rule ``add_alias``
        follows, and for the same reason: two ids differing only by case are
        two matters, and picking one would let the gate call a legitimate
        recipient an outsider. A withdrawn key falls back to *unresolved*, and
        an exactly-spelled citation still resolves for both.
        """
        # An already-folded id is indexed too, rather than short-circuited: it is
        # a live claimant on the key, and skipping it would let a lower-case
        # sibling registered earlier win the folded lookup unchallenged.
        key = _norm_matter(matter_id)
        if not key or key in self._ambiguous_ids:
            return
        existing = self._by_matter_folded.get(key)
        if existing is not None:
            if existing != matter_id:
                del self._by_matter_folded[key]
                self._ambiguous_ids.add(key)
                logger.debug("matter_binding: folded id %s is ambiguous; withdrawn", key)
            return
        self._by_matter_folded[key] = matter_id

    def add_alias(self, alias: str, matter_id: str) -> None:
        """Record a matter's human-readable number as another name for its id.

        Ambiguity is withdrawn, never resolved by guessing: if a number is ever
        seen pointing at a second matter, the alias is dropped and blacklisted,
        so a body citing it reads as *unresolved*. The alternative — keeping
        either binding — would let the gate call a legitimate recipient an
        outsider on the strength of a collision, which is the one verdict this
        module exists to never produce.
        """
        key = _norm_matter(alias)
        if not key or not matter_id or key == matter_id:
            return
        if key in self._ambiguous:
            return
        existing = self._alias.get(key)
        if existing is not None and existing != matter_id:
            del self._alias[key]
            self._ambiguous.add(key)
            logger.debug("matter_binding: alias %s is ambiguous; withdrawn", key)
            return
        if existing is None and len(self._alias) >= _MAX_MATTERS_PER_SESSION:
            return
        self._alias[key] = matter_id

    def resolve(self, token: str) -> str:
        """The canonical matter id for a cited token — the id itself, or the
        matter whose number it is. Empty when this session read no such matter,
        which the gate reads as *unresolved*.

        Three lookups, most-exact first: the id as spelled, the id in any case,
        then the number. The middle one exists because the extractor is
        IGNORECASE and returns its match verbatim (ss#2290)."""
        if not isinstance(token, str) or not token:
            return ""
        if token in self._by_matter:
            return token
        folded = _norm_matter(token)
        matter_id = self._by_matter_folded.get(folded)
        if matter_id:
            return matter_id
        return self._alias.get(folded, "")

    def parties(self, matter_id: str) -> set[str]:
        return set(self._by_matter.get(matter_id, ()))

    def is_closed(self, matter_id: str) -> bool:
        """True only when the matter's OWN complete party list was read."""
        return matter_id in self._complete

    def close_contact(self, email: str) -> None:
        """Record that the FULL set of matters this address is party to was read
        (ss#2264). Only ``record_from_read`` calls this, and only when the
        connector proved the listing was unfiltered and untruncated
        (``matters_for_contact_complete``)."""
        addr = _norm(email)
        if addr and len(self._contact_complete) < _MAX_MATTERS_PER_SESSION:
            self._contact_complete.add(addr)

    def is_contact_closed(self, email: str) -> bool:
        """True only when this address's OWN complete matter list was read.

        The contact-axis twin of :meth:`is_closed`, and it carries the same
        warning: absence from an OPEN set proves nothing. A caller that treats
        ``False`` as "not a party" collapses *unresolved* into *non-member* and
        tells a paralegal a legitimate client is an outsider.
        """
        addr = _norm(email)
        return bool(addr) and addr in self._contact_complete

    def known_matters(self) -> set[str]:
        return set(self._by_matter)

    def matters_for(self, email: str) -> set[str]:
        """Every matter this address is known to be a party to."""
        addr = _norm(email)
        if not addr:
            return set()
        return {m for m, emails in self._by_matter.items() if addr in emails}


_sessions: OrderedDict[str, MatterMembership] = OrderedDict()


def _norm(value: Any) -> str:
    """Bare lower-cased address, matching shared.outbound_recipient normalization
    so a recipient and a party compare equal."""
    if not isinstance(value, str):
        return ""
    addr = value.strip().lower()
    if "<" in addr and ">" in addr:
        addr = addr[addr.rfind("<") + 1 : addr.rfind(">")].strip()
    return addr if "@" in addr else ""


def _norm_matter(value: Any) -> str:
    """Case-folded matter token — a number or a connector id.

    Smokeball renders "2026-PI-101"; a body may carry a different case, and two
    spellings of one matter must not become two matters. Whitespace only — no
    punctuation stripping, because the separators are part of the number, not
    formatting.

    ONE folding function for both token kinds, deliberately: the id path
    shipped without an equivalent and an uppercased GUID citation resolved to
    nothing (ss#2290). Direction is arbitrary but must stay uniform — the
    folded id index and the alias map share this key space, and a second
    implementation folding the other way would silently rebuild the split.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().upper()


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
            # A matter record carries its human-readable number in the SAME dict
            # the party list arrives in (connector server.py:363). Aliased on any
            # matter-shaped node, not only a party-bearing one, so a matter read
            # for other reasons still teaches this session what its number means.
            if matter_id and isinstance(node.get("number"), str):
                m.add_alias(node["number"], str(matter_id))
            # Direction 2 — "the matters THIS person is party to". Each entry is
            # open on the MATTER axis (a contact-keyed listing says nothing about
            # who else is on a matter), but the SET can be closed on the contact
            # axis when the connector proved the listing was unfiltered and
            # untruncated — see `_contact_listing_is_complete` in the Smokeball
            # connector, and `is_contact_closed` above. Absent or false, nothing
            # closes and the verdict stays *unresolved*, exactly as before.
            contact_id = node.get("matters_for_contact")
            if contact_id:
                # Same payload first, then the address this session read earlier.
                email = _contact_email_in(payload, str(contact_id)) or contact_email(
                    session_id, str(contact_id)
                )
                if email:
                    if node.get("matters_for_contact_complete") is True:
                        m.close_contact(email)
                    for item in _iter_dicts(node.get("value")):
                        mid = item.get("id")
                        if mid:
                            m.add(str(mid), [email], complete=False)
                            # The listing is /matters, so each item carries its
                            # number too — this is the reply lane's main source
                            # of the number->id join (list_matters fires on 34 of
                            # 86 reply turns, get_matter on 8).
                            if isinstance(item.get("number"), str):
                                m.add_alias(item["number"], str(mid))
            # Direction 3 — a ROLE / RELATIONSHIP record: "this person holds a
            # role on this matter" (ADR 0086's named seeding sources,
            # get_roles_on_matter / get_relationships_on_matter).
            #
            # ADD-ONLY, and never closing. The direction it moves the gate is the
            # one the ADR ranks above the true positive: a party who is neither on
            # `clientIds` nor `otherSideIds` — opposing counsel, an adjuster, the
            # OUTSIDE recipients that must pair — was invisible to Direction 1, so
            # on a matter whose party list HAD closed, a correct letter to them
            # read as a mismatch and named a legitimate recipient an outsider. A
            # roles read can only ever make more addresses provable parties.
            #
            # The key is the connector's explicit assertion, not an inference from
            # co-occurrence: `party_of_matter` is attached in code from a resolved
            # contact fetch (smokeball server.py `_attach_matter_party_join`). A
            # record the connector could not resolve carries no key, supplies no
            # membership, and leaves the verdict *unresolved*.
            party_matter = node.get("party_of_matter")
            if party_matter:
                addr = _norm(node.get("email"))
                if addr:
                    m.add(str(party_matter), [addr], complete=False)
                    if isinstance(node.get("matterNumber"), str):
                        m.add_alias(node["matterNumber"], str(party_matter))
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


_contacts: OrderedDict[str, dict[str, str]] = OrderedDict()


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
