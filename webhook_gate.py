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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from shared import runtime_read

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


# Svix's documented webhook tolerance: deliveries whose signed timestamp is
# more than this many seconds away from now (either direction — clock skew is
# symmetric) are rejected even with a valid signature. Closes the replay
# window (threat model OP-P2-3): before this, a captured legitimate delivery
# replayed indefinitely until Machine restart (2026-06-12 code review).
SVIX_TIMESTAMP_TOLERANCE_SECONDS = 300


def verify_svix_signature(
    body: bytes,
    svix_id: str,
    svix_ts: str,
    svix_sig_header: str,
    secret: str,
    now: float | None = None,
) -> bool:
    """Svix webhook verification — AgentMail delivers via Svix.

    (Verified against AgentMail's webhook-verification docs: headers
    ``svix-id`` / ``svix-timestamp`` / ``svix-signature``, secret prefixed
    ``whsec_``.) Scheme: signed content = ``f"{id}.{timestamp}.{body}"``; the
    HMAC key is the base64-decoded secret (after the ``whsec_`` prefix); the
    expected value is base64(HMAC-SHA256(key, signed)); the ``svix-signature``
    header is a space-delimited list of ``v1,<base64>`` — match any in
    constant time. Any missing field is a reject (fail-closed).

    Freshness: the signed timestamp must be within
    ``SVIX_TIMESTAMP_TOLERANCE_SECONDS`` of ``now`` (injectable for tests;
    defaults to wall clock). A non-numeric timestamp is a reject — the
    timestamp is part of the signed content, so a legitimate Svix delivery
    always carries a parseable epoch.
    """
    if not (svix_id and svix_ts and svix_sig_header and secret):
        return False
    try:
        ts = int(svix_ts)
    except ValueError:
        return False
    reference = time.time() if now is None else now
    if abs(reference - ts) > SVIX_TIMESTAMP_TOLERANCE_SECONDS:
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


_RUNTIME_PREFIX = "/runtime/"
_RUNTIME_KIND_RE = re.compile(r"^[a-z_]{1,32}$")


def _db_path_from_binding(binding_env: str, *, fallback_env: str | None = None) -> str | None:
    """Resolve a per-customer D1 file path from a binding env var.

    The binding var may carry EITHER a direct filesystem path (e.g.
    ``/opt/data/audit.db`` — how the live Machine sets it) OR the NAME of
    another env var that holds the path (the documented indirection). Handle
    both: a value starting with ``/`` is the path itself; otherwise it is a
    var name to look up. Returns None when nothing resolves (→ honest empty
    page, never a guess)."""
    binding = os.environ.get(binding_env)
    if binding and binding.startswith("/"):
        return binding
    if binding:
        return os.environ.get(binding) or None
    if fallback_env:
        return os.environ.get(fallback_env) or None
    return None


def _audit_db_path() -> str | None:
    """Per-customer audit D1 file path (``SMD_D1_AUDIT_BINDING`` → ``CUSTOMER_DB``)."""
    return _db_path_from_binding("SMD_D1_AUDIT_BINDING", fallback_env="CUSTOMER_DB")


def _observations_db_path() -> str | None:
    """ADR-0016 persona_observations DB path (memory-mirror's binding)."""
    return _db_path_from_binding("SMD_D1_OBSERVATIONS_BINDING")


def _agent_state_db_path() -> str | None:
    """agent_skills_inventory DB path; falls back to the audit binding exactly
    as the audit plugin does when ``SMD_D1_AGENT_STATE_BINDING`` is unset."""
    return _db_path_from_binding("SMD_D1_AGENT_STATE_BINDING") or _audit_db_path()


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

    def _json_nostore(self, status: int, obj: dict) -> None:
        """Like ``_json`` but with ``Cache-Control: no-store`` — runtime reads
        return tenant audit data that must never be cached by any proxy."""
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        split = urlsplit(self.path)
        path = split.path
        if path.rstrip("/") == "/health":
            self._json(200, {"status": "ok", "platform": "webhook-gate"})
        elif path.startswith(_RUNTIME_PREFIX):
            self._handle_runtime(path, split.query)
        else:
            self._json(404, {"error": "not found"})

    def _handle_runtime(self, path: str, query: str) -> None:
        """Console→Machine runtime read (ADR 0043 A). Authenticated, read-only,
        single-customer. Auth failures return an opaque 401 — never a hint about
        which check failed, and never the bearer or any row in a log."""
        # The gate is a SEPARATE process from the Hermes agent. SMD_CUSTOMER_SLUG
        # is injected only into the agent's process (for the plugins); the
        # Machine-wide slug the gate process actually has is CUSTOMER_SLUG (the
        # Dockerfile ARG→ENV). Prefer SMD_CUSTOMER_SLUG when present, fall back to
        # CUSTOMER_SLUG — without the fallback, own_slug is None and every read
        # 401s regardless of key.
        ok = runtime_read.verify_runtime_auth(
            self.headers.get("Authorization"),
            self.headers.get("X-Tenant-Slug"),
            key=os.environ.get("OPERATOR_RUNTIME_READ_KEY"),
            own_slug=os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG"),
        )
        if not ok:
            logger.warning("runtime read: unauthorized from %s", self.address_string())
            self._json_nostore(401, {"error": "unauthorized"})
            return

        kind = path[len(_RUNTIME_PREFIX) :].strip("/").split("/")[0]
        if not _RUNTIME_KIND_RE.match(kind) or kind not in runtime_read.SUPPORTED_KINDS:
            self._json_nostore(404, {"error": "unknown kind"})
            return

        params = parse_qs(query)
        try:
            if kind == "config":
                # config is a single materialized-state snapshot — no DB, no
                # pagination. (auth + slug-sanity already passed above.)
                result = runtime_read.read_config()
            else:
                result = runtime_read.read_runtime(
                    kind,
                    db_path=_audit_db_path(),
                    cursor=(params.get("cursor") or [None])[0],
                    limit=(params.get("limit") or [None])[0],
                    table=(params.get("table") or [None])[0],
                    observations_db_path=_observations_db_path(),
                    agent_state_db_path=_agent_state_db_path(),
                )
                if result.get("error") == "unknown table":
                    self._json_nostore(400, {"error": "unknown table"})
                    return
        except Exception as exc:  # never leak detail; fail closed
            logger.error("runtime read: error serving kind %r: %s", kind, exc)
            self._json_nostore(500, {"error": "read failed"})
            return
        self._json_nostore(200, result)

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


def svix_self_check() -> bool:
    """Boot self-check: round-trip a Svix-signed probe through the REAL
    verifier so a crypto/encoding bug surfaces at boot, not as phantom 401s
    on live traffic.

    The probe timestamp MUST be current: the #61 replay window rejects
    anything outside ±SVIX_TIMESTAMP_TOLERANCE_SECONDS, and a fixed epoch
    here crash-looped the gate on the v0.4.17 deploy (2026-06-12). Signing
    with now() exercises the production path, freshness window included.
    Extracted from main() so the regression test can call it directly.
    """
    whsec = "whsec_" + base64.b64encode(b"selfcheckkey").decode()
    key = base64.b64decode(whsec.split("_", 1)[1])
    probe_ts = str(int(time.time()))
    signed = f"id1.{probe_ts}.".encode() + b"probe"
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return verify_svix_signature(b"probe", "id1", probe_ts, f"v1,{sig}", whsec)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBHOOK_GATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get("WEBHOOK_GATE_PORT", DEFAULT_GATE_PORT))
    assert svix_self_check(), "webhook-gate Svix self-check failed"
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
