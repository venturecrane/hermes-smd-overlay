"""Inbound webhook signature + replay verification (issue #13).

The webhook router refuses to route an inbound webhook it cannot
cryptographically attribute to the configured provider. Three checks:

  1. **HMAC-SHA256 signature** over the raw request body, compared in
     constant time (``hmac.compare_digest``) against the per-customer
     signing secret. Defeats forged events from anyone who learns the
     dispatch URL.
  2. **Signed-timestamp freshness window.** Defeats replay of an old,
     once-valid request body.
  3. **Event-ID de-duplication.** Defeats replay of the *same* request
     within the cache TTL.

The exact header names and signing scheme vary by provider; the security
properties (HMAC + constant-time compare + freshness + dedupe) do not.
The signing input is ``"{timestamp}.{body}"`` when a timestamp is present
(Stripe-style), else the bare body. Confirm Filevine's concrete scheme
against its webhook docs before enabling that connector in production —
``compute_signature`` is the one place to adapt.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How far an inbound signed timestamp may drift from now before the
# request is rejected as stale. Five minutes is the common default.
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300

# How long a seen event ID is remembered for replay protection.
_REPLAY_TTL_SECONDS = 3600


class WebhookVerificationError(Exception):
    """Raised when an inbound webhook fails signature / freshness checks."""


def _to_bytes(body: object) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    raise WebhookVerificationError("raw body must be bytes or str")


def _strip_prefix(signature: str) -> str:
    """Accept ``sha256=<hex>`` (GitHub-style) or a bare hex digest."""
    if "=" in signature and signature.lower().startswith("sha256="):
        return signature.split("=", 1)[1]
    return signature


def compute_signature(secret: str, raw_body: object, timestamp: str | None = None) -> str:
    """Compute the expected HMAC-SHA256 hex digest for a request body.

    Signing input is ``"{timestamp}.".encode + body`` when a timestamp is
    supplied, else the bare body. Returns a lowercase hex digest.
    """
    body = _to_bytes(raw_body)
    signing_input = (f"{timestamp}.".encode() + body) if timestamp else body
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str | None,
    raw_body: object,
    signature: str | None,
    timestamp: str | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    now: float | None = None,
) -> None:
    """Raise :class:`WebhookVerificationError` unless the request verifies.

    Order: secret present → signature present → timestamp fresh (when
    supplied) → HMAC matches in constant time.
    """
    if not secret:
        raise WebhookVerificationError("no signing secret configured")
    if not signature:
        raise WebhookVerificationError("missing signature header")
    if timestamp is not None:
        try:
            ts = float(timestamp)
        except (TypeError, ValueError) as exc:
            raise WebhookVerificationError("invalid timestamp header") from exc
        current = now if now is not None else time.time()
        if abs(current - ts) > tolerance_seconds:
            raise WebhookVerificationError(
                f"timestamp outside {tolerance_seconds}s tolerance window"
            )
    expected = compute_signature(secret, raw_body, timestamp)
    provided = _strip_prefix(signature.strip()).lower()
    if not hmac.compare_digest(expected, provided):
        raise WebhookVerificationError("signature mismatch")


@dataclass
class ReplayCache:
    """In-memory event-ID dedupe with a sliding TTL.

    Per-process and best-effort: a Machine restart clears it (acceptable —
    the signature + timestamp window still bound the replay surface). For
    a single long-lived per-customer Machine this defeats same-event
    replay within the window.
    """

    ttl_seconds: int = _REPLAY_TTL_SECONDS
    _seen: dict[str, float] = field(default_factory=dict)

    def check_and_record(self, event_id: str, *, now: float | None = None) -> bool:
        """Return ``True`` if the event is fresh (and record it); ``False``
        if it was already seen within the TTL."""
        current = now if now is not None else time.time()
        # Prune expired entries so the cache cannot grow unbounded.
        expired = [k for k, seen_at in self._seen.items() if current - seen_at > self.ttl_seconds]
        for k in expired:
            del self._seen[k]
        if event_id in self._seen:
            return False
        self._seen[event_id] = current
        return True
