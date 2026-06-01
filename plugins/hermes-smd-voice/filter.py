"""Partner-authored filter for voice samples.

Ported from ss-console/operator/adapter/voice/filter.py.

The voice library is the partner's voice, not the agent's. Samples that
the AI Employee drafted (and the partner sent unedited or lightly
edited) would feed the agent back its own output through Layer 2.

The filter has three signals, applied in this order:

1. ``SentItem.likely_agent_drafted`` — when the adapter populates this
   field (MS Graph and Gmail can tag drafts created by the agent's
   AgentMail identity), the filter trusts it. ``True`` means exclude.

2. Audit log cross-check — when the adapter does not populate the field
   (mobile clients, IMAP/SMTP), the filter consults the audit log for a
   ``DRAFT_CREATED`` row whose ``input_payload`` digest matches the
   sent message body. The lookup is by digest, not body — bodies are
   never persisted to the audit log either.

3. Body-shape heuristic — a final pass that excludes messages whose
   shape (length, structure, signature markers) matches the agent's
   draft templates. This is a soft signal and the only one that can
   produce false positives; rejecting a true partner-authored message
   has lower cost than admitting a stowaway AI-drafted message, so the
   filter biases toward exclusion when shape signals fire.

The filter does NOT inspect the recipient address or the matter to
decide partner-authoredness. Those are the cohort tag's job, and any
content-based filtering would be a privacy regression.

Output is a :class:`FilterResult` carrying the boolean decision and a
short reason string (max 200 chars) used by the dashboard's
"why was this excluded" drill-down.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("aie.voice.filter")


# Reason codes (closed set) — keep the string short so the D1 column
# stays compact and the dashboard can map them onto friendly labels.
REASON_ADAPTER_AGENT_DRAFTED = "adapter_agent_drafted"
REASON_AUDIT_LOG_DIGEST_MATCH = "audit_log_digest_match"
REASON_SHAPE_HEURISTIC = "shape_heuristic"
REASON_EMPTY_BODY = "empty_body"
REASON_TOO_SHORT = "too_short"

ACCEPT_REASON = "partner_authored"

# Body shorter than this is skipped even before the AI filter — there is
# not enough signal for the structural-diff to learn from.
MIN_WORD_COUNT_FOR_SAMPLE = 15


@dataclass(frozen=True)
class FilterResult:
    """Decision for one candidate sent message.

    Args:
        accept: True when the message should land in the voice library.
            False when it is excluded (AI-drafted, too short, empty).
        reason: One of the REASON_* constants. Used by the dashboard
            drill-down and by tests.
    """

    accept: bool
    reason: str


class AuditDigestLookup(Protocol):
    """Read-only protocol the filter uses to consult the audit log.

    Production wires this to a small D1 SELECT (``WHERE action_type =
    'DRAFT_CREATED' AND input_digest = ?``); tests pass an in-memory
    fake.
    """

    async def has_draft_with_digest(self, digest: str) -> bool: ...


@dataclass(frozen=True)
class CandidateMessage:
    """Vendor-neutral input to the filter.

    Built by the pipeline from the ``SentItem`` returned by the Email
    capability adapter. The pipeline computes ``body_digest`` once and
    passes it to both the filter (for audit-log lookup) and the
    deduplication path (so we do not re-extract the same sample).
    """

    body_text: str
    word_count: int
    likely_agent_drafted: bool | None  # None when the adapter cannot tell
    body_digest: str  # SHA-256 hex of body_text


def compute_body_digest(body_text: str | None) -> str:
    """SHA-256 hex of the body as UTF-8 bytes. Empty string hashes to
    the empty-input SHA-256 — that case is short-circuited by the
    filter's empty-body check before any audit lookup happens."""
    if not body_text:
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(body_text.encode("utf-8")).hexdigest()


class PartnerAuthoredFilter:
    """Three-pass filter for partner-authored voice samples.

    Construction takes the audit-log lookup; that lookup is the only
    side-effecting collaborator. The filter has no other state.
    """

    def __init__(self, audit_lookup: AuditDigestLookup) -> None:
        self._audit_lookup = audit_lookup

    async def evaluate(self, candidate: CandidateMessage) -> FilterResult:
        """Run the three-pass evaluation. Returns a :class:`FilterResult`."""
        if not candidate.body_text or not candidate.body_text.strip():
            return FilterResult(accept=False, reason=REASON_EMPTY_BODY)

        if candidate.word_count < MIN_WORD_COUNT_FOR_SAMPLE:
            return FilterResult(accept=False, reason=REASON_TOO_SHORT)

        # Pass 1: adapter-reported provenance.
        if candidate.likely_agent_drafted is True:
            return FilterResult(accept=False, reason=REASON_ADAPTER_AGENT_DRAFTED)

        # Pass 2: audit-log digest match. Only consulted when the
        # adapter could not determine provenance (None) or said False
        # but we want a second check.
        try:
            digest_match = await self._audit_lookup.has_draft_with_digest(candidate.body_digest)
        except Exception as e:  # noqa: BLE001 — defensive against D1 hiccups
            log.warning(
                "audit_log digest lookup failed for body digest %s: %s",
                candidate.body_digest[:12],
                e,
            )
            digest_match = False

        if digest_match:
            return FilterResult(accept=False, reason=REASON_AUDIT_LOG_DIGEST_MATCH)

        # Pass 3: shape heuristic. Cheap signals that an AI-drafted
        # message slipped past the first two passes. A single hit is
        # enough to exclude — the cost of a false-positive exclusion
        # is one missed sample; the cost of a false-positive inclusion
        # is the voice library learning the agent's voice from the
        # agent's own output.
        if _shape_looks_agent_drafted(candidate.body_text):
            return FilterResult(accept=False, reason=REASON_SHAPE_HEURISTIC)

        return FilterResult(accept=True, reason=ACCEPT_REASON)


# ---------------------------------------------------------------------------
# Shape heuristic
# ---------------------------------------------------------------------------


_AGENT_DRAFT_MARKERS = (
    # Signature blocks that AgentMail / persona templates emit verbatim.
    # The persona signature is platform property; when it appears in the
    # body, the message went out of the agent's drafts folder, not the
    # partner's.
    "[Drafted by your AI Employee for review]",
    "[Drafted for review]",
    "-- This draft was prepared by",
    # The platform's safety footer that the draft pipeline appends
    # automatically. Partner-edited sends keep it; clean sends drop it.
    # Either way its presence is a strong signal.
    "(this draft has not been sent — review before pressing send)",
)


def _shape_looks_agent_drafted(body_text: str) -> bool:
    """Return True when the body shape matches the agent's draft templates."""
    if not body_text:
        return False
    lowered = body_text.lower()
    for marker in _AGENT_DRAFT_MARKERS:
        if marker.lower() in lowered:
            return True
    return False


__all__ = [
    "ACCEPT_REASON",
    "AuditDigestLookup",
    "CandidateMessage",
    "FilterResult",
    "MIN_WORD_COUNT_FOR_SAMPLE",
    "PartnerAuthoredFilter",
    "REASON_ADAPTER_AGENT_DRAFTED",
    "REASON_AUDIT_LOG_DIGEST_MATCH",
    "REASON_EMPTY_BODY",
    "REASON_SHAPE_HEURISTIC",
    "REASON_TOO_SHORT",
    "compute_body_digest",
]
