"""Per-source ingestion state for voice samples — D1 reads and writes.

Ported from ss-console/operator/adapter/voice/state.py.

The dashboard reads ``voice_source_state`` to render the
``last-ingestion-at`` health indicator and the per-cohort sample count.

The pipeline upserts on every run regardless of outcome so that even a
failed run produces a fresh row the dashboard can light up red.

Decommission walks ``voice_ingestion_items``, removes every R2 object,
and clears the state row. The retention enforcer uses the same table to
select expired rows and remove their R2 objects per the
``voice_retention_days`` setting on customer.yaml.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

log = logging.getLogger("aie.voice.state")


# Ingestion status vocabulary. The dashboard maps these onto health
# colors (green / yellow / red).
INGEST_STATUS_OK = "ok"
INGEST_STATUS_STALE = "stale"
INGEST_STATUS_ERROR = "error"
INGEST_STATUS_NEVER_RUN = "never_run"

VALID_STATUSES = frozenset(
    {INGEST_STATUS_OK, INGEST_STATUS_STALE, INGEST_STATUS_ERROR, INGEST_STATUS_NEVER_RUN}
)


# When a recipient has no cohort assigned in memory rules, the sample
# is tagged with this sentinel.
COHORT_UNASSIGNED = "unassigned"


# ---------------------------------------------------------------------------
# Small helpers (local copies to avoid coupling to audit_log internals)
# ---------------------------------------------------------------------------


def _iso_utc(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def _ulid(now_ms: int | None = None) -> str:
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceSourceState:
    """Dashboard read model for one (source_kind, source_id) pair.

    The Captain dashboard renders a row per source. ``samples_by_cohort``
    is the JSON object the pipeline wrote on the last run, decoded into
    a Python dict; the dashboard renders it as a one-line histogram.
    """

    source_kind: str
    source_id: str
    last_ingestion_at: str
    last_success_at: str | None
    last_error: str | None
    ingest_status: str
    items_last_run: int
    samples_by_cohort: dict
    schema_version: int


@dataclass(frozen=True)
class VoiceIngestionItem:
    """Provenance row for one ingested voice sample.

    Used by:

    * The retention enforcer (selects rows older than the retention
      window).
    * The decommission hook (walks every row for one source and removes
      the R2 object).
    * Tests verifying the audit-style insert path.

    ``source_message_digest`` is the SHA-256 of the upstream message ID,
    not the message ID itself.
    """

    id: str
    source_kind: str
    source_id: str
    source_message_digest: str
    recipient_cohort_id: str
    partner_authored: bool
    filter_reason: str | None
    ingested_at: str
    sent_at: str
    r2_key: str | None
    structural_diff_digest: str | None
    word_count: int | None
    schema_version: int
    deleted_at: str | None


# ---------------------------------------------------------------------------
# Executor protocols
# ---------------------------------------------------------------------------


class WriteExecutor(Protocol):
    async def execute(self, sql: str, params: list) -> None: ...


class QueryExecutor(Protocol):
    """A query executor returns rows for SELECT statements.

    Production wires this to the Cloudflare D1 HTTP API's ``query``
    endpoint; tests pass a sqlite-backed executor that returns
    ``list[dict[str, object]]``.
    """

    async def query(self, sql: str, params: list) -> list[dict]: ...


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_UPSERT_STATE_SQL = (
    "INSERT INTO voice_source_state "
    "(source_kind, source_id, last_ingestion_at, last_success_at, last_error, "
    "ingest_status, items_last_run, samples_by_cohort_json, schema_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(source_kind, source_id) DO UPDATE SET "
    "last_ingestion_at      = excluded.last_ingestion_at, "
    "last_success_at        = COALESCE(excluded.last_success_at, voice_source_state.last_success_at), "
    "last_error             = excluded.last_error, "
    "ingest_status          = excluded.ingest_status, "
    "items_last_run         = excluded.items_last_run, "
    "samples_by_cohort_json = excluded.samples_by_cohort_json, "
    "schema_version         = excluded.schema_version"
)


_INSERT_ITEM_SQL = (
    "INSERT INTO voice_ingestion_items "
    "(id, source_kind, source_id, source_message_digest, recipient_cohort_id, "
    "partner_authored, filter_reason, ingested_at, sent_at, r2_key, "
    "structural_diff_digest, word_count, schema_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


_SELECT_STATES_SQL = (
    "SELECT source_kind, source_id, last_ingestion_at, last_success_at, "
    "last_error, ingest_status, items_last_run, samples_by_cohort_json, "
    "schema_version "
    "FROM voice_source_state "
    "ORDER BY last_ingestion_at DESC"
)


_SELECT_ITEMS_FOR_DECOMMISSION_SQL = (
    "SELECT id, r2_key, structural_diff_digest "
    "FROM voice_ingestion_items "
    "WHERE source_kind = ? AND source_id = ? AND deleted_at IS NULL"
)


_SELECT_ITEMS_FOR_RETENTION_SQL = (
    "SELECT id, r2_key, structural_diff_digest "
    "FROM voice_ingestion_items "
    "WHERE ingested_at < ? AND deleted_at IS NULL AND r2_key IS NOT NULL"
)


_MARK_ITEMS_DELETED_BY_SOURCE_SQL = (
    "UPDATE voice_ingestion_items "
    "SET deleted_at = ? "
    "WHERE source_kind = ? AND source_id = ? AND deleted_at IS NULL"
)


_MARK_ITEMS_DELETED_BY_ID_SQL = (
    "UPDATE voice_ingestion_items SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL"
)


_DELETE_STATE_SQL = "DELETE FROM voice_source_state WHERE source_kind = ? AND source_id = ?"


_SELECT_EXISTING_DIGEST_SQL = (
    "SELECT id FROM voice_ingestion_items "
    "WHERE source_kind = ? AND source_id = ? AND source_message_digest = ? "
    "AND deleted_at IS NULL LIMIT 1"
)


# ---------------------------------------------------------------------------
# Dataclasses for writes
# ---------------------------------------------------------------------------


@dataclass
class IngestionStateUpdate:
    """One ingestion-run outcome destined for ``voice_source_state``.

    ``samples_by_cohort`` is the histogram for THIS run only — the
    upsert replaces the row's snapshot wholesale. The dashboard renders
    "what was ingested in the last run."
    """

    source_kind: str
    source_id: str
    ingested_at: str
    status: str
    items_last_run: int
    samples_by_cohort: dict
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"ingest_status {self.status!r} not in {sorted(VALID_STATUSES)}")


@dataclass
class IngestionItemRecord:
    """One provenance row destined for ``voice_ingestion_items``."""

    source_kind: str
    source_id: str
    source_message_digest: str
    recipient_cohort_id: str
    partner_authored: bool
    sent_at: str
    filter_reason: str | None = None
    r2_key: str | None = None
    structural_diff_digest: str | None = None
    word_count: int | None = None
    schema_version: int = 1


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class VoiceSourceStateStore:
    """D1-backed store for voice ingestion state and provenance.

    Construction takes a write executor and (optionally) a query
    executor. Reads are only required for the dashboard (read_states)
    and the retention / decommission code paths. The pipeline itself
    only needs writes.
    """

    def __init__(
        self,
        write_executor: WriteExecutor,
        query_executor: QueryExecutor | None = None,
    ) -> None:
        self._write = write_executor
        self._query = query_executor

    async def upsert_state(self, update: IngestionStateUpdate) -> None:
        """Upsert the per-source state row. Called once per run."""
        params = [
            update.source_kind,
            update.source_id,
            update.ingested_at,
            update.ingested_at if update.status == INGEST_STATUS_OK else None,
            update.error,
            update.status,
            update.items_last_run,
            json.dumps(update.samples_by_cohort, sort_keys=True, separators=(",", ":")),
            1,
        ]
        await self._write.execute(_UPSERT_STATE_SQL, params)

    async def insert_item(self, item: IngestionItemRecord) -> str:
        """Insert one provenance row. Returns the generated ULID."""
        ulid = _ulid()
        ts = _iso_utc()
        params = [
            ulid,
            item.source_kind,
            item.source_id,
            item.source_message_digest,
            item.recipient_cohort_id,
            1 if item.partner_authored else 0,
            item.filter_reason,
            ts,
            item.sent_at,
            item.r2_key,
            item.structural_diff_digest,
            item.word_count,
            item.schema_version,
        ]
        await self._write.execute(_INSERT_ITEM_SQL, params)
        return ulid

    async def already_ingested(
        self, source_kind: str, source_id: str, source_message_digest: str
    ) -> bool:
        """Return True when a non-deleted row already exists for this
        (source, message digest). Lets the pipeline skip re-ingestion
        without duplicating R2 objects.
        """
        if self._query is None:
            return False
        rows = await self._query.query(
            _SELECT_EXISTING_DIGEST_SQL,
            [source_kind, source_id, source_message_digest],
        )
        return len(rows) > 0

    async def read_states(self) -> list[VoiceSourceState]:
        """Return all (source_kind, source_id) rows, newest run first."""
        if self._query is None:
            raise RuntimeError("read_states requires a query executor")
        rows = await self._query.query(_SELECT_STATES_SQL, [])
        out: list[VoiceSourceState] = []
        for row in rows:
            samples_json = row.get("samples_by_cohort_json")
            samples_by_cohort: dict = {}
            if samples_json:
                try:
                    samples_by_cohort = json.loads(samples_json)
                except json.JSONDecodeError:
                    log.warning(
                        "voice_source_state.samples_by_cohort_json failed to parse for "
                        "(%s, %s); rendering empty",
                        row.get("source_kind"),
                        row.get("source_id"),
                    )
            out.append(
                VoiceSourceState(
                    source_kind=row["source_kind"],
                    source_id=row["source_id"],
                    last_ingestion_at=row["last_ingestion_at"],
                    last_success_at=row.get("last_success_at"),
                    last_error=row.get("last_error"),
                    ingest_status=row["ingest_status"],
                    items_last_run=row["items_last_run"],
                    samples_by_cohort=samples_by_cohort,
                    schema_version=row.get("schema_version", 1),
                )
            )
        return out

    async def list_items_for_decommission(self, source_kind: str, source_id: str) -> list[dict]:
        """Enumerate provenance rows still active for one source."""
        if self._query is None:
            raise RuntimeError("list_items_for_decommission requires a query executor")
        return await self._query.query(_SELECT_ITEMS_FOR_DECOMMISSION_SQL, [source_kind, source_id])

    async def list_items_for_retention(self, older_than_iso: str) -> list[dict]:
        """Enumerate active rows ingested before ``older_than_iso``.

        Caller computes the cutoff from ``voice_retention_days`` on
        customer.yaml; this method does not know the policy.
        """
        if self._query is None:
            raise RuntimeError("list_items_for_retention requires a query executor")
        return await self._query.query(_SELECT_ITEMS_FOR_RETENTION_SQL, [older_than_iso])

    async def mark_items_deleted_by_source(self, source_kind: str, source_id: str) -> None:
        """Soft-delete all active items for one source. Called by the
        decommission hook before the state row is removed."""
        await self._write.execute(
            _MARK_ITEMS_DELETED_BY_SOURCE_SQL,
            [_iso_utc(), source_kind, source_id],
        )

    async def mark_item_deleted(self, item_id: str) -> None:
        """Soft-delete a single item by ID. Used by the retention
        enforcer after the R2 object has been removed."""
        await self._write.execute(_MARK_ITEMS_DELETED_BY_ID_SQL, [_iso_utc(), item_id])

    async def delete_state(self, source_kind: str, source_id: str) -> None:
        """Remove the source's state row. Called only by the decommission
        hook after all items have been soft-deleted and their R2 objects
        removed."""
        await self._write.execute(_DELETE_STATE_SQL, [source_kind, source_id])


__all__ = [
    "COHORT_UNASSIGNED",
    "INGEST_STATUS_ERROR",
    "INGEST_STATUS_NEVER_RUN",
    "INGEST_STATUS_OK",
    "INGEST_STATUS_STALE",
    "IngestionItemRecord",
    "IngestionStateUpdate",
    "QueryExecutor",
    "VALID_STATUSES",
    "VoiceIngestionItem",
    "VoiceSourceState",
    "VoiceSourceStateStore",
    "WriteExecutor",
]
