"""The seat's own calendar: every event it has invited people to, or been invited to.

WHY A STORE AT ALL (ss-console#3264, ADR 0092 pending). The calendar a person
sees lives in THEIR client (Google, Apple); the seat only sends invitations.
But an update has to carry the same ``UID`` with a higher ``SEQUENCE``, a cancel
has to name the event it removes, and "what is on our calendar next week" has to
be answerable without reading anyone's mailbox. So the seat keeps the events it
sent and the ones people sent it, here.

WHERE IT LIVES. ``/opt/data/smd/calendar.db``, beside the seat's other ledgers
(``exposure_override.db``, ``admin_possession.db``). ``/opt/data/smd`` is not a
skill root, a spec tree or a spillover directory, so on a seat that keeps
records the file tools cannot reach it (``hermes-smd-record-store/path_fence``);
the calendar plugin adds the same refusal for this one path on every seat that
registers the calendar tools, so a calendar is opened only through
``calendar_invite_list``, which applies the space rules.

WHAT A ROW HOLDS. The event's identity (``uid``, ``sequence``), what it is
(``title``, times, ``rrule``, ``description``, ``location``, ``reminders``), who
it is for (``attendees``, ``organizer``), its ``status`` (``CONFIRMED`` or
``CANCELLED``), the ``space`` it belongs to on a seat more than one person
shares (``person:<id>`` or ``shared``; ``""`` elsewhere), where it came from
(``origin``: ``agent`` for an invitation this seat sent, ``inbound`` for one it
received), and whether the latest change reached people (``delivery``).

SEQUENCE ONLY GOES UP. :meth:`CalendarStore.next_sequence` is the only way a
caller gets the number for a change, and :meth:`CalendarStore.put` refuses a
row whose sequence is lower than the one stored. A calendar client ignores an
update whose SEQUENCE is not higher than the last one it filed, silently, so a
sequence that went down would be a change nobody sees.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DB_PATH_ENV = "SMD_CALENDAR_DB_PATH"
DEFAULT_DB_PATH = "/opt/data/smd/calendar.db"

ORIGIN_AGENT = "agent"
ORIGIN_INBOUND = "inbound"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_CANCELLED = "CANCELLED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    uid TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    tzid TEXT NOT NULL DEFAULT '',
    rrule TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    reminders TEXT NOT NULL DEFAULT '[]',
    attendees TEXT NOT NULL DEFAULT '[]',
    organizer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    space TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


_COLUMNS = (
    "uid",
    "sequence",
    "title",
    "start",
    "end",
    "all_day",
    "tzid",
    "rrule",
    "description",
    "location",
    "reminders",
    "attendees",
    "organizer",
    "status",
    "space",
    "origin",
    "delivery",
    "created_at",
    "updated_at",
)
_UPSERT = (
    "INSERT OR REPLACE INTO events (uid, sequence, title, start, end, all_day, tzid, rrule, "
    "description, location, reminders, attendees, organizer, status, space, origin, delivery, "
    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class CalendarStoreError(RuntimeError):
    """The store could not be read or written, or a write would break a rule."""


@dataclass
class StoredEvent:
    uid: str
    sequence: int
    title: str
    start: str
    end: str
    all_day: bool
    tzid: str = ""
    rrule: str = ""
    description: str = ""
    location: str = ""
    reminders: list[int] = field(default_factory=list)
    attendees: list[dict[str, str]] = field(default_factory=list)
    organizer: str = ""
    status: str = STATUS_CONFIRMED
    space: str = ""
    origin: str = ORIGIN_AGENT
    delivery: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def attendee_emails(self) -> list[str]:
        return [str(a.get("email") or "") for a in self.attendees if a.get("email")]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def db_path() -> Path:
    return Path(os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


class CalendarStore:
    """One seat's calendar. Thread-safe; every call opens and closes its own
    connection, so a gateway thread and a hook never share a cursor."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or db_path()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=10)
        except (OSError, sqlite3.Error) as exc:
            raise CalendarStoreError(
                f"the calendar could not be opened ({type(exc).__name__})"
            ) from exc
        try:
            conn.execute(_SCHEMA)
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise CalendarStoreError(
                f"the calendar could not be read or written ({type(exc).__name__})"
            ) from exc
        finally:
            conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | tuple[Any, ...], names: list[str]) -> StoredEvent:
        data = dict(zip(names, row, strict=True))
        data["all_day"] = bool(data["all_day"])
        data["reminders"] = json.loads(data["reminders"] or "[]")
        data["attendees"] = json.loads(data["attendees"] or "[]")
        return StoredEvent(**data)

    def get(self, uid: str) -> StoredEvent | None:
        with self._lock, self._db() as conn:
            cur = conn.execute("SELECT * FROM events WHERE uid = ?", (uid,))
            names = [d[0] for d in cur.description]
            row = cur.fetchone()
        return self._row(row, names) if row else None

    def list(self, *, include_cancelled: bool = False) -> list[StoredEvent]:
        sql = "SELECT * FROM events"
        if not include_cancelled:
            sql += " WHERE status != 'CANCELLED'"
        sql += " ORDER BY start, uid"
        with self._lock, self._db() as conn:
            cur = conn.execute(sql)
            names = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [self._row(r, names) for r in rows]

    def next_sequence(self, uid: str) -> int:
        """The SEQUENCE the next change to ``uid`` must carry: 0 for a new event,
        one more than the stored one otherwise."""
        existing = self.get(uid)
        return 0 if existing is None else existing.sequence + 1

    def put(self, event: StoredEvent) -> StoredEvent:
        """Insert or replace ``event``. Refuses a sequence lower than the stored
        one, and (for an existing event) any change of origin or space."""
        now = time.time()
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "SELECT sequence, origin, space, created_at FROM events WHERE uid = ?", (event.uid,)
            )
            found = cur.fetchone()
            if found is not None:
                stored_seq, origin, space, created = found
                if event.sequence < stored_seq:
                    raise CalendarStoreError(
                        f"event {event.uid} is at SEQUENCE {stored_seq}; a change may not carry {event.sequence}"
                    )
                if origin != event.origin or space != event.space:
                    raise CalendarStoreError(
                        f"event {event.uid} cannot move between origins or spaces"
                    )
                event.created_at = float(created)
            else:
                event.created_at = event.created_at or now
            event.updated_at = now
            data = event.as_dict()
            data["all_day"] = int(event.all_day)
            data["reminders"] = json.dumps(list(event.reminders))
            data["attendees"] = json.dumps(list(event.attendees), ensure_ascii=False)
            conn.execute(_UPSERT, tuple(data[name] for name in _COLUMNS))
        return event

    def set_partstat(self, uid: str, email: str, partstat: str) -> bool:
        """Record one attendee's response (``ACCEPTED`` / ``DECLINED`` /
        ``TENTATIVE``) on ``uid``. False when the event or the attendee is not
        there. The SEQUENCE does not move: a response is the attendee's act, not
        a change to the event."""
        wanted = email.strip().lower()
        with self._lock, self._db() as conn:
            found = conn.execute("SELECT attendees FROM events WHERE uid = ?", (uid,)).fetchone()
            if found is None:
                return False
            attendees = json.loads(found[0] or "[]")
            hit = False
            for attendee in attendees:
                if str(attendee.get("email") or "").lower() == wanted:
                    attendee["partstat"] = partstat
                    hit = True
            if not hit:
                return False
            conn.execute(
                "UPDATE events SET attendees = ?, updated_at = ? WHERE uid = ?",
                (json.dumps(attendees, ensure_ascii=False), time.time(), uid),
            )
        return True

    def set_delivery(self, uid: str, delivery: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute("UPDATE events SET delivery = ? WHERE uid = ?", (delivery, uid))


STORE = CalendarStore()

__all__ = [
    "DB_PATH_ENV",
    "DEFAULT_DB_PATH",
    "ORIGIN_AGENT",
    "ORIGIN_INBOUND",
    "STATUS_CANCELLED",
    "STATUS_CONFIRMED",
    "STORE",
    "CalendarStore",
    "CalendarStoreError",
    "StoredEvent",
    "db_path",
]
