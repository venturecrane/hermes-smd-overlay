"""Voice sample ingestion pipeline.

Ported from ss-console/ai-employee/adapter/voice/pipeline.py.

The pipeline runs in two modes — scheduled (daily cron) and on-demand
(synchronous call) — through a single entrypoint,
:meth:`VoiceIngestionRunner.run_ingestion`. Both modes share the same
write path:

  1. Read the Email capability's sent folder from the last cursor.
  2. For each :class:`SentMessage`, build a :class:`CandidateMessage`.
  3. Run the partner-authored filter. Excluded messages get a
     provenance row with ``partner_authored=0`` and no R2 object.
  4. Extract the structural-diff via
     :func:`extract_structural_diff`. The raw body is dropped after
     this call.
  5. Resolve the recipient cohort via
     :class:`CohortResolver`. Unassigned recipients tag the sample
     ``unassigned``.
  6. Write the structural-diff JSON to R2 at
     ``{slug}/voice/cohort/{cohort}/{ulid}.json``.
  7. Insert one provenance row in ``voice_ingestion_items``.

After the loop, upsert ``voice_source_state`` with the per-cohort
histogram. Failures during the loop are caught per-item; the run does
NOT abort on a single bad message — the run summary records the count
of items processed vs filtered vs errored, and ``ingest_status`` lands
on ``"error"`` only when zero successful items were ingested AND at
least one error fired.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol, Sequence

from .diff import (
    SCHEMA_VERSION as DIFF_SCHEMA_VERSION,
    extract_structural_diff,
    structural_diff_digest,
)
from .filter import (
    ACCEPT_REASON,
    AuditDigestLookup,
    CandidateMessage,
    FilterResult,
    PartnerAuthoredFilter,
    compute_body_digest,
)
from .state import (
    COHORT_UNASSIGNED,
    INGEST_STATUS_ERROR,
    INGEST_STATUS_OK,
    IngestionItemRecord,
    IngestionStateUpdate,
    VoiceSourceStateStore,
    _iso_utc,
)

log = logging.getLogger("aie.voice.pipeline")


# ---------------------------------------------------------------------------
# Domain types — vendor-neutral
# ---------------------------------------------------------------------------


class IngestionMode(str, enum.Enum):
    """Sibling to ``memory.IngestionMode``. Recorded in metadata so the
    dashboard renders which kind of run produced the last state row."""

    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class SentMessage:
    """Vendor-neutral sent-folder message.

    Mirrors the relevant subset of the Email capability's SentItem — the
    pipeline does not depend on the TypeScript type, only on this shape.
    """

    message_id: str
    sent_at: str
    body_text: Optional[str]
    subject: Optional[str]
    recipients: Sequence[str]              # email addresses; used by the cohort resolver
    likely_agent_drafted: Optional[bool]


@dataclass(frozen=True)
class IngestionResult:
    """One-run summary returned by :meth:`run_ingestion`."""

    source_kind: str
    source_id: str
    mode: IngestionMode
    items_seen: int
    items_ingested: int                    # partner_authored=1, R2 object written
    items_filtered: int                    # partner_authored=0, provenance row only
    items_skipped_duplicate: int           # already_ingested short-circuit
    items_errored: int
    cohort_histogram: dict                 # cohort_id -> count of ingested
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    next_cursor: Optional[str]
    error: Optional[str] = None


class StorageError(RuntimeError):
    """Raised when R2 write or delete fails. The caller logs and continues
    to the next item — one bad R2 write does not abort the entire run."""


# ---------------------------------------------------------------------------
# Capability-adapter wrappers (vendor-neutral)
# ---------------------------------------------------------------------------


class EmailSource(Protocol):
    """The pipeline's view of the Email capability.

    Implementations wrap the TypeScript ``Email`` capability surface
    (``list_sent_since`` + ``get_sent_item``). The pipeline does not
    care which adapter produces the rows — MS Graph, Gmail, IMAP — only
    that the contract is honored.
    """

    source_id: str

    async def list_sent_since(
        self, cursor: Optional[str]
    ) -> tuple[Sequence[SentMessage], Optional[str]]: ...


class NoEmailSource:
    """Fallback when no Email connector is bound to this customer.

    A scheduled run still completes — it upserts a zero-items state row
    so the dashboard can render "no email source bound" without erroring.
    """

    source_id = "none"

    async def list_sent_since(
        self, cursor: Optional[str]
    ) -> tuple[Sequence[SentMessage], Optional[str]]:
        return [], cursor


class CohortResolver(Protocol):
    """Reads the customer's memory rules to resolve a recipient -> cohort.

    Production implementation reads ``memory_rules`` rows where
    ``rule_type='voice'`` and ``category='recipient_cohort'`` and
    matches by email domain or full address. Tests pass in-memory fakes.
    """

    async def resolve(self, recipient_email: str) -> Optional[str]: ...


class StaticCohortResolver:
    """Trivial resolver used by ``NoEmailSource`` flows and tests.

    Returns ``None`` for every recipient — i.e. tags every sample
    ``unassigned`` per state.COHORT_UNASSIGNED.
    """

    async def resolve(self, recipient_email: str) -> Optional[str]:  # noqa: ARG002
        return None


class R2Client(Protocol):
    """Per-customer R2 binding. The pipeline writes structural-diff
    objects and (on retention / decommission) deletes them."""

    customer_slug: str

    async def put(self, key: str, body: bytes, content_type: str) -> None: ...
    async def delete(self, key: str) -> None: ...


class CursorStore(Protocol):
    """Persisted cursor for the sent-folder watcher.

    In production this is a small D1 row on ``sent_folder_state``. The
    pipeline does not own the schema; it asks for the cursor and stores
    the new one.
    """

    async def get(self) -> Optional[str]: ...
    async def set(self, cursor: Optional[str]) -> None: ...


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class VoiceIngestionRunner:
    """Orchestrates one ingestion run.

    Construction binds the runner to one customer's full storage stack.
    A single instance per Machine is sufficient; the runner has no
    process-wide state.
    """

    source: EmailSource
    cohort_resolver: CohortResolver
    r2_client: R2Client
    state_store: VoiceSourceStateStore
    cursor_store: CursorStore
    audit_lookup: AuditDigestLookup
    source_kind: str = "email"
    _clock: Optional[Callable[[], datetime]] = None

    def _now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)

    async def run_ingestion(self, *, mode: IngestionMode) -> IngestionResult:
        """Run one ingestion pass.

        Returns a :class:`IngestionResult` regardless of outcome. The
        caller logs the result; the row written to ``voice_source_state``
        is the durable record.
        """
        started_at_dt = self._now()
        started_at = _iso_utc(started_at_dt)
        t0 = time.perf_counter()

        filter_pass = PartnerAuthoredFilter(self.audit_lookup)
        cursor = await self.cursor_store.get()
        items_seen = 0
        items_ingested = 0
        items_filtered = 0
        items_skipped_duplicate = 0
        items_errored = 0
        cohort_histogram: dict = {}
        next_cursor = cursor
        run_error: Optional[str] = None

        try:
            messages, next_cursor = await self.source.list_sent_since(cursor)
        except Exception as e:  # noqa: BLE001
            run_error = f"list_sent_since failed: {type(e).__name__}: {e}"[:500]
            log.error(run_error)
            messages = []
            next_cursor = cursor

        for message in messages:
            items_seen += 1
            try:
                outcome = await self._ingest_one(message, filter_pass)
            except Exception as e:  # noqa: BLE001 — one bad row never aborts the run
                items_errored += 1
                log.exception(
                    "voice ingestion errored on message digest=%s: %s",
                    compute_body_digest(message.body_text)[:12],
                    e,
                )
                continue

            if outcome == "ingested":
                items_ingested += 1
                cohort = self._latest_cohort
                cohort_histogram[cohort] = cohort_histogram.get(cohort, 0) + 1
            elif outcome == "duplicate":
                items_skipped_duplicate += 1
            else:  # filtered
                items_filtered += 1

        if run_error is None:
            try:
                await self.cursor_store.set(next_cursor)
            except Exception as e:  # noqa: BLE001
                # Cursor write failure is recoverable on the next run, but it
                # would mean the same window gets re-scanned; flag it on the
                # state row so the dashboard surfaces the problem.
                run_error = f"cursor_store.set failed: {type(e).__name__}: {e}"[:500]
                log.error(run_error)

        finished_at_dt = self._now()
        duration_ms = int((time.perf_counter() - t0) * 1000)

        status = self._compute_status(
            items_ingested=items_ingested,
            items_errored=items_errored,
            run_error=run_error,
        )

        update = IngestionStateUpdate(
            source_kind=self.source_kind,
            source_id=self.source.source_id,
            ingested_at=_iso_utc(finished_at_dt),
            status=status,
            items_last_run=items_ingested,
            samples_by_cohort=cohort_histogram,
            error=run_error,
        )
        await self.state_store.upsert_state(update)

        return IngestionResult(
            source_kind=self.source_kind,
            source_id=self.source.source_id,
            mode=mode,
            items_seen=items_seen,
            items_ingested=items_ingested,
            items_filtered=items_filtered,
            items_skipped_duplicate=items_skipped_duplicate,
            items_errored=items_errored,
            cohort_histogram=cohort_histogram,
            status=status,
            started_at=started_at,
            finished_at=_iso_utc(finished_at_dt),
            duration_ms=duration_ms,
            next_cursor=next_cursor,
            error=run_error,
        )

    # ----- single-message ingest path -----

    _latest_cohort: str = COHORT_UNASSIGNED   # set inside _ingest_one for histogramming

    async def _ingest_one(
        self,
        message: SentMessage,
        filter_pass: PartnerAuthoredFilter,
    ) -> str:
        """Process one message. Returns 'ingested' | 'filtered' | 'duplicate'."""
        import hashlib

        source_message_digest = hashlib.sha256(
            message.message_id.encode("utf-8")
        ).hexdigest()

        # Deduplication: if we already ingested this message, skip without
        # re-writing anything. Lets scheduled re-runs be idempotent.
        if await self.state_store.already_ingested(
            self.source_kind, self.source.source_id, source_message_digest
        ):
            return "duplicate"

        body_text = message.body_text or ""
        body_digest = compute_body_digest(body_text)
        word_count = len(_word_split(body_text))

        candidate = CandidateMessage(
            body_text=body_text,
            word_count=word_count,
            likely_agent_drafted=message.likely_agent_drafted,
            body_digest=body_digest,
        )
        decision: FilterResult = await filter_pass.evaluate(candidate)

        if not decision.accept:
            # Record the exclusion so the dashboard can drill into "why
            # was this not learned from" without retaining the body.
            await self.state_store.insert_item(
                IngestionItemRecord(
                    source_kind=self.source_kind,
                    source_id=self.source.source_id,
                    source_message_digest=source_message_digest,
                    recipient_cohort_id=COHORT_UNASSIGNED,
                    partner_authored=False,
                    sent_at=message.sent_at,
                    filter_reason=decision.reason,
                    r2_key=None,
                    structural_diff_digest=None,
                    word_count=word_count,
                    schema_version=DIFF_SCHEMA_VERSION,
                )
            )
            return "filtered"

        # Resolve cohort from the customer's memory rules. The pipeline
        # consults the resolver for each recipient and picks the first
        # cohort-bearing match; when no recipient has a cohort, the
        # sample is tagged 'unassigned'.
        cohort_id = await self._resolve_cohort(message.recipients)
        self._latest_cohort = cohort_id

        diff = extract_structural_diff(
            body_text=body_text,
            subject=message.subject,
            recipient_cohort=cohort_id,
        )
        # The body lifetime ends here. Everything below operates on the
        # structural-diff only.
        body_text = ""  # noqa: F841 — explicit local-scope drop

        digest = structural_diff_digest(diff)

        # ULID generated inside the store so retention/decommission can
        # find the row by ID; the same ULID is the R2 sample-id segment.
        sample_id = await self.state_store.insert_item(
            IngestionItemRecord(
                source_kind=self.source_kind,
                source_id=self.source.source_id,
                source_message_digest=source_message_digest,
                recipient_cohort_id=cohort_id,
                partner_authored=True,
                sent_at=message.sent_at,
                filter_reason=ACCEPT_REASON,
                r2_key=None,                # filled in after the R2 put
                structural_diff_digest=digest,
                word_count=diff.word_count,
                schema_version=DIFF_SCHEMA_VERSION,
            )
        )

        r2_key = (
            f"{self.r2_client.customer_slug}/voice/cohort/{cohort_id}/{sample_id}.json"
        )
        try:
            await self.r2_client.put(
                r2_key, diff.to_json_bytes(), "application/json"
            )
        except Exception as e:  # noqa: BLE001
            # The provenance row exists but the R2 object failed. Mark
            # the row soft-deleted so the next run can re-ingest, and
            # raise StorageError so the per-item counter increments.
            await self.state_store.mark_item_deleted(sample_id)
            raise StorageError(
                f"R2 put failed for {r2_key}: {type(e).__name__}: {e}"
            ) from e

        # We inserted with r2_key=NULL because the ULID is generated
        # inside the store. Patch the row in-place with the resolved
        # key; this is the only mutating UPDATE the pipeline performs.
        await self.state_store._write.execute(  # noqa: SLF001 — co-owned module
            "UPDATE voice_ingestion_items SET r2_key = ? WHERE id = ?",
            [r2_key, sample_id],
        )

        return "ingested"

    async def _resolve_cohort(self, recipients: Sequence[str]) -> str:
        for recipient in recipients:
            if not recipient:
                continue
            try:
                cohort = await self.cohort_resolver.resolve(recipient)
            except Exception as e:  # noqa: BLE001
                log.warning("cohort resolver failed for recipient: %s", e)
                cohort = None
            if cohort:
                return cohort
        return COHORT_UNASSIGNED

    @staticmethod
    def _compute_status(
        *, items_ingested: int, items_errored: int, run_error: Optional[str]
    ) -> str:
        if run_error and items_ingested == 0:
            return INGEST_STATUS_ERROR
        if items_errored > 0 and items_ingested == 0:
            return INGEST_STATUS_ERROR
        return INGEST_STATUS_OK


# ---------------------------------------------------------------------------
# Retention enforcer
# ---------------------------------------------------------------------------


async def enforce_retention(
    *,
    state_store: VoiceSourceStateStore,
    r2_client: R2Client,
    voice_retention_days: int,
    now: Optional[datetime] = None,
) -> dict:
    """Delete voice samples older than ``voice_retention_days``.

    Reads the retention window from the caller (sourced from
    customer.yaml). Walks ``voice_ingestion_items`` for active rows
    older than the cutoff, deletes the R2 object, and soft-deletes the
    provenance row.

    Returns a summary dict: ``{"considered": N, "deleted": M, "errors": E}``.
    """
    now_dt = now or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=voice_retention_days)
    cutoff_iso = _iso_utc(cutoff)

    rows = await state_store.list_items_for_retention(cutoff_iso)
    considered = len(rows)
    deleted = 0
    errors = 0

    for row in rows:
        r2_key = row.get("r2_key")
        item_id = row.get("id")
        if not r2_key or not item_id:
            continue
        try:
            await r2_client.delete(r2_key)
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.error("retention enforcer R2 delete failed for %s: %s", r2_key, e)
            continue
        try:
            await state_store.mark_item_deleted(item_id)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            errors += 1
            log.error("retention enforcer state update failed for %s: %s", item_id, e)

    return {"considered": considered, "deleted": deleted, "errors": errors}


# ---------------------------------------------------------------------------
# Decommission hook
# ---------------------------------------------------------------------------


async def decommission_source(
    *,
    state_store: VoiceSourceStateStore,
    r2_client: R2Client,
    source_kind: str,
    source_id: str,
) -> dict:
    """Remove every voice artifact the pipeline persisted for one source.

    Called by ``bin/decommission-customer.sh`` and by
    ``bin/pause-customer.sh`` (the latter snapshots first; this function
    only removes the live artifacts).

    Steps:
      1. Enumerate active provenance rows for the source.
      2. For each row with an R2 key, delete the R2 object.
      3. Soft-delete every provenance row for the source.
      4. Remove the state row.

    Returns ``{"removed": N, "errors": E}``.
    """
    rows = await state_store.list_items_for_decommission(source_kind, source_id)
    removed = 0
    errors = 0
    for row in rows:
        r2_key = row.get("r2_key")
        if r2_key:
            try:
                await r2_client.delete(r2_key)
                removed += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.error("decommission R2 delete failed for %s: %s", r2_key, e)
    try:
        await state_store.mark_items_deleted_by_source(source_kind, source_id)
        await state_store.delete_state(source_kind, source_id)
    except Exception as e:  # noqa: BLE001
        errors += 1
        log.error(
            "decommission state cleanup failed for (%s, %s): %s",
            source_kind,
            source_id,
            e,
        )
    return {"removed": removed, "errors": errors}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


import re as _re  # local import to keep top-level imports tight

_WORD_RE = _re.compile(r"\b\w+\b")


def _word_split(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


__all__ = [
    "CohortResolver",
    "CursorStore",
    "EmailSource",
    "IngestionMode",
    "IngestionResult",
    "NoEmailSource",
    "R2Client",
    "SentMessage",
    "StaticCohortResolver",
    "StorageError",
    "VoiceIngestionRunner",
    "decommission_source",
    "enforce_retention",
]
