"""Shared escalation-telemetry ledger (WP-A / WP-B).

CANONICAL SOURCE: ``operator/workspace_broker/escalation_ledger.py``. Byte-identical
copies are vendored into each consuming skill directory as ``escalation_ledger.py``
so a skill's ``pre_run.py`` (and the agent's ``execute_code`` turn) can import it
without a package install; ``operator/tests/test_escalation_ledger_sync.py``
enforces the sync. Edit here, restamp the copies.

What this ledger IS
-------------------
An append-only JSONL of **operator communication telemetry** — when a skill
fired an escalation on an item, when a human acked it, when it was handed off or
resolved. It is the same class of state as the audit journal (the broker owns
the write handle; the agent uid reads it but cannot forge a row). It is NOT the
firm's system of record: matter work state still re-derives from Smokeball. If
the volume state is lost, items re-fire once (annoying, never dangerous) and the
Smokeball memos let a person reconstruct history.

Record shape (one JSON object per line)::

    {"v":2,"ts":<iso>,"skill":<str>,"matter_id":<str|null>,
     "item_key":<hex>,"event":<fired|chased|acked|handed_off|resolved>,
     "attempt":<int>,"token":<ACK-XXXXXX|null>,"id":<ulid, broker-stamped>}

``v`` is also the item-identity epoch — see ``IDENTITY_EPOCH`` and ``item_key``.
A row below the epoch keyed the same deadline differently and cannot be joined
against, or acked, by anything current.

The security line
-----------------
An ``acked`` event silences a deadline alarm, so it must not be writable by an
injectable surface without validation. ``validate_append`` REJECTS an ``acked``
whose token has no prior ``fired``/``chased`` event. The LLM turn never writes
the file directly; every write goes through the broker's uid-gated
``escalation_event_append`` verb, which calls ``validate_append`` and stamps
``ts``/``id`` server-side (the agent cannot backdate).

Pure stdlib. No imports from other ``workspace_broker`` modules, so the vendored
copy is safe to load standalone inside a skill.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

SCHEMA_VERSION = 2

# The item-identity epoch. Raises written below this version had their
# ``item_key`` derived by a different function (it hashed the model-composed
# ``label``; see ``item_key``), so a pre-epoch key can never name a live item.
# Acking one would tell a human an alarm was silenced when nothing changed, which
# is why ``validate_append`` refuses it by name rather than accepting it as a
# harmless no-op. Pre-epoch rows are otherwise left in place: they are history,
# and their keys cannot collide with current ones.
IDENTITY_EPOCH = 2

# The full event vocabulary. A ``fired`` or ``chased`` is a "raise" — an alarm
# surfaced to a human; an ``acked`` references a prior raise; ``handed_off`` and
# ``resolved`` are terminal for autonomous re-firing.
EVENTS: tuple[str, ...] = ("fired", "chased", "acked", "handed_off", "resolved")
RAISING_EVENTS: tuple[str, ...] = ("fired", "chased")
_REQUIRED_KEYS: tuple[str, ...] = ("ts", "skill", "item_key", "event")

# Agent read path (broker writes the same inode via the /run/smd-audit bind).
# Override with SMD_ESCALATION_LEDGER_PATH (tests, and the broker's write side).
DEFAULT_LEDGER_PATH = "/opt/data/audit/escalation-ledger.jsonl"

# Crockford base32 minus I L O U — the human types the token back off an email.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ledger_path() -> str:
    """The escalation ledger path: env override, else the agent-read default."""
    return os.environ.get("SMD_ESCALATION_LEDGER_PATH") or DEFAULT_LEDGER_PATH


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# Placeholders a caller may hand in for a component it could not READ off the
# source record. ``_matter_id_of`` in both skills' ``pre_run.py`` emits
# "unknown-matter" when the Smokeball payload carries no resolvable matter link.
# A sentinel is a fabricated component: an identity built on one moves the moment
# the real value arrives, so it cannot carry a per-item ACK code
# (see ``has_stable_identity``). It is NOT excluded from ``item_key`` — the item
# still fires, it just acks with the blanket code.
UNKNOWN_SENTINELS: frozenset[str] = frozenset({"unknown-matter"})


def _normalize_id(value) -> str:
    """Canonical form of an identifier component of the item key.

    The connector reads Smokeball GUIDs off the wire and passes them through
    verbatim (``_source_id_of`` returns ``str(value)``); the agent's tool arg is
    schema-typed ``string`` and the model may pad or re-case what it retypes. Two
    spellings of ONE id must not be two items, so both sides fold here — strip,
    then case-fold. Smokeball ids are ASCII GUIDs, where case carries no meaning
    and two ids differing only in case cannot exist.
    """
    if value is None:
        return ""
    return str(value).strip().casefold()


def _as_iso_date(value) -> str:
    """Canonical ``YYYY-MM-DD`` for the date component of the item key, or ``""``
    when the caller's identity convention omits it (the verification chase does).

    Every caller spells this differently and all of them mean one day. ``pre_run``
    reads a ``date`` off the record; the append tool's ``authored_date`` is a
    schema ``string``, so the model has written the bare day, the full timestamp
    the Smokeball payload carried, and (via ``execute_code``) a ``datetime``
    handed straight through — which the old ``isinstance(value, date)`` branch
    turned into ``2026-08-11T14:32:07+00:00``, since ``datetime`` subclasses
    ``date``. Each spelling was its own item.

    The date is taken AS WRITTEN — no timezone conversion. The connector's
    ``_parse_iso_date`` reads ``value[:10]``, so a 23:00-0700 record is the 11th
    on both sides; shifting to UTC here would fork the join it exists to make.

    Unparseable input RAISES rather than hashing verbatim: a component the module
    cannot canonicalize is exactly how "tomorrow" and "2026-08-11" became two
    identities, and a rejected tool arg is visible to the turn while it can still
    be fixed. Silent acceptance is not.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):  # MUST precede date — datetime subclasses it
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        raise ValueError(
            f"authored_date {value!r} is not an ISO-8601 date or datetime; pass "
            "YYYY-MM-DD (or null when the skill's identity convention omits the "
            "date). An uncanonical date component forks item identity — the same "
            "deadline becomes two items and every ACK code names one of them."
        ) from None


# ---------------------------------------------------------------------------
# Item identity + token
# ---------------------------------------------------------------------------


def item_key(matter_id, source_id, label, authored_date) -> str:
    """Stable per-item key: sha256 hex of (matter_id, Smokeball task/event id,
    authored date). The ``source_id`` is the load-bearing anti-collision field —
    two same-day tasks on one matter differ only by it. Callers pass
    ``source_id=None`` ONLY for items with no stable id; those get no per-item
    token and render in the blanket-ack-only group (see ``has_stable_identity``).

    ``label`` is ACCEPTED AND DELIBERATELY IGNORED (ss #2151). It was in the hash
    until the identity epoch below, and it is model-composed free text: ``pre_run``
    assigns it from a closed set (``task-deadline`` / ``court-date``) while the
    agent's turn writes a descriptor it invents that run (``settlement-offer-lapsed``,
    ``rfa-confirm-service-date``). The two halves therefore hashed the SAME deadline
    to different keys, so the ledger join never matched: on the pilot seat, 160
    events had produced 128 item states, and NONE of them corresponded to any open
    Smokeball task. Fire-once and the seven-day ack snooze were both inert — every
    in-range item re-fired every run and every per-item ACK code named a phantom.

    The parameter survives only so the two call sites (this repo's ``pre_run.py``
    and the overlay's escalation plugin) keep working without a lockstep cross-repo
    signature change. Nothing may put it back in the hash;
    ``test_item_key_ignores_label`` is the guard.

    Every surviving component is NORMALIZED before hashing (ss #2289): ids are
    stripped and case-folded, the date is canonicalized to ``YYYY-MM-DD`` and an
    unparseable one raises. The residual of the same defect: ``label`` was not the
    only key component the model typed by hand — ``matter_id``, ``source_id`` and
    ``authored_date`` are all free-text tool args (see the append tool's schema),
    so ``2026-08-11`` and ``2026-08-11T00:00:00Z`` were two items on one deadline
    and nothing rejected the second.
    """
    raw = "\x1f".join(
        (
            _normalize_id(matter_id),
            _normalize_id(source_id),
            _as_iso_date(authored_date),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def has_stable_identity(source_id, matter_id) -> bool:
    """True iff this item's identity tuple is built ENTIRELY from values read off
    the source, so it can hold a per-item ACK token. Otherwise the item is
    blanket-ack only: it still fires, it just has no code of its own.

    Two ways to fail. No ``source_id`` — the item has no stable Smokeball id, and
    a token keyed on the matter alone would silence every item on that matter.
    Or a sentinel in either position — ``pre_run``'s ``_matter_id_of`` emits
    ``"unknown-matter"`` when the payload carries no resolvable matter link, and
    a key with a fabricated component moves the moment the real value arrives, so
    the code printed today names nothing tomorrow.

    ``matter_id`` is REQUIRED, not defaulted (ss #2289 fix 2). The guard used to
    test ``source_id`` against the sentinel — but ``_source_id_of`` never emits
    it and ``_matter_id_of`` does, so the exclusion could not fire on any row the
    connector writes: a control pointed at the wrong field measures nothing. An
    optional second argument would have restored exactly that hole for any caller
    that omitted it.
    """
    if not _normalize_id(source_id):
        return False
    return not ({_normalize_id(source_id), _normalize_id(matter_id)} & UNKNOWN_SENTINELS)


def token_for(key_hex: str) -> str:
    """A short human-typable ack token derived from the item_key. Deterministic:
    any reader recomputes it, so a reply quoting ``ACK-7Q3M2K`` maps back to its
    item without a lookup table. Six Crockford chars ~= 1e9 space; collisions
    across a firm's live item set are negligible and would only under-ack (the
    confirmation reply enumerates what was acked, so a mismatch stays visible)."""
    digest = hashlib.sha256(("ack-token:" + key_hex).encode("utf-8")).digest()
    n = int.from_bytes(digest[:5], "big")
    out = []
    for _ in range(6):
        n, rem = divmod(n, 32)
        out.append(_CROCKFORD[rem])
    return "ACK-" + "".join(reversed(out))


# ---------------------------------------------------------------------------
# Event construction + (de)serialization
# ---------------------------------------------------------------------------


def make_event(
    *,
    skill: str,
    matter_id,
    item_key: str,
    event: str,
    attempt: int,
    token: str | None = None,
    ts: str | None = None,
) -> dict:
    """Build a well-formed event dict. ``ts`` is normally left None so the broker
    stamps it server-side (the agent cannot backdate)."""
    if event not in EVENTS:
        raise ValueError(f"unknown escalation event {event!r}; expected one of {EVENTS}")
    row: dict = {
        "v": SCHEMA_VERSION,
        "ts": ts,
        "skill": str(skill),
        "matter_id": None if matter_id is None else str(matter_id),
        "item_key": str(item_key),
        "event": event,
        "attempt": int(attempt),
        "token": None if token is None else str(token),
    }
    return row


def serialize_event(event: dict) -> str:
    """One canonical JSON line (no trailing newline)."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def _valid_event(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in _REQUIRED_KEYS:
        if key not in obj:
            return False
    return obj.get("event") in EVENTS


def read_ledger(path: str | None = None) -> list[dict]:
    """Read every parseable event from the JSONL, oldest first.

    Corrupt or unparseable lines are SKIPPED, never guessed at — fail-noisy: a
    dropped line means that item is treated as if it had no such event (an ack
    we cannot read stays un-acked, so a deadline keeps firing rather than going
    silent). A missing file is an empty ledger (first run).
    """
    path = path or ledger_path()
    try:
        with open(path, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    events: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if _valid_event(obj):
            events.append(obj)
    return events


# ---------------------------------------------------------------------------
# Per-item state derivation
# ---------------------------------------------------------------------------


@dataclass
class ItemState:
    """Ledger-derived state for one item_key. ``attempts`` counts raises
    (fired + chased). ``acked`` is reset by any raise that follows an ack, so a
    re-surfaced item is awaiting a fresh ack, not still silenced by the old one.
    """

    item_key: str
    matter_id: str | None = None
    token: str | None = None
    last_raised_ts: str | None = None
    last_raised_date: date | None = None
    attempts: int = 0
    acked: bool = False
    last_acked_date: date | None = None
    handed_off: bool = False
    resolved: bool = False


def _parse_ts_date(ts) -> date | None:
    if not isinstance(ts, str) or len(ts) < 10:
        return None
    try:
        return date.fromisoformat(ts[:10])
    except ValueError:
        return None


def derive_state(events) -> dict[str, ItemState]:
    """Fold events (any order) into per-item_key state. Events are sorted by
    ``ts`` so ack-then-refire vs refire-then-ack resolve deterministically."""
    ordered = sorted(events, key=lambda e: str(e.get("ts") or ""))
    states: dict[str, ItemState] = {}
    for event in ordered:
        key = str(event.get("item_key") or "")
        if not key:
            continue
        state = states.get(key)
        if state is None:
            state = ItemState(item_key=key)
            states[key] = state
        if event.get("matter_id") is not None:
            state.matter_id = str(event.get("matter_id"))
        if event.get("token") is not None:
            state.token = str(event.get("token"))
        kind = event.get("event")
        ts = event.get("ts")
        if kind in RAISING_EVENTS:
            state.attempts += 1
            state.last_raised_ts = ts if isinstance(ts, str) else state.last_raised_ts
            parsed = _parse_ts_date(ts)
            if parsed is not None:
                state.last_raised_date = parsed
            # A fresh raise supersedes any prior ack: the item is loud again.
            state.acked = False
        elif kind == "acked":
            state.acked = True
            parsed = _parse_ts_date(ts)
            if parsed is not None:
                state.last_acked_date = parsed
        elif kind == "handed_off":
            state.handed_off = True
        elif kind == "resolved":
            state.resolved = True
    return states


def next_attempt(state: ItemState | None) -> int:
    """The attempt number the next raise on this item will carry."""
    return 1 if state is None else state.attempts + 1


def should_fire(
    state: ItemState | None,
    today: date,
    *,
    refire_days: int,
    ack_snooze_days: int,
) -> bool:
    """Should this in-range item raise an alarm on today's tick?

    - never raised            -> fire (attempt 1)
    - resolved / handed_off   -> never (terminal; a person owns it)
    - acked, still unresolved -> re-surface only after ``ack_snooze_days``
                                 (ack is a snooze, not a tombstone)
    - raised, not acked       -> re-fire only after ``refire_days``
                                 (fire once, not daily)
    """
    if state is None:
        return True
    if state.resolved or state.handed_off:
        return False
    if state.acked:
        if state.last_acked_date is None:
            return True
        return today >= state.last_acked_date + timedelta(days=max(0, ack_snooze_days))
    if state.last_raised_date is None:
        return True
    return today >= state.last_raised_date + timedelta(days=max(0, refire_days))


# ---------------------------------------------------------------------------
# Append (broker-side write path)
# ---------------------------------------------------------------------------


def is_pre_identity_epoch(event) -> bool:
    """True for a raise written before the ss #2151 identity fix. PUBLIC so the
    overlay's escalation plugin resolves ack tokens against the same rule the
    broker validates with — a second implementation there would be a second
    authority over one decision, and the two would disagree the first time
    either changed. Its ``item_key``
    came from a different derivation (it hashed the model-composed label), so it
    can never name a live item. Acking one would tell a human an alarm was
    silenced when nothing changed — the exact class of false report the fix
    exists to end. A row with a missing or unparseable ``v`` is treated as
    pre-epoch: unknown provenance is not evidence of a current key."""
    try:
        return int(event.get("v") or 1) < IDENTITY_EPOCH
    except (TypeError, ValueError):
        return True


def validate_append(existing_events, new_event: dict) -> None:
    """Raise ValueError unless ``new_event`` is a well-formed event that may be
    appended. The load-bearing rule: an ``acked`` MUST reference a prior
    ``fired``/``chased`` with the same token (or item_key) — you cannot ack an
    alarm that never rang."""
    if not isinstance(new_event, dict):
        raise ValueError("escalation event must be an object")
    kind = new_event.get("event")
    if kind not in EVENTS:
        raise ValueError(f"unknown escalation event {kind!r}; expected one of {EVENTS}")
    if not str(new_event.get("item_key") or "").strip():
        raise ValueError("escalation event requires a non-empty item_key")
    if not str(new_event.get("skill") or "").strip():
        raise ValueError("escalation event requires a skill")
    if kind == "acked":
        token = new_event.get("token")
        key = str(new_event.get("item_key") or "")
        raised = False
        stale_only = False
        for prior in existing_events:
            if prior.get("event") not in RAISING_EVENTS:
                continue
            if token is not None and prior.get("token") == token:
                pass
            elif prior.get("item_key") == key:
                pass
            else:
                continue
            if is_pre_identity_epoch(prior):
                # Matches, but its key came from the superseded derivation, so it
                # names nothing live. Keep looking for a current raise.
                stale_only = True
                continue
            raised = True
            break
        if not raised:
            if stale_only:
                raise ValueError(
                    "that ACK code was issued before the item-identity fix (ss #2151) and no "
                    "longer names a live item; the deadline will re-raise with a current code. "
                    "Do not report it as acknowledged"
                )
            raise ValueError("acked event has no prior fired/chased raise for its token/item_key")


def _ulid() -> str:
    ts = int(time.time() * 1000)
    n = (ts << 80) | secrets.randbits(80)
    out = []
    for _ in range(26):
        n, rem = divmod(n, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def stamp_event(event: dict) -> dict:
    """Return a copy with a server-stamped ``ts`` (if absent) and a fresh ``id``.
    Called by the broker so the caller cannot backdate or set the id."""
    stamped = dict(event)
    stamped["ts"] = _now_iso()
    stamped["id"] = _ulid()
    stamped.setdefault("v", SCHEMA_VERSION)
    return stamped


def append_line(path: str, event: dict) -> None:
    """Append one serialized event to the JSONL, creating the file world-readable
    (0644) so the agent-uid read seam can consume it. Caller serializes writes
    (the broker holds a lock); this function does no locking of its own."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    newly_created = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(serialize_event(event) + "\n")
    if newly_created:
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
