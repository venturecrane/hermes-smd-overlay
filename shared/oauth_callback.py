"""Machine-hosted OAuth consent callback for firm-delegated connectors (ADR 0054).

The firm-delegated (authorization_code) connect flow lands HERE, on the customer's
own Machine, not on a shared Worker. The firm's connector app registers
``https://hermes-<slug>.fly.dev/oauth/<connector>/callback`` as its redirect URI;
the webhook gate dispatches that GET to :func:`handle_smokeball_callback`.

What this does, entirely within the customer's isolated Machine:

  1. Verify the signed ``state`` with the Machine's OWN per-customer key
     (``SMOKEBALL_OAUTH_STATE_KEY`` = HMAC(master, slug), staged at provision —
     ADR 0043 derivation). A state minted for another customer cannot verify here,
     and a state whose ``customer_id`` is not this Machine's slug is rejected.
  2. Exchange the ``code`` for a refresh token at Smokeball's token endpoint using
     the Machine's OWN ``SMOKEBALL_CLIENT_ID/SECRET`` (already Fly secrets here).
     No shared client secret, no call to any Worker.
  3. Write the refresh token to a hermes-owned 0600 file on the per-customer
     volume, where the connector reads it (the Clio token-file pattern). The file
     is durable across restarts/deploys (it lives on the Fly volume).

Stdlib only (the gate carries no httpx). Secrets and tokens are NEVER logged.
State auth is opaque on failure (no hint which check failed).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

logger = logging.getLogger("hermes_smd.oauth_callback")

# (region, environment) -> auth host. MUST match the connector's host table
# (ss-console operator/connectors/smokeball/.../client.py _HOSTS) and the
# initiator (bin/connect-smokeball.sh).
_SMOKEBALL_AUTH_HOSTS = {
    ("us", "production"): "auth.smokeball.com",
    ("us", "staging"): "datastaging-auth.smokeball.com",
    ("au", "production"): "auth.smokeball.com.au",
    ("au", "staging"): "datastaging-auth.smokeball.com.au",
    ("uk", "production"): "auth.smokeball.co.uk",
    ("uk", "staging"): "datastaging-auth.smokeball.co.uk",
}

# Where the connector reads the firm-delegated refresh token. Overridable so the
# connector and this writer agree via one env var; default is the volume path.
_DEFAULT_TOKEN_FILE = "/opt/data/.smokeball-mcp/refresh_token"

_STATE_MAX_TTL_SECONDS = 600  # defense-in-depth cap even if a payload claims more


class CallbackError(Exception):
    """A callback step failed. ``reason`` is the short, user-safe code shown on the
    page and recorded; the message detail stays server-side."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


# ---- state -----------------------------------------------------------------
def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_state(state: str, *, key: str, own_slug: str, now: int | None = None) -> dict:
    """Verify a signed state and return its payload, or raise CallbackError.

    Format: ``<b64url(payload_json)>.<b64url(hmac_sha256(key, b64url_payload))>``.
    ``key`` is the per-customer SMOKEBALL_OAUTH_STATE_KEY (used as raw UTF-8 bytes,
    matching the initiator). The payload's ``customer_id`` MUST equal ``own_slug``.
    """
    now = int(time.time()) if now is None else now
    if not key:
        raise CallbackError("bad_state", "no state key configured")
    parts = state.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CallbackError("bad_state", "malformed")
    payload_b64, sig_b64 = parts
    expected = hmac.new(key.encode(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        given = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise CallbackError("bad_state", f"sig decode: {exc}") from exc
    if not hmac.compare_digest(expected, given):
        raise CallbackError("bad_state", "signature mismatch")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise CallbackError("bad_state", f"payload decode: {exc}") from exc
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now or exp > now + _STATE_MAX_TTL_SECONDS:
        raise CallbackError("expired_state", "exp out of window")
    if payload.get("customer_id") != own_slug:
        # A state for a different customer must never be honored on this Machine.
        raise CallbackError("wrong_customer", "customer_id != own slug")
    if not str(payload.get("provider", "")).startswith("smokeball"):
        raise CallbackError("unknown_provider", "provider mismatch")
    return payload


# ---- code exchange ---------------------------------------------------------
def exchange_code(
    *,
    region: str,
    environment: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    timeout: float = 30.0,
) -> str:
    """Exchange an authorization code for a refresh token. Returns the refresh
    token, or raises CallbackError. Never includes the response body in the error."""
    host = _SMOKEBALL_AUTH_HOSTS.get((region.lower(), environment.lower()))
    if not host:
        raise CallbackError("config_error", f"unknown region/env {region}/{environment}")
    if not client_id or not client_secret:
        raise CallbackError("config_error", "client credentials not configured")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout)
    try:
        conn.request(
            "POST",
            "/oauth2/token",
            body=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            # Never echo the body — it can carry the grant.
            raise CallbackError("exchange_failed", f"HTTP {resp.status} at {host}")
        data = json.loads(raw)
    except CallbackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CallbackError("exchange_failed", f"{type(exc).__name__}") from exc
    finally:
        conn.close()
    refresh = data.get("refresh_token")
    if not refresh or not isinstance(refresh, str):
        raise CallbackError("missing_refresh_token", "no refresh_token in response")
    return refresh


# ---- token persistence -----------------------------------------------------
def write_token_file(refresh_token: str, *, path: str | None = None) -> None:
    """Write the refresh token to the connector-read file: hermes-owned, 0600,
    parent 0700. On the per-customer volume (durable across restarts/deploys)."""
    target = Path(path or os.environ.get("SMOKEBALL_REFRESH_TOKEN_FILE") or _DEFAULT_TOKEN_FILE)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Write to a temp sibling then rename — never leave a half-written token.
    tmp = target.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, refresh_token.encode())
    finally:
        os.close(fd)
    os.replace(str(tmp), str(target))


# ---- HTML pages ------------------------------------------------------------
def _page(title: str, badge: str, heading: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:32rem;"
        "margin:4rem auto;padding:0 1.5rem;color:#1a1a2e;line-height:1.5}"
        ".badge{font-size:2.5rem}h1{font-size:1.4rem;margin:.5rem 0}p{color:#444}"
        f'</style></head><body><div class="badge">{badge}</div>'
        f"<h1>{heading}</h1><p>{body}</p></body></html>"
    )


def _connected_page() -> str:
    return _page(
        "Connected",
        "✓",
        "Smokeball connected",
        "Your Operator is now linked to your Smokeball account. You can close this window.",
    )


def _failed_page(reason: str) -> str:
    return _page(
        "Connection failed",
        "⚠️",
        "We couldn't finish connecting Smokeball",
        "Please close this window and let your SMD contact know "
        f"(reference: <code>{reason}</code>). You can safely try again.",
    )


# ---- orchestration ---------------------------------------------------------
def handle_smokeball_callback(query: str, host: str | None, env: dict) -> tuple[int, str]:
    """Top-level handler. Returns (http_status, html_body). Reads everything from
    ``env`` (the gate's os.environ) so it is unit-testable. Logs no secret/token."""
    own_slug = env.get("SMD_CUSTOMER_SLUG") or env.get("CUSTOMER_SLUG") or ""
    params = parse_qs(query)

    def first(name: str) -> str:
        vals = params.get(name)
        return vals[0] if vals else ""

    issuer_error = first("error")
    if issuer_error:
        logger.warning("smokeball oauth: provider error %r", issuer_error)
        return 400, _failed_page("provider_error")

    code = first("code")
    state = first("state")
    if not code or not state:
        return 400, _failed_page("missing_params")

    try:
        verify_state(state, key=env.get("SMOKEBALL_OAUTH_STATE_KEY", ""), own_slug=own_slug)
        if not host:
            raise CallbackError("config_error", "no host header")
        # The redirect_uri sent to the token endpoint MUST byte-match the one in
        # the authorize step — exactly this endpoint's own public URL.
        redirect_uri = f"https://{host}/oauth/smokeball/callback"
        refresh = exchange_code(
            region=env.get("SMOKEBALL_REGION", "us"),
            environment=env.get("SMOKEBALL_ENVIRONMENT", "staging"),
            client_id=env.get("SMOKEBALL_CLIENT_ID", ""),
            client_secret=env.get("SMOKEBALL_CLIENT_SECRET", ""),
            code=code,
            redirect_uri=redirect_uri,
        )
        write_token_file(refresh, path=env.get("SMOKEBALL_REFRESH_TOKEN_FILE"))
    except CallbackError as exc:
        logger.warning("smokeball oauth: rejected (%s) for slug=%s", exc.reason, own_slug)
        return 400, _failed_page(exc.reason)
    except Exception as exc:  # noqa: BLE001  — never leak; fail closed
        logger.error("smokeball oauth: unexpected %s", type(exc).__name__)
        return 400, _failed_page("internal_error")

    logger.info("smokeball oauth: connected slug=%s (token written, not logged)", own_slug)
    return 200, _connected_page()
