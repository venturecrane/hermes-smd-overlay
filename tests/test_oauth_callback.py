"""Unit coverage for the Machine-hosted Smokeball OAuth callback (ADR 0054).

No network: the code exchange is monkeypatched. State signing mirrors the
initiator (bin/connect-smokeball.sh) so verify_state is tested against the real
wire format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from shared import oauth_callback
from shared.oauth_callback import CallbackError


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign_state(payload: dict, key: str) -> str:
    payload_b64 = _b64url(json.dumps(payload).encode())
    sig = hmac.new(key.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url(sig)}"


def _payload(slug="pilot-smokeball", exp_delta=300, provider="smokeball:us:staging"):
    return {
        "v": 1,
        "customer_id": slug,
        "provider": provider,
        "reviewer_id": "connect-script",
        "nonce": "n-1",
        "exp": int(time.time()) + exp_delta,
    }


KEY = "deadbeef" * 8  # a 64-char hex-like key string (used as raw bytes)


# ---- verify_state ----------------------------------------------------------
def test_verify_state_accepts_a_valid_state():
    state = _sign_state(_payload(), KEY)
    out = oauth_callback.verify_state(state, key=KEY, own_slug="pilot-smokeball")
    assert out["customer_id"] == "pilot-smokeball"


def test_verify_state_rejects_tampered_signature():
    state = _sign_state(_payload(), KEY)
    bad = state[:-2] + ("aa" if not state.endswith("aa") else "bb")
    with pytest.raises(CallbackError) as e:
        oauth_callback.verify_state(bad, key=KEY, own_slug="pilot-smokeball")
    assert e.value.reason == "bad_state"


def test_verify_state_rejects_wrong_key():
    state = _sign_state(_payload(), KEY)
    with pytest.raises(CallbackError) as e:
        oauth_callback.verify_state(state, key="other-key", own_slug="pilot-smokeball")
    assert e.value.reason == "bad_state"


def test_verify_state_rejects_expired():
    state = _sign_state(_payload(exp_delta=-10), KEY)
    with pytest.raises(CallbackError) as e:
        oauth_callback.verify_state(state, key=KEY, own_slug="pilot-smokeball")
    assert e.value.reason == "expired_state"


def test_verify_state_rejects_overlong_ttl():
    # A payload claiming a far-future exp is capped (defense-in-depth).
    state = _sign_state(_payload(exp_delta=999999), KEY)
    with pytest.raises(CallbackError) as e:
        oauth_callback.verify_state(state, key=KEY, own_slug="pilot-smokeball")
    assert e.value.reason == "expired_state"


def test_verify_state_rejects_cross_customer():
    # A state minted for another customer must not verify on this Machine.
    state = _sign_state(_payload(slug="other-firm"), KEY)
    with pytest.raises(CallbackError) as e:
        oauth_callback.verify_state(state, key=KEY, own_slug="pilot-smokeball")
    assert e.value.reason == "wrong_customer"


def test_verify_state_requires_a_key():
    state = _sign_state(_payload(), KEY)
    with pytest.raises(CallbackError):
        oauth_callback.verify_state(state, key="", own_slug="pilot-smokeball")


# ---- write_token_file ------------------------------------------------------
def test_write_token_file_is_0600_and_roundtrips(tmp_path):
    target = tmp_path / "nest" / "refresh_token"
    oauth_callback.write_token_file("rt-secret", path=str(target))
    assert target.read_text() == "rt-secret"
    assert (target.stat().st_mode & 0o777) == 0o600


# ---- handle_smokeball_callback (orchestration) -----------------------------
def _env(slug="pilot-smokeball", token_file=None):
    return {
        "CUSTOMER_SLUG": slug,
        "SMOKEBALL_OAUTH_STATE_KEY": KEY,
        "SMOKEBALL_REGION": "us",
        "SMOKEBALL_ENVIRONMENT": "staging",
        "SMOKEBALL_CLIENT_ID": "cid",
        "SMOKEBALL_CLIENT_SECRET": "sec",
        "SMOKEBALL_REFRESH_TOKEN_FILE": token_file or "",
    }


def test_handle_missing_params_returns_failed_page():
    status, html = oauth_callback.handle_smokeball_callback("", "hermes-pilot-smokeball.fly.dev", _env())
    assert status == 400
    assert "missing_params" in html


def test_handle_provider_error_returns_failed_page():
    status, html = oauth_callback.handle_smokeball_callback(
        "error=access_denied", "h.fly.dev", _env()
    )
    assert status == 400
    assert "provider_error" in html


def test_handle_success_writes_token_and_renders_connected(tmp_path, monkeypatch):
    token_file = str(tmp_path / "refresh_token")
    captured = {}

    def fake_exchange(**kwargs):
        captured.update(kwargs)
        return "rt-from-exchange"

    monkeypatch.setattr(oauth_callback, "exchange_code", fake_exchange)
    state = _sign_state(_payload(), KEY)
    status, html = oauth_callback.handle_smokeball_callback(
        f"code=the-code&state={state}",
        "hermes-pilot-smokeball.fly.dev",
        _env(token_file=token_file),
    )
    assert status == 200
    assert "Smokeball connected" in html
    # The exchange used this Machine's own callback URL as redirect_uri.
    assert captured["redirect_uri"] == "https://hermes-pilot-smokeball.fly.dev/oauth/smokeball/callback"
    assert captured["code"] == "the-code"
    # The token landed on disk, never in the HTML.
    assert open(token_file).read() == "rt-from-exchange"
    assert "rt-from-exchange" not in html


def test_handle_exchange_failure_is_opaque(monkeypatch):
    def boom(**kwargs):
        raise CallbackError("exchange_failed", "HTTP 400")

    monkeypatch.setattr(oauth_callback, "exchange_code", boom)
    state = _sign_state(_payload(), KEY)
    status, html = oauth_callback.handle_smokeball_callback(
        f"code=c&state={state}", "h.fly.dev", _env()
    )
    assert status == 400
    assert "exchange_failed" in html
