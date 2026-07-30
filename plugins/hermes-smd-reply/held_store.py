"""Durable store for rate-held replies awaiting auto-release (ss-console #2070).

Before #2070 a rate-limited reply was audited (``REPLY_HELD``) and DROPPED: the
draft stayed a draft, no window-clear ever released it, and nobody was told.
From the sender's chair the Operator simply went silent mid-conversation. This
store is the durable half of the fix; :mod:`sweeper` is the release half.

Scope: ONLY mechanical, time-clearing holds enqueue here
(``rate_limited_per_sender|global|backstop``, plus ``queued_behind_held``).
Semantic refusals — off-roster sender, recipient-lock mismatch, content floor,
fabrication gate, empty body — are decisions, not delays: they stay drop-only.

Custody: the store lives on the seat volume beside customer.yaml and the audit
db, and holds the reply BODY only while the reply is pending. On transition to
any terminal state the body columns are nulled — a held reply can be client
work product (for a law firm, potentially privileged), and once it has shipped
or expired the only thing worth keeping is the digest already in the audit row.

At-most-once by construction: the claim is a conditional UPDATE, and a row
found in ``sending`` at boot (the process died mid-transmit) is marked
``failed_interrupted`` and NEVER auto-resent. A rare lost release is visible in
the audit row and in Sentry; a duplicate email to a client is not acceptable.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HELD_DB_PATH = "/opt/data/held_replies.db"

# Terminal rows are kept briefly so an operator inspecting the seat can see what
# released and what expired; the body is already gone by then.
_TERMINAL_RETENTION_S = 7 * 24 * 3600.0

STATUS_HELD = "held"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"
STATUS_FAILED_INTERRUPTED = "failed_interrupted"
STATUS_FAILED_SEND = "failed_send"

_TERMINAL_STATUSES = (
    STATUS_SENT,
    STATUS_EXPIRED,
    STATUS_FAILED_INTERRUPTED,
    STATUS_FAILED_SEND,
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS held_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  sender TEXT NOT NULL,
  sender_class TEXT NOT NULL DEFAULT '',
  adapter TEXT NOT NULL,
  inbox_id TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL,
  send_text TEXT,
  send_html TEXT,
  body_digest TEXT NOT NULL DEFAULT '',
  hold_reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'held',
  released_at REAL,
  last_error TEXT
)
"""

_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_held_replies_status ON held_replies(status, id)"
_CREATE_SENDER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_held_replies_sender ON held_replies(sender, status)"
)


@dataclass(frozen=True)
class HeldReply:
    """One pending reply, as read back for release."""

    id: int
    created_at: float
    sender: str
    sender_class: str
    adapter: str
    inbox_id: str
    message_id: str
    send_text: str
    send_html: str
    body_digest: str
    hold_reason: str


class HeldReplyStore:
    """SQLite-backed queue of rate-held replies.

    Every method is exception-safe at the call sites that matter (the plugin
    hook and the sweeper thread both guard), but the store itself raises so a
    genuinely broken db surfaces in logs rather than silently swallowing.
    """

    def __init__(
        self,
        path: str = DEFAULT_HELD_DB_PATH,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock or time.time
        self._conn: sqlite3.Connection | None = None

    # -- connection ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            # WAL + a busy timeout: the hook thread enqueues while the sweeper
            # thread claims/releases, so writers must not trip over each other.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_SENDER_INDEX_SQL)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- write --------------------------------------------------------------

    def enqueue(
        self,
        *,
        sender: str,
        sender_class: str,
        adapter: str,
        inbox_id: str,
        message_id: str,
        send_text: str,
        send_html: str,
        body_digest: str,
        hold_reason: str,
    ) -> int:
        """Persist one held reply. Returns the row id."""
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO held_replies (created_at, sender, sender_class, adapter, inbox_id, "
            "message_id, send_text, send_html, body_digest, hold_reason, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._clock(),
                sender,
                sender_class,
                adapter,
                inbox_id,
                message_id,
                send_text,
                send_html,
                body_digest,
                hold_reason,
                STATUS_HELD,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)

    def claim(self, row_id: int) -> bool:
        """Atomically move ``held`` -> ``sending``. False if someone else won.

        The conditional UPDATE is the at-most-once guarantee: two sweepers (or a
        sweeper racing a restart) cannot both transmit the same reply.
        """
        conn = self._connect()
        cur = conn.execute(
            "UPDATE held_replies SET status=? WHERE id=? AND status=?",
            (STATUS_SENDING, row_id, STATUS_HELD),
        )
        conn.commit()
        return cur.rowcount == 1

    def mark_terminal(self, row_id: int, status: str, *, error: str | None = None) -> None:
        """Move a row to a terminal status and DROP the stored body.

        Body hygiene: ``send_text`` / ``send_html`` are nulled here, so a
        shipped or expired reply leaves only the digest behind.
        """
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"not a terminal status: {status!r}")
        conn = self._connect()
        conn.execute(
            "UPDATE held_replies SET status=?, released_at=?, last_error=?, "
            "send_text=NULL, send_html=NULL WHERE id=?",
            (status, self._clock(), error, row_id),
        )
        conn.commit()

    def fail_interrupted_on_boot(self) -> list[int]:
        """Resolve rows left in ``sending`` by a process death. Returns their ids.

        Deliberately NOT re-queued: the transmit may have completed before the
        process died, and a duplicate reply to a client is worse than a lost
        one. The ids are returned so the caller can report them.
        """
        conn = self._connect()
        rows = conn.execute(
            "SELECT id FROM held_replies WHERE status=?", (STATUS_SENDING,)
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        if ids:
            conn.execute(
                "UPDATE held_replies SET status=?, released_at=?, last_error=?, "
                "send_text=NULL, send_html=NULL WHERE status=?",
                (
                    STATUS_FAILED_INTERRUPTED,
                    self._clock(),
                    "process exited mid-send; not auto-resent (at-most-once)",
                    STATUS_SENDING,
                ),
            )
            conn.commit()
        return ids

    def purge_terminal(self, older_than_s: float = _TERMINAL_RETENTION_S) -> int:
        """Delete terminal rows older than the retention window."""
        conn = self._connect()
        horizon = self._clock() - older_than_s
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        cur = conn.execute(
            f"DELETE FROM held_replies WHERE status IN ({placeholders}) "
            "AND COALESCE(released_at, created_at) < ?",
            (*_TERMINAL_STATUSES, horizon),
        )
        conn.commit()
        return cur.rowcount

    # -- read ---------------------------------------------------------------

    def iter_held(self, limit: int = 200) -> list[HeldReply]:
        """Pending rows, oldest first (release order is FIFO by id)."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, created_at, sender, sender_class, adapter, inbox_id, message_id, "
            "send_text, send_html, body_digest, hold_reason FROM held_replies "
            "WHERE status=? ORDER BY id ASC LIMIT ?",
            (STATUS_HELD, limit),
        ).fetchall()
        return [
            HeldReply(
                id=int(r[0]),
                created_at=float(r[1]),
                sender=str(r[2]),
                sender_class=str(r[3] or ""),
                adapter=str(r[4]),
                inbox_id=str(r[5] or ""),
                message_id=str(r[6]),
                send_text=str(r[7] or ""),
                send_html=str(r[8] or ""),
                body_digest=str(r[9] or ""),
                hold_reason=str(r[10]),
            )
            for r in rows
        ]

    def has_pending(self, sender: str) -> bool:
        """True iff this sender already has a reply waiting to be released.

        The live send path consults this BEFORE the rate check: without it, a
        later reply whose window has cleared would overtake an earlier held one
        (the client reads answer 5 before answer 4), and under sustained traffic
        the live path would keep consuming the freed slots so the held row never
        releases at all.
        """
        conn = self._connect()
        row = conn.execute(
            "SELECT 1 FROM held_replies WHERE sender=? AND status IN (?, ?) LIMIT 1",
            (sender, STATUS_HELD, STATUS_SENDING),
        ).fetchone()
        return row is not None

    def pending_count(self) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM held_replies WHERE status=?", (STATUS_HELD,)
        ).fetchone()
        return int(row[0]) if row else 0

    def get(self, row_id: int) -> dict[str, Any] | None:
        """Full row as a dict (tests + diagnostics)."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM held_replies WHERE id=?", (row_id,)).fetchone()
        conn.row_factory = None
        return dict(row) if row is not None else None


__all__ = [
    "DEFAULT_HELD_DB_PATH",
    "HeldReply",
    "HeldReplyStore",
    "STATUS_EXPIRED",
    "STATUS_FAILED_INTERRUPTED",
    "STATUS_FAILED_SEND",
    "STATUS_HELD",
    "STATUS_SENDING",
    "STATUS_SENT",
]
