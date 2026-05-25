"""TTL archival of aged Honcho conclusions.

Ported from ss-console/ai-employee/adapter/memory/retention.py. The
original retention.py walked the customer-owned ``memory_ingested_items``
table; under ADR 0016 (Honcho mirror, not artifact) the analogous shape
is the ``persona_observations`` mirror — and the destructive operation
now also targets Honcho (physical delete via ``DELETE /conclusions/{id}``)
instead of R2 + Vectorize.

Two operations live in this module:

* :func:`archive_aged_conclusions` — periodic sweep. Walks
  ``persona_observations`` for rows whose ``honcho_created_at`` is
  older than ``archive_after_days`` (default 180). For each row:
    1. Copy the row into ``persona_observations_archive`` with a fresh
       ``archived_at`` stamp.
    2. Physically delete the corresponding Honcho conclusion.
    3. Delete the row from ``persona_observations``.

  Order matters: archive-first, Honcho-delete-second, live-delete-last.
  If Honcho is unreachable mid-sweep we still have the archive copy
  and the live row, so a subsequent pass picks up where this one
  stopped. If the live-row delete fails after Honcho is already
  deleted, the live row remains as a tombstone — the
  ``honcho_conclusion_id`` UNIQUE constraint prevents a future mirror
  pass from re-inserting; the row is detectable by Captain as "Honcho
  missing" and can be cleaned up via admin tooling.

* :func:`restore_from_archive` — Captain restore path. Re-inserts the
  archived conclusion into Honcho, copies the row back into
  ``persona_observations``, and removes the archive row. Used when
  Captain decides an archived observation is still relevant.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shared.d1_client import D1Client
from shared.secrets import require

from .honcho_client import HonchoClient, HonchoUnreachable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — match ss-console/ai-employee/adapter/memory/retention.py
# (180 days is a working-set window for an active customer; longer than
# the per-document retention window and shorter than the audit-log
# retention norm).
# ---------------------------------------------------------------------------


DEFAULT_ARCHIVE_AFTER_DAYS: int = 180


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_SELECT_AGED_SQL = (
    "SELECT observation_id, honcho_conclusion_id, session_id, persona_slug, "
    "observation_type, observation_body, source_message_ids, confidence, "
    "evidence_status, honcho_created_at, mirrored_at, schema_version, "
    "dismissed_at, dismissed_by, dismissed_reason "
    "FROM persona_observations "
    "WHERE honcho_created_at < ? "
    "ORDER BY honcho_created_at ASC "
    "LIMIT ?"
)


_INSERT_ARCHIVE_SQL = (
    "INSERT INTO persona_observations_archive "
    "(observation_id, honcho_conclusion_id, session_id, persona_slug, "
    "observation_type, observation_body, source_message_ids, confidence, "
    "evidence_status, honcho_created_at, mirrored_at, archived_at, "
    "archive_reason, schema_version, dismissed_at, dismissed_by, dismissed_reason) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


_DELETE_LIVE_SQL = "DELETE FROM persona_observations WHERE observation_id = ?"


_SELECT_ARCHIVED_SQL = (
    "SELECT observation_id, honcho_conclusion_id, session_id, persona_slug, "
    "observation_type, observation_body, source_message_ids, confidence, "
    "evidence_status, honcho_created_at, mirrored_at, schema_version "
    "FROM persona_observations_archive "
    "WHERE observation_id = ?"
)


_INSERT_LIVE_SQL = (
    "INSERT INTO persona_observations "
    "(observation_id, honcho_conclusion_id, session_id, persona_slug, "
    "observation_type, observation_body, source_message_ids, confidence, "
    "evidence_status, honcho_created_at, mirrored_at, schema_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


_DELETE_ARCHIVE_SQL = "DELETE FROM persona_observations_archive WHERE observation_id = ?"


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _iso_utc(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of one archival sweep.

    The cron caller logs ``archived`` + ``errors`` and surfaces both
    counts to the admin dashboard.
    """

    archive_after_days: int
    rows_considered: int
    rows_archived: int
    rows_with_honcho_missing: int
    errors: int
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of one Captain-triggered restore."""

    observation_id: str
    honcho_conclusion_id_new: str
    restored_at: str


# ---------------------------------------------------------------------------
# Archive sweep
# ---------------------------------------------------------------------------


def archive_aged_conclusions(
    *,
    archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS,
    batch_size: int = 100,
    honcho_client: HonchoClient | None = None,
    d1_client: D1Client | None = None,
    now: datetime | None = None,
) -> ArchiveResult:
    """Move conclusions older than ``archive_after_days`` from live to archive.

    Sweeps in batches of ``batch_size`` to avoid loading the entire aged
    set into memory. Per-row errors are counted but do not abort the
    sweep — one Honcho hiccup never blocks the rest of the work.

    Args:
        archive_after_days: TTL window in days. Defaults to 180 (matches
            the retention default carried forward from ADR 0008's
            ``DEFAULT_MATTERS_DAYS / 4`` working-set heuristic; tuned to
            keep Honcho's active set bounded without dropping context).
        batch_size: Maximum rows processed per call. Subsequent calls
            pick up where this one stopped (selection is by
            ``honcho_created_at ASC``).
        honcho_client: optional pre-constructed; built from env when omitted.
        d1_client: optional pre-constructed; built from env when omitted.
        now: optional clock override for tests.
    """
    if archive_after_days <= 0:
        raise ValueError("archive_after_days must be a positive int")

    started = now or datetime.now(UTC)
    started_iso = _iso_utc(started)
    cutoff_iso = _iso_utc(started - timedelta(days=archive_after_days))

    if honcho_client is None or d1_client is None:
        secrets = require(
            "SMD_CUSTOMER_SLUG",
            "SMD_D1_OBSERVATIONS_BINDING",
            "HONCHO_BASE_URL",
            "HONCHO_API_KEY",
        )
        if honcho_client is None:
            honcho_client = HonchoClient(
                base_url=secrets["HONCHO_BASE_URL"],
                api_key=secrets["HONCHO_API_KEY"],
            )
        if d1_client is None:
            d1_client = D1Client(
                binding_name=secrets["SMD_D1_OBSERVATIONS_BINDING"],
                customer_slug=secrets["SMD_CUSTOMER_SLUG"],
            )

    rows = d1_client.query(_SELECT_AGED_SQL, cutoff_iso, batch_size)
    rows_considered = len(rows)
    rows_archived = 0
    rows_with_honcho_missing = 0
    errors = 0

    for row in rows:
        observation_id = _row_get(row, "observation_id", 0)
        honcho_conclusion_id = _row_get(row, "honcho_conclusion_id", 1)
        if not observation_id or not honcho_conclusion_id:
            errors += 1
            logger.warning("archive: skipping row missing identifiers row=%r", row)
            continue
        archived_at = _iso_utc()
        try:
            # 1. Copy to archive table. We pull all 17 columns from the
            # row in stable order to mirror _INSERT_ARCHIVE_SQL.
            d1_client.execute(
                _INSERT_ARCHIVE_SQL,
                observation_id,
                honcho_conclusion_id,
                _row_get(row, "session_id", 2),
                _row_get(row, "persona_slug", 3),
                _row_get(row, "observation_type", 4),
                _row_get(row, "observation_body", 5),
                _row_get(row, "source_message_ids", 6),
                _row_get(row, "confidence", 7),
                _row_get(row, "evidence_status", 8),
                _row_get(row, "honcho_created_at", 9),
                _row_get(row, "mirrored_at", 10),
                archived_at,
                "ttl",
                _row_get(row, "schema_version", 11) or 1,
                _row_get(row, "dismissed_at", 12),
                _row_get(row, "dismissed_by", 13),
                _row_get(row, "dismissed_reason", 14),
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                "archive: archive INSERT failed observation=%s err=%s",
                observation_id,
                exc,
            )
            continue

        # 2. Physically delete from Honcho. ADR 0016 §1: corrections do
        # not propagate through the reasoning tree, so a hard delete is
        # the only reliable way to age a conclusion out.
        try:
            existed = honcho_client.delete_conclusion(honcho_conclusion_id)
            if not existed:
                rows_with_honcho_missing += 1
        except HonchoUnreachable as exc:
            # Honcho is down; stop the sweep so we don't keep archiving
            # rows whose Honcho copies survive (the next pass picks up
            # exactly the rows we missed because they're still in the
            # live table).
            errors += 1
            logger.warning(
                "archive: Honcho delete failed observation=%s err=%s; halting sweep",
                observation_id,
                exc,
            )
            break

        # 3. Delete from live table. Now safe — the archive copy is
        # durable and Honcho's copy is gone.
        try:
            d1_client.execute(_DELETE_LIVE_SQL, observation_id)
            rows_archived += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.error(
                "archive: live DELETE failed after Honcho-delete observation=%s "
                "err=%s — row will need manual cleanup (Honcho already gone)",
                observation_id,
                exc,
            )

    finished_iso = _iso_utc()
    logger.info(
        "archive: cutoff=%s considered=%d archived=%d honcho_missing=%d errors=%d",
        cutoff_iso,
        rows_considered,
        rows_archived,
        rows_with_honcho_missing,
        errors,
    )
    return ArchiveResult(
        archive_after_days=archive_after_days,
        rows_considered=rows_considered,
        rows_archived=rows_archived,
        rows_with_honcho_missing=rows_with_honcho_missing,
        errors=errors,
        started_at=started_iso,
        finished_at=finished_iso,
    )


# ---------------------------------------------------------------------------
# Restore from archive
# ---------------------------------------------------------------------------


def restore_from_archive(
    observation_id: str,
    *,
    honcho_client: HonchoClient | None = None,
    d1_client: D1Client | None = None,
    now: datetime | None = None,
) -> RestoreResult:
    """Restore an archived observation back into the live Honcho store.

    Steps:
      1. Read the archive row.
      2. POST the conclusion payload back to Honcho; capture the new id.
      3. Insert a row into ``persona_observations`` with the new
         ``honcho_conclusion_id`` and a fresh ``mirrored_at``.
      4. Delete the archive row.

    Args:
        observation_id: the archive table's observation_id (NOT the
            Honcho conclusion id, which will change on restore).

    Raises:
        ValueError: archive row is missing.
        HonchoUnreachable: Honcho POST failed.
    """
    if not observation_id:
        raise ValueError("observation_id is required")

    restored_at = _iso_utc(now)

    if honcho_client is None or d1_client is None:
        secrets = require(
            "SMD_CUSTOMER_SLUG",
            "SMD_D1_OBSERVATIONS_BINDING",
            "HONCHO_BASE_URL",
            "HONCHO_API_KEY",
        )
        if honcho_client is None:
            honcho_client = HonchoClient(
                base_url=secrets["HONCHO_BASE_URL"],
                api_key=secrets["HONCHO_API_KEY"],
            )
        if d1_client is None:
            d1_client = D1Client(
                binding_name=secrets["SMD_D1_OBSERVATIONS_BINDING"],
                customer_slug=secrets["SMD_CUSTOMER_SLUG"],
            )

    rows = d1_client.query(_SELECT_ARCHIVED_SQL, observation_id)
    if not rows:
        raise ValueError(f"archive row not found: observation_id={observation_id}")
    row = rows[0]

    body_text = _row_get(row, "observation_body", 5)
    try:
        body = json.loads(body_text) if body_text else {}
    except (TypeError, ValueError):
        body = {"raw": body_text}

    source_ids_text = _row_get(row, "source_message_ids", 6)
    try:
        source_ids = json.loads(source_ids_text) if source_ids_text else []
    except (TypeError, ValueError):
        source_ids = []

    payload = {
        "session_id": _row_get(row, "session_id", 2),
        "persona_slug": _row_get(row, "persona_slug", 3),
        "observation_type": _row_get(row, "observation_type", 4),
        "body": body,
        "source_message_ids": source_ids,
        "confidence": _row_get(row, "confidence", 7),
        "created_at": _row_get(row, "honcho_created_at", 9),
    }

    created = honcho_client.create_conclusion(payload)
    new_id = created.get("id") if isinstance(created, dict) else None
    if not new_id:
        raise HonchoUnreachable("honcho POST /conclusions returned no id; cannot restore")

    # Build the live row with the new Honcho id but a fresh mirrored_at.
    d1_client.execute(
        _INSERT_LIVE_SQL,
        _row_get(row, "observation_id", 0),
        str(new_id),
        _row_get(row, "session_id", 2),
        _row_get(row, "persona_slug", 3),
        _row_get(row, "observation_type", 4),
        body_text,
        source_ids_text,
        _row_get(row, "confidence", 7),
        _row_get(row, "evidence_status", 8),
        _row_get(row, "honcho_created_at", 9),
        restored_at,
        _row_get(row, "schema_version", 11) or 1,
    )
    d1_client.execute(_DELETE_ARCHIVE_SQL, observation_id)

    logger.info(
        "archive: restored observation=%s new_honcho_id=%s",
        observation_id,
        new_id,
    )
    return RestoreResult(
        observation_id=observation_id,
        honcho_conclusion_id_new=str(new_id),
        restored_at=restored_at,
    )


# ---------------------------------------------------------------------------
# Row accessor
# ---------------------------------------------------------------------------


def _row_get(row: object, key: str, idx: int) -> object:
    """Read a column from a D1 row supporting both dict and sequence shapes.

    The D1 HTTP executor returns dicts; some test executors return
    tuples. Supporting both lets the archive sweep work without coupling
    to one specific D1Client implementation.
    """
    if isinstance(row, dict):
        return row.get(key)
    if isinstance(row, (list, tuple)):
        if 0 <= idx < len(row):
            return row[idx]
    return None


__all__ = [
    "ArchiveResult",
    "DEFAULT_ARCHIVE_AFTER_DAYS",
    "RestoreResult",
    "archive_aged_conclusions",
    "restore_from_archive",
]
