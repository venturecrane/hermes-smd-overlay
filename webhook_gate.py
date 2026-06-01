"""hermes-smd-webhook-gate — the deterministic front-door for inbound webhooks.

Why this exists (read first): Hermes' native webhook adapter verifies only
GitHub / GitLab / Generic(``X-Webhook-Signature`` = hex HMAC-SHA256 of the body)
signatures. AgentMail delivers via **Svix** (``svix-id`` / ``svix-timestamp`` /
``svix-signature`` headers, ``whsec_`` secret, base64 v1 scheme) — a different
verification entirely. The overlay must not modify Hermes core, so this thin
front-door is the single HTTP-edge verifier and scheme bridge:

  public POST (AgentMail/Svix)  ->  this gate (verify Svix signature)
                                ->  localhost:8644 (Hermes adapter, Generic verify)

On the forward hop the gate sets ``X-Webhook-Signature`` (hex HMAC-SHA256 over the
exact forwarded bytes, same secret string) so the adapter re-verifies, and sets
``X-Request-ID`` to the Svix delivery id so the adapter's idempotency cache dedupes
vendor retries. Only the gate is exposed publicly (Fly ``http_service`` points at
``GATE_PORT``); the gateway's 8644 stays loopback-reachable.

Security posture: the gate is the deterministic auth boundary. A forged POST
(bad/missing signature) is rejected 401 before any agent work. Sender-trust
(allowlist) + recipient-lock live downstream (the skill replies in-thread via
``reply_to_message``, so the reply target is structurally the original sender,
never a body-derived address). DMARC-in-code is the documented evolution
upgrade (AgentMail surfaces ``headers['Authentication-Results']``).

Stdlib only (http.server) — no aiohttp dependency, runs anywhere the overlay
is installed. Launched by bootstrap.sh as a tini-supervised child.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Route names are slugs (== adapter slug). Strictly validated before being used
# to build the forward URL, so the only dynamic part of the urllib call is a
# charset-safe path segment over a fixed loopback base.
_ROUTE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

logger = logging.getLogger("hermes_smd.webhook_gate")

DEFAULT_GATE_PORT = 8643
# Fixed loopback target — the Hermes adapter on this same machine. host:port are
# constants (not a dynamic URL), so the forward is not an SSRF surface.
GATEWAY_HOST = os.environ.get("WEBHOOK_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("WEBHOOK_GATEWAY_PORT", "8644"))
_MAX_BODY_BYTES = 1_048_576  # 1 MB, matches the Hermes adapter cap


def _hex_hmac_sha256(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_svix_signature(
    body: bytes, svix_id: str, svix_ts: str, svix_sig_header: str, secret: str
) -> bool:
    """Svix webhook verification — AgentMail delivers via Svix.

    (Verified against AgentMail's webhook-verification docs: headers
    ``svix-id`` / ``svix-timestamp`` / ``svix-signature``, secret prefixed
    ``whsec_``.) Scheme: signed content = ``f"{id}.{timestamp}.{body}"``; the
    HMAC key is the base64-decoded secret (after the ``whsec_`` prefix); the
    expected value is base64(HMAC-SHA256(key, signed)); the ``svix-signature``
    header is a space-delimited list of ``v1,<base64>`` — match any in
    constant time. Any missing field is a reject (fail-closed).
    """
    if not (svix_id and svix_ts and svix_sig_header and secret):
        return False
    try:
        key_b64 = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        key = base64.b64decode(key_b64)
    except Exception:
        return False
    signed = svix_id.encode() + b"." + svix_ts.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in svix_sig_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and sig and hmac.compare_digest(sig, expected):
            return True
    return False


def _route_secret(route: str) -> str | None:
    """Per-vendor secret env: ``WEBHOOK_SECRET_<ROUTE>`` (route == adapter slug)."""
    env = f"WEBHOOK_SECRET_{route.upper().replace('-', '_')}"
    val = os.environ.get(env)
    return val or None


def _message_id(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except Exception:
        return None
    msg = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(msg, dict):
        mid = msg.get("message_id") or msg.get("id")
        if mid:
            return str(mid)
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "hermes-smd-webhook-gate/1.0"

    def log_message(self, fmt: str, *args) -> None:  # route through logging
        logger.info("gate %s - " + fmt, self.address_string(), *args)

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        if self.path.rstrip("/") == "/health":
            self._json(200, {"status": "ok", "platform": "webhook-gate"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/webhooks/"):
            self._json(404, {"error": "unknown path"})
            return
        route = self.path[len("/webhooks/") :].strip("/").split("/")[0]
        if not _ROUTE_RE.match(route):
            self._json(404, {"error": "unknown route"})
            return
        secret = _route_secret(route)
        if not secret:
            logger.warning("gate: no secret for route %r — rejecting", route)
            self._json(401, {"error": "unconfigured route"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length) if length else b""

        svix_id = self.headers.get("svix-id", "")
        svix_ts = self.headers.get("svix-timestamp", "")
        svix_sig = self.headers.get("svix-signature", "")
        if not verify_svix_signature(body, svix_id, svix_ts, svix_sig, secret):
            # Diagnostic: header NAMES only (never values/secrets) so a future
            # provider scheme change is debuggable without a leak.
            logger.warning(
                "gate: invalid signature for route %r (headers present: %s)",
                route,
                sorted(self.headers.keys()),
            )
            self._json(401, {"error": "invalid signature"})
            return

        # Forward to the Hermes adapter with the Generic header it understands
        # (hex HMAC over the exact bytes, same secret) + the Svix delivery id as
        # the idempotency key so adapter dedupes vendor retries.
        fwd_sig = _hex_hmac_sha256(body, secret)
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "X-Webhook-Signature": fwd_sig,
            "X-Request-ID": svix_id or (_message_id(body) or "")[:64],
        }
        # Forward over a fixed loopback host:port (http.client, not a dynamic
        # URL) — route is charset-validated above and only forms the path.
        conn = http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=30)
        try:
            conn.request("POST", f"/webhooks/{route}", body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # gateway down / cold-start race
            logger.error("gate: forward failed: %s", exc)
            # 503 invites a vendor retry while the gateway finishes booting.
            self._json(503, {"error": "gateway unavailable", "retry": True})
        finally:
            conn.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBHOOK_GATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get("WEBHOOK_GATE_PORT", DEFAULT_GATE_PORT))
    # Boot self-check: round-trip a Svix-signed probe so a crypto/encoding bug
    # surfaces here, not as phantom 401s on live traffic.
    _whsec = "whsec_" + base64.b64encode(b"selfcheckkey").decode()
    _key = base64.b64decode(_whsec.split("_", 1)[1])
    _signed = b"id1.1700000000." + b"probe"
    _sig = base64.b64encode(hmac.new(_key, _signed, hashlib.sha256).digest()).decode()
    probe = verify_svix_signature(b"probe", "id1", "1700000000", f"v1,{_sig}", _whsec)
    assert probe, "webhook-gate Svix self-check failed"
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    logger.info(
        "webhook-gate listening on 0.0.0.0:%d -> %s:%d (HMAC self-check ok)",
        port,
        GATEWAY_HOST,
        GATEWAY_PORT,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
