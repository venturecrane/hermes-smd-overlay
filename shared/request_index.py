"""Seat-local index of the requests people email in, for SMD's request cards.

When a firm person emails an Operator seat, SMD gets one email card per request
(who, subject, the opening of the Operator's reply, tools run, outcome, time to
first reply), a NO REPLY alarm when nothing answers within 30 minutes, and a
follow-up card when a job the request started finishes. The audit ledger can
answer every one of those questions except three: who wrote, what the subject
was, and what the reply said. The ledger deliberately holds none of them (an
audit export is a file that leaves the Machine). This file holds exactly those
three, on the seat volume, for as long as a card can still need them.

ss-console persists no client text (ADR 0052 s5): the subject, the sender and
the reply opening live ONLY here and in the transient card email. So custody is
this module's job, done on every write rather than by a sweeper that could
stop:

* the text columns (``sender``, ``subject``, ``reply_opening``) are nulled on
  rows older than :data:`TEXT_RETENTION_DAYS`, which is longer than any card
  can wait (a card retries for a day);
* the rows themselves are deleted after :data:`ROW_RETENTION_DAYS`.

ONE WRITER PROCESS. The agent process writes this file (the router at intake,
the reply plugin at each send, the held-reply sweeper on release); the gate's
heartbeat only reads it, read-only. The card-sent state lives in a separate,
gate-owned file (``shared.request_cards``) so neither file has two writer
processes.

Never raises into a caller. Every public function logs and returns; a broken
index costs a card, never an intake or a send. ``logger.error`` reaches Sentry
through the logging integration, so a persistently broken index is not silent.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/opt/data/requests.db"

#: Text columns are nulled after this. A card retries for at most a day, so a
#: week is a generous margin and still a hard bound on how long a subject sits.
TEXT_RETENTION_DAYS = 7
#: Rows (ids and timestamps only, by then) are deleted after this.
ROW_RETENTION_DAYS = 30

#: Bounds at the write, so a pathological subject or body never bloats the file.
SENDER_MAX = 160
SUBJECT_MAX = 200
#: The opening is cut to about this many characters at a word boundary.
OPENING_CHARS = 240

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
  vendor_message_id   TEXT PRIMARY KEY,
  internet_message_id TEXT,
  received_at         REAL NOT NULL,
  sender              TEXT,
  subject             TEXT,
  reply_opening       TEXT,
  opening_at          REAL
)
"""
_CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_requests_received ON requests(received_at)"


def db_path() -> str:
    """The index path: ``SMD_REQUEST_INDEX_DB_PATH`` (tests/dev) or the volume default."""
    return os.environ.get("SMD_REQUEST_INDEX_DB_PATH") or DEFAULT_DB_PATH


def _connect(path: str) -> sqlite3.Connection:
    """A fresh writer connection. One per call: the hook thread and the sweeper
    thread both write, and a per-call connection with WAL + a busy timeout is
    the simplest thing that cannot share a cursor across threads."""
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(_CREATE_SQL)
    conn.execute(_CREATE_INDEX_SQL)
    return conn


def _custody(conn: sqlite3.Connection, now: float) -> None:
    """Null old text, delete old rows. Runs inside every write's transaction."""
    conn.execute(
        "UPDATE requests SET sender=NULL, subject=NULL, reply_opening=NULL "
        "WHERE received_at < ? AND (sender IS NOT NULL OR subject IS NOT NULL "
        "OR reply_opening IS NOT NULL)",
        (now - TEXT_RETENTION_DAYS * 86400.0,),
    )
    conn.execute(
        "DELETE FROM requests WHERE received_at < ?",
        (now - ROW_RETENTION_DAYS * 86400.0,),
    )


def _clean(value: object, limit: int) -> str | None:
    """Whitespace-collapsed, control-free, bounded text, or None when empty."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit] or None


# A first line that only greets: "Hi Sam,", "Hello -", "Good morning all,".
_SALUTATION_RE = re.compile(
    r"^(hi|hello|hey|dear|good (morning|afternoon|evening)|greetings)\b", re.IGNORECASE
)


def reply_opening(body: object) -> str | None:
    """The first ~240 characters of a reply after its salutation line.

    The salutation is the first non-empty line when it greets (``Hi Name,``) or
    is a short line ending in a comma. Whitespace is collapsed; the cut lands on
    a word boundary with an ellipsis when the body runs longer. ``None`` for an
    empty or non-string body.
    """
    if not isinstance(body, str):
        return None
    lines = [line.strip() for line in body.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if lines:
        first = lines[0]
        if _SALUTATION_RE.match(first) or (len(first) <= 60 and first.endswith(",")):
            lines.pop(0)
    text = _clean("\n".join(lines), 10_000)
    if not text:
        return None
    if len(text) <= OPENING_CHARS:
        return text
    cut = text[:OPENING_CHARS]
    space = cut.rfind(" ")
    if space > OPENING_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "..."


def record_inbound(
    *,
    vendor_message_id: object,
    internet_message_id: object = "",
    sender: object = "",
    subject: object = "",
    received_at: float | None = None,
    path: str | None = None,
) -> bool:
    """Index one person's request at intake. First write wins (a redelivered
    message does not move its received time). Returns whether a row was
    written; never raises."""
    if not isinstance(vendor_message_id, str) or not vendor_message_id:
        return False
    now = time.time()
    try:
        conn = _connect(path or db_path())
    except Exception:  # noqa: BLE001 — a broken index costs a card, never an intake
        logger.error("request_index: cannot open the request index", exc_info=True)
        return False
    try:
        with conn:
            _custody(conn, now)
            cur = conn.execute(
                "INSERT OR IGNORE INTO requests (vendor_message_id, internet_message_id, "
                "received_at, sender, subject) VALUES (?,?,?,?,?)",
                (
                    vendor_message_id[:512],
                    (internet_message_id[:512] if isinstance(internet_message_id, str) else "")
                    or None,
                    float(received_at if received_at is not None else now),
                    _clean(sender, SENDER_MAX),
                    _clean(subject, SUBJECT_MAX),
                ),
            )
        return cur.rowcount == 1
    except Exception:  # noqa: BLE001
        logger.error("request_index: record_inbound failed", exc_info=True)
        return False
    finally:
        conn.close()


def record_reply_opening(in_reply_to: object, body: object, *, path: str | None = None) -> bool:
    """Keep the opening of the FIRST reply to a request. A later reply never
    overwrites it, and a reply to an unindexed message writes nothing. Returns
    whether a row was updated; never raises."""
    if not isinstance(in_reply_to, str) or not in_reply_to:
        return False
    now = time.time()
    try:
        conn = _connect(path or db_path())
    except Exception:  # noqa: BLE001 — a broken index costs a card, never a send
        logger.error("request_index: cannot open the request index", exc_info=True)
        return False
    try:
        with conn:
            _custody(conn, now)
            cur = conn.execute(
                "UPDATE requests SET reply_opening=?, opening_at=? "
                "WHERE vendor_message_id=? AND opening_at IS NULL",
                (reply_opening(body), now, in_reply_to),
            )
        return cur.rowcount == 1
    except Exception:  # noqa: BLE001
        logger.error("request_index: record_reply_opening failed", exc_info=True)
        return False
    finally:
        conn.close()


def read_requests(since: float, *, path: str | None = None) -> list[dict]:
    """Every indexed request received at or after ``since``, READ-ONLY (the
    gate's side). ``[]`` when the file does not exist yet; raises on a file
    that exists and cannot be read, so the caller can tell "none" from "could
    not look" and skip the tick rather than decide on an empty list."""
    target = path or db_path()
    if not os.path.exists(target):
        return []
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT vendor_message_id, internet_message_id, received_at, sender, subject, "
            "reply_opening, opening_at FROM requests WHERE received_at >= ? "
            "ORDER BY received_at",
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "DEFAULT_DB_PATH",
    "ROW_RETENTION_DAYS",
    "TEXT_RETENTION_DAYS",
    "db_path",
    "read_requests",
    "record_inbound",
    "record_reply_opening",
    "reply_opening",
]
