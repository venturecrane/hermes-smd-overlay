"""hermes-smd-webhook-gate — the deterministic front-door for inbound webhooks.

Why this exists (read first): Hermes' native webhook adapter verifies only
GitHub / GitLab / Generic(``X-Webhook-Signature`` = hex HMAC-SHA256 of the body)
signatures. Real vendors sign differently — AgentMail via **Svix** (``svix-id`` /
``svix-timestamp`` / ``svix-signature`` headers, ``whsec_`` secret, base64 v1
scheme); Smokeball via a ``Signature`` header (raw-key hex HMAC over
``{Timestamp}|{RequestId}|{ClientId}``, body unsigned). The overlay must not
modify Hermes core, so this thin front-door is the single HTTP-edge verifier and
scheme bridge, dispatching per route slug (``_VERIFIERS``):

  public POST (vendor scheme)  ->  this gate (per-vendor verify)
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
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from shared import (
    gate_inbound_cap,
    gate_trigger_exclusions,
    heartbeat,
    mcp_result_store,
    mcp_thread_store,
    oauth_callback,
    runtime_read,
    sentry_init,
)

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


# Smokeball webhook verification. Smokeball signs each delivery with HMAC-SHA256
# (lowercase hex) in a ``Signature`` header over the pipe-joined string
# ``f"{Timestamp}|{RequestId}|{ClientId}"`` — confirmed against
# https://docs.smokeball.com/docs/api-docs/wivbkstcwngb5-webhooks and the
# published golden vector. Three properties differ from Svix and are easy to get
# wrong by cloning the Svix path:
#   * the secret is the subscription ``key`` used as RAW UTF-8 bytes — NOT
#     base64-decoded, NOT ``whsec_``-stripped;
#   * the signed content is metadata only — the HTTP BODY IS NOT SIGNED;
#   * ``ClientId`` is OUR Smokeball API client id (configured, never present in
#     the delivery), supplied from ``WEBHOOK_SMOKEBALL_CLIENT_ID``.
# ``Timestamp`` is .NET ticks (100ns since 0001-01-01 UTC).
SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS = 300
_DOTNET_TICKS_PER_SECOND = 10_000_000
# Seconds between .NET epoch (0001-01-01) and Unix epoch (1970-01-01).
_DOTNET_EPOCH_OFFSET_SECONDS = 62_135_596_800


def verify_smokeball_signature(
    body: bytes,
    timestamp: str,
    request_id: str,
    client_id: str,
    signature_header: str,
    secret: str,
    now: float | None = None,
) -> bool:
    """Smokeball webhook verification.

    ``body`` is accepted for call-site symmetry with the Svix verifier and a
    future body-binding upgrade, but is INTENTIONALLY UNUSED in the HMAC:
    Smokeball signs metadata only ({Timestamp}|{RequestId}|{ClientId}), by
    design — not a dropped check. Payload integrity is enforced downstream (the
    forwarded body is treated as untrusted data and the handler re-fetches
    authoritative state from the Smokeball API rather than trusting the body).

    Fail-closed on any missing field. ``Timestamp`` is parsed as .NET ticks and
    must be within ``SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS`` of ``now`` (signed,
    so this bounds replay). The key is the subscription secret used as raw
    UTF-8 bytes; the comparison is constant-time over the lowercase hex digest.
    """
    if not (timestamp and request_id and client_id and signature_header and secret):
        return False
    # Normalize the timestamp ONCE and use the normalized value for both the
    # freshness parse and the signed string, so they can never diverge. Ticks
    # are pure digits, so this is a no-op in the normal case (HTTP parsers
    # already OWS-trim header values) and only recovers a match if a proxy pads
    # the header after Smokeball signed the clean value.
    ts = timestamp.strip()
    try:
        ticks = int(ts)
    except (ValueError, AttributeError):
        return False
    ts_unix = ticks // _DOTNET_TICKS_PER_SECOND - _DOTNET_EPOCH_OFFSET_SECONDS
    reference = time.time() if now is None else now
    if abs(reference - ts_unix) > SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS:
        return False
    signed = f"{ts}|{request_id}|{client_id}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.strip().lower(), expected)


# Gate-level replay guard for metadata-only schemes. ``RequestId`` is inside the
# signed string, so only a signature-valid delivery reaches here; rejecting a
# duplicate RequestId at the edge (before any forward / agent work) is
# defense-in-depth atop the downstream adapter's X-Request-ID idempotency. This
# does NOT close first-arrival body forgery (the body is unsigned) — that is
# bounded by the untrusted-body posture + the re-fetch handler.
_REPLAY_TTL_SECONDS = SMOKEBALL_TIMESTAMP_TOLERANCE_SECONDS + 60
_replay_lock = threading.Lock()
_replay_seen: dict[str, float] = {}


def _replay_check_and_record(request_id: str, now: float | None = None) -> bool:
    """Return True if ``request_id`` is fresh (record it); False if seen within
    the TTL. An empty id is treated as fresh (nothing to dedupe on — the
    signature already validated)."""
    if not request_id:
        return True
    reference = time.time() if now is None else now
    with _replay_lock:
        for k in [k for k, exp in _replay_seen.items() if exp <= reference]:
            del _replay_seen[k]
        if request_id in _replay_seen:
            return False
        _replay_seen[request_id] = reference + _REPLAY_TTL_SECONDS
        return True


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


def _stamp_source(body: bytes, route: str, event_id: str = "") -> bytes:
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
    break the forward).

    SOURCE (authoritative): the ingress provenance ``source = route`` is the
    verified vendor and OVERRIDES any body-supplied ``source``. Smokeball's event
    body carries its own top-level ``source`` (``"API"``/``"UI"`` — the change's
    origin inside Smokeball), which is a different semantic and collides on the
    same key the router matches on; without the override a verified
    ``matter.updated`` would look up ``("API", …)`` and silently no-op. The
    vendor's original ``source`` is preserved under ``origin_source`` so the
    handler can still read it.

    EVENT TYPE (2026-06-13): the original contract assumed vendor payloads carry
    a top-level ``event_type``. AgentMail delivers over Svix, whose envelope puts
    the event name under ``type`` (``{"type":"message.received","data":{...}}``),
    and Smokeball likewise carries ``type`` (``{"type":"matter.updated",…}``), so
    the router's ``(source, event_type)`` match never fired on a bare ``type`` —
    the route was silently skipped and the demo relay's recipient-lock origin was
    never recorded (the agent ran the skill autonomously, masking it). We stamp
    ``event_type`` from the vendor's native ``type``/``event`` field so the router
    contract is satisfied regardless of the vendor's envelope spelling.

    EVENT ID (2026-06-28): the router runs header-less (the Hermes hook contract),
    so it can only read the replay-protection event id from the BODY (top-level
    ``event_id``/``id``). Smokeball's payload carries no such field — its unique
    per-delivery id is the ``RequestId`` HEADER (which the gate consumed for the
    signature and sets as ``X-Request-ID``). So the gate, the only place that knows
    that verified id, stamps it as top-level ``event_id`` when absent. Without it
    the router refuses to route ("missing event id required for replay protection")
    and the skill never fires (caught on the first real Smokeball matter.updated)."""
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
    existing_source = payload.get("source")
    if existing_source != route:
        if isinstance(existing_source, str) and existing_source:
            # Preserve the vendor's own ``source`` semantic (e.g. Smokeball's
            # "API"/"UI") before the authoritative provenance overrides it.
            payload.setdefault("origin_source", existing_source)
        payload["source"] = route
        changed = True
    if not payload.get("event_type"):
        for k in ("type", "event"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                payload["event_type"] = v
                changed = True
                break
    # Stamp the verified per-delivery id as the router's replay key, when the body
    # carries none of its own (guarded so a vendor that DOES supply one is never
    # overridden — AgentMail unaffected).
    if event_id and not payload.get("event_id"):
        payload["event_id"] = event_id
        changed = True
    if not changed:
        return body
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ---- PER-VENDOR VERIFICATION DISPATCH ----------------------------------------
# Each route adapter pulls ITS OWN headers, runs the vendor's verifier, and
# returns ``(ok, request_id)`` — the request_id (the vendor's delivery id) flows
# to ``X-Request-ID`` for the downstream adapter's idempotency dedupe. The
# pure crypto fns stay header-agnostic (unit-testable in isolation). A route with
# no registered verifier is rejected (fail-closed) by the handler, which closes
# the prior hole where any secret-bearing route fell through to Svix.


def _agentmail_route_verify(headers: Any, body: bytes, secret: str) -> tuple[bool, str]:
    """AgentMail/Svix route. Unchanged behavior from the pre-dispatch handler."""
    svix_id = headers.get("svix-id", "")
    svix_ts = headers.get("svix-timestamp", "")
    svix_sig = headers.get("svix-signature", "")
    ok = verify_svix_signature(body, svix_id, svix_ts, svix_sig, secret)
    request_id = svix_id or (_message_id(body) or "")[:64]
    return ok, request_id


def _smokeball_route_verify(headers: Any, body: bytes, secret: str) -> tuple[bool, str]:
    """Smokeball route. Headers ``Timestamp`` / ``RequestId`` / ``Signature``;
    ``ClientId`` is OUR configured Smokeball API client id (never delivered),
    read from ``WEBHOOK_SMOKEBALL_CLIENT_ID`` and fed into the HMAC. Fail-closed
    when that env is unset (the signed string can't be reconstructed). A valid
    signature is additionally checked against the gate replay guard."""
    client_id = os.environ.get("WEBHOOK_SMOKEBALL_CLIENT_ID", "")
    timestamp = headers.get("Timestamp", "")
    request_id = headers.get("RequestId", "")
    signature = headers.get("Signature", "")
    ok = verify_smokeball_signature(body, timestamp, request_id, client_id, signature, secret)
    if ok and not _replay_check_and_record(request_id):
        logger.warning("gate: smokeball replay rejected (RequestId already seen)")
        ok = False
    return ok, request_id


# route slug -> verifier. Unknown route => fail-closed at the handler.
_VERIFIERS: dict[str, Any] = {
    "agentmail": _agentmail_route_verify,
    "smokeball": _smokeball_route_verify,
}


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

# ADR 0062 (ss-console #1661): the inbound wake guard — breaker HARD_STOP +
# authored daily wake cap — armed in main(). None until then (unit tests that
# drive handlers directly get today's ungated behavior unless they inject one).
_INBOUND_GUARD: gate_inbound_cap.InboundWakeGuard | None = None
_GATE_AUDIT_CLIENT = None  # set in main(); shared by the inbound guard + trigger exclusions


def _audit_suppression(*, route: str, request_id: str, reason: str) -> None:
    """Best-effort WEBHOOK_SUPPRESSED audit row for an excluded delivery. The
    suppression stands whether or not the write succeeds (mirrors the inbound
    guard's park-audit posture); failure is logged loudly so the audit-trail
    gap is visible."""
    if _GATE_AUDIT_CLIENT is None:
        logger.warning(
            "gate: suppressed %s/%s (%s) — no audit client, log-only", route, request_id, reason
        )
        return
    try:
        from shared.audit_contract import INSERT_SQL, agent_event_params

        params = agent_event_params(
            action_type="WEBHOOK_SUPPRESSED",
            metadata={
                "gate_trigger_exclusion": True,
                "reason": reason,
                "route": route,
                "request_id": request_id,
            },
        )
        # The generic ``audit_append`` verb is PID-gated to the gateway; this
        # gate runs as the agent uid on a non-gateway PID, so on the broker path
        # it must use the uid-gated ``webhook_suppressed_append`` verb (mirrors
        # the cron pre_run SUPPRESSED_WAKE heartbeat). The direct/legacy client
        # (no broker socket) has no such verb and writes the INSERT itself.
        suppressed_append = getattr(_GATE_AUDIT_CLIENT, "execute_suppressed_webhook", None)
        if suppressed_append is not None:
            suppressed_append(INSERT_SQL, *params)
        else:
            _GATE_AUDIT_CLIENT.execute(INSERT_SQL, *params)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "gate: suppression audit write failed (suppression stands): route=%s id=%s reason=%s err=%s",
            route,
            request_id,
            reason,
            exc,
        )


def _breaker_hard_stopped() -> bool:
    """True when the Machine-wide cost breaker is pinned at HARD_STOP.
    Read-only, fail-toward-False (a broken read must not park the console
    surface; the vendor-webhook path applies its own guard with park+audit)."""
    try:
        from shared.cost_breaker import read_level
        from shared.sticky_stop import StickyStopLevel

        return read_level() == StickyStopLevel.HARD_STOP.value
    except Exception:  # noqa: BLE001
        return False


def _sticky_stop_clear(req: dict) -> tuple[int, dict]:
    """Pure core of the Captain clear endpoint (ADR 0062 §6).

    Body: ``{captain_id, reason}`` — both required (the state machine's own
    clear() contract). Clears every non-OK sticky_stop row via
    shared.cost_breaker.clear_hard_stops (each emits an audited
    AGENT_RESUMED) and returns what was cleared plus the resulting level.
    Auth (console-proxy bearer) lives in the handler, like /mcp/turn.
    """
    captain_id = str(req.get("captain_id") or "").strip()
    reason = str(req.get("reason") or "").strip()
    if not captain_id or not reason:
        return 400, {"error": "captain_id and reason are required"}
    try:
        from shared.cost_breaker import clear_hard_stops, read_level

        # No audit_client: the audit-ledger broker PID-gates appends to the
        # gateway process (OP-P1-4), so this gate process CANNOT write the
        # Machine ledger — by design. The state reset is the recovery; the
        # resume is a governance action, audited control-plane-side by the
        # console's admin clear endpoint (which authenticated the Captain).
        cleared = clear_hard_stops(captain_id=captain_id, reason=reason)
        return 200, {"cleared": cleared, "level": read_level() or "OK"}
    except Exception as exc:  # noqa: BLE001 — surface the failure honestly
        logger.error("sticky-stop clear failed: %s", exc)
        return 500, {"error": "clear failed", "detail": str(exc)}


MCP_ROUTE = "mcp"  # webhook route name; session id == webhook:mcp:<correlation_id>
HANDOFF_ROUTE = "handoff"  # console→Machine async task handoff (Phase 2, ADR 0043)

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

# Tool surface: ONE conversational verb. The MCP connector is a communication
# channel to the worker, not a remote-procedure menu — so it exposes a single
# "talk to it" verb and lets the worker's intelligence decide what to do, exactly
# as it would with an email or a chat. (Earlier beats listed typed verbs —
# echo/fetch_documents/store_document — but a verb menu invites the connecting
# client to route conversation into RPCs, which is the narrowing this channel
# exists to remove. The worker reaches Drive, the inbox, memory, etc. with its
# own tools inside the turn.)
ASK_TOOL_NAME = "ask_operator"
_MCP_TOOLS = [
    {
        "name": ASK_TOOL_NAME,
        "description": (
            "Talk to the Operator. Send it a message — a question, an instruction, "
            "anything you would say to a capable coworker who has your Drive, your "
            "inbox, your memory, and judgment. It understands what you want, does it, "
            "and replies. Pass a stable 'thread_id' to keep the same conversation "
            "going across turns (it remembers what was already said in that thread)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What you want to say to the Operator.",
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Optional. A stable id for THIS conversation; reuse it across "
                        "turns so the Operator keeps context. Scoped to your identity."
                    ),
                },
            },
            "required": ["message"],
        },
    },
]
_MCP_TOOL_NAMES = frozenset(t["name"] for t in _MCP_TOOLS)


# Console-sole Claude door (ADR 0057 amendment). The Machine no longer exposes a
# direct public MCP door: the shared-static-token stub auth and the Clerk-direct
# authorization path (which never consulted the grant table, so it bypassed the
# ADR 0057 kill switch) are BOTH retired. The console
# (smd.services/api/operator/<slug>/mcp) is the one public Claude door; it
# authenticates the caller, enforces the grant kill-switch per request, and then
# proxies the turn to this Machine's authenticated `/mcp/turn` endpoint over the
# console-proxy bearer (WEBHOOK_SECRET_MCP). The direct `/mcp` JSON-RPC door now
# returns 410 Gone. The JSON-RPC dispatch engine below (`_mcp_dispatch` and the
# job verbs) is retained as the turn-execution core and for future console-proxied
# re-exposure; it is no longer reachable from an unauthenticated public request.


def _rpc_ok(req_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _drive_agent_turn(
    message: str, *, principal_subject: str, thread_id: str | None
) -> dict | None:
    """Forward one conversational message into the agent loop, return its reply.

    Mints a fresh per-call correlation id (the result-bridge handle: it rides the
    BODY so the route prompt renders it into the turn's user_message, and the
    agent-side result-sink stores the completed turn's answer under it for us to
    collect). The correlation id is ALSO the Hermes delivery/dedup id
    (X-Request-ID), so it MUST stay unique per call — which is why it cannot
    double as the conversation key.

    Continuity is supplied in the overlay (see ``mcp_thread_store``): when the
    caller passes a ``thread_id``, we load the recent transcript for THIS
    principal+thread and render it into the turn so the worker remembers the
    conversation; after the reply lands we append this exchange. The thread key
    is principal-namespaced, so a caller can only ever read/extend their own
    thread. Returns the stored payload (``{"answer": ...}``) or None on forward
    failure / timeout.
    """
    secret = _route_secret(MCP_ROUTE)
    if not secret:
        logger.warning("mcp: WEBHOOK_SECRET_%s unset — cannot forward", MCP_ROUTE.upper())
        return None

    # Principal-namespaced conversation key (None => one-shot, no continuity).
    tkey = mcp_thread_store.thread_key(principal_subject, thread_id) if thread_id else None
    history_text = mcp_thread_store.render(mcp_thread_store.history(tkey)) if tkey else ""

    correlation_id = uuid.uuid4().hex
    body = json.dumps(
        {
            "source": MCP_ROUTE,
            "event_type": ASK_TOOL_NAME,
            "message": message,
            "history": history_text,
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
            # Persist the exchange so the next turn on this thread has context.
            if tkey:
                answer = result.get("answer")
                reply_text = answer if isinstance(answer, str) else json.dumps(answer)
                mcp_thread_store.append(tkey, message, reply_text)
            return result
        time.sleep(_MCP_POLL_INTERVAL_S)
    logger.warning("mcp: poll timed out (thread=%s)", bool(tkey))
    return None


def _mcp_tools_call(req: dict, *, principal_subject: str) -> tuple[int, dict]:
    """Handle a JSON-RPC ``tools/call`` → drive the agent turn, return in-line.

    ``principal_subject`` is the authenticated caller (from /mcp auth); it
    namespaces the conversation thread so a caller can only ever read/extend
    their own.
    """
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

    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return 200, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "ask_operator requires a 'message'")
    thread_id = args.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        return 200, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "'thread_id' must be a string")

    result = _drive_agent_turn(message, principal_subject=principal_subject, thread_id=thread_id)
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


def _mcp_turn(req: dict) -> tuple[int, dict]:
    """Validate a console-proxied turn request and drive one agent turn.

    Pure of transport/auth — the handler does the console-proxy bearer check and
    the body read; this takes the already-parsed JSON body and returns
    ``(http_status, response_body)``. The ``principal_subject`` is asserted by the
    console (which authenticated the caller and enforced the grant kill-switch),
    so it is required and namespaces the conversation thread. 400 on a malformed
    field, 504 on turn timeout, 200 ``{reply, thread_id}`` on success.
    """
    if not isinstance(req, dict):
        return 400, {"error": "invalid body"}
    message = req.get("message")
    if not isinstance(message, str) or not message.strip():
        return 400, {"error": "message (non-empty string) required"}
    principal_subject = req.get("principal_subject")
    if not isinstance(principal_subject, str) or not principal_subject:
        return 400, {"error": "principal_subject required"}
    thread_id = req.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        return 400, {"error": "thread_id must be a string"}

    result = _drive_agent_turn(message, principal_subject=principal_subject, thread_id=thread_id)
    if result is None:
        return 504, {"error": "turn_timeout"}
    answer = result.get("answer")
    reply = answer if isinstance(answer, str) else json.dumps(answer)
    return 200, {"reply": reply, "thread_id": thread_id}


# Job-status projection: the operator-visible control facts a caller needs to
# verify a background job, drawn straight from the ledger row. No agent turn, no
# DB file — these verbs read the broker-owned ledger over its socket, exactly
# like the ``jobs`` runtime-read kind, so they stay read-only and synchronous.
_JOB_STATUS_FIELDS: tuple[str, ...] = (
    "status",
    "spent_cents",
    "budget_cents",
    "result_ref",
    "error",
    "attempts",
)


def _job_id_arg(req: dict) -> tuple[str | None, dict | None]:
    """Extract + validate a required ``job_id`` arg from a JSON-RPC job verb.

    Returns ``(job_id, None)`` on success or ``(None, error_response)`` so the
    caller can early-return the JSON-RPC error. ``job_id`` may be passed under
    ``params`` (JSON-RPC) for symmetry with ``tools/call``."""
    req_id = req.get("id")
    params = req.get("params")
    if not isinstance(params, dict):
        return None, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "params object required")
    job_id = params.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return None, _rpc_err(req_id, _JSON_RPC_INVALID_PARAMS, "job_id (string) required")
    return job_id, None


def _mcp_job_status(req: dict) -> tuple[int, dict]:
    """Synchronous, read-only ``job_status`` — project the broker-owned ledger
    row to the operator-visible control facts. No agent turn. A missing job or
    an unreachable/unconfigured broker returns an explicit not-found result
    rather than a transport error, mirroring the runtime-read fail-safe."""
    from shared.job_ledger_client import BrokerJobClient, JobLedgerError

    req_id = req.get("id")
    job_id, err = _job_id_arg(req)
    if err is not None:
        return 200, err
    try:
        job = BrokerJobClient().read(job_id)
    except JobLedgerError as exc:
        logger.warning("mcp: job_status broker error: %s", exc)
        return 200, _rpc_ok(req_id, {"found": False, "job_id": job_id})
    if job is None:
        return 200, _rpc_ok(req_id, {"found": False, "job_id": job_id})
    projected = {field: job.get(field) for field in _JOB_STATUS_FIELDS}
    return 200, _rpc_ok(req_id, {"found": True, "job_id": job_id, **projected})


def _mcp_job_cancel(req: dict) -> tuple[int, dict]:
    """Synchronous ``job_cancel`` — set the ledger cancel flag (the worker
    observes it at its next per-iteration check and dead-letters). No agent
    turn. ``cancelled`` is the ledger's boolean outcome (False == the job was
    already terminal or unknown); an unreachable broker surfaces as a JSON-RPC
    internal error so the caller can retry."""
    from shared.job_ledger_client import BrokerJobClient, JobLedgerError

    req_id = req.get("id")
    job_id, err = _job_id_arg(req)
    if err is not None:
        return 200, err
    try:
        cancelled = BrokerJobClient().cancel(job_id)
    except JobLedgerError as exc:
        logger.warning("mcp: job_cancel broker error: %s", exc)
        return 200, _rpc_err(req_id, _JSON_RPC_INTERNAL_ERROR, "job ledger unavailable")
    return 200, _rpc_ok(req_id, {"job_id": job_id, "cancelled": cancelled})


def _mcp_dispatch(req: dict, *, principal_subject: str = "") -> tuple[int, dict | None]:
    """Dispatch one already-authorized JSON-RPC MCP request.

    Returns ``(http_status, json_rpc_response | None)``. The ``initialized``
    notification gets 202 + no body (per JSON-RPC, a notification has no
    response); every other method returns 200 + a JSON-RPC response object.

    ``principal_subject`` is the authenticated caller, threaded to ``tools/call``
    so the conversation thread is namespaced to that identity. The unauthenticated
    metadata methods (initialize/ping/tools/list) ignore it.
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
        return _mcp_tools_call(req, principal_subject=principal_subject)
    if method == "job_status":
        return _mcp_job_status(req)
    if method == "job_cancel":
        return _mcp_job_cancel(req)
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

    def _json_headers(self, status: int, obj: dict, extra: dict | None = None) -> None:
        """``_json`` plus optional extra response headers (e.g. WWW-Authenticate)."""
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
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

    def _html(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        split = urlsplit(self.path)
        path = split.path
        if path.rstrip("/") == "/health":
            self._json(200, {"status": "ok", "platform": "webhook-gate"})
        elif path.rstrip("/") == "/oauth/smokeball/callback":
            # Machine-hosted firm-delegated OAuth consent landing (ADR 0054).
            # Public, authorized solely by the signed per-customer state; the
            # firm's browser lands here after Allow. No agent work is triggered.
            status, html = oauth_callback.handle_smokeball_callback(
                split.query, self.headers.get("Host"), os.environ
            )
            self._html(status, html)
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

    def _handle_sticky_stop_clear(self) -> None:
        """Captain recovery endpoint (POST /sticky-stop/clear, ADR 0062 §6).

        AUTH: Bearer WEBHOOK_SECRET_MCP — the console-proxy key, identical to
        /mcp/turn and /webhooks/handoff. The console authenticates the Captain
        and proxies the clear; the Machine trusts the console's bearer. This is
        deliberately NOT gated on _breaker_hard_stopped (it is the un-trip
        path) and works at any level (clears WARN/SOFT_STOP pins too).
        """
        secret = os.environ.get("WEBHOOK_SECRET_MCP")
        if not secret:
            logger.warning("gate: WEBHOOK_SECRET_MCP not set — /sticky-stop/clear not configured")
            self._json(503, {"error": "clear route not configured"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(
            auth[len("Bearer ") :], secret
        ):
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            self._json(400, {"error": "invalid json"})
            return
        status, body = _sticky_stop_clear(req if isinstance(req, dict) else {})
        self._json(status, body)

    def _handle_mcp_turn(self) -> None:
        """Console → Machine synchronous turn endpoint (/mcp/turn, ADR 0057
        amendment — console-sole door).

        AUTH: Bearer WEBHOOK_SECRET_MCP (the per-customer console-proxy key, same
        derivation as /webhooks/handoff). The console has already authenticated
        the caller and enforced the ADR 0057 grant kill-switch per request; this
        endpoint trusts the console's asserted ``principal_subject`` and never
        re-derives identity. Fail-closed: unset secret → 503, bad bearer → 401.

        Body: ``{message, thread_id?, principal_subject}``. Drives ONE agent turn
        through the shared turn spine (``_drive_agent_turn``) and returns
        ``{reply, thread_id}`` synchronously — a Cloudflare Worker has no
        wall-clock cap on the awaiting HTTP request. A turn that does not resolve
        within the poll budget returns 504 so the console maps it to a
        fail-closed ``delivery_failed`` (the async handoff path remains the
        fallback for long work).
        """
        secret = os.environ.get("WEBHOOK_SECRET_MCP")
        if not secret:
            logger.warning("gate: WEBHOOK_SECRET_MCP not set — /mcp/turn not configured")
            self._json(503, {"error": "mcp turn route not configured"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(
            auth[len("Bearer ") :], secret
        ):
            self._json(401, {"error": "unauthorized"})
            return

        # ADR 0062: while the cost breaker is pinned at HARD_STOP no path
        # wakes the agent — the console surfaces the pause honestly instead
        # of burning further spend. Recovery is Captain clear().
        if _breaker_hard_stopped():
            self._json(
                503,
                {"error": "operator paused", "detail": "cost breaker hard stop (sticky_stop)"},
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw)
        except Exception:
            self._json(400, {"error": "invalid JSON"})
            return
        status, body = _mcp_turn(req)
        self._json(status, body)

    def _handle_handoff(self) -> None:
        """Console → Machine async task handoff endpoint (/webhooks/handoff, Phase 2 ADR 0043).

        AUTH: Bearer WEBHOOK_SECRET_MCP (per-customer HMAC-derived key, set at provision).
        Fail-closed: missing env var → 503 (handoff route not configured).

        Body: HandoffEnvelope JSON (see ss-console/src/lib/operator/mcp/webhook-transport.ts).
        Dispatches to the Hermes agent loop via the same internal loopback the Svix routes
        use (X-Webhook-Signature = hex-HMAC(body, WEBHOOK_SECRET_MCP) so the adapter
        re-verifies). WEBHOOK_SECRET_HANDOFF == WEBHOOK_SECRET_MCP so the adapter
        satisfies its secret lookup. Returns 202 + {accepted, handoff_id} synchronously;
        the agent works the task async and reports via its authored output channels.
        """
        secret = os.environ.get("WEBHOOK_SECRET_MCP")
        if not secret:
            logger.warning("gate: WEBHOOK_SECRET_MCP not set — handoff route not configured")
            self._json(503, {"error": "handoff route not configured"})
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._json(401, {"error": "bearer token required"})
            return
        bearer = auth[len("Bearer ") :]
        if not hmac.compare_digest(bearer, secret):
            logger.warning("gate: handoff: invalid bearer (length %d)", len(bearer))
            self._json(401, {"error": "invalid bearer"})
            return

        # ADR 0062: HARD_STOP parks async handoffs too (503 so the console
        # maps it to an honest failure, not a silent queue).
        if _breaker_hard_stopped():
            self._json(
                503,
                {"error": "operator paused", "detail": "cost breaker hard stop (sticky_stop)"},
            )
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        if (
            not isinstance(envelope, dict)
            or not isinstance(envelope.get("handoff_id"), str)
            or not isinstance(envelope.get("task"), str)
            or not envelope.get("handoff_id")
            or not envelope.get("task")
        ):
            self._json(400, {"error": "invalid envelope: handoff_id and task are required"})
            return

        handoff_id = envelope["handoff_id"]

        # Stamp provenance (source = handoff) and forward to Hermes adapter.
        body = _stamp_source(raw, HANDOFF_ROUTE)
        fwd_sig = _hex_hmac_sha256(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": fwd_sig,
            "X-Request-ID": handoff_id[:64],
        }
        conn = http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=30)
        try:
            conn.request("POST", f"/webhooks/{HANDOFF_ROUTE}", body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()  # drain; we do not proxy the body
            if resp.status >= 500:
                logger.error("gate: handoff forward returned %d", resp.status)
                self._json(503, {"error": "gateway error", "retry": True})
                return
        except Exception as exc:
            logger.error("gate: handoff forward failed: %s", exc)
            self._json(503, {"error": "gateway unavailable", "retry": True})
            return
        finally:
            conn.close()

        self._json(202, {"accepted": True, "handoff_id": handoff_id})

    def do_POST(self) -> None:  # noqa: N802
        post_path = urlsplit(self.path).path.rstrip("/")
        if post_path == "/mcp/turn":
            self._handle_mcp_turn()
            return
        if post_path == "/sticky-stop/clear":
            self._handle_sticky_stop_clear()
            return
        if post_path == "/mcp":
            # Direct public JSON-RPC Claude door retired (ADR 0057 amendment —
            # console-sole). Claude reaches the Operator only through the console,
            # which authenticates the caller and enforces the grant kill-switch
            # per request, then proxies the turn to /mcp/turn. No direct public
            # Claude door (stub bearer or Clerk) remains on the Machine.
            self._json(410, {"error": "gone", "detail": "connect via the console MCP endpoint"})
            return
        if post_path == f"/webhooks/{HANDOFF_ROUTE}":
            self._handle_handoff()
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

        # Per-vendor verification dispatch (fail-closed): a route with a secret
        # but no registered verifier is rejected, rather than falling through to
        # the Svix scheme. The adapter returns the vendor delivery id used for
        # downstream idempotency.
        verifier = _VERIFIERS.get(route)
        if verifier is None:
            logger.warning("gate: no verifier registered for route %r — rejecting", route)
            self._json(401, {"error": "unconfigured route"})
            return
        ok, request_id = verifier(self.headers, body, secret)
        if not ok:
            # Diagnostic: header NAMES only (never values/secrets) so a future
            # provider scheme change is debuggable without a leak.
            logger.warning(
                "gate: invalid signature for route %r (headers present: %s)",
                route,
                sorted(self.headers.keys()),
            )
            self._json(401, {"error": "invalid signature"})
            return

        # ADR 0062 inbound wake guard: breaker HARD_STOP or the authored daily
        # wake cap parks the delivery — acknowledged (202, so the vendor does
        # not retry-storm), audited by the guard, NOT forwarded to the agent.
        if _INBOUND_GUARD is not None:
            forward, park_reason = _INBOUND_GUARD.check(route=route, request_id=request_id)
            if not forward:
                self._json(202, {"accepted": True, "parked": park_reason})
                return

        # Authored trigger exceptions (#1766 lineage): a verified delivery whose
        # matter/actor the seat has excluded for this (source, event_type) is
        # acknowledged 202 (suppression is authored policy, not a vendor error),
        # audited (WEBHOOK_SUPPRESSED — never silent), and NOT forwarded, so the
        # agent never wakes. Fail-open: any config/parse issue forwards normally
        # (see shared/gate_trigger_exclusions.py).
        suppress_reason = gate_trigger_exclusions.check_excluded(
            route=route, body=body, exclusions=gate_trigger_exclusions.live_exclusions()
        )
        if suppress_reason is not None:
            _audit_suppression(route=route, request_id=request_id, reason=suppress_reason)
            logger.info(
                "gate: suppressed %s/%s (%s) — authored trigger exclusion",
                route,
                request_id,
                suppress_reason,
            )
            self._json(202, {"accepted": True, "suppressed": suppress_reason})
            return

        # Stamp the verified ingress provenance (source == route) + the verified
        # per-delivery id (as event_id, the router's replay key) so the header-less
        # router can route on (source, event_type) and dedupe. Done after
        # verification and before the forward HMAC, so the re-signed bytes ARE the
        # forwarded bytes.
        body = _stamp_source(body, route, event_id=request_id)

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


def smokeball_self_check() -> bool:
    """Boot self-check: round-trip a Smokeball-signed probe through the REAL
    verifier so a crypto/encoding bug (raw-bytes key, ticks math, hex digest)
    surfaces at boot, not as phantom 401s on live traffic.

    Like the Svix self-check, the probe timestamp MUST be current — the
    freshness window rejects a fixed epoch and a stale probe would crash-loop
    the gate on boot. Builds current .NET ticks from now()."""
    secret = "selfcheckkey"
    client_id = "selfcheckclient"
    request_id = "00000000-0000-0000-0000-000000000000"
    ticks = (int(time.time()) + _DOTNET_EPOCH_OFFSET_SECONDS) * _DOTNET_TICKS_PER_SECOND
    timestamp = str(ticks)
    signed = f"{timestamp}|{request_id}|{client_id}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return verify_smokeball_signature(b"probe", timestamp, request_id, client_id, sig, secret)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBHOOK_GATE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # ADR 0023 Wave 1: Sentry error monitoring for the gate process. Disabled-safe
    # (no SENTRY_DSN -> no-op). Init first so the self-checks below are covered.
    sentry_init.init_sentry("gate")
    port = int(os.environ.get("WEBHOOK_GATE_PORT", DEFAULT_GATE_PORT))
    assert svix_self_check(), "webhook-gate Svix self-check failed"
    assert smokeball_self_check(), "webhook-gate Smokeball self-check failed"
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    logger.info(
        "webhook-gate listening on 0.0.0.0:%d -> %s:%d (HMAC self-check ok)",
        port,
        GATEWAY_HOST,
        GATEWAY_PORT,
    )
    # ADR 0023 Wave 1: control-plane + healthchecks.io heartbeat. Hosted here
    # because the gate is the one non-agent process on every Machine and holds
    # the (agent-stripped) MACHINE_HEARTBEAT_KEY. Daemon thread; fail-soft — a
    # heartbeat failure never perturbs the webhook/MCP surface.
    heartbeat.emitter_from_env(_audit_db_path).start()
    # ADR 0062 (ss-console #1661): arm the inbound wake guard. The park audit
    # row goes through the broker audit client (best-effort; the park stands
    # regardless). The cap is live-read from customer.yaml per delivery.
    global _INBOUND_GUARD
    try:
        from shared.audit_client import audit_client_from_env

        _audit_client = audit_client_from_env(
            customer_slug=os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG")
        )
    except Exception as exc:  # noqa: BLE001 — guard still parks, log-only audit
        logger.warning("gate: no audit client for inbound guard (log-only parks): %s", exc)
        _audit_client = None
    global _GATE_AUDIT_CLIENT
    _GATE_AUDIT_CLIENT = _audit_client
    _INBOUND_GUARD = gate_inbound_cap.InboundWakeGuard(
        cap_resolver=gate_inbound_cap.default_cap_resolver,
        audit_client=_audit_client,
    )
    logger.info("gate: inbound wake guard armed (ADR 0062)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
