"""Captain dismissal entry point for active Honcho conclusions.

Ported from ss-console/ai-employee/adapter/honcho_interceptor.py
``HonchoInterceptor.dismiss``. The original interceptor required Captain
to provide a customer.yaml-style PR-anchored promotion path; under
ADR 0016 (mirror, don't gate) we no longer require that ceremony for
dismissal — dismissing a conclusion is itself a low-stakes operation
because Honcho is the live store and the dismissal physically removes
the row.

Workflow:

1. Captain clicks "Dismiss" on a row in the admin portal.
2. The portal calls :func:`dismiss_conclusion` with the
   ``observation_id`` (D1's stable id) plus a free-form ``reason``
   string and the principal username.
3. The function:
   a. Looks up the row in ``persona_observations`` to find the
      ``honcho_conclusion_id``.
   b. Physically deletes the row from Honcho via
      ``DELETE /conclusions/{id}`` — works around upstream bug #658
      (corrections don't propagate through the reasoning tree).
   c. Updates the D1 row with ``dismissed_at`` / ``dismissed_by`` /
      ``dismissed_reason`` so the dismissal corpus signal is retained
      (per ADR 0016 §4 — dismissed rows stay in the table for tuning
      Honcho's extraction signal over time).

The row is NOT moved to the archive table on dismissal — archival is a
TTL operation, dismissal is an operator signal. The two stay distinct
so the dismissed-row corpus remains queryable in the live table.

Reason is required. Silent dismissal hides systematic over-firing of
Honcho's extraction signal (same constraint as the original interceptor).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from shared.d1_client import D1Client
from shared.secrets import require

from .honcho_client import HonchoClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


_SELECT_FOR_DISMISS_SQL = (
    "SELECT honcho_conclusion_id, dismissed_at FROM persona_observations WHERE observation_id = ?"
)


_STAMP_DISMISSAL_SQL = (
    "UPDATE persona_observations "
    "SET dismissed_at = ?, dismissed_by = ?, dismissed_reason = ? "
    "WHERE observation_id = ? AND dismissed_at IS NULL"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ObservationNotFound(LookupError):
    """Raised when the dismissal target row does not exist in D1."""


class AlreadyDismissed(RuntimeError):
    """Raised when the target observation is already dismissed.

    The admin portal surfaces this to Captain as "this row was already
    dismissed at <stamp>"; no retry is offered because dismissal is
    one-way.
    """


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DismissResult:
    """Outcome of one dismissal."""

    observation_id: str
    honcho_conclusion_id: str
    honcho_row_existed: bool
    dismissed_at: str
    dismissed_by: str


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _iso_utc(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def dismiss_conclusion(
    observation_id: str,
    *,
    reason: str,
    dismissed_by: str,
    honcho_client: HonchoClient | None = None,
    d1_client: D1Client | None = None,
    now: datetime | None = None,
) -> DismissResult:
    """Physical-delete the conclusion in Honcho; stamp the D1 mirror row.

    Args:
        observation_id: D1 ``persona_observations.observation_id`` —
            the stable id Captain sees in the admin portal (NOT the
            Honcho conclusion id, which changes on restore).
        reason: free-form rationale. Required; empty string raises.
            Surfaces in the dismissal corpus for tuning extraction.
        dismissed_by: principal username from the admin session.
        honcho_client: optional pre-constructed; built from env when omitted.
        d1_client: optional pre-constructed; built from env when omitted.
        now: optional clock override for tests.

    Returns:
        :class:`DismissResult` recording what happened. The
        ``honcho_row_existed`` flag is ``False`` if Honcho already
        lost the row (404 on DELETE); we still stamp the D1 mirror
        because the dismissal is durable from the operator's
        perspective.

    Raises:
        ValueError: ``observation_id`` / ``reason`` / ``dismissed_by``
            empty.
        ObservationNotFound: no D1 row matches ``observation_id``.
        AlreadyDismissed: target row's ``dismissed_at`` is non-null.
        HonchoUnreachable: non-404 sidecar error. Caller decides
            whether to retry; the D1 mirror is NOT stamped in this case
            so a retry produces the same idempotent outcome.
    """
    if not observation_id:
        raise ValueError("observation_id is required")
    if not reason:
        raise ValueError(
            "reason is required; silent dismissal hides Honcho over-firing "
            "(ADR 0016 §4 — dismissed observations stay in the table for "
            "tuning extraction signal over time)"
        )
    if not dismissed_by:
        raise ValueError("dismissed_by is required (admin-session principal)")

    dismissed_at = _iso_utc(now)

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

    rows = d1_client.query(_SELECT_FOR_DISMISS_SQL, observation_id)
    if not rows:
        raise ObservationNotFound(
            f"persona_observations row not found: observation_id={observation_id}"
        )
    row = rows[0]
    if isinstance(row, dict):
        honcho_conclusion_id = row.get("honcho_conclusion_id")
        existing_dismissed_at = row.get("dismissed_at")
    elif isinstance(row, (list, tuple)) and len(row) >= 2:
        honcho_conclusion_id = row[0]
        existing_dismissed_at = row[1]
    else:
        raise ObservationNotFound(
            f"persona_observations row had unexpected shape: {type(row).__name__}"
        )

    if not honcho_conclusion_id:
        raise ObservationNotFound(
            f"persona_observations row missing honcho_conclusion_id: "
            f"observation_id={observation_id}"
        )
    if existing_dismissed_at:
        raise AlreadyDismissed(
            f"observation_id={observation_id} already dismissed at {existing_dismissed_at}"
        )

    # 1. Physical delete in Honcho. 404 (row already gone) is non-fatal;
    # any other error halts the operation so the D1 stamp doesn't race
    # ahead of the durable Honcho state.
    honcho_row_existed = honcho_client.delete_conclusion(str(honcho_conclusion_id))

    # 2. Stamp the D1 mirror. The UPDATE WHERE dismissed_at IS NULL is
    # the race guard: two concurrent dismissal attempts both find a
    # null dismissed_at, but only one stamp lands.
    d1_client.execute(
        _STAMP_DISMISSAL_SQL,
        dismissed_at,
        dismissed_by,
        reason,
        observation_id,
    )

    logger.info(
        "dismiss: observation=%s honcho_id=%s honcho_existed=%s by=%s",
        observation_id,
        honcho_conclusion_id,
        honcho_row_existed,
        dismissed_by,
    )
    return DismissResult(
        observation_id=observation_id,
        honcho_conclusion_id=str(honcho_conclusion_id),
        honcho_row_existed=honcho_row_existed,
        dismissed_at=dismissed_at,
        dismissed_by=dismissed_by,
    )


__all__ = [
    "AlreadyDismissed",
    "DismissResult",
    "ObservationNotFound",
    "dismiss_conclusion",
]
