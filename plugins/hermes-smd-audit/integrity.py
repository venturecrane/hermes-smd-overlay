"""Periodic integrity check: Logpush mirror == D1 audit_log contents.

Ported from ss-console/operator/adapter/audit_log_integrity.py.

This module compares the audit_log rows present in D1 against the rows
mirrored to the immutable Logpush archive (R2 with Object Lock). Any
drift surfaces as an ``IntegrityFinding`` inside the returned
``IntegrityReport``.

Three drift classes are detected:

  1. ``IN_D1_NOT_IN_MIRROR`` — a D1 row has no matching mirror entry. The
     most common benign cause is mirror lag (the Logpush stream batches);
     the integrity check skips rows newer than ``_MIRROR_LAG_GRACE_SECONDS``
     to avoid false positives.

  2. ``IN_MIRROR_NOT_IN_D1`` — a mirror row has no matching D1 row. The
     load-bearing case for immutability: either a substrate-level
     violation (D1 was deleted) or a Captain-cleared legal-hold redaction.

  3. ``DIGEST_MISMATCH`` — both stores carry the same id, but a
     load-bearing column differs. The ``metadata`` column is the ONLY
     exclusion, because nested JSON ordering may legitimately vary; the
     audit writer canonicalizes its own metadata, but future non-writer
     paths may not. The ``prev_hash`` / ``row_hash`` chain columns ARE
     compared (ss-console #2312 — they had been silently excluded, which
     hid the single drift class a tamper-evidence chain exists to detect),
     and ``ts`` is normalized before comparison so a rendering difference
     between the stores cannot mark every row in the window as drifted.

The check is read-only and side-effect-free. Callers (a Cloudflare Cron
Trigger Worker or the compliance-evidence-packet generator) decide what
to do with the report.

Note on the ``async`` -> sync port
----------------------------------

The original module used ``AsyncIterator`` loaders. The hermes-smd-overlay
audit plugin runs inside synchronous Hermes hook callbacks, so the
loaders here use the synchronous ``Iterator`` protocol. The Cron Trigger
Worker that drives the check is also synchronous in the Machine context.
"""

import enum
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)


# How many seconds we tolerate between a D1 write and the mirror seeing it.
# Logpush latency is typically under a minute; the grace window picks a
# conservative 5 minutes so the periodic check (recommended hourly cadence)
# does not page on benign batching.
_MIRROR_LAG_GRACE_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class FindingKind(str, enum.Enum):
    IN_D1_NOT_IN_MIRROR = "in_d1_not_in_mirror"
    IN_MIRROR_NOT_IN_D1 = "in_mirror_not_in_d1"
    DIGEST_MISMATCH = "digest_mismatch"


@dataclass(frozen=True)
class IntegrityFinding:
    """A single drift between D1 and the Logpush mirror.

    Findings carry the row id and the kind of drift, not the row payload.
    Reconstruction goes through the loader. This keeps the report small
    for the dashboard surface and avoids leaking digest or metadata
    content into log streams.
    """

    kind: FindingKind
    row_id: str
    detail: str | None = None


@dataclass
class IntegrityReport:
    """Result of one integrity-check pass.

    ``clean`` is True if and only if ``findings`` is empty AND the loaders
    did not raise. A loader exception is surfaced via the ``loader_error``
    field and forces ``clean=False`` even if no finding was produced.
    """

    d1_rows_checked: int = 0
    mirror_rows_checked: int = 0
    findings: list[IntegrityFinding] = field(default_factory=list)
    loader_error: str | None = None

    @property
    def clean(self) -> bool:
        return not self.findings and self.loader_error is None


@dataclass(frozen=True)
class AuditRow:
    """Row shape exchanged between the loaders and the comparator.

    Matches the audit_log column set 1:1 — including the ``prev_hash`` /
    ``row_hash`` chain columns (``shared/audit_contract.py``
    ``CHAIN_COLUMN_ALTERS``) — so the comparator can do a tuple-comparison on
    the load-bearing fields without per-source translation. The metadata column
    is excluded from comparison but is carried so the dashboard can surface the
    offending row if needed.

    The chain columns default to ``None`` for pre-chain ledgers, whose rows
    genuinely have no hashes. A loader that cannot read them must yield ``None``
    from BOTH stores; supplying them from one side only reads as drift, which is
    the correct answer — one store carrying a chain the other does not is
    exactly what this check exists to catch.
    """

    id: str
    ts: str
    action_type: str
    actor: str
    actor_role: str | None
    skill_name: str | None
    matter_ref: str | None
    input_digest: str | None
    output_digest: str | None
    diff_digest: str | None
    trust_ceiling: str | None
    metadata: str | None
    prev_hash: str | None = None
    row_hash: str | None = None

    def compare_key(self) -> tuple:
        """Tuple of load-bearing columns — every column except ``metadata``.

        ``metadata`` is the single exclusion: nested JSON ordering may legitimately
        vary between stores. Everything else participates, and that deliberately
        includes ``prev_hash`` / ``row_hash``. They were absent from this shape
        until ss-console #2312, which made drift in the tamper-evidence chain —
        the one thing the chain exists to detect — invisible to the comparator.

        ``ts`` is NORMALIZED rather than compared raw. The writer emits a single
        rendering (``shared/ids.py`` ``iso_utc``: millisecond precision, ``Z``
        suffix), but a mirror that round-trips its rows through any other JSON
        datetime encoder can render the same instant as ``+00:00`` or drop
        trailing-zero milliseconds. Raw comparison turned that into a
        DIGEST_MISMATCH on EVERY row in the window, and a control that cries wolf
        gets muted. Normalization cannot hide real drift: two different instants
        still differ afterwards, and an unparseable timestamp falls back to its
        exact string.
        """
        return (
            self.id,
            _normalize_ts(self.ts),
            self.action_type,
            self.actor,
            self.actor_role,
            self.skill_name,
            self.matter_ref,
            self.input_digest,
            self.output_digest,
            self.diff_digest,
            self.trust_ceiling,
            self.prev_hash,
            self.row_hash,
        )


# ---------------------------------------------------------------------------
# Loader protocols
#
# Two iterators yielding ``AuditRow`` in id (ULID) ascending order. The D1
# loader queries the per-customer database; the mirror loader reads R2
# objects under the per-customer archive prefix.
# ---------------------------------------------------------------------------


class D1AuditLoader(Protocol):
    """Yield ``AuditRow`` rows from D1 within the [start_ts, end_ts] window."""

    def load(self, start_ts: str, end_ts: str) -> Iterator[AuditRow]: ...


class LogpushArchiveLoader(Protocol):
    """Yield ``AuditRow`` rows from the Logpush archive within the window."""

    def load(self, start_ts: str, end_ts: str) -> Iterator[AuditRow]: ...


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


def _drain(stream: Iterator[AuditRow]) -> dict[str, AuditRow]:
    """Read an iterator into an id-keyed dict."""
    out: dict[str, AuditRow] = {}
    for row in stream:
        out[row.id] = row
    return out


def _parse_iso(ts: str) -> datetime | None:
    """Parse an audit-log ``ts`` into an aware UTC datetime, or ``None``.

    The writer emits one rendering (``shared/ids.py`` ``iso_utc``: millisecond
    precision, ``Z`` suffix), but the two stores this module compares do not
    have to agree on it — a mirror that round-trips its rows through another
    JSON datetime encoder can emit ``+00:00``, or drop trailing-zero
    milliseconds. Parsing tolerantly is what lets the comparator treat those as
    the same instant instead of as drift.

    Returns ``None`` on parse failure — the integrity check is best-effort on
    timestamp comparison; a bad timestamp does not block the rest of the
    comparison.
    """
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # A naive timestamp is UTC by contract (the column is documented UTC).
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalize_ts(ts: str) -> str:
    """Canonical rendering of ``ts`` for comparison purposes.

    Falls back to the exact input string when it cannot be parsed, so an
    unreadable timestamp is never made to compare equal to a different
    unreadable one.
    """
    parsed = _parse_iso(ts)
    return ts if parsed is None else parsed.isoformat()


def _within_lag_grace(row_ts: str, now: datetime) -> bool:
    """True if the row's ts is recent enough that mirror lag is plausible."""
    parsed = _parse_iso(row_ts)
    if parsed is None:
        # Unparseable — treat as old (don't grant the grace)
        return False
    return parsed > now - timedelta(seconds=_MIRROR_LAG_GRACE_SECONDS)


def check_audit_integrity(
    d1_loader: D1AuditLoader,
    logpush_archive_loader: LogpushArchiveLoader,
    *,
    start_ts: str,
    end_ts: str,
    now: Callable[[], datetime] | None = None,
) -> IntegrityReport:
    """Compare D1 audit_log against the Logpush mirror.

    Both loaders are scanned over the same ``[start_ts, end_ts]`` window
    (ISO 8601 UTC strings, matching the audit_log ``ts`` column shape).
    The comparator builds two id-keyed maps, then walks the union.

    Returns an ``IntegrityReport``. Caller decides what to do with the
    findings — alert, escalate, file a compliance ticket. This module
    does not write.
    """
    report = IntegrityReport()
    now_dt = (now or (lambda: datetime.now(UTC)))()

    try:
        d1_rows = _drain(d1_loader.load(start_ts, end_ts))
        mirror_rows = _drain(logpush_archive_loader.load(start_ts, end_ts))
    except Exception as exc:  # noqa: BLE001 — loader failures must surface
        logger.error("integrity-check loader failure: %s", exc)
        report.loader_error = f"{type(exc).__name__}: {exc}"
        return report

    report.d1_rows_checked = len(d1_rows)
    report.mirror_rows_checked = len(mirror_rows)

    only_in_d1 = set(d1_rows) - set(mirror_rows)
    only_in_mirror = set(mirror_rows) - set(d1_rows)
    in_both = set(d1_rows) & set(mirror_rows)

    for row_id in sorted(only_in_d1):
        row = d1_rows[row_id]
        # Apply the lag grace — recent rows may not have hit the mirror yet.
        if _within_lag_grace(row.ts, now_dt):
            continue
        report.findings.append(
            IntegrityFinding(
                kind=FindingKind.IN_D1_NOT_IN_MIRROR,
                row_id=row_id,
                detail=(
                    f"row in D1 (ts={row.ts}) has no mirror entry beyond "
                    f"{_MIRROR_LAG_GRACE_SECONDS}s grace"
                ),
            )
        )

    for row_id in sorted(only_in_mirror):
        # Any mirror row without a D1 row is a finding — either an
        # immutability violation or a Captain-cleared legal-hold
        # redaction the operator reconciles against the exceptions ledger.
        report.findings.append(
            IntegrityFinding(
                kind=FindingKind.IN_MIRROR_NOT_IN_D1,
                row_id=row_id,
                detail="row present in Logpush mirror but missing from D1",
            )
        )

    for row_id in sorted(in_both):
        d1_row = d1_rows[row_id]
        mirror_row = mirror_rows[row_id]
        if d1_row.compare_key() != mirror_row.compare_key():
            report.findings.append(
                IntegrityFinding(
                    kind=FindingKind.DIGEST_MISMATCH,
                    row_id=row_id,
                    detail="load-bearing column drift between D1 and Logpush mirror",
                )
            )

    return report


__all__ = [
    "AuditRow",
    "D1AuditLoader",
    "FindingKind",
    "IntegrityFinding",
    "IntegrityReport",
    "LogpushArchiveLoader",
    "check_audit_integrity",
]
