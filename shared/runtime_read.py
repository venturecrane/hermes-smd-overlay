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

# The console's RUNTIME_READ_KINDS. ``audit_log`` reads the per-customer table;
# ``config`` is a single facts snapshot (no table, no pagination — see
# read_config); the rest return an honest empty page until their tables exist.
#
# ``audit_export`` / ``memory_export`` (ss-console#1355, pull-before-destroy):
# the decommission pipeline's preservation reads. ``audit_export`` serves the
# FULL audit_log row (digests + metadata included — the console UI kind
# deliberately omits them, a compliance export must not); ``memory_export``
# serves the ADR-0016 Machine-local memory tables one at a time via ``table=``.
# Both are read-only, same per-customer auth, and exist precisely so the
# console can preserve Machine-local state BEFORE `fly apps destroy` burns it.
SUPPORTED_KINDS: frozenset[str] = frozenset(
    {
        "audit_log",
        "activity",
        "draft",
        "matter",
        "config",
        "audit_export",
        "memory_export",
        "config_export",
        "jobs",
        "entitlements",
        "usage_export",
    }
)
_REAL_KINDS: frozenset[str] = frozenset(
    {
        "audit_log",
        "audit_export",
        "memory_export",
        "config_export",
        "jobs",
        "entitlements",
        "usage_export",
    }
)

# config_export section allow-list (ADR 0048). Unlike ``config`` (a
# materialized-state *facts snapshot* the drift audit diffs), ``config_export``
# serves authored CONTENT blocks from the live ``customer.yaml`` for the admin
# relationship surface. It is allow-listed to specific non-secret sections and
# never returns the whole document — ``config.yaml``/``customer.yaml`` carry
# connector secrets, and a blanket export would leak them. ``relationship`` is
# the authored behavioral lane; new safe content sections append here.
CONFIG_EXPORT_SECTIONS: frozenset[str] = frozenset({"relationship"})

# memory_export table allow-list → which DB path argument serves it. The
# ADR-0016 mirror tables live on the observations binding; the skills
# inventory and the ADR-0048 per-peer learned preferences live on the
# agent-state binding (audit-binding fallback mirrors the audit plugin's
# own fallback).
MEMORY_EXPORT_TABLES: frozenset[str] = frozenset(
    {
        "persona_observations",
        "persona_observations_archive",
        "agent_skills_inventory",
        "peer_preferences",
    }
)

# Tables served from the agent-state DB path (hermes-writable) rather than the
# observations DB. Both are agent-authored, runtime-read, admin-visible.
_AGENT_STATE_TABLES: frozenset[str] = frozenset(
    {
        "agent_skills_inventory",
        "peer_preferences",
    }
)

# jobs columns served by the ``jobs`` observability kind, in a stable order.
# The B1 durable-job control plane lives in the broker-owned job ledger, NOT in
# a sqlite file the gate can open mode=ro (the ledger is broker-uid-owned; only
# the gateway-PID-gated broker verbs read it). So ``jobs`` is served over the
# broker socket via BrokerJobClient.list_all(), not a direct DB read. The
# projection drops nothing material — it is the operator-visible control facts
# (status / cost / lease / result / error) the console surface needs to verify a
# job end-to-end. (Secrets never live in the jobs row, so no allow-list trim is
# needed; the brief itself is authored task text, surfaced as-is.)
_JOBS_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "updated_at",
    "customer_slug",
    "persona_id",
    "model",
    "status",
    "deliver_to",
    "lease_owner",
    "lease_epoch",
    "attempts",
    "budget_cents",
    "spent_cents",
    "cancel_requested",
    "result_ref",
    "error",
)

# audit_log columns, in canonical schema order, for the export kind. Must stay
# in sync with shared.audit_contract INSERT_SQL / the ss-console d1-schema doc.
_AUDIT_EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "action_type",
    "actor",
    "actor_role",
    "skill_name",
    "matter_ref",
    "input_digest",
    "output_digest",
    "diff_digest",
    "trust_ceiling",
    "metadata",
    # Hash-chain columns (#1686): the compliance export carries the chain so
    # an exported ledger is verifiable offline (shared/audit_chain.verify_chain).
    "prev_hash",
    "row_hash",
)

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


def read_config() -> dict[str, Any]:
    """Read the ``operator.runtime.config/v1`` facts snapshot for this Machine.

    Unlike ``read_runtime`` (paginated table reads), ``config`` is a single
    snapshot of *materialized state* (env presence, overlay ref, per-profile
    config + cron) the console's drift audit diffs against declared desired-state.
    Built by ``shared.config_snapshot`` — presence-only (never a secret value),
    truthful-or-degraded (never fabricated). Imported lazily so the audit_log
    read path carries no extra import cost."""
    from shared import config_snapshot

    return config_snapshot.snapshot()


def read_runtime(
    kind: str,
    *,
    db_path: str | None,
    cursor: str | None = None,
    limit: str | None = None,
    table: str | None = None,
    observations_db_path: str | None = None,
    agent_state_db_path: str | None = None,
    section: str | None = None,
    customer_yaml_path: str | None = None,
) -> dict[str, Any]:
    """Read one page of runtime detail for this Machine's single customer.

    Returns ``{"entries": [...], "cursor": <next|None>}``. Unknown or
    not-yet-materialized kinds return an empty page (honest, never fabricated).
    The caller has already authenticated the request.

    ``audit_export`` reads the same DB as ``audit_log`` but serves the full
    row. ``memory_export`` requires ``table`` (from MEMORY_EXPORT_TABLES) and
    reads the observations / agent-state DB for that table. ``config_export``
    requires ``section`` (from CONFIG_EXPORT_SECTIONS) and reads the authored
    block from the live ``customer.yaml`` (ADR 0048).
    """
    if kind not in SUPPORTED_KINDS or kind not in _REAL_KINDS:
        return {"entries": [], "cursor": None}

    if kind == "config_export":
        if section not in CONFIG_EXPORT_SECTIONS:
            # Unknown section is a caller error, not a degraded read — refuse
            # rather than guessing (the gate maps this to a 400).
            return {"entries": [], "cursor": None, "error": "unknown section"}
        return _read_config_export(section, customer_yaml_path)

    if kind == "jobs":
        # The job ledger is broker-owned (gateway-PID-gated verbs), not a
        # sqlite file the gate can open mode=ro — so this read goes over the
        # broker socket, not ``db_path``. Fail-safe: a broker that is down or
        # has no job ledger configured returns an honest empty page (same
        # posture as a missing audit DB), never a 500.
        return _read_jobs()

    if kind == "entitlements":
        # Live runtime exposure overrides (ss#2003 Q7 — the entitlement dial).
        # Serves the override store directly so the console and the live
        # probes read the Machine's actual posture, never a projection. A
        # missing store file is "no override was ever set" — honest empty.
        from shared.exposure_override import read_all

        return {"entries": read_all(), "cursor": None}

    if kind == "usage_export":
        # Per-person token meter (ss-console #2070). Lives on the agent-state
        # db beside the other agent-authored tables. SMD-only consumption: the
        # console renders it on the admin cost plane, never to the client.
        # A seat that has not metered anything yet is an honest empty page.
        if not agent_state_db_path or not os.path.exists(agent_state_db_path):
            return {"entries": [], "cursor": None}
        return _read_table_export(agent_state_db_path, "usage_meter", cursor, clamp_limit(limit))

    if kind == "memory_export":
        if table not in MEMORY_EXPORT_TABLES:
            # Unknown table is a caller error, not a degraded read — refuse
            # rather than guessing (the gate maps this to a 400).
            return {"entries": [], "cursor": None, "error": "unknown table"}
        if table in _AGENT_STATE_TABLES:
            target = agent_state_db_path
        else:
            target = observations_db_path
        if not target or not os.path.exists(target):
            return {"entries": [], "cursor": None}
        return _read_table_export(target, table, cursor, clamp_limit(limit))

    # No binding, or the DB file doesn't exist yet (a fresh Machine before the
    # audit subsystem's first write legitimately has no audit.db) → honest empty,
    # never a 500.
    if not db_path or not os.path.exists(db_path):
        return {"entries": [], "cursor": None}
    if kind == "audit_export":
        return _read_audit_export(db_path, _valid_cursor(cursor), clamp_limit(limit))
    return _read_audit_log(db_path, _valid_cursor(cursor), clamp_limit(limit))


def _read_config_export(section: str, customer_yaml_path: str | None) -> dict[str, Any]:
    """Serve an allow-listed authored block from the live ``customer.yaml``.

    Currently serves ``relationship`` (ADR 0048) as one entry per person, via
    the normalized, closed-set accessor on :class:`CustomerConfig` (so unknown
    keys can never leak through the seam). Fail-safe: an absent / unparseable
    ``customer.yaml`` (a fresh or half-provisioned box) returns an honest empty
    page rather than a 500 — same posture as the audit reads on a missing DB.
    Imported lazily so the hot audit_log read path carries no extra import cost.
    """
    from shared.customer_config import (
        DEFAULT_VOLUME_PATH,
        CustomerConfig,
        CustomerConfigError,
    )

    path = customer_yaml_path or DEFAULT_VOLUME_PATH
    try:
        cfg = CustomerConfig.from_volume(path)
    except CustomerConfigError:
        # Missing (CustomerConfigMissingError) or unparseable — both subclasses.
        # The relationship surface fails closed to "empty", never leaks a fault.
        return {"entries": [], "cursor": None}

    if section == "relationship":
        try:
            return {"entries": cfg.relationship_people(), "cursor": None}
        except CustomerConfigError:
            return {"entries": [], "cursor": None}
    return {"entries": [], "cursor": None}


def _read_jobs() -> dict[str, Any]:
    """Serve the B1 job ledger as the ``jobs`` observability kind.

    Reads over the broker socket (``BrokerJobClient.list_all`` →
    ``job_list``), projects each row to the stable ``_JOBS_COLUMNS`` set, and
    returns a single unpaginated page (the job count per Machine is small).
    Fail-safe: an unreachable / unconfigured broker (socket env unset, broker
    down, or no job ledger) returns an honest empty page rather than a 500 —
    same posture as the audit reads on a missing DB. Imported lazily so the hot
    audit_log read path carries no extra import cost.
    """
    from shared.job_ledger_client import BrokerJobClient, JobLedgerError

    try:
        rows = BrokerJobClient().list_all()
    except JobLedgerError:
        return {"entries": [], "cursor": None}
    entries = [{col: row.get(col) for col in _JOBS_COLUMNS} for row in rows]
    return {"entries": entries, "cursor": None}


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


def _read_audit_export(db_path: str, cursor: str | None, limit: int) -> dict[str, Any]:
    """Full-row audit_log export, keyset-paginated ASCENDING by id (ULID).

    The export kind walks oldest→newest so a paged pull that is interrupted
    and resumed never misses rows written behind the cursor. All twelve
    canonical columns are served — digests and metadata included, because the
    compliance archive must carry the integrity material the UI kind omits.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        cols = ", ".join(_AUDIT_EXPORT_COLUMNS)
        if cursor is not None:
            sql = f"SELECT {cols} FROM audit_log WHERE id > ? ORDER BY id ASC LIMIT ?"
            rows = conn.execute(sql, (cursor, limit)).fetchall()
        else:
            sql = f"SELECT {cols} FROM audit_log ORDER BY id ASC LIMIT ?"
            rows = conn.execute(sql, (limit,)).fetchall()
    except sqlite3.OperationalError:
        return {"entries": [], "cursor": None}
    finally:
        conn.close()

    entries = [{col: row[col] for col in _AUDIT_EXPORT_COLUMNS} for row in rows]
    next_cursor = entries[-1]["id"] if len(entries) == limit and entries else None
    return {"entries": entries, "cursor": next_cursor}


def _read_table_export(db_path: str, table: str, cursor: str | None, limit: int) -> dict[str, Any]:
    """Generic full-row table export, keyset-paginated by sqlite rowid.

    Serves the ADR-0016 memory tables (allow-listed in MEMORY_EXPORT_TABLES —
    ``table`` is validated by the caller, never interpolated from raw input).
    rowid keyset works regardless of each table's PK naming; ascending order
    gives the same resume-safe property as the audit export.
    """
    try:
        cursor_rowid = int(cursor) if cursor is not None else None
    except ValueError:
        cursor_rowid = None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        if cursor_rowid is not None:
            sql = (
                f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid ASC LIMIT ?"  # noqa: S608 — table is allow-listed
            )
            rows = conn.execute(sql, (cursor_rowid, limit)).fetchall()
        else:
            sql = f"SELECT rowid AS _rowid, * FROM {table} ORDER BY rowid ASC LIMIT ?"  # noqa: S608 — table is allow-listed
            rows = conn.execute(sql, (limit,)).fetchall()
    except sqlite3.OperationalError:
        # Table not created yet (mirror never wrote) → honest empty.
        return {"entries": [], "cursor": None}
    finally:
        conn.close()

    entries = [dict(row) for row in rows]
    next_cursor = str(entries[-1]["_rowid"]) if len(entries) == limit and entries else None
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
    "MEMORY_EXPORT_TABLES",
    "CONFIG_EXPORT_SECTIONS",
    "MIN_KEY_LEN",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "verify_runtime_auth",
    "clamp_limit",
    "read_runtime",
    "read_config",
]
