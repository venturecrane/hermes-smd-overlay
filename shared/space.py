"""Which space a turn is in, on a seat that more than one person shares.

WHY THIS EXISTS (ss-console #3264, ADR 0091 "privacy by space"). A couple shares
one hosted agent. Each partner talks to it privately in their own Telegram DM
and from their own approved email address, and together in one Telegram group.
What partner A says in A's DM must never reach partner B: not through memory,
not through a record, not through a group reply, not through a calendar invite.

The unit of privacy is the SPACE. There are two kinds:

* ``person:<id>`` — one person's private space (their DM, their email);
* ``shared`` — the couple's shared space (the authored group chat).

A turn in ``person:X`` may read ``shared`` and ``person:X``. A turn in ``shared``
may read only ``shared``. A turn whose space cannot be resolved reads NOTHING.

THE SPACE IS STRUCTURAL, NEVER FROM THE PROMPT. Nothing the model writes, and
nothing a person types, moves a turn between spaces. The resolver reads only:

1. a cron run: the run's delivery target (``HERMES_CRON_AUTO_DELIVER_*``, set
   per job by Hermes' scheduler from the job's authored ``deliver``). A routine
   authored ``space: person:X`` delivers to X's DM, whose chat id is X's
   Telegram id; ``space: shared`` delivers to the group. The run's space and its
   delivery target are one authored value, so they cannot disagree;
2. a Telegram turn: Hermes' per-turn session context (``HERMES_SESSION_PLATFORM``
   / ``CHAT_TYPE`` / ``CHAT_ID`` / ``USER_ID``, ``gateway/session_context.py``).
   A DM from a person's Telegram id is that person's space; a message in the
   authored shared group is ``shared``; any other chat is unresolved;
3. an email turn: the Svix-verified sender the webhook router recorded for the
   session (``shared.inbound.SESSION_INBOUND_ORIGIN``), mapped to a person by
   their authored address. When that message's own verified To/Cc also
   includes ANOTHER person's address it is a joint thread, ``shared``
   (source ``email_joint``); otherwise it is the sender's space;
4. anything else: unresolved (``None``).

SPACE MODE IS AUTHORED. A seat is in space mode iff its customer.yaml authors a
``people:`` block. Every enforcement point asks :func:`space_mode` first, and a
seat that authors no ``people:`` is byte-for-byte unchanged: the resolver is
never consulted there. On a space-mode seat every enforcement point FAILS CLOSED
on an unresolved space. A malformed ``people:``/``spaces:`` block raises
:class:`SpaceConfigError`, which callers turn into a refusal: "could not tell
whose turn this is" must never read as "nobody's, so open".

The one exception is a seat with no customer.yaml at all (a bare image, the test
fan-out): that is not a seat this module can say anything about, and it is read
as "not space mode", the same posture ``shared.record_store`` takes. A real seat
never boots without the file.

This module is the ONE resolver. The record-store fence, space memory, the
unresolved-space write refusal, cron delivery and calendar invites all import it,
so they cannot disagree about whose turn it is.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.customer_config import (
    DEFAULT_VOLUME_PATH,
    CustomerConfig,
    CustomerConfigError,
    CustomerConfigMissingError,
)

#: The space keys. ``person:<id>`` is built by :func:`person_key`.
SHARED = "shared"
_PERSON_PREFIX = "person:"

#: An authored person id: a stable slug.
PERSON_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
#: A Telegram user id (a person's DM chat id is the same number).
TELEGRAM_USER_RE = re.compile(r"^[0-9]{1,20}$")
#: A Telegram group chat id: negative.
TELEGRAM_GROUP_RE = re.compile(r"^-[0-9]{1,20}$")

_TELEGRAM = "telegram"
_DM = "dm"

# Where a space was resolved from. Carried on the Space for logs and refusals.
SOURCE_CRON = "cron"
SOURCE_TELEGRAM_DM = "telegram_dm"
SOURCE_TELEGRAM_GROUP = "telegram_group"
SOURCE_EMAIL = "email"
#: An email from one person that also went to another person: a joint thread.
SOURCE_EMAIL_JOINT = "email_joint"


class SpaceConfigError(ValueError):
    """The seat's ``people:`` / ``spaces:`` block is present but cannot be read.

    Callers refuse on this; they never treat it as "not space mode"."""


@dataclass(frozen=True)
class Space:
    """The space a turn runs in. ``person_id`` is set iff ``kind == "person"``."""

    kind: str  # "person" | "shared"
    person_id: str | None
    source: str

    @property
    def key(self) -> str:
        """``person:<id>`` or ``shared``: the value stored on a space-keyed row."""
        return person_key(self.person_id or "") if self.kind == "person" else SHARED

    @property
    def private(self) -> bool:
        return self.kind == "person"


#: How a person prefers the seat to reach them (``people[].channel``). Email is
#: the default (Captain, 2026-10-17): Telegram is optional and connected later.
CHANNEL_EMAIL = "email"
CHANNEL_TELEGRAM = "telegram"
CHANNELS = frozenset({CHANNEL_EMAIL, CHANNEL_TELEGRAM})


@dataclass(frozen=True)
class Person:
    """One authored person. Empty ``telegram_id`` / ``email`` mean "not bound".

    ``channel`` is how the seat reaches them on its own initiative (routines,
    hellos): ``telegram`` only when authored AND bound, else email.
    It never changes where a REPLY goes: a reply answers in the chat it was
    asked in."""

    id: str
    name: str
    telegram_id: str
    email: str
    channel: str = CHANNEL_EMAIL

    @property
    def reached_by_telegram(self) -> bool:
        """True when the seat should reach this person in their Telegram DM."""
        return self.channel == CHANNEL_TELEGRAM and bool(self.telegram_id)


@dataclass(frozen=True)
class SpaceConfig:
    """The authored people and the shared group, as the resolver needs them."""

    people: tuple[Person, ...]
    shared_telegram_chat: str

    def person(self, person_id: str) -> Person | None:
        return next((p for p in self.people if p.id == person_id), None)

    def person_for_telegram(self, user_id: Any) -> Person | None:
        uid = str(user_id or "").strip()
        if not uid:
            return None
        return next((p for p in self.people if p.telegram_id and p.telegram_id == uid), None)

    def person_for_email(self, address: Any) -> Person | None:
        addr = str(address or "").strip().lower()
        if not addr:
            return None
        return next((p for p in self.people if p.email and p.email == addr), None)

    def space_for_key(self, key: Any, *, source: str) -> Space | None:
        """The Space an authored key (``person:<id>`` / ``shared``) names, or None."""
        if key == SHARED:
            return Space(kind="shared", person_id=None, source=source)
        if isinstance(key, str) and key.startswith(_PERSON_PREFIX):
            person = self.person(key[len(_PERSON_PREFIX) :])
            if person is not None:
                return Space(kind="person", person_id=person.id, source=source)
        return None

    def delivery_chat_for_space(self, space: Space) -> str:
        """The Telegram chat the seat delivers a space's ROUTINE to, honouring
        each person's authored ``channel``: a person's DM only when they prefer
        Telegram and are bound; the shared group only when it is bound and every
        person prefers Telegram (otherwise each is emailed). "" means email."""
        if space.kind == "shared":
            if self.people and all(p.reached_by_telegram for p in self.people):
                return self.shared_telegram_chat
            return ""
        person = self.person(space.person_id or "")
        return person.telegram_id if person is not None and person.reached_by_telegram else ""

    def chat_for_space(self, space: Space) -> str:
        """The Telegram chat a space's messages go to ("" when not bound yet)."""
        if space.kind == "shared":
            return self.shared_telegram_chat
        person = self.person(space.person_id or "")
        return person.telegram_id if person is not None else ""


def person_key(person_id: str) -> str:
    return f"{_PERSON_PREFIX}{person_id}"


def readable_spaces(space: Space | None) -> frozenset[str]:
    """The space keys a turn in ``space`` may read.

    ``person:X`` reads ``{shared, person:X}``; ``shared`` reads ``{shared}``;
    ``None`` reads nothing."""
    if space is None:
        return frozenset()
    if space.kind == "person" and space.person_id:
        return frozenset({SHARED, person_key(space.person_id)})
    if space.kind == "shared":
        return frozenset({SHARED})
    return frozenset()


def may_read(space: Space | None, target_key: Any) -> bool:
    """Whether a turn in ``space`` may read an item stored under ``target_key``."""
    return isinstance(target_key, str) and target_key in readable_spaces(space)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SpaceConfigError(f"expected a string, got {type(value).__name__}")
    return str(value).strip()


def parse_space_config(raw: dict[str, Any]) -> SpaceConfig | None:
    """The space config a parsed customer.yaml authors, or ``None`` (no ``people:``).

    Raises :class:`SpaceConfigError` on any malformed entry. The ss-console
    validator rejects the same shapes at authoring time; this is the runtime half,
    which must not trust that it ran."""
    if "people" not in raw or raw.get("people") is None:
        return None
    people_raw = raw.get("people")
    if not isinstance(people_raw, list) or not people_raw:
        raise SpaceConfigError("people must be a non-empty list")
    people: list[Person] = []
    seen_ids: set[str] = set()
    seen_tg: set[str] = set()
    seen_email: set[str] = set()
    for i, entry in enumerate(people_raw):
        if not isinstance(entry, dict):
            raise SpaceConfigError(f"people[{i}] must be a mapping")
        pid = _str(entry.get("id"))
        if not PERSON_ID_RE.match(pid):
            raise SpaceConfigError(f"people[{i}].id must be a slug (a-z, 0-9, -)")
        if pid in seen_ids:
            raise SpaceConfigError(f"people: id {pid!r} is authored twice")
        seen_ids.add(pid)
        tg = _str(entry.get("telegram_id"))
        if tg and not TELEGRAM_USER_RE.match(tg):
            raise SpaceConfigError(f"people[{i}].telegram_id must be a numeric string")
        if tg and tg in seen_tg:
            raise SpaceConfigError(f"people: telegram_id {tg} is authored twice")
        email = _str(entry.get("email")).lower()
        if email and ("@" not in email or " " in email):
            raise SpaceConfigError(f"people[{i}].email is not an address")
        if email and email in seen_email:
            raise SpaceConfigError(f"people: email {email} is authored twice")
        if tg:
            seen_tg.add(tg)
        if email:
            seen_email.add(email)
        channel = _str(entry.get("channel")).lower() or CHANNEL_EMAIL
        if channel not in CHANNELS:
            raise SpaceConfigError(f"people[{i}].channel must be 'email' or 'telegram'")
        people.append(
            Person(
                id=pid,
                name=_str(entry.get("name")),
                telegram_id=tg,
                email=email,
                channel=channel,
            )
        )
    spaces = raw.get("spaces")
    if not isinstance(spaces, dict):
        raise SpaceConfigError("spaces must be a mapping when people is authored")
    group = _str(spaces.get("shared_telegram_chat"))
    if group and not TELEGRAM_GROUP_RE.match(group):
        raise SpaceConfigError("spaces.shared_telegram_chat must be a negative numeric string")
    return SpaceConfig(people=tuple(people), shared_telegram_chat=group)


# Parsed-config cache keyed on the file's identity, so the resolver (called on
# every gated tool call) does not re-parse YAML each time, and an edited file is
# picked up without a restart. Single tenant per Machine (AGENTS.md #5).
_cache: dict[str, tuple[tuple[int, int], SpaceConfig | None]] = {}


def _config_path() -> str:
    return os.environ.get("SMD_CUSTOMER_YAML_PATH") or DEFAULT_VOLUME_PATH


def space_config(config: CustomerConfig | None = None) -> SpaceConfig | None:
    """The seat's space config, or ``None`` when the seat authors no ``people:``.

    ``config`` is the test seam; production reads the volume. A missing file is
    "not space mode" (see the module docstring); any other unreadable state
    raises :class:`SpaceConfigError`."""
    if config is not None:
        return parse_space_config(config.raw)
    path = _config_path()
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SpaceConfigError(f"customer.yaml unreadable: {exc}") from exc
    ident = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit is not None and hit[0] == ident:
        return hit[1]
    try:
        loaded = CustomerConfig.from_volume(path)
    except CustomerConfigMissingError:
        return None
    except CustomerConfigError as exc:
        raise SpaceConfigError(f"customer.yaml unreadable: {exc}") from exc
    parsed = parse_space_config(loaded.raw)
    _cache[path] = (ident, parsed)
    return parsed


def space_mode(config: CustomerConfig | None = None) -> bool:
    """True iff this seat authors ``people:``. Raises SpaceConfigError when it
    cannot tell."""
    return space_config(config) is not None


def _reset_cache_for_tests() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _session_env(name: str) -> str:
    """One Hermes per-turn session value: the gateway ContextVar when importable,
    else ``os.environ`` (Hermes' own fallback). Never raises."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — outside the gateway, env is the source
        return str(os.environ.get(name, "") or "").strip()
    try:
        return str(get_session_env(name, "") or "").strip()
    except Exception:  # noqa: BLE001 — an unreadable context is an unknown one
        return ""


def _resolve_cron(cfg: SpaceConfig, session_id: Any = None) -> Space | None:
    platform = _session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM").lower()
    if not platform and session_id:
        # ``deliver: local`` publishes no target: the routine's Telegram is not
        # bound and it is delivered by email (shared.cron_space). Its space is
        # the one its managed job name was materialized for, read from the
        # run's own ``cron_<job_id>_...`` session id. Never another platform's
        # target, and never a guess: an unreadable job is no space.
        from shared import cron_space  # local: cron_space imports this module

        return cfg.space_for_key(cron_space.routine_space_key(session_id), source=SOURCE_CRON)
    if platform != _TELEGRAM:
        return None
    chat = _session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID")
    if not chat:
        return None
    if cfg.shared_telegram_chat and chat == cfg.shared_telegram_chat:
        return Space(kind="shared", person_id=None, source=SOURCE_CRON)
    person = cfg.person_for_telegram(chat)
    if person is not None:
        return Space(kind="person", person_id=person.id, source=SOURCE_CRON)
    return None


def _resolve_telegram(cfg: SpaceConfig) -> Space | None:
    chat_type = _session_env("HERMES_SESSION_CHAT_TYPE").lower()
    chat_id = _session_env("HERMES_SESSION_CHAT_ID")
    user_id = _session_env("HERMES_SESSION_USER_ID")
    if chat_type == _DM:
        # A Telegram DM's chat id is the user's id; a DM whose chat says
        # otherwise is not a shape this resolver will vouch for.
        if chat_id and chat_id != user_id:
            return None
        person = cfg.person_for_telegram(user_id)
        if person is None:
            return None
        return Space(kind="person", person_id=person.id, source=SOURCE_TELEGRAM_DM)
    if chat_id and cfg.shared_telegram_chat and chat_id == cfg.shared_telegram_chat:
        return Space(kind="shared", person_id=None, source=SOURCE_TELEGRAM_GROUP)
    return None


def _resolve_email(cfg: SpaceConfig, session_id: str) -> Space | None:
    if not session_id:
        return None
    try:
        from shared.inbound import SESSION_INBOUND_ORIGIN
    except Exception:  # noqa: BLE001 — no register, no verified sender
        return None
    origin = SESSION_INBOUND_ORIGIN.get(session_id)
    if origin is None or not origin.sender_address:
        return None
    person = cfg.person_for_email(origin.sender_address)
    if person is None:
        return None
    # A joint thread: the verified To/Cc of this very message (the webhook
    # router copies them off the signed payload) includes another person.
    # Never the body, never a name in the text.
    recipients = {str(r).strip().lower() for r in getattr(origin, "recipients", ()) or ()}
    others = {p.email for p in cfg.people if p.email and p.id != person.id}
    if recipients & others:
        return Space(kind="shared", person_id=None, source=SOURCE_EMAIL_JOINT)
    return Space(kind="person", person_id=person.id, source=SOURCE_EMAIL)


def resolve_space(
    session_id: Any = None, *, config: CustomerConfig | SpaceConfig | None = None
) -> Space | None:
    """The space the running turn is in, or ``None`` when it cannot be resolved.

    ``session_id`` is the Hermes session the hook or tool runs under (used only for
    the email origin); when omitted, ``HERMES_SESSION_ID`` from the session context
    is used. Returns ``None`` on a seat that is not in space mode as well: callers
    decide space mode with :func:`space_mode` first and never consult the resolver
    otherwise. Raises :class:`SpaceConfigError` when the people block is malformed.
    """
    cfg = config if isinstance(config, SpaceConfig) else space_config(config)
    if cfg is None:
        return None
    if _session_env("HERMES_CRON_SESSION") == "1":
        # A cron run is bound to its authored target and to nothing else: the
        # session vars are cleared for it, and an email origin cannot apply.
        return _resolve_cron(cfg, session_id)
    if _session_env("HERMES_SESSION_PLATFORM").lower() == _TELEGRAM:
        return _resolve_telegram(cfg)
    sid = str(session_id or "").strip() or _session_env("HERMES_SESSION_ID")
    return _resolve_email(cfg, sid)


def person_for_sender(
    sender: Any, *, config: CustomerConfig | SpaceConfig | None = None
) -> Person | None:
    """The authored person a channel identity belongs to (a Telegram user id or an
    email address), or ``None``. One person, however many channels: this is what
    unifies a partner's Telegram id and email into one person id."""
    cfg = config if isinstance(config, SpaceConfig) else space_config(config)
    if cfg is None:
        return None
    value = str(sender or "").strip()
    if not value:
        return None
    if "@" in value:
        return cfg.person_for_email(value)
    return cfg.person_for_telegram(value)


__all__ = [
    "PERSON_ID_RE",
    "SHARED",
    "SOURCE_CRON",
    "SOURCE_EMAIL",
    "SOURCE_EMAIL_JOINT",
    "SOURCE_TELEGRAM_DM",
    "SOURCE_TELEGRAM_GROUP",
    "Person",
    "Space",
    "SpaceConfig",
    "SpaceConfigError",
    "may_read",
    "parse_space_config",
    "person_for_sender",
    "person_key",
    "readable_spaces",
    "resolve_space",
    "space_config",
    "space_mode",
]
