"""Observation state machine — closed vocabulary + dataclasses.

This module owns the in-Python shape of a persona observation, the
closed enums it carries, and the helper that converts a Honcho
conclusion payload into an :class:`ObservationRecord` ready for D1
insertion.

Ported from ss-console/ai-employee/adapter/memory/state.py. The original
``IngestedItemRecord`` (centered on R2 keys and vector chunk IDs) is
replaced with :class:`ObservationRecord` (centered on Honcho conclusion
ids and source-message provenance) because the architectural posture
has shifted from "customer-owned memory artifact" (ADR 0008, superseded)
to "Honcho mirror" (ADR 0016). The state-machine shape — dataclass with
``__post_init__`` validation against a closed enum — is preserved.

Three life-cycle states:

* ``active``    — row is in ``persona_observations``; Honcho still has it.
* ``dismissed`` — row is in ``persona_observations`` with non-null
  ``dismissed_at``; Honcho row has been physically deleted (per ADR 0016).
* ``archived``  — row is in ``persona_observations_archive``; Honcho row
  has been physically deleted by the TTL pass.

State transitions are one-way; the only path from ``archived`` back to
``active`` is the restore path in :mod:`archive`, which re-inserts into
Honcho and removes from the archive table.
"""

from __future__ import annotations

import enum
import json
import secrets
import time
from dataclasses import dataclass, field

from .schemas import VALID_EVIDENCE_STATUSES

# ---------------------------------------------------------------------------
# Observation type — closed vocabulary (matches honcho_interceptor.py)
#
# Adding a new type requires updating this enum and the admin portal so
# Captain has a defined surface for the new kind.
# ---------------------------------------------------------------------------


class ObservationType(str, enum.Enum):
    VOICE_DRIFT = "voice_drift"
    RECURRING_CORRECTION = "recurring_correction"
    PREFERENCE_SIGNAL = "preference_signal"
    OTHER = "other"


VALID_OBSERVATION_TYPES = frozenset(t.value for t in ObservationType)


# ---------------------------------------------------------------------------
# Lifecycle state — derived, not stored
#
# The state is derived from the table the row is in plus the dismissed_at
# column. There is no stored state column; the schema enforces invariants
# (a row cannot be in both tables; dismissed_at is null on un-dismissed
# rows). The enum exists so callers can name states unambiguously.
# ---------------------------------------------------------------------------


class ObservationState(str, enum.Enum):
    ACTIVE = "active"
    DISMISSED = "dismissed"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# ULID helper — vendored, identical to ss-console/ai-employee/adapter/memory
# /state.py and honcho_interceptor.py. The three sites use the same Crockford
# alphabet and produce sortable strings of identical length.
# ---------------------------------------------------------------------------


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def ulid(now_ms: int | None = None) -> str:
    """Return a 26-char ULID. Sortable by creation time."""
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ObservationRecord:
    """One row destined for ``persona_observations``.

    The mirror writer (in :mod:`mirror`) builds an
    :class:`ObservationRecord` per new Honcho conclusion and inserts it
    into D1. The archive writer (in :mod:`archive`) shares the same shape
    and copies the row to ``persona_observations_archive`` with an added
    ``archived_at`` stamp.

    Required fields are validated in ``__post_init__``:

    * ``honcho_conclusion_id`` non-empty.
    * ``observation_type`` is a member of :class:`ObservationType`.
    * ``source_message_ids`` non-empty (ADR 0016 §5 — every mirrored
      conclusion must point to the messages that grounded it).
    * ``evidence_status`` is a member of
      :data:`schemas.VALID_EVIDENCE_STATUSES`.

    Confidence is optional and not range-checked here — Honcho's own
    confidence may legitimately fall outside ``[0.0, 1.0]`` if upstream
    changes its scale; surfacing the value verbatim is preferred over
    silently clamping.
    """

    honcho_conclusion_id: str
    session_id: str
    observation_type: ObservationType
    observation_body: dict
    source_message_ids: list[str]
    honcho_created_at: str
    mirrored_at: str
    evidence_status: str
    persona_slug: str | None = None
    confidence: float | None = None
    observation_id: str = field(default_factory=ulid)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.honcho_conclusion_id:
            raise ValueError("honcho_conclusion_id must be a non-empty string")
        if not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        if isinstance(self.observation_type, str):
            # Coerce raw strings to enum members for callers that
            # construct from JSON (e.g. when reading back from D1).
            try:
                self.observation_type = ObservationType(self.observation_type)
            except ValueError as exc:
                raise ValueError(
                    f"observation_type {self.observation_type!r} not in "
                    f"{sorted(VALID_OBSERVATION_TYPES)}"
                ) from exc
        if not isinstance(self.observation_type, ObservationType):
            raise ValueError(
                f"observation_type must be an ObservationType (got {type(self.observation_type).__name__})"
            )
        if not isinstance(self.observation_body, dict):
            raise ValueError("observation_body must be a dict")
        if not self.source_message_ids:
            raise ValueError(
                "source_message_ids must be non-empty (ADR 0016 §5: every "
                "mirrored conclusion must point to the messages that grounded it)"
            )
        if self.evidence_status not in VALID_EVIDENCE_STATUSES:
            raise ValueError(
                f"evidence_status {self.evidence_status!r} not in "
                f"{sorted(VALID_EVIDENCE_STATUSES)}"
            )

    # -- Serialization helpers ------------------------------------------

    def body_json(self) -> str:
        """Stable JSON serialization of ``observation_body`` for D1 storage."""
        return json.dumps(self.observation_body, sort_keys=True, separators=(",", ":"))

    def source_message_ids_json(self) -> str:
        """Stable JSON serialization of ``source_message_ids`` for D1 storage."""
        return json.dumps(list(self.source_message_ids), separators=(",", ":"))


__all__ = [
    "ObservationRecord",
    "ObservationState",
    "ObservationType",
    "VALID_OBSERVATION_TYPES",
    "ulid",
]
