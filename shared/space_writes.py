"""Which tool calls would carry a private turn's words out of its space (ADR 0090 R1).

WHY THIS EXISTS (ss-console #3264, critique revision R1). On a seat a couple
shares, a turn in partner A's DM may READ only A's space and the shared one
(``shared.space``). Reading is fenced everywhere; this module is about WRITING.
A fact A says in A's DM must not reach partner B by being written somewhere B
reads: the shared record store or list, the shared group chat, a calendar
invite B receives, a shared Google doc, an email to B. Each of those is a
legitimate thing A may ask for ("add it to our list", "invite us both"), so the
answer is not to refuse. It is to hold the write and release it only when A,
in the same chat, confirms the exact words that will land.

:func:`crossing` is the classifier. Given a tool call and the turn's space it
returns the :class:`Crossing` the call would make, or ``None``. It knows:

* ``record_store_write``: a record whose target space is ``shared`` (a store
  with no owner field, or a record stamped ``shared``);
* ``send_message`` (Hermes): a target that is not the requester's own DM;
* any email send (``to``/``cc``/``bcc``): a recipient who is another person;
* a calendar invite (any tool whose name starts ``calendar_invite``): an
  attendee who is not the requester;
* every non-read ``workspace_*`` tool: Google holds shared items only (R5).

A crossing from the SHARED space is not a crossing (the words are already
shared), and a crossing from an unresolved space is refused outright by the
gate (``plugins/hermes-smd-space-gate``), never held: nobody could confirm it.

:data:`SPACE_WRITES` is the held-write register. It is a second instance of the
binding-aware register from overlay #465 (``shared.pending_send``): each record
is bound to the person and the chat that raised it, only that person in that
chat can approve it, a group "yes" never releases a DM-raised write, and what
lands is the STORED payload, never a re-composition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from shared import space as space_mod
from shared.outbound_recipient import extract_send_recipients
from shared.pending_send import PendingSendRegister

#: The held private-to-shared writes. Separate from ``PENDING_SEND`` so the trust
#: gate's send approvals and these confirmations can never answer for each other.
SPACE_WRITES = PendingSendRegister()

#: Tool names (or prefixes) this module classifies.
RECORD_WRITE = "record_store_write"
SEND_MESSAGE = "send_message"
CALENDAR_INVITE_PREFIX = "calendar_invite"
WORKSPACE_PREFIX = "workspace_"

#: Workspace tools that read. Every other ``workspace_*`` tool is a write.
_WORKSPACE_READS = frozenset(
    {
        "workspace_gmail_search",
        "workspace_gmail_get",
        "workspace_calendar_list",
        "workspace_calendar_get",
        "workspace_drive_list",
        "workspace_drive_get",
        "workspace_drive_export",
        "workspace_docs_get",
        "workspace_sheets_get_values",
    }
)

#: Arg keys a calendar invite may name its attendees under.
_ATTENDEE_KEYS = ("attendees", "to", "invitees", "recipients", "emails")

#: The longest payload a person is asked to confirm in chat. Anything larger
#: cannot be shown exactly in a message, so it is refused, never held.
MAX_CONFIRM_CHARS = 3000


@dataclass(frozen=True)
class Crossing:
    """A write that would carry this turn's words out of its space.

    ``targets`` is the fingerprint the held record is matched by on the
    re-invoked call (with the tool name); ``where`` says in plain words where
    the words would land."""

    tool_name: str
    targets: frozenset[str]
    where: str


def is_candidate(tool_name: str) -> bool:
    """Whether :func:`crossing` has anything to say about this tool at all."""
    name = (tool_name or "").strip().lower()
    return (
        name in (RECORD_WRITE, SEND_MESSAGE)
        or name.startswith(CALENDAR_INVITE_PREFIX)
        or (name.startswith(WORKSPACE_PREFIX) and name not in _WORKSPACE_READS)
        or _is_email_send(name)
    )


def _is_email_send(name: str) -> bool:
    try:
        from shared.action_classes import ActionClass, classify_tool

        cls = classify_tool(name).action_class
    except Exception:  # noqa: BLE001 — an unclassifiable tool is not a known send
        return False
    return cls in (
        ActionClass.EXTERNAL_SEND,
        ActionClass.EXTERNAL_SEND_INTERNAL,
        ActionClass.EXTERNAL_SEND_CLIENT,
        ActionClass.EXTERNAL_SEND_VENDOR,
        ActionClass.EXTERNAL_SEND_AS_STAFF,
    )


def _emails(value: Any) -> set[str]:
    items = (
        [value]
        if isinstance(value, str)
        else list(value)
        if isinstance(value, (list, tuple))
        else []
    )
    out: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("email") or item.get("address") or ""
        if isinstance(item, str) and "@" in item:
            out.add(item.strip().lower())
    return out


def _others(cfg: space_mod.SpaceConfig, me: str | None) -> dict[str, space_mod.Person]:
    """Every authored person except the requester, by lowercased email."""
    return {p.email: p for p in cfg.people if p.email and p.id != me}


def crossing(
    tool_name: str,
    args: Any,
    current: space_mod.Space,
    cfg: space_mod.SpaceConfig,
    *,
    record_policies: dict[str, Any] | None = None,
) -> Crossing | None:
    """The crossing this call would make from ``current``, or ``None``.

    Only a PRIVATE space can cross. ``record_policies`` is the authored store
    map (``record_store.authored_policies``); it is read lazily when omitted.
    """
    if not current.private:
        return None
    name = (tool_name or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    me = current.person_id
    if name == RECORD_WRITE:
        from shared import record_store

        policies = (
            record_policies if record_policies is not None else record_store.authored_policies()
        )
        store = args.get("store")
        policy = policies.get(store) if isinstance(store, str) else None
        if policy is None:
            return None  # an unauthored store is the handler's refusal
        content = args.get("content") if isinstance(args.get("content"), str) else None
        if record_store.target_space(policy, content) != space_mod.SHARED:
            return None
        return Crossing(
            name, frozenset({f"record:{store}/{args.get('name')}"}), f"the shared {store} store"
        )
    if name == SEND_MESSAGE:
        target = str(args.get("target") or "").strip()
        own = cfg.chat_for_space(current)
        if own and target in (own, f"telegram:{own}"):
            return None
        return Crossing(name, frozenset({f"chat:{target}"}), "a chat outside this private one")
    if name.startswith(CALENDAR_INVITE_PREFIX):
        return _calendar_crossing(name, args, cfg, me)
    if name.startswith(WORKSPACE_PREFIX) and name not in _WORKSPACE_READS:
        return Crossing(
            name, frozenset({f"workspace:{name}"}), "a shared Google calendar, doc or folder"
        )
    if _is_email_send(name):
        to_others = extract_send_recipients(args) & set(_others(cfg, me))
        if not to_others:
            return None
        return Crossing(
            name, frozenset(f"mail:{a}" for a in sorted(to_others)), "an email to the other person"
        )
    return None


def _calendar_crossing(
    name: str, args: dict[str, Any], cfg: space_mod.SpaceConfig, me: str | None
) -> Crossing | None:
    """A calendar call that reaches anyone but the requester.

    The tool's arguments do not name its reach: ``calendar_invite_create`` takes
    PERSON IDS on a shared seat, and ``calendar_invite_update`` /
    ``calendar_invite_cancel`` take only a ``uid``, reaching the stored event's
    attendees. ``shared.calendar_reach`` answers the way the tool resolves it.
    Addresses written into the arguments count too. A reach that cannot be read
    and names nobody is a crossing (``invite:unresolved``): it is held, never
    waved through, and the tool refuses it after if it is still unresolvable."""
    from shared import calendar_reach

    attendees: set[str] = set()
    for key in _ATTENDEE_KEYS:
        attendees |= _emails(args.get(key))
    unresolved = False
    try:
        attendees |= calendar_reach.invite_reach(name, args)
    except calendar_reach.ReachUnknown:
        unresolved = not attendees
    mine = cfg.person(me or "")
    own_email = mine.email if mine is not None else ""
    others = {a for a in attendees if a != own_email}
    if unresolved:
        others.add("unresolved")
    if not others:
        return None
    return Crossing(
        name,
        frozenset(f"invite:{a}" for a in sorted(others)),
        "a calendar invite the other person receives",
    )


#: Set when the space gate plugin registers its hooks on this process. The
#: calendar tools' own R1 backstop (``hermes-smd-calendar/seat.py``) lets a
#: crossing through only when this is set AND :func:`crossing` sees the call,
#: because then this gate held it and a person released it.
_ARMED = False


def arm() -> None:
    """Called by ``hermes-smd-space-gate.register``."""
    global _ARMED
    _ARMED = True


def gate_armed() -> bool:
    return _ARMED


def exact_text(args: Any) -> str:
    """The payload a person confirms, exactly as it will be written: every
    argument, as JSON, keys sorted. It is the stored payload that lands, so the
    text shown and the text written are one string."""
    return json.dumps(
        args if isinstance(args, dict) else {}, ensure_ascii=False, indent=1, sort_keys=True
    )


__all__ = [
    "CALENDAR_INVITE_PREFIX",
    "Crossing",
    "MAX_CONFIRM_CHARS",
    "RECORD_WRITE",
    "SEND_MESSAGE",
    "SPACE_WRITES",
    "WORKSPACE_PREFIX",
    "arm",
    "crossing",
    "exact_text",
    "gate_armed",
    "is_candidate",
]
