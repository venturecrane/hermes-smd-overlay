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
* a calendar invitation (``calendar_invite_create`` / ``_update`` /
  ``_cancel``): a reach, read the way the tool resolves it
  (``shared.calendar_reach``: person ids, or a stored event's attendees by
  uid), that includes anyone but the requester; an unreadable reach crosses;
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
import time
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

#: Arg keys a calendar invite may name attendee addresses under.
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


def target_space(
    tool_name: str, args: Any, current: space_mod.Space | None, cfg: space_mod.SpaceConfig
) -> str | None:
    """The space key a call writes into, as this module judges it: ``shared``
    when it crosses out of a private space, else the turn's own key; ``None``
    when the turn's space is unresolved."""
    if current is None:
        return None
    return space_mod.SHARED if crossing(tool_name, args, current, cfg) else current.key


# ---------------------------------------------------------------------------
# Release tokens: how a tool's own R1 check learns the gate released THIS call.
#
# The gate (plugins/hermes-smd-space-gate) holds and releases in pre_tool_call.
# A tool that also checks the crossing itself, in its handler (the calendar
# tools, overlay #475, seat.private_to_shared_gate), asks :func:`in_tool_gate`,
# which passes a crossing only when the gate released this tool for this person
# in this chat moments ago, and spends that release. Without it the handler's
# own check would refuse the very call the person just confirmed.
# ---------------------------------------------------------------------------

_RELEASE_TTL_S = 120.0
_releases: dict[frozenset[tuple[str, str, str]], float] = {}

IN_TOOL_REFUSAL = (
    "this would put something from a private conversation in front of the other "
    "person, and it was not confirmed. Nothing was sent. The person confirms by "
    'replying "share it" in their own chat when the seat shows them the exact words.'
)


def _keys(tool_name: str, who: Any) -> set[tuple[str, str, str]]:
    """The keys a release is found by: the person's space (``person:<id>``, any
    channel) and, for a chat principal, its user and chat."""
    keys: set[tuple[str, str, str]] = set()
    if isinstance(who, space_mod.Space):
        if who.private and who.person_id:
            keys.add((tool_name, who.key, ""))
        return keys
    user = str(getattr(who, "user_id", "") or "")
    if user:
        keys.add((tool_name, user, str(getattr(who, "chat_id", "") or "")))
    return keys


def note_release(tool_name: str, principal: Any, space: space_mod.Space | None = None) -> None:
    """Called by the gate when it releases a confirmed call: one release, found
    by the confirming chat principal or by the person's space."""
    keys = _keys(tool_name, principal) | _keys(tool_name, space)
    if keys:
        _releases[frozenset(keys)] = time.monotonic() + _RELEASE_TTL_S


def take_release(tool_name: str, who: Any) -> bool:
    """Spend the release the gate noted for this tool and this person. ``who``
    is the turn's chat principal (``shared.turn_principal``) or its Space (the
    person, whichever channel confirmed). Single use across both keys."""
    now = time.monotonic()
    for k in [k for k, exp in _releases.items() if exp < now]:
        _releases.pop(k, None)
    wanted = _keys(tool_name, who)
    if not wanted:
        return False
    for k in list(_releases):
        if k & wanted:
            _releases.pop(k, None)
            return True
    return False


def in_tool_gate(act: str, current: space_mod.Space | None, target_key: str) -> str | None:
    """The R1 check for a tool's own handler. ``None`` when the write stays in
    its space, or when the space gate released this call after the person
    confirmed it; otherwise the refusal to return to the model."""
    if not space_mod.is_private_to_shared(current, target_key):
        return None
    from shared import turn_principal

    if take_release(act, current) or take_release(act, turn_principal.current()):
        return None
    return IN_TOOL_REFUSAL


# ---------------------------------------------------------------------------
# The confirmation notice: the exact payload, sent by code, never by the model.
# ---------------------------------------------------------------------------

NOTICE_HEAD = "About to share with both of you:"
NOTICE_TAIL = "Reply 'share it' to send."
_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_S = 10.0


def notice_text(payload: str) -> str:
    """The fixed frame around the stored payload, which appears unchanged."""
    return f"{NOTICE_HEAD}\n\n{payload}\n\n{NOTICE_TAIL}"


def post_notice(chat_id: str, text: str) -> None:
    """Send ``text`` to a Telegram chat through the Bot API. Raises on any failure.

    ``urllib``, not ``httpx`` (httpx logs request URLs, and a Bot API URL holds
    the token); an error is re-raised without the URL. Same posture as
    ``shared.group_audience``."""
    import urllib.parse
    import urllib.request

    from shared.secrets import get_secret

    token = get_secret("TELEGRAM_BOT_TOKEN").strip()
    if not token or not chat_id:
        raise RuntimeError("no bot token or no chat to send the notice to")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    # Scheme and host are the constant _TELEGRAM_API; only the token varies.
    req = urllib.request.Request(
        f"{_TELEGRAM_API}/bot{token}/sendMessage", data=data, method="POST"
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — strip the URL (it holds the token)
        raise RuntimeError(f"Bot API sendMessage failed: {type(exc).__name__}") from None
    if not isinstance(body, dict) or not body.get("ok"):
        raise RuntimeError("Bot API sendMessage returned not ok")


__all__ = [
    "CALENDAR_INVITE_PREFIX",
    "IN_TOOL_REFUSAL",
    "NOTICE_HEAD",
    "NOTICE_TAIL",
    "in_tool_gate",
    "note_release",
    "notice_text",
    "post_notice",
    "take_release",
    "target_space",
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
