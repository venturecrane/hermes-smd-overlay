"""Who a calendar tool call would reach: for the R1 private-to-shared gate.

WHY THIS EXISTS (ss-console#3264, ADR 0091 R1). The space write gate
(``shared.space_writes``, ``hermes-smd-space-gate``) judges a call from its
arguments, before it runs. A calendar call's arguments do not name addresses:

* ``calendar_invite_create`` names PERSON IDS on a seat more than one person
  shares (``attendees: ["p1", "p2"]``); the tool maps them to
  ``people[].email`` in code, so an address-only reading sees nobody;
* ``calendar_invite_update`` and ``calendar_invite_cancel`` name only the event's
  ``uid``. Whom they reach is the STORED event's attendees.

So the gate asks here, and this answers the way the tool itself resolves it.
Nothing here decides anything; it reads.

:func:`invite_reach` returns the addresses, or raises :class:`ReachUnknown`
when it cannot tell (an id that maps to nobody, a uid not in the store, an
unreadable config or store). A gate must treat "unknown" as a crossing: a call
whose audience could not be read is not one that stays private.
"""

from __future__ import annotations

from typing import Any

from shared import calendar_store, space

CREATE = "calendar_invite_create"
CHANGES = ("calendar_invite_update", "calendar_invite_cancel")


class ReachUnknown(RuntimeError):
    """Whom the call would reach could not be read. Treat as a crossing."""


def invite_reach(tool_name: str, args: Any) -> frozenset[str]:
    """The addresses this calendar call would send an invitation to (lowercased).

    Empty for a tool that sends nothing (``calendar_invite_list``)."""
    name = (tool_name or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    if name == CREATE:
        return _created(args.get("attendees"))
    if name in CHANGES:
        uid = args.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise ReachUnknown("no uid")
        try:
            event = calendar_store.STORE.get(uid.strip())
        except calendar_store.CalendarStoreError as exc:
            raise ReachUnknown(str(exc)) from exc
        if event is None:
            raise ReachUnknown("no such event")
        return frozenset(a.lower() for a in event.attendee_emails())
    return frozenset()


def _created(attendees: Any) -> frozenset[str]:
    if not isinstance(attendees, list):
        raise ReachUnknown("attendees is not a list")
    try:
        cfg = space.space_config()
    except space.SpaceConfigError as exc:
        raise ReachUnknown(str(exc)) from exc
    out: set[str] = set()
    for item in attendees:
        if not isinstance(item, str) or not item.strip():
            raise ReachUnknown("an attendee is not a name")
        value = item.strip().lower()
        if cfg is None:
            out.add(value)  # a seat without people: the model names addresses
            continue
        person = cfg.person(value)
        if person is None or not person.email:
            raise ReachUnknown(f"{value!r} is not one of the seat's people")
        out.add(person.email)
    return frozenset(out)


__all__ = ["CHANGES", "CREATE", "ReachUnknown", "invite_reach"]
