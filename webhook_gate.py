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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from shared import mcp_result_store, runtime_read

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


def _stamp_source(body: bytes, route: str) -> bytes:
    """Stamp the verified ingress provenance (``source = <route>``) onto the
    forwarded JSON body so the downstream webhook router can route on
    ``(source, event_type)``.

    The gate is the only place that authoritatively knows the vendor: the route
    slug (``agentmail``) is the adapter that signed this delivery. Vendor
    payloads (AgentMail's ``message.received``) carry ``event_type`` but no
    ``source`` — the router's contract anticipates this provenance being added
    by the authenticated ingress (``hermes-smd-webhook-router`` accepts a
    top-level ``source`` or ``metadata.source``). We add the top-level form.

    Stamped AFTER signature verification and BEFORE the forward HMAC is computed,
    so the re-signed bytes and the stamped bytes are the same — the downstream
    Generic verify still passes. Fail-safe: a non-JSON or non-object body is
    forwarded UNCHANGED (it would not route anyway, and a parse error must not
    break the forward); an existing ``source``/``event_type`` is never overwritten.

    EVENT TYPE (2026-06-13): the original contract assumed vendor payloads carry
    a top-level ``event_type``. AgentMail delivers over Svix, whose envelope puts
    the event name under ``type`` (``{"type":"message.received","data":{...}}``),
    so the router's ``(source, event_type)`` match never fired — the route was
    silently skipped and the demo relay's recipient-lock origin was never
    recorded (the agent ran the skill autonomously, masking it). We now also
    stamp ``event_type`` from the vendor's native ``type``/``event`` field so the
    router contract is satisfied regardless of the vendor's envelope spelling."""
    try:
        payload = json.loads(body)
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    # Concise structural log (keys/markers only — never body content) so a future
    # vendor envelope change is debuggable from the gate's own (visible) INFO.
    logger.info(
        "gate: stamping route=%s event_type=%r type=%r has_message=%s",
        route,
        payload.get("event_type"),
        payload.get("type"),
        isinstance(payload.get("message"), dict),
    )
    changed = False
    if not payload.get("source"):
        payload["source"] = route
        changed = True
    if not payload.get("event_type"):
        for k in ("type", "event"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                payload["event_type"] = v
                changed = True
                break
    if not changed:
        return body
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ---- MCP CHANNEL (Claude as an inbound channel) ------------------------------
# Beat-1 synchronous-return spine. The /mcp route terminates a JSON-RPC MCP
# request and drives ONE agent turn through the same loopback path the webhook
# routes use (the `mcp` webhook route → router → agent loop → broker → memory,
# identical to email), then long-polls the cross-process result store the
# agent-side result-sink wrote and returns the answer IN-LINE — the synchronous
# return MCP requires but Hermes' fire-and-forget dispatch (202 + out-of-band
# deliver) does not provide. See docs/design/operator/03-mcp-server-exposure.md.
#
# AUTH (beat 1): a stub bearer token (SMD_MCP_STUB_TOKEN), fail-closed when
# unset so a Machine that has not authored it never exposes an open /mcp. Beat 2
# replaces this with Clerk OAuth (per-customer JWKS / iss / aud / sub), ported
# from ss-console src/lib/operator/mcp/token-validation.ts.

MCP_ROUTE = "mcp"  # webhook route name; session id == webhook:mcp:<correlation_id>
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SERVER_INFO = {
    "name": "smd-operator-connector",
    "title": "SMD Operator",
    "version": "0.1.0",
}

# Long-poll budget for the synchronous return. Kept under typical MCP client
# tool timeouts; on expiry the call returns an explicit "still working" result
# rather than hanging the client (MCP is stateless — the client may retry).
_MCP_POLL_TIMEOUT_S = 55.0
_MCP_POLL_INTERVAL_S = 0.25

_JSON_RPC_PARSE_ERROR = -32700
_JSON_RPC_INVALID_REQUEST = -32600
_JSON_RPC_METHOD_NOT_FOUND = -32601
_JSON_RPC_INVALID_PARAMS = -32602
_JSON_RPC_INTERNAL_ERROR = -32603

# Beat-1 tool surface: one echo verb to prove the spine end to end. Real verbs
# (fetch/store) are authored per customer and advertised here from that authored
# set in later beats.
_MCP_TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back through the Operator (synchronous-spine proof).",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
    {
        "name": "fetch_documents",
        "description": (
            "List the documents the Operator can reach in Google Drive, optionally "
            "filtered. Read-only; returns file names and ids."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional Drive search query (e.g. a name fragment).",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Optional Drive folder id to list within.",
                },
            },
        },
    },
    {
        "name": "store_document",
        "description": (
            "Store content back to Google Drive as a Google Doc — create a new doc "
            "('title' + 'content') or append to an existing one ('document_id' + "
            "'text'). The human approves this action in their Claude client."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Title for a new doc."},
                "content": {"type": "string", "description": "Body for a new doc."},
                "document_id": {"type": "string", "description": "Existing doc id to append to."},
                "text": {"type": "string", "description": "Text to append."},
            },
        },
    },
]
_MCP_TOOL_NAMES = frozenset(t["name"] for t in _MCP_TOOLS)


def _mcp_stub_authorized(auth_header: str | None) -> bool:
    """Beat-1 stub auth: constant-time bearer check against SMD_MCP_STUB_TOKEN.

    Fail-closed: an unset token rejects every request, so a Machine that never
    authored the stub token can never expose an open /mcp. Replaced by Clerk
    OAuth in beat 2.
    """
    token = os.environ.get("SMD_MCP_STUB_TOKEN")
    if not token:
        return False
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[len("Bearer ") :], token)


def _rpc_ok(req_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _drive_agent_turn(tool_name: str, args: dict) -> dict | None:
    """Forward one MCP verb into the agent loop and long-poll for its answer.

    Mints a correlation id that becomes the Hermes delivery id (X-Request-ID),
    so the turn's session is ``webhook:mcp:<cid>`` and the result-sink stores the
    completed turn's answer under ``<cid>`` for us to collect here. Returns the
    stored payload (``{"answer": ...}``) or None on forward failure / timeout.
    """
    secret = _route_secret(MCP_ROUTE)
    if not secret:
        logger.warning("mcp: WEBHOOK_SECRET_%s unset — cannot forward", MCP_ROUTE.upper())
        return None

    correlation_id = uuid.uuid4().hex
    # correlation_id rides in the BODY (not just the X-Request-ID header) so the
    # route prompt can render it into the turn's user_message — the result-sink
    # recovers it there. The gateway's webhook:mcp:<id> chat-id is NOT the
    # agent-loop session_id (they differ), so message-carried correlation is the
    # reliable handle, verified on staging.
    body = json.dumps(
        {
            "source": MCP_ROUTE,
            "event_type": tool_name,
            "message": args,
            "correlation_id": correlation_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": _hex_hmac_sha256(body, secret),
        "X-Request-ID": correlation_id,
    }
    conn = http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=30)
    try:
        conn.request("POST", f"/webhooks/{MCP_ROUTE}", body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        if resp.status not in (200, 202):
            logger.warning("mcp: forward returned %d", resp.status)
            return None
    except Exception as exc:  # gateway down / cold-start race
        logger.error("mcp: forward failed: %s", exc)
        return None
    finally:
        conn.close()

    # Long-poll the cross-process store the agent-side result-sink writes.
    deadline = time.monotonic() + _MCP_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        result = mcp_result_store.take(correlation_id)
        if result is not None:
            return result
        time.sleep(_MCP_POLL_INTERVAL_S)
    logger.warning("mcp: poll timed out (tool=%s)", tool_name)
    return None


def _mcp_tools_call(req: dict) -> tuple[int, dict]:
    """Handle a JSON-RPC ``tools/call`` → drive the agent turn, return in-line."""
    req_id = req.get("id")
    params = req.get("params")
    if not isinstance(params, dict):
        return 200, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "tools/call requires params")
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        return 200, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "tools/call requires a tool name")
    if not isinstance(args, dict):
        return 200, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "arguments must be an object")
    if name not in _MCP_TOOL_NAMES:
        return 200, _rpc_err(req_id, _JSON_RPC_METHOD_NOT_FOUND, f"unknown tool: {name}")

    result = _drive_agent_turn(name, args)
    if result is None:
        return 200, _rpc_ok(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "The Operator did not return a result in time. Please retry.",
                    }
                ],
                "isError": True,
            },
        )
    answer = result.get("answer")
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return 200, _rpc_ok(req_id, {"content": [{"type": "text", "text": text}]})


def _mcp_dispatch(req: dict) -> tuple[int, dict | None]:
    """Dispatch one already-authorized JSON-RPC MCP request.

    Returns ``(http_status, json_rpc_response | None)``. The ``initialized``
    notification gets 202 + no body (per JSON-RPC, a notification has no
    response); every other method returns 200 + a JSON-RPC response object.
    """
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return 200, _rpc_ok(
            req_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": MCP_SERVER_INFO,
            },
        )
    if method == "notifications/initialized":
        return 202, None
    if method == "ping":
        return 200, _rpc_ok(req_id, {})
    if method == "tools/list":
        return 200, _rpc_ok(req_id, {"tools": _MCP_TOOLS})
    if method == "tools/call":
        return _mcp_tools_call(req)
    return 200, _rpc_err(req_id, _JSON_RPC_METHOD_NOT_FOUND, f"method not found: {method}")


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
                    section=(params.get("section") or [None])[0],
                )
                if result.get("error") == "unknown table":
                    self._json_nostore(400, {"error": "unknown table"})
                    return
                if result.get("error") == "unknown section":
                    self._json_nostore(400, {"error": "unknown section"})
                    return
        except Exception as exc:  # never leak detail; fail closed
            logger.error("runtime read: error serving kind %r: %s", kind, exc)
            self._json_nostore(500, {"error": "read failed"})
            return
        self._json_nostore(200, result)

    def _handle_mcp(self) -> None:
        """Terminate a JSON-RPC MCP request on /mcp (Claude-as-a-channel).

        Beat-1 stub auth (bearer) then JSON-RPC dispatch; ``tools/call`` drives
        one agent turn and returns its answer in-line via the result store.
        """
        if not _mcp_stub_authorized(self.headers.get("Authorization")):
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw)
        except Exception:
            self._json(200, _rpc_err(None, _JSON_RPC_PARSE_ERROR, "invalid JSON"))
            return
        if (
            not isinstance(req, dict)
            or req.get("jsonrpc") != "2.0"
            or not isinstance(req.get("method"), str)
        ):
            self._json(200, _rpc_err(None, _JSON_RPC_INVALID_REQUEST, "not a JSON-RPC 2.0 request"))
            return
        try:
            status, body = _mcp_dispatch(req)
        except Exception as exc:  # never leak detail; fail closed
            logger.error("mcp: dispatch error: %s", exc)
            self._json(200, _rpc_err(req.get("id"), _JSON_RPC_INTERNAL_ERROR, "internal error"))
            return
        if body is None:
            self.send_response(status)
            self.end_headers()
            return
        self._json(status, body)

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path.rstrip("/") == "/mcp":
            self._handle_mcp()
            return
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

        # Stamp the verified ingress provenance (source == route) so the
        # downstream router can route on (source, event_type). Done after Svix
        # verify and before the forward HMAC, so the re-signed bytes ARE the
        # forwarded bytes. _message_id is read from the ORIGINAL body (the stamp
        # never touches the message block).
        request_id = svix_id or (_message_id(body) or "")[:64]
        body = _stamp_source(body, route)

        # Forward to the Hermes adapter with the Generic header it understands
        # (hex HMAC over the exact bytes, same secret) + the Svix delivery id as
        # the idempotency key so adapter dedupes vendor retries.
        fwd_sig = _hex_hmac_sha256(body, secret)
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "X-Webhook-Signature": fwd_sig,
            "X-Request-ID": request_id,
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
