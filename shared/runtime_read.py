"""Console→Machine runtime read (ADR 0043 path A) — Machine-side core.

The console drill-ins (admin §5.5 + client Activity) read this Machine's deep
runtime detail on demand, one customer per call, read-only, authenticated. This
module is the pure core the webhook gate's ``do_GET`` calls: auth verification +
the read itself. Keeping it out of ``webhook_gate.py`` keeps the HTTP handler
thin and lets the auth + pagination logic be unit-tested without a socket.

Security posture (per ADR 0043 + the per-customer-key model the console uses):

* **Per-customer bearer.** The console sends ``Authorization: Bearer <key>``
  where ``<key> = HMAC-SHA256(master, customer_id)``. Each Machine holds ONLY
  its own derived key (set as a Fly secret at provision time); the master lives
  only on the console. A key extracted from one Machine cannot read another.
  Verification is a constant-time compare against this Machine's own
  ``OPERATOR_RUNTIME_READ_KEY`` — we never see or need the master.
* **Fail-closed on misconfig.** If the key env is unset or too short, the
  endpoint refuses (opaque 401) rather than serving unauthenticated — a
  half-provisioned Machine never leaks its audit log.
* **Tenant-slug sanity.** ``X-Tenant-Slug`` must equal this Machine's own
  ``SMD_CUSTOMER_SLUG``. With a per-customer key this is belt-and-suspenders,
  but it keeps a misrouted request from ever touching the DB.
* **Read-only at the engine.** Reads open a fresh ``mode=ro`` SQLite connection
  per request (never the audit writer's RW connection), so a read physically
  cannot mutate. ``busy_timeout`` lets a read wait out the sub-millisecond audit
  write rather than raising ``SQLITE_BUSY`` — WAL is intentionally NOT required
  (see the gate's read path).

What it serves: ``audit_log`` is read from the per-customer ``audit_log`` table
and shaped to the console's frozen wire contract (``parseAuditEntries`` in
ss-console ``src/lib/portal/operator/activity-read.ts``). ``draft`` / ``matter``
/ ``activity`` have no runtime table on the Machine yet, so they return an
honest empty page (never fabricated rows); they light up when those tables land.
"""

from __future__ import annotations

import hmac
import os
import sqlite3
from typing import Any

# The console's RUNTIME_READ_KINDS. Only audit_log has a Machine table today;
# the rest return an honest empty page until their runtime tables exist.
SUPPORTED_KINDS: frozenset[str] = frozenset({"audit_log", "activity", "draft", "matter"})
_REAL_KINDS: frozenset[str] = frozenset({"audit_log"})

# A derived key is hex(HMAC-SHA256) = 64 chars; reject anything implausibly short
# so a blank/placeholder secret can never authenticate.
MIN_KEY_LEN = 32

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def verify_runtime_auth(
    auth_header: str | None,
    slug_header: str | None,
    *,
    key: str | None,
    own_slug: str | None,
) -> bool:
    """Constant-time verify a console→Machine read request.

    Returns False (→ opaque 401) on any of: key unset/too short (fail-closed
    misconfig), missing/malformed bearer, bearer mismatch, or tenant-slug not
    equal to this Machine's own slug. No branch reveals which check failed.
    """
    if not key or len(key) < MIN_KEY_LEN:
        return False
    if not own_slug:
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[len("Bearer ") :]
    if not hmac.compare_digest(provided, key):
        return False
    if not slug_header or not hmac.compare_digest(slug_header, own_slug):
        return False
    return True


def clamp_limit(raw: str | None) -> int:
    """Parse + clamp the ``limit`` query param to [1, MAX_LIMIT]."""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if n < 1:
        return 1
    return min(n, MAX_LIMIT)


def _valid_cursor(cursor: str | None) -> str | None:
    """Accept an opaque ULID cursor; reject implausible values.

    The cursor is the last ``id`` (a ULID: 26 Crockford-base32 chars) the
    console received. We keep it conservative — a malformed cursor returns the
    first page rather than erroring, but an over-long value is rejected so a
    crafted cursor can't bloat the query.
    """
    if not cursor:
        return None
    if len(cursor) > 64:
        return None
    return cursor


def read_runtime(
    kind: str,
    *,
    db_path: str | None,
    cursor: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    """Read one page of runtime detail for this Machine's single customer.

    Returns ``{"entries": [...], "cursor": <next|None>}``. Unknown or
    not-yet-materialized kinds return an empty page (honest, never fabricated).
    The caller has already authenticated the request.
    """
    if kind not in SUPPORTED_KINDS or kind not in _REAL_KINDS:
        return {"entries": [], "cursor": None}
    # No binding, or the DB file doesn't exist yet (a fresh Machine before the
    # audit subsystem's first write legitimately has no audit.db) → honest empty,
    # never a 500.
    if not db_path or not os.path.exists(db_path):
        return {"entries": [], "cursor": None}
    return _read_audit_log(db_path, _valid_cursor(cursor), clamp_limit(limit))


# audit_log columns (per-customer D1) → the console wire shape consumed by
# parseAuditEntries (id/ts/actor/action required; the rest optional).
_AUDIT_SELECT = (
    "SELECT id, ts, action_type, actor, actor_role, skill_name, matter_ref FROM audit_log"
)


def _read_audit_log(db_path: str, cursor: str | None, limit: int) -> dict[str, Any]:
    """Keyset-paginate the audit_log newest-first.

    ``id`` is a ULID (lexicographically time-sortable TEXT), so ``ORDER BY id
    DESC`` + ``WHERE id < :cursor`` (string compare) is a correct keyset cursor.
    The connection is read-only (``mode=ro``) with a busy timeout so a
    concurrent audit write never turns into a 500.
    """
    # mode=ro: the engine refuses any write on this connection. uri=True is
    # required for the file: URI form.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if cursor is not None:
            sql = f"{_AUDIT_SELECT} WHERE id < ? ORDER BY id DESC LIMIT ?"
            rows = conn.execute(sql, (cursor, limit)).fetchall()
        else:
            sql = f"{_AUDIT_SELECT} ORDER BY id DESC LIMIT ?"
            rows = conn.execute(sql, (limit,)).fetchall()
    except sqlite3.OperationalError:
        # DB exists but has no audit_log table yet (the audit subsystem hasn't
        # created it). Honest empty, not a 500.
        return {"entries": [], "cursor": None}
    finally:
        conn.close()

    entries = [_shape_audit_row(r) for r in rows]
    # Only advertise a next cursor when the page was full (more may remain).
    next_cursor = entries[-1]["id"] if len(entries) == limit and entries else None
    return {"entries": entries, "cursor": next_cursor}


def _shape_audit_row(row: sqlite3.Row) -> dict[str, Any]:
    """Map an audit_log row to the console wire contract.

    Required: id/ts/actor/action. Optional pass-throughs the console validates
    on its side (actorRole against its enum; the rest as nullable strings).
    Internal digest columns are deliberately NOT exposed.
    """
    return {
        "id": row["id"],
        "ts": row["ts"],
        "action": row["action_type"],
        "actor": row["actor"],
        "actorRole": row["actor_role"],
        "skill": row["skill_name"],
        "matterRef": row["matter_ref"],
    }


__all__ = [
    "SUPPORTED_KINDS",
    "MIN_KEY_LEN",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "verify_runtime_auth",
    "clamp_limit",
    "read_runtime",
]
