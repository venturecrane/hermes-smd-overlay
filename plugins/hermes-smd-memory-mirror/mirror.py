"""Honcho conclusion poller + D1 writer.

Ported from ss-console/ai-employee/adapter/memory/. The original
"customer-owned memory artifact" model (ADR 0008, superseded) is
replaced by the Honcho mirror pattern (ADR 0016).

The mirror runs on the ``on_session_end`` hook (per-turn cadence, matching
Honcho's ``writeFrequency: session`` which produces new conclusions at
the per-turn boundary). For each new Honcho conclusion produced during
the turn, the mirror writes one ``persona_observations`` row with full
provenance:

* ``honcho_conclusion_id`` — the Honcho row's id; carried as the durable
  cross-reference between Honcho and D1.
* ``source_message_ids`` — pulled from Honcho's reasoning tree per
  conclusion; the list of message identifiers that grounded the
  inference (ADR 0016 §5).
* ``confidence`` — Honcho-assigned score, carried through unchanged.
* ``evidence_status`` — computed at mirror time from the source-message
  list. Three values:
    - ``evidenced`` — source list is well-formed and non-trivial.
    - ``unevidenced`` — Honcho produced a conclusion with no source
      messages (should not happen in practice; flagged here so Captain
      sees the anomaly).
    - ``insufficient`` — source list exists but falls below the policy
      floor for the observation type (e.g. one message for a recurring
      correction). Surfaced for review; never used as an auto-gate.
* ``mirrored_at`` — wall-clock timestamp of the D1 write, distinct from
  Honcho's ``created_at``.

Honcho remains the live store. D1 is the parallel record Captain
operates on through the admin portal. The mirror NEVER reads from
Honcho into runtime persona state — only into D1.

Degradation mode: if the Honcho sidecar is unreachable, the mirror logs
a warning and returns (per AGENTS.md hard rule #3 — plugin callbacks
must be exception safe). The audit plugin emits a
``MEMORY_PROVIDER_DEGRADED`` row when this happens; cross-correlate via
session_id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from shared.d1_client import D1Client
from shared.secrets import require

from .honcho_client import HonchoClient
from .schemas import (
    EVIDENCE_STATUS_EVIDENCED,
    EVIDENCE_STATUS_INSUFFICIENT,
    EVIDENCE_STATUS_UNEVIDENCED,
)
from .state import ObservationRecord, ObservationType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence-status floor per observation type
#
# A recurring_correction backed by a single message is suspicious — the
# "recurring" qualifier implies more than one occurrence. The floor is a
# review signal, not an auto-gate; rows below the floor land in D1 with
# evidence_status='insufficient' so Captain sees them.
#
# Voice drift and preference signal can legitimately fire on a single
# message (a single explicit correction is a valid voice drift signal).
# ---------------------------------------------------------------------------


_EVIDENCE_FLOOR: dict[str, int] = {
    ObservationType.VOICE_DRIFT.value: 1,
    ObservationType.RECURRING_CORRECTION.value: 2,
    ObservationType.PREFERENCE_SIGNAL.value: 1,
    ObservationType.OTHER.value: 1,
}


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_INSERT_OBSERVATION_SQL = (
    "INSERT INTO persona_observations "
    "(observation_id, honcho_conclusion_id, session_id, persona_slug, "
    "observation_type, observation_body, source_message_ids, confidence, "
    "evidence_status, honcho_created_at, mirrored_at, schema_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


_SELECT_LAST_MIRRORED_SQL = (
    "SELECT honcho_created_at "
    "FROM persona_observations "
    "WHERE session_id = ? "
    "ORDER BY honcho_created_at DESC "
    "LIMIT 1"
)


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _iso_utc(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirrorResult:
    """Outcome of one mirror pass over a session.

    The on_session_end callback uses this only for logging; the data is
    already durably in D1 by the time the callback returns.
    """

    session_id: str
    conclusions_polled: int
    rows_written: int
    rows_skipped: int


# ---------------------------------------------------------------------------
# Evidence classification
# ---------------------------------------------------------------------------


def compute_evidence_status(
    *,
    observation_type: str,
    source_message_ids: list[str],
) -> str:
    """Classify a conclusion's source-evidence list into the closed vocabulary.

    See module docstring for the three buckets. The floor lookup falls
    back to 1 for unrecognized types so a forward-compatible observation
    type from a newer Honcho is never silently dropped — it lands with
    ``evidenced`` if it has any message at all.
    """
    if not source_message_ids:
        return EVIDENCE_STATUS_UNEVIDENCED
    floor = _EVIDENCE_FLOOR.get(observation_type, 1)
    if len(source_message_ids) < floor:
        return EVIDENCE_STATUS_INSUFFICIENT
    return EVIDENCE_STATUS_EVIDENCED


# ---------------------------------------------------------------------------
# Honcho conclusion → ObservationRecord
# ---------------------------------------------------------------------------


def _coerce_observation_type(raw: object) -> str:
    """Normalize a Honcho ``type`` field to a closed-vocabulary string.

    Honcho conclusion payloads use a free-form ``type`` (or sometimes
    ``observation_type``) field. We map known values to the closed enum
    and fall back to ``other`` for anything unrecognized — the row still
    lands in D1; Captain sees the original payload via
    ``observation_body``.
    """
    if not isinstance(raw, str):
        return ObservationType.OTHER.value
    if raw in {t.value for t in ObservationType}:
        return raw
    return ObservationType.OTHER.value


def _extract_source_messages(conclusion: dict) -> list[str]:
    """Pull the source-message id list from a Honcho conclusion payload.

    Honcho's reasoning-tree shape varies across versions; the common
    payload variants we accept are (in order of preference):
      1. Top-level ``source_message_ids: list[str]``.
      2. Top-level ``evidence: list[{"message_id": str, ...}]``.
      3. Nested ``reasoning.source_messages: list[str | dict]``.

    Returns a list of string ids; unsupported shapes return ``[]`` so
    the evidence classifier downgrades the row to ``unevidenced``.
    """
    direct = conclusion.get("source_message_ids")
    if isinstance(direct, list):
        return [str(x) for x in direct if x]

    evidence = conclusion.get("evidence")
    if isinstance(evidence, list):
        ids: list[str] = []
        for entry in evidence:
            if isinstance(entry, dict):
                mid = entry.get("message_id") or entry.get("id")
                if mid:
                    ids.append(str(mid))
            elif isinstance(entry, str):
                ids.append(entry)
        if ids:
            return ids

    reasoning = conclusion.get("reasoning")
    if isinstance(reasoning, dict):
        nested = reasoning.get("source_messages")
        if isinstance(nested, list):
            ids = []
            for entry in nested:
                if isinstance(entry, str):
                    ids.append(entry)
                elif isinstance(entry, dict):
                    mid = entry.get("id") or entry.get("message_id")
                    if mid:
                        ids.append(str(mid))
            return ids

    return []


def conclusion_to_record(
    conclusion: dict, *, session_id: str, mirrored_at: str
) -> ObservationRecord:
    """Translate a Honcho conclusion payload into an :class:`ObservationRecord`.

    Raises ``ValueError`` on shapes the mirror cannot make sense of
    (missing ``id``, no body). The caller (:func:`mirror_session`)
    catches and skips such rows so one malformed conclusion does not
    block the rest of the pass.
    """
    conclusion_id = conclusion.get("id")
    if not conclusion_id:
        raise ValueError("conclusion missing required 'id' field")

    raw_type = conclusion.get("observation_type") or conclusion.get("type")
    obs_type_value = _coerce_observation_type(raw_type)

    body = conclusion.get("body")
    if body is None:
        # Honcho versions that don't separate body from the top-level
        # payload — wrap the whole conclusion (minus id/created_at) as
        # the body so we never lose data.
        body = {k: v for k, v in conclusion.items() if k not in {"id", "created_at"}}
    if not isinstance(body, dict):
        body = {"raw": body}

    source_ids = _extract_source_messages(conclusion)

    honcho_created_at = conclusion.get("created_at") or mirrored_at
    if not isinstance(honcho_created_at, str):
        honcho_created_at = str(honcho_created_at)

    confidence = conclusion.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None

    persona_slug = conclusion.get("persona_slug")
    if persona_slug is not None and not isinstance(persona_slug, str):
        persona_slug = None

    evidence_status = compute_evidence_status(
        observation_type=obs_type_value,
        source_message_ids=source_ids,
    )

    # Source-message floor: ObservationRecord requires source_message_ids
    # non-empty. When Honcho returns truly empty evidence we still want
    # to record the conclusion (with evidence_status='unevidenced') so
    # Captain sees it. Stamp a synthetic marker so the schema CHECK
    # passes; the audit signal is in the evidence_status column.
    if not source_ids:
        source_ids = ["__none__"]

    return ObservationRecord(
        honcho_conclusion_id=str(conclusion_id),
        session_id=session_id,
        observation_type=obs_type_value,
        observation_body=body,
        source_message_ids=source_ids,
        honcho_created_at=honcho_created_at,
        mirrored_at=mirrored_at,
        evidence_status=evidence_status,
        persona_slug=persona_slug,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Main entry point — called by the on_session_end hook
# ---------------------------------------------------------------------------


def mirror_session(
    *,
    session_id: str,
    honcho_client: HonchoClient | None = None,
    d1_client: D1Client | None = None,
    now: datetime | None = None,
) -> MirrorResult:
    """Poll Honcho for new conclusions on this session and write them to D1.

    Args:
        session_id: Hermes session identifier (matches Honcho session id).
        honcho_client: optional pre-constructed client; if ``None`` one is
            built from environment via :func:`shared.secrets.require`.
        d1_client: optional pre-constructed client; if ``None`` one is
            built from environment via :func:`shared.secrets.require`.
        now: optional clock override for tests.

    Returns:
        :class:`MirrorResult` with counts. The data is already durable
        in D1 by the time this function returns.

    Raises:
        HonchoUnreachable: sidecar HTTP failure. The on_session_end
            callback catches this and degrades to a warning log.
        KeyError: a required env var is missing (will surface at the
            shared.secrets.require call). The callback catches.
    """
    if not session_id:
        # No session means nothing to mirror; return an empty result.
        return MirrorResult(session_id="", conclusions_polled=0, rows_written=0, rows_skipped=0)

    mirrored_at = _iso_utc(now)

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

    # High-water-mark: only fetch conclusions newer than the most recent
    # one we've already mirrored for this session. On the very first
    # call for a session this returns no rows and we fall back to a
    # full session fetch.
    since = _read_last_mirrored_at(d1_client, session_id)
    conclusions = honcho_client.list_conclusions(session_id=session_id, since=since)

    rows_written = 0
    rows_skipped = 0
    for conclusion in conclusions:
        try:
            record = conclusion_to_record(
                conclusion,
                session_id=session_id,
                mirrored_at=mirrored_at,
            )
        except ValueError as exc:
            rows_skipped += 1
            logger.warning(
                "memory-mirror: skipping malformed conclusion session=%s reason=%s",
                session_id,
                exc,
            )
            continue
        try:
            _write_record(d1_client, record)
            rows_written += 1
        except Exception as exc:  # noqa: BLE001 — per-row resilience
            rows_skipped += 1
            logger.warning(
                "memory-mirror: D1 write failed session=%s conclusion=%s err=%s",
                session_id,
                record.honcho_conclusion_id,
                exc,
            )

    logger.info(
        "memory-mirror: session=%s polled=%d written=%d skipped=%d",
        session_id,
        len(conclusions),
        rows_written,
        rows_skipped,
    )

    return MirrorResult(
        session_id=session_id,
        conclusions_polled=len(conclusions),
        rows_written=rows_written,
        rows_skipped=rows_skipped,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_last_mirrored_at(d1_client: D1Client, session_id: str) -> str | None:
    """Return the highest honcho_created_at already mirrored for this session.

    Used to bound the Honcho poll to only new rows. Returns ``None`` on
    the first pass (no prior rows) or on any read failure — a fresh full
    poll is correct in that case (idempotent insert by
    ``honcho_conclusion_id`` UNIQUE constraint deduplicates).
    """
    try:
        rows = d1_client.query(_SELECT_LAST_MIRRORED_SQL, session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "memory-mirror: high-water-mark read failed session=%s err=%s "
            "(falling back to full session fetch)",
            session_id,
            exc,
        )
        return None
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        value = first.get("honcho_created_at")
    elif isinstance(first, (list, tuple)) and first:
        value = first[0]
    else:
        value = None
    return value if isinstance(value, str) else None


def _write_record(d1_client: D1Client, record: ObservationRecord) -> None:
    """Insert one ObservationRecord into ``persona_observations``.

    The UNIQUE constraint on ``honcho_conclusion_id`` makes the insert
    idempotent — a repeated mirror over the same conclusion no-ops with
    a constraint violation (caller catches and reports as a skip).
    """
    d1_client.execute(
        _INSERT_OBSERVATION_SQL,
        record.observation_id,
        record.honcho_conclusion_id,
        record.session_id,
        record.persona_slug,
        record.observation_type.value,
        record.body_json(),
        record.source_message_ids_json(),
        record.confidence,
        record.evidence_status,
        record.honcho_created_at,
        record.mirrored_at,
        record.schema_version,
    )


__all__ = [
    "MirrorResult",
    "compute_evidence_status",
    "conclusion_to_record",
    "mirror_session",
]
