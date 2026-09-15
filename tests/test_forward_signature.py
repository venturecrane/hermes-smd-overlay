"""shared/forward_signature: the forward hop signs the adapter's HMAC V2 form.

The adapter (gateway/platforms/webhook.py at the blessed pin) verifies
``X-Webhook-Signature-V2`` as hex HMAC-SHA256 over ``"<timestamp>.<body>"``
and requires ``X-Webhook-Timestamp`` within 300 s. These tests recompute that
formula independently, cross-check it against the router plugin's own
``verify.compute_signature`` (the same scheme), and pin that the legacy
body-only header is not sent.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path

from shared import forward_signature

SECRET = "route-secret"
BODY = b'{"source":"handoff","task":"x"}'


def _router_verify():
    root = Path(__file__).parent.parent
    path = root / "plugins" / "hermes-smd-webhook-router" / "verify.py"
    spec = importlib.util.spec_from_file_location("router_verify_for_forward_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sign_forward_matches_the_adapters_v2_formula() -> None:
    ts, sig = forward_signature.sign_forward(BODY, SECRET, now=1_757_950_000.9)
    assert ts == "1757950000"  # whole seconds, as the adapter parses with int()
    expected = hmac.new(SECRET.encode(), b"1757950000." + BODY, hashlib.sha256).hexdigest()
    assert sig == expected
    # Distinct from the legacy body-only digest the adapter warns about.
    assert sig != hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


def test_forward_headers_carry_v2_timestamp_and_request_id_and_no_legacy_header() -> None:
    headers = forward_signature.forward_headers(BODY, SECRET, request_id="evt-1", now=1_757_950_000)
    assert set(headers) == {
        "Content-Type",
        "X-Webhook-Signature-V2",
        "X-Webhook-Timestamp",
        "X-Request-ID",
    }
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Webhook-Timestamp"] == "1757950000"
    assert headers["X-Request-ID"] == "evt-1"
    assert "X-Webhook-Signature" not in headers


def test_content_type_passes_through_for_vendor_routes() -> None:
    headers = forward_signature.forward_headers(
        BODY, SECRET, request_id="r", content_type="text/plain"
    )
    assert headers["Content-Type"] == "text/plain"


def test_signature_verifies_under_the_router_plugins_scheme() -> None:
    verify = _router_verify()
    ts, sig = forward_signature.sign_forward(BODY, SECRET)
    assert verify.compute_signature(SECRET, BODY, ts) == sig
    verify.verify_signature(secret=SECRET, raw_body=BODY, signature=sig, timestamp=ts)


def test_a_signature_with_a_different_timestamp_does_not_verify() -> None:
    verify = _router_verify()
    ts, sig = forward_signature.sign_forward(BODY, SECRET)
    other = str(int(ts) + 1)
    assert verify.compute_signature(SECRET, BODY, other) != sig
