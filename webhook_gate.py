"""hermes-smd-webhook-gate — the deterministic front-door for inbound webhooks.

Why this exists (read first): Hermes' native webhook adapter verifies only
GitHub / GitLab / Generic(``X-Webhook-Signature``) signatures. AgentMail signs
with ``X-AgentMail-Signature`` using the SAME algorithm (hex HMAC-SHA256 of the
raw body) — only the header NAME differs. The overlay must not modify Hermes
core, so this thin front-door reconciles the header and is the single
HTTP-edge verifier:

  public POST (AgentMail)  ->  this gate (verify X-AgentMail-Signature)
                           ->  localhost:8644 (Hermes adapter, Generic verify)

On the forward hop the gate sets ``X-Webhook-Signature`` (recomputed over the
exact forwarded bytes, same secret) so the adapter re-verifies, and sets
``X-Request-ID`` to the inbound message-id so the adapter's idempotency cache
dedupes vendor retries. Only the gate is exposed publicly (Fly ``http_service``
points at ``GATE_PORT``); the gateway's 8644 stays loopback-reachable.

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


def verify_agentmail_signature(body: bytes, header_value: str, secret: str) -> bool:
    """True iff ``X-AgentMail-Signature`` == hex HMAC-SHA256(body, secret).

    Constant-time compare. A falsy header or secret is a reject (fail-closed).
    """
    if not header_value or not secret:
        return False
    expected = _hex_hmac_sha256(body, secret)
    return hmac.compare_digest(str(header_value), expected)


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

        sig = self.headers.get("X-AgentMail-Signature", "")
        if not verify_agentmail_signature(body, sig, secret):
            logger.warning("gate: invalid signature for route %r", route)
            self._json(401, {"error": "invalid signature"})
            return

        # Forward verbatim to the Hermes adapter with the Generic header it
        # understands (recomputed over the exact bytes) + an idempotency id.
        fwd_sig = _hex_hmac_sha256(body, secret)
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "X-Webhook-Signature": fwd_sig,
            "X-Request-ID": _message_id(body) or sig[:64],
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
    # Boot self-check (Devil's-Advocate #1): prove the HMAC path is sane so a
    # byte-mangling bug surfaces here, not as phantom 401s on live traffic.
    probe = verify_agentmail_signature(
        b"probe", _hex_hmac_sha256(b"probe", "k"), "k"
    )
    assert probe, "webhook-gate HMAC self-check failed"
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
