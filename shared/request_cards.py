"""Request cards: one email to SMD per request a person sends a seat.

When a firm person emails an Operator seat, SMD (team@smd.services) gets:

* a ``replied`` card once the Operator has answered and gone quiet: who, the
  subject, the opening of the first reply, the matter, the tools that ran, how
  many calls were refused or failed, and the minutes to first reply;
* a ``no_reply`` alarm when nothing has answered the request within
  :data:`NO_REPLY_AFTER` and nothing else is already handling it;
* a ``job_done`` card when a demand, drafting, litigation status or chronology job the request started
  reaches ``delivered``, ``failed`` or ``held``.

The seat decides; ss-console sends. This module is the seat half, hosted by the
gate's heartbeat ticker (``shared.heartbeat``): :func:`decide` is the pure
decision over three reads, and :func:`send_due_cards` is the leg that POSTs each
due card to ``/api/internal/operator-request-card`` with the heartbeat's own
bearer + tenant headers. The wire contract is fixed in the ss-console plan; the
console dedupes on ``card_key`` and answers 200 for both a fresh send and a
duplicate, so the seat treats ANY 200 as done.

The three reads, all read-only:

* ``shared.request_index`` (agent-written): the requests, with the only text a
  card carries (sender, subject, reply opening). Only requests indexed there are
  carded; nothing is backfilled from the ledger.
* ``audit_log``: ``REPLY_SENT`` / ``REPLY_HELD`` / ``REPLY_FAILED`` rows joined
  on the inbound's vendor message id, and the per-call rows of the replying
  session, classified with the SAME ``_classify_call`` the shortfall alert uses.
* ``demand_jobs`` / ``drafting_jobs`` / ``litigation_jobs`` / ``medchron_jobs`` in the same audit db file, joined on
  ``request_ref`` (the request's internetMessageId). A seat without one of those
  tables simply has no jobs in that lane.

Card-sent state lives in its own gate-owned file (:class:`CardStore`), never in
the agent's index, so each file has exactly one writer process. A card is
retried every tick until a 200 or until :data:`RETRY_FOR` after its first
attempt, when it is marked done and reported once (``logger.error`` reaches
Sentry through the logging integration).

Never raises into the ticker: :func:`send_due_cards` catches everything and
logs.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared import request_index

logger = logging.getLogger(__name__)

DEFAULT_CARDS_DB_PATH = "/opt/data/smd-gate/request_cards.db"
CARD_PATH = "/api/internal/operator-request-card"

#: A replied card waits until the first reply AND the session's last call are
#: this old, so an "on it" acknowledgement followed by real work is carded once,
#: after the work, with the work's tools.
QUIET = timedelta(minutes=5)
#: Nothing answered and nothing is handling it after this: the alarm.
NO_REPLY_AFTER = timedelta(minutes=30)
#: How far back a request is still considered. Matches the index's text window.
LOOKBACK = timedelta(days=request_index.TEXT_RETENTION_DAYS)
#: A card that has not landed by this long after its first attempt is given up.
RETRY_FOR = timedelta(hours=24)

#: Leg bounds, per tick. The heartbeat POST has already gone by the time this
#: runs; these keep the leg from holding the ticker past its period.
POST_TIMEOUT_SECONDS = 5.0
TICK_BUDGET_SECONDS = 20.0
MAX_CARDS_PER_TICK = 10

JOB_LANES = ("demand", "drafting", "litigation", "medchron")
JOB_TERMINAL = ("delivered", "failed", "held")

_TOOL_TOKEN_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")
_TOOL_BAD_RE = re.compile(r"[^a-z0-9_:.-]")
_REASON_HEAD_RE = re.compile(r"^[a-z0-9_.-]+")
_MAX_TOOLS = 40

_CARDS_SQL = """
CREATE TABLE IF NOT EXISTS cards (
  card_key         TEXT PRIMARY KEY,
  first_attempt_at REAL NOT NULL,
  attempts         INTEGER NOT NULL DEFAULT 0,
  done_at          REAL
)
"""


def cards_db_path() -> str:
    """The card-state path: ``SMD_REQUEST_CARDS_DB_PATH`` (tests/dev) or the default."""
    return os.environ.get("SMD_REQUEST_CARDS_DB_PATH") or DEFAULT_CARDS_DB_PATH


def card_url(ingest_url: str) -> str:
    """The card endpoint on the same console the heartbeat reports to."""
    match = re.match(r"^(https?://[^/]+)", ingest_url or "")
    base = match.group(1) if match else "https://smd.services"
    return base + CARD_PATH


def card_key(vendor_message_id: str, kind: str) -> str:
    return hashlib.sha256(vendor_message_id.encode("utf-8")).hexdigest() + ":" + kind


def job_done_key(vendor_message_id: str, state: str, attempt: int) -> str:
    """One ``job_done`` card per job ENDING. A resumed job ends again (a new
    attempt, or a new state), and that ending is its own card; keyed on the
    request hash so the console still groups it under its request. Not keyed on
    ``updated_at``: a same-state note rewrites it and would card one ending twice."""
    return card_key(vendor_message_id, "job_done") + f":{state}-{int(attempt)}"


# ---------------------------------------------------------------------------
# Card state (gate-owned)
# ---------------------------------------------------------------------------


class CardStore:
    """Which cards have been attempted and which are done. Holds no text: the
    key is a hash of the vendor message id plus the kind."""

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or cards_db_path())
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_CARDS_SQL)
            conn.commit()
            self._conn = conn
        return self._conn

    def load(self) -> dict[str, tuple[float, int, float | None]]:
        rows = self._connection().execute(
            "SELECT card_key, first_attempt_at, attempts, done_at FROM cards"
        )
        return {str(r[0]): (float(r[1]), int(r[2]), r[3]) for r in rows}

    def note_attempt(self, key: str, now: float) -> None:
        conn = self._connection()
        conn.execute(
            "INSERT INTO cards (card_key, first_attempt_at, attempts) VALUES (?,?,1) "
            "ON CONFLICT(card_key) DO UPDATE SET attempts = attempts + 1",
            (key, now),
        )
        conn.commit()

    def mark_done(self, key: str, now: float) -> None:
        conn = self._connection()
        conn.execute(
            "INSERT INTO cards (card_key, first_attempt_at, attempts, done_at) VALUES (?,?,0,?) "
            "ON CONFLICT(card_key) DO UPDATE SET done_at = excluded.done_at",
            (key, now, now),
        )
        conn.commit()

    def prune(self, now: float) -> None:
        """Forget cards older than the index keeps rows: their requests are gone."""
        conn = self._connection()
        conn.execute(
            "DELETE FROM cards WHERE first_attempt_at < ?",
            (now - request_index.ROW_RETENTION_DAYS * 86400.0,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Ledger reads
# ---------------------------------------------------------------------------


def _parse_ts(value: object) -> datetime | None:
    """An audit or job ``ts`` as an aware UTC datetime; None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _reply_rows(conn: sqlite3.Connection, horizon: str) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, action_type, matter_ref,"
        " json_extract(metadata,'$.in_reply_to') AS in_reply_to,"
        " json_extract(metadata,'$.message_id') AS message_id,"
        " json_extract(metadata,'$.session_id') AS session_id,"
        " json_extract(metadata,'$.held_for_release') AS held_for_release"
        " FROM audit_log"
        " WHERE action_type IN ('REPLY_SENT', 'REPLY_HELD', 'REPLY_FAILED')"
        " AND substr(ts,1,19) >= ?"
        " ORDER BY ts, id",
        (horizon,),
    ).fetchall()
    keys = ("ts", "action_type", "matter_ref", "in_reply_to", "message_id", "session_id", "hfr")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _session_calls(conn: sqlite3.Connection, sessions: list[str], horizon: str) -> dict:
    """Per session, its per-call rows as ``heartbeat._CallRow`` in ledger order.

    One fixed query over the window, filtered here: the window is days of one
    seat's calls, and a fixed statement is one no placeholder list can bend."""
    from shared import heartbeat

    wanted = set(sessions)
    out: dict[str, list] = {s: [] for s in sessions}
    if not wanted:
        return out
    rows = conn.execute(
        heartbeat.CALL_ROW_SELECT
        + " WHERE action_type IN ('TOOL_CALL_COMPLETED', 'INVARIANT_VIOLATION')"
        " AND substr(ts,1,19) >= ?"
        " ORDER BY ts, id",
        (horizon,),
    ).fetchall()
    for row in rows:
        call = heartbeat._CallRow(*row)
        if call.session_id in wanted:
            out[call.session_id].append(call)
    return out


#: One fixed statement per lane (no table name is ever composed into SQL).
_JOB_SQL = {
    "demand": (
        "SELECT request_ref, state, reason, matter_number, updated_at, attempt FROM demand_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    "drafting": (
        "SELECT request_ref, state, reason, matter_number, updated_at, attempt FROM drafting_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    "medchron": (
        "SELECT request_ref, state, reason, matter_number, updated_at, attempt FROM medchron_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    # A litigation status job spans the firm's matters and files to the firm's
    # library matter, so it names no matter on its card.
    "litigation": (
        "SELECT request_ref, state, reason, NULL AS matter_number, updated_at, attempt FROM litigation_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
}
#: The same reads for a ledger older than the ``attempt`` column: attempt 1.
_JOB_SQL_NO_ATTEMPT = {
    "demand": (
        "SELECT request_ref, state, reason, matter_number, updated_at, 1 AS attempt FROM demand_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    "drafting": (
        "SELECT request_ref, state, reason, matter_number, updated_at, 1 AS attempt FROM drafting_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    "medchron": (
        "SELECT request_ref, state, reason, matter_number, updated_at, 1 AS attempt FROM medchron_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
    # A litigation status job spans the firm's matters and files to the firm's
    # library matter, so it names no matter on its card.
    "litigation": (
        "SELECT request_ref, state, reason, NULL AS matter_number, updated_at, 1 AS attempt FROM litigation_jobs"
        " WHERE substr(updated_at,1,19) >= ? ORDER BY updated_at"
    ),
}


def _job_rows(conn: sqlite3.Connection, refs: list[str], horizon: str) -> dict[str, dict]:
    """``request_ref`` -> its job (lane, state, reason, matter, updated_at). A
    missing table is a lane this seat does not run, not an error."""
    wanted = set(refs)
    out: dict[str, dict] = {}
    if not wanted:
        return out
    for lane in JOB_LANES:
        try:
            rows = _lane_rows(conn, lane, horizon)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                continue
            raise
        for ref, state, reason, matter, updated_at, attempt in rows:
            if ref not in wanted:
                continue
            out[str(ref)] = {
                "lane": lane,
                "state": state,
                "reason": reason,
                "matter": matter,
                "updated_at": updated_at,
                "attempt": attempt if isinstance(attempt, int) and attempt > 0 else 1,
            }
    return out


def _lane_rows(conn: sqlite3.Connection, lane: str, horizon: str) -> list:
    """One lane's job rows with their ``attempt``. A ledger older than the
    attempt column (or a lane that never resumes) reads as attempt 1."""
    try:
        return conn.execute(_JOB_SQL[lane], (horizon,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such column" not in str(exc):
            raise
        return conn.execute(_JOB_SQL_NO_ATTEMPT[lane], (horizon,)).fetchall()


def _tool_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = _TOOL_BAD_RE.sub("_", value.strip().lower())[:64]
    return token if _TOOL_TOKEN_RE.match(token) else None


def _reason_token(value: object) -> str | None:
    """The leading word of a job's reason, as a closed token. The free text after
    it (which can name a document) never leaves the seat."""
    if not isinstance(value, str):
        return None
    match = _REASON_HEAD_RE.match(value.strip().lower())
    return match.group(0)[:64] if match else None


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    return text or None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def decide(
    requests: list[dict],
    audit_conn: sqlite3.Connection,
    done: set[str],
    now: datetime,
    done_at: dict[str, float] | None = None,
) -> list[dict]:
    """The cards due now, oldest event first, as wire-shaped dicts.

    ``requests`` are :func:`shared.request_index.read_requests` rows; ``done``
    the card keys already landed; ``done_at`` (key -> when it landed) lets a
    legacy per-request ``job_done`` key cover the ending it was sent for, so a
    rollout re-sends nothing. Pure apart from the read-only queries on
    ``audit_conn``. Raises on a ledger it cannot read; the leg then sends
    nothing this tick rather than deciding "no reply" from a failed read.
    """
    from shared import heartbeat

    now = now.astimezone(timezone.utc)
    if not requests:
        return []
    horizon = _iso(now - LOOKBACK - timedelta(days=1))[:19]
    replies = _reply_rows(audit_conn, horizon)
    sent_by_vid: dict[str, list[dict]] = {}
    held_vids: set[str] = set()
    for row in replies:
        if row["action_type"] == "REPLY_SENT":
            if isinstance(row["in_reply_to"], str) and row["in_reply_to"]:
                sent_by_vid.setdefault(row["in_reply_to"], []).append(row)
        elif isinstance(row["message_id"], str) and row["message_id"]:
            # A hold queued for automatic release is still on its way; anything
            # else already reached SMD through the shortfall alert.
            if row["action_type"] == "REPLY_HELD" and row["hfr"] in (1, True):
                continue
            held_vids.add(row["message_id"])

    sessions = sorted(
        {
            str(sent[0]["session_id"])
            for sent in sent_by_vid.values()
            if isinstance(sent[0]["session_id"], str) and sent[0]["session_id"]
        }
    )
    calls_by_session = _session_calls(audit_conn, sessions, horizon)
    refs = sorted(
        {r["internet_message_id"] for r in requests if r.get("internet_message_id")}
        | {r["vendor_message_id"] for r in requests}
    )
    jobs = _job_rows(audit_conn, refs, horizon)

    cards: list[dict] = []
    for req in requests:
        vid = str(req["vendor_message_id"])
        received = datetime.fromtimestamp(float(req["received_at"]), tz=timezone.utc)
        base = {
            "received_at": _iso(received),
            "who": _clip(req.get("sender"), 160) or "(unknown sender)",
            "subject": _clip(req.get("subject"), 200) or "(no subject)",
        }
        job = jobs.get(str(req.get("internet_message_id") or "")) or jobs.get(vid)
        sent = sent_by_vid.get(vid, [])

        if sent and card_key(vid, "replied") not in done:
            card = _replied_card(req, vid, base, sent, calls_by_session, now, heartbeat)
            if card is not None:
                cards.append(card)

        if (
            not sent
            and job is None
            and vid not in held_vids
            and now - received >= NO_REPLY_AFTER
            and card_key(vid, "no_reply") not in done
        ):
            cards.append(
                {
                    "card_key": card_key(vid, "no_reply"),
                    "kind": "no_reply",
                    **base,
                    "event_at": _iso(now),
                    "minutes": _minutes(received, now),
                    "reply_opening": None,
                    "matter": None,
                    "tools": [],
                    "replies": 0,
                    "refused": 0,
                    "failed": 0,
                    "job": None,
                }
            )

        if job is not None and job["state"] in JOB_TERMINAL:
            ended = _parse_ts(job["updated_at"]) or now
            key = job_done_key(vid, job["state"], job["attempt"])
            if key in done or _legacy_covers(vid, ended, done, done_at):
                continue
            cards.append(
                {
                    "card_key": key,
                    "kind": "job_done",
                    **base,
                    "event_at": _iso(ended),
                    "minutes": _minutes(received, ended),
                    "reply_opening": None,
                    "matter": _clip(job["matter"], 60),
                    "tools": [],
                    "replies": len(sent),
                    "refused": 0,
                    "failed": 0,
                    "job": {
                        "lane": job["lane"],
                        "state": job["state"],
                        "reason": _reason_token(job["reason"]),
                    },
                }
            )
    cards.sort(key=lambda c: (c["event_at"], c["card_key"]))
    return cards


def _legacy_covers(
    vid: str, ended: datetime, done: set[str], done_at: dict[str, float] | None
) -> bool:
    """A card sent under the old per-request key covers the ending that
    happened before it landed; a later ending (a resume) is not covered.
    Without a landing time, the legacy key covers everything (never re-send)."""
    legacy = card_key(vid, "job_done")
    if legacy not in done:
        return False
    landed = (done_at or {}).get(legacy)
    if landed is None:
        return True
    return ended.timestamp() <= float(landed)


def _replied_card(req, vid, base, sent, calls_by_session, now, heartbeat) -> dict | None:
    """The replied card, or None while the reply or its session is not yet quiet."""
    first = sent[0]
    first_at = _parse_ts(first["ts"])
    if first_at is None or now - first_at < QUIET:
        return None
    session = first["session_id"] if isinstance(first["session_id"], str) else ""
    calls = calls_by_session.get(session, []) if session else []
    latest = max(
        [t for t in (_parse_ts(c.ts) for c in calls) if t is not None]
        + [t for t in (_parse_ts(s["ts"]) for s in sent) if t is not None]
    )
    if now - latest < QUIET:
        return None
    tools: list[str] = []
    refused = failed = 0
    for index, call in enumerate(calls):
        token = _tool_token(call.tool)
        if token and token not in tools and len(tools) < _MAX_TOOLS:
            tools.append(token)
        verdict = heartbeat._classify_call(call, calls, index)
        if verdict is None:
            continue
        if verdict[0] == "not_allowed":
            refused += 1
        else:
            failed += 1
    received = datetime.fromtimestamp(float(req["received_at"]), tz=timezone.utc)
    matter = next((s["matter_ref"] for s in sent if s["matter_ref"]), None)
    return {
        "card_key": card_key(vid, "replied"),
        "kind": "replied",
        **base,
        "event_at": _iso(first_at),
        "minutes": _minutes(received, first_at),
        "reply_opening": _clip(req.get("reply_opening"), 300),
        "matter": _clip(matter, 60),
        "tools": tools,
        "replies": len(sent),
        "refused": refused,
        "failed": failed,
        "job": None,
    }


# ---------------------------------------------------------------------------
# The leg
# ---------------------------------------------------------------------------


def send_due_cards(
    *,
    slug: str,
    key: str,
    url: str,
    audit_db_path: str | None,
    post_fn: Callable[..., int],
    store: CardStore | None = None,
    requests_db_path: str | None = None,
    now_fn: Callable[[], float] = time.time,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    """POST every due card, within the per-tick bounds. Returns how many landed.

    ``post_fn(url, headers, body, timeout)`` returns the HTTP status. A card is
    done on any 200; anything else (or a raise) leaves it for the next tick
    until :data:`RETRY_FOR` after its first attempt. Never raises.
    """
    landed = 0
    try:
        now_s = now_fn()
        now = datetime.fromtimestamp(now_s, tz=timezone.utc)
        requests = request_index.read_requests(
            now_s - LOOKBACK.total_seconds(), path=requests_db_path
        )
        if not requests:
            return 0
        if not audit_db_path or not os.path.exists(audit_db_path):
            # Without the ledger "no reply" cannot be told from "could not look".
            return 0
        store = store or CardStore()
        state = store.load()
        landed_at = {
            k: done_at for k, (_first, _n, done_at) in state.items() if done_at is not None
        }
        done = set(landed_at)
        conn = sqlite3.connect(f"file:{audit_db_path}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=2000")
            cards = decide(requests, conn, done, now, done_at=landed_at)
        finally:
            conn.close()
        started = monotonic_fn()
        sent_this_tick = 0
        for card in cards:
            if sent_this_tick >= MAX_CARDS_PER_TICK:
                break
            if monotonic_fn() - started >= TICK_BUDGET_SECONDS:
                break
            first = state.get(card["card_key"], (None, 0, None))[0]
            if first is not None and now_s - first >= RETRY_FOR.total_seconds():
                store.mark_done(card["card_key"], now_s)
                logger.error(
                    "request_cards: a %s card did not reach the console in %d hours; "
                    "giving up on it (key %s)",
                    card["kind"],
                    int(RETRY_FOR.total_seconds() // 3600),
                    card["card_key"][:16],
                )
                continue
            store.note_attempt(card["card_key"], now_s)
            sent_this_tick += 1
            body = json.dumps(card).encode("utf-8")
            headers = {
                "Authorization": f"Bearer {key}",
                "X-Tenant-Slug": slug,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            }
            try:
                status = post_fn(url, headers, body, POST_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 — one bad POST never stops the leg
                logger.warning("request_cards: card POST failed: %s", exc)
                continue
            if status == 200:
                store.mark_done(card["card_key"], now_s)
                landed += 1
            else:
                logger.warning(
                    "request_cards: console returned %d for a %s card", status, card["kind"]
                )
        store.prune(now_s)
    except Exception:  # noqa: BLE001 — the leg must never take the ticker down
        logger.error("request_cards: leg failed", exc_info=True)
    return landed


__all__ = [
    "CARD_PATH",
    "CardStore",
    "MAX_CARDS_PER_TICK",
    "NO_REPLY_AFTER",
    "QUIET",
    "RETRY_FOR",
    "card_key",
    "card_url",
    "cards_db_path",
    "decide",
    "job_done_key",
    "send_due_cards",
]
