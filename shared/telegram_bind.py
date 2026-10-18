"""Telegram deep-link binding for couples onboarding (ss-console ADR 0091 s5, R4).

A partner taps ``t.me/<bot>?start=<token>`` on their onboarding page. Telegram
delivers ``/start <token>`` to the seat's bot from a user the seat does not
know yet. This module carries that token to ss-console, which redeems it and
records the binding on the partner's onboarding row.

WHAT THIS NEVER DOES: admit the user. The seat's Telegram allowlist is
authored config. The bound id reaches ``customer.yaml`` only through
ss-console's generator and a reviewed PR, and the seat reads it on restart.
Nothing here writes config, an allowlist, or a pairing approval.

TWO PROCESSES, TWO FILES, ONE WRITER EACH (the request-card discipline):

* the AGENT process (``plugins/hermes-smd-couples-onboarding``, at
  ``pre_gateway_dispatch``) appends each ``/start <token>`` it sees to the
  spool (:func:`spool_start`). It holds no credential, by design: bootstrap
  strips ``MACHINE_HEARTBEAT_KEY`` from the agent's environment.
* the GATE's heartbeat ticker (:func:`send_due_binds`) reads the spool
  read-only and POSTs each pending token to ``/api/internal/telegram-bind``
  with the heartbeat's own bearer and tenant headers. What it has sent lives in
  its own file (:class:`SentStore`).

The console answers 200 for a bind and for a refusal alike (it never says
why), so ANY 200 is done; a non-200 or a transport error is retried each tick
until the token could no longer be valid (:data:`RETRY_FOR`).

CUSTODY. A token is single-use and expires 30 minutes after it is minted, so
the spool prunes rows after :data:`SPOOL_RETENTION_SECONDS` on every write.
The token never reaches a log line; the Telegram user id is logged by count
only.

Never raises into a caller: a broken spool costs one bind, which the partner
repeats from their setup page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SPOOL_PATH = "/opt/data/telegram_binds.db"
DEFAULT_SENT_PATH = "/opt/data/smd-gate/telegram_binds_sent.db"
BIND_PATH = "/api/internal/telegram-bind"

#: The console mints 32 random bytes as base64url: exactly 43 characters of
#: Telegram's start-payload alphabet. Anything else is not one of our tokens.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
#: ``/start <token>`` or ``/start@<bot> <token>``, nothing else on the line.
START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]{1,64})?\s+([A-Za-z0-9_-]{43})\s*$")
TELEGRAM_USER_RE = re.compile(r"^[0-9]{1,20}$")

#: Longer than a token can live (30 minutes) so a slow tick never drops one.
SPOOL_RETENTION_SECONDS = 2 * 60 * 60
RETRY_FOR = 45 * 60
POST_TIMEOUT_SECONDS = 5.0
MAX_PER_TICK = 10

#: The one reply a /start with a token gets, whatever the console decides.
#: It promises nothing the seat has not done: it received the link.
CONFIRMATION_TEXT = (
    "Thanks, your link was received. Your setup page shows when your Telegram "
    "is connected. You can chat with me here once setup is complete."
)

_SPOOL_SQL = """
CREATE TABLE IF NOT EXISTS binds (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  token            TEXT NOT NULL,
  telegram_user_id TEXT NOT NULL,
  received_at      REAL NOT NULL
)
"""
_SENT_SQL = """
CREATE TABLE IF NOT EXISTS sent (
  token_sha256     TEXT PRIMARY KEY,
  first_attempt_at REAL NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  done_at          REAL
)
"""


def spool_path() -> str:
    return os.environ.get("SMD_TELEGRAM_BIND_SPOOL_PATH") or DEFAULT_SPOOL_PATH


def sent_path() -> str:
    return os.environ.get("SMD_TELEGRAM_BIND_SENT_PATH") or DEFAULT_SENT_PATH


def bind_url(ingest_url: str) -> str:
    """The bind endpoint on the same console the heartbeat reports to."""
    match = re.match(r"^(https?://[^/]+)", ingest_url or "")
    base = match.group(1) if match else "https://smd.services"
    return base + BIND_PATH


def start_token(text: object) -> str | None:
    """The token in a ``/start <token>`` message, or None."""
    if not isinstance(text, str):
        return None
    match = START_RE.match(text.strip())
    return match.group(1) if match else None


def _connect(path: str, schema: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(schema)
    return conn


def spool_start(token: str, telegram_user_id: str, *, now: float | None = None) -> bool:
    """Agent side: record one ``/start <token>``. Returns False when refused or
    on any failure (logged). Prunes old rows in the same transaction."""
    if not TOKEN_RE.match(token or "") or not TELEGRAM_USER_RE.match(telegram_user_id or ""):
        return False
    at = time.time() if now is None else now
    try:
        conn = _connect(spool_path(), _SPOOL_SQL)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM binds WHERE received_at < ?", (at - SPOOL_RETENTION_SECONDS,)
                )
                conn.execute(
                    "INSERT INTO binds (token, telegram_user_id, received_at) VALUES (?, ?, ?)",
                    (token, telegram_user_id, at),
                )
        finally:
            conn.close()
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.error("telegram-bind: spool write failed (%s)", type(exc).__name__)
        return False


def _key(token: str) -> str:
    """The sent-store key: a hash, so the gate's file never holds a token, and
    stable across a recreated spool (row ids are not)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SentStore:
    """Gate side: which tokens were attempted and which are done. No token."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or sent_path()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _connect(self._path, _SENT_SQL)
        return self._conn

    def state(self) -> dict[str, tuple[float, float | None]]:
        rows = self._connection().execute(
            "SELECT token_sha256, first_attempt_at, done_at FROM sent"
        )
        return {str(r[0]): (float(r[1]), r[2]) for r in rows}

    def note_attempt(self, key: str, now: float) -> None:
        conn = self._connection()
        with conn:
            conn.execute(
                "INSERT INTO sent (token_sha256, first_attempt_at, attempts) VALUES (?, ?, 1) "
                "ON CONFLICT(token_sha256) DO UPDATE SET attempts = attempts + 1",
                (key, now),
            )

    def mark_done(self, key: str, now: float) -> None:
        conn = self._connection()
        with conn:
            conn.execute(
                "INSERT INTO sent (token_sha256, first_attempt_at, attempts, done_at) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(token_sha256) DO UPDATE SET done_at = excluded.done_at",
                (key, now, now),
            )


def _pending(now: float) -> list[tuple[int, str, str, float]]:
    path = spool_path()
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = conn.execute(
            "SELECT id, token, telegram_user_id, received_at FROM binds "
            "WHERE received_at >= ? ORDER BY id",
            (now - SPOOL_RETENTION_SECONDS,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # created but empty schema: nothing spooled yet
    finally:
        conn.close()
    return [(int(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]


def send_due_binds(
    *,
    slug: str,
    key: str,
    url: str,
    post_fn: Callable[..., int],
    store: SentStore,
    now: float | None = None,
) -> int:
    """POST every pending bind. Returns how many were posted. Never raises."""
    at = time.time() if now is None else now
    posted = 0
    try:
        state = store.state()
        due = [row for row in _pending(at) if state.get(_key(row[1]), (0.0, None))[1] is None]
        for _row_id, token, user_id, received_at in due[:MAX_PER_TICK]:
            key_ = _key(token)
            first = state.get(key_, (at, None))[0]
            if at - first > RETRY_FOR or at - received_at > RETRY_FOR:
                store.mark_done(key_, at)
                logger.error("telegram-bind: a bind could not be delivered; given up")
                continue
            store.note_attempt(key_, at)
            body = json.dumps({"token": token, "telegram_user_id": user_id}).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {key}",
                "X-Tenant-Slug": slug,
                "Content-Type": "application/json",
            }
            try:
                status = post_fn(url, headers, body, POST_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 — retried next tick
                logger.warning("telegram-bind: POST failed (%s)", type(exc).__name__)
                continue
            posted += 1
            if status == 200:
                store.mark_done(key_, at)
            else:
                logger.warning("telegram-bind: console answered %s; retrying", status)
    except Exception as exc:  # noqa: BLE001 — never break the ticker
        logger.error("telegram-bind: leg failed (%s)", type(exc).__name__)
    return posted
