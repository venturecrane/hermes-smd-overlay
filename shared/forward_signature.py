"""The forward hop's signature: Hermes' generic HMAC V2 (timestamp bound).

Every inbound delivery reaches the Hermes webhook adapter on loopback through
one of our own forwarders: the gate (``webhook_gate.py``: the vendor routes,
``/webhooks/handoff``, ``/mcp``) and the Graph mail poller
(``shared/msgraph_poller.py``). Each used to sign that hop with the adapter's
generic V1 scheme, ``X-Webhook-Signature`` = hex HMAC-SHA256 over the body
alone, which the adapter at v0.21 accepts with a once-per-route warning that it
"is vulnerable to replay attacks". The hop is loopback-only, so the exposure
was the warning, not a door, but the adapter's V2 scheme is what it asks for
and costs one header: ``X-Webhook-Signature-V2`` = hex HMAC-SHA256 over
``"<unix seconds>.<body>"`` with ``X-Webhook-Timestamp`` carrying the seconds,
verified inside a 300 s window (``gateway/platforms/webhook.py`` at the blessed
pin, ``_V2_REPLAY_WINDOW_SECONDS``). V2 exists at v2026.8.18 too, so a pin
rollback keeps forwarding.

Only the V2 headers are sent. Sending V1 alongside would let a captured request
be replayed with the V2 headers stripped: the adapter commits to V2 when the
header is present and rejects a missing timestamp rather than falling back, but
a request with no V2 header at all still takes the V1 path.

The router plugin's own ``verify.compute_signature(secret, body, timestamp)``
uses the identical signing input, so a forward signed here verifies there too.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Webhook-Signature-V2"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
REQUEST_ID_HEADER = "X-Request-ID"


def sign_forward(body: bytes, secret: str, *, now: float | None = None) -> tuple[str, str]:
    """Return ``(timestamp, signature)`` for *body*: the unix seconds as a decimal
    string and the hex HMAC-SHA256 of ``f"{timestamp}." + body`` under *secret*."""
    timestamp = str(int(time.time() if now is None else now))
    signature = hmac.new(
        secret.encode("utf-8"), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return timestamp, signature


def forward_headers(
    body: bytes,
    secret: str,
    *,
    request_id: str,
    content_type: str = "application/json",
    now: float | None = None,
) -> dict[str, str]:
    """The complete header set for one forward to the adapter: content type,
    the V2 signature and its timestamp, and ``X-Request-ID`` (the adapter's
    idempotency key, chosen by the caller)."""
    timestamp, signature = sign_forward(body, secret, now=now)
    return {
        "Content-Type": content_type,
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: timestamp,
        REQUEST_ID_HEADER: request_id,
    }
