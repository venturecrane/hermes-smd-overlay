"""hermes-smd-webhook-router - route inbound webhook payloads to skills.

Attaches to one hook at the pinned Hermes ref (v2026.5.16):

- ``pre_gateway_dispatch`` (``hermes_cli/plugins.py:128-168`` lists it
  in VALID_HOOKS) - the gateway-side hook that runs before each
  inbound message is dispatched into the agent loop. The plugin's
  return value can rewrite the dispatch.

ADR 0021 Stream E. The router reads
``customer.yaml.webhook_triggers[]`` at register time and builds an
in-memory ``(source, event_type)`` -> skill mapping. On each fire,
the inbound payload is inspected for webhook markers; matches are
rewritten to invoke the configured skill, non-matches pass through
unchanged.

Per ADR 0016 mirror-don't-gate: a successful route emits one
``WEBHOOK_ROUTED`` audit row directly via the shared D1Client. The
router is observation + dispatch-rewrite only; it never blocks the
dispatch on audit failure.

ADR 0027 inbound convergence: on a verified route the router also attaches
an :class:`shared.inbound.InboundEnvelope` (item_id, trust_class=
unknown_external, source, surface, ingested_at, verification, content_digest)
to the dispatch directive, emits an ``INBOUND_RECEIVED`` audit row, and
records the item in :data:`shared.inbound.PENDING` so the ``hermes-smd-inbound``
plugin's ``pre_llm_call`` chokepoint can wrap it in a nonce-fenced quarantine
block before the engine reasons over it. Envelope/enqueue failures never break
routing — the trust gate (``hermes-smd-trust`` refusing injected sends) remains
the enforcing wall; the envelope + fence are defense-in-depth + provenance.

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

import json
import logging
import os
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from shared import inbound
from shared.audit_client import audit_client_from_env
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import agent_event_params
from shared.secrets import get_secret, require

from . import router, verify  # noqa: F401 - surface for tests

logger = logging.getLogger(__name__)


# Module-level state - populated by ``register()``. Hook callbacks are
# fast paths that never re-read disk.
_TABLE: router.RoutingTable = router.RoutingTable.empty()
_CUSTOMER_SLUG: str | None = None
_D1_CLIENT: Any | None = None
# Per-customer webhook signing secret (issue #13). None disables routing:
# an unverifiable webhook must not drive skill actions.
_SIGNING_SECRET: str | None = None
# Per-process replay cache for event-ID de-duplication.
_REPLAY = verify.ReplayCache()

# Inbound header names carrying the provider signature material. Lower-cased
# for case-insensitive lookup. Provider-specific; defaults are generic.
_SIGNATURE_HEADER = "x-webhook-signature"
_TIMESTAMP_HEADER = "x-webhook-timestamp"
_EVENT_ID_HEADER = "x-webhook-id"


_DEFAULT_CUSTOMER_YAML_PATH = "/opt/data/customer.yaml"


# ULID, ISO-Z timestamps, and the audit_log INSERT contract are single-sourced
# in shared.ids / shared.audit_contract (imported above). The row params are
# built via agent_event_params so this writer's column order can never drift
# from hermes-smd-audit/emit.py.


def _emit_webhook_routed(
    *,
    client: Any,
    customer: str,
    trigger: router.WebhookTrigger,
) -> None:
    """Write one WEBHOOK_ROUTED row directly via D1Client.

    Sidesteps the dynamic-import dance of pulling AuditLogWriter from
    the sibling audit plugin, but shares its row contract
    (``shared.audit_contract``) so the two can never desync.
    """
    metadata = {
        "per_webhook_route": True,
        "customer": customer,
        "source": trigger.source,
        "event_type": trigger.event_type,
        "persona": trigger.persona,
        "skill": trigger.skill,
    }
    params = agent_event_params(
        action_type="WEBHOOK_ROUTED",
        skill_name=trigger.skill,
        metadata=metadata,
    )
    client.execute(_INSERT_SQL, *params)


def _emit_inbound_received(
    *,
    client: Any,
    customer: str,
    envelope: inbound.InboundEnvelope,
) -> None:
    """Write one INBOUND_RECEIVED row directly via D1Client (ADR 0027).

    Carries the provenance envelope via ``envelope.audit_metadata()`` (source,
    surface, ingested_at, trust_class, verification, verification_detail,
    content_digest, item_id) — NEVER the inbound content itself, only its
    digest. ``INBOUND_RECEIVED`` is added to ACCEPTED_ACTION_TYPES ss-console-
    side by PR-B; the direct-INSERT path does not validate action_type, so the
    row writes through regardless. If the audit writer ever rejects the type,
    coordinate with team-lead (the schema is the canonical source).
    """
    metadata = {
        "per_inbound_received": True,
        "customer": customer,
        **envelope.audit_metadata(),
    }
    # input_digest stays NULL — inbound content is never persisted, only its
    # digest (carried inside metadata via envelope.audit_metadata()).
    params = agent_event_params(
        action_type="INBOUND_RECEIVED",
        metadata=metadata,
    )
    client.execute(_INSERT_SQL, *params)


def _inbound_content_for(payload: Any) -> str:
    """Best-effort extraction of the untrusted text body from an inbound payload.

    Used for the envelope's content_digest and the quarantine fence. Tries the
    common body keys (top-level then nested under ``data``/``body``); falls back
    to a canonical JSON serialization of the whole payload so SOMETHING is
    always quarantined (fail-closed: we never hand un-fenced inbound to the
    engine just because we didn't recognize the body shape).
    """
    if isinstance(payload, dict):
        for key in ("body", "body_plain", "text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("body", "body_plain", "text", "content", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _inbound_origin_from(payload: Any, *, content: str) -> inbound.InboundOrigin | None:
    """Extract the recipient-lock origin from an AgentMail ``message.received``
    payload, or ``None`` if the sender/message-id cannot be resolved.

    The ``message`` block of the AgentMail webhook carries ``from`` (a
    ``"Display Name <addr@host>"`` string), ``message_id``, and ``inbox_id``.
    We normalize ``from`` to a bare lower-cased address via ``parseaddr`` — the
    recipient-lock compares the agent draft's ``to`` against THIS address, so it
    must be canonical. Returns ``None`` (fail closed — no recorded origin, the
    relay refuses to send) when there is no sender address or no message id.

    Tolerant of two shapes: the ``message`` block nested under ``message`` (the
    AgentMail webhook) or under ``data`` (some ingress wrappers). The fields are
    never trusted as instructions — only as attribution for the structural lock.
    """
    if not isinstance(payload, dict):
        return None
    msg = payload.get("message")
    if not isinstance(msg, dict):
        data = payload.get("data")
        msg = data.get("message") if isinstance(data, dict) else None
    if not isinstance(msg, dict):
        return None

    raw_from = msg.get("from")
    sender = parseaddr(raw_from)[1].strip().lower() if isinstance(raw_from, str) else ""
    message_id = msg.get("message_id")
    message_id = message_id.strip() if isinstance(message_id, str) else ""
    inbox_id = msg.get("inbox_id")
    inbox_id = inbox_id.strip() if isinstance(inbox_id, str) else ""

    if not sender or not message_id:
        return None
    return inbound.InboundOrigin(
        sender_address=sender,
        message_id=message_id,
        content_digest=inbound.content_digest(content),
        inbox_id=inbox_id,
    )


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup. Returns None if absent/not a dict."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            return value if isinstance(value, str) else None
    return None


def _raw_body_for(kwargs: dict[str, Any], payload: Any) -> bytes:
    """Resolve the exact bytes the signature was computed over.

    The gateway SHOULD pass ``raw_body`` (the verbatim request body) so
    the HMAC matches the provider's signature byte-for-byte. When it does
    not, we fall back to a canonical JSON serialization of the parsed
    payload — this only matches our own internal signer/tests, NOT a real
    third-party provider, which is why an absent raw_body for a real
    inbound webhook will fail verification and (correctly) not route.
    """
    raw = kwargs.get("raw_body")
    if isinstance(raw, (bytes, str)):
        return raw if isinstance(raw, bytes) else raw.encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _event_id_from_payload(payload: Any) -> str | None:
    """Best-effort event-ID extraction for replay dedupe."""
    if not isinstance(payload, dict):
        return None
    for key in ("event_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("event_id", "id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _verification_failure(kwargs: dict[str, Any], payload: Any) -> str | None:
    """Return a human-readable reason the inbound webhook is unverified,
    or ``None`` when it verifies. Routing proceeds only on ``None``.

    Refusing to route on any failure is the safe default (issue #13): a
    forged or replayed event must not drive skill actions. The dispatch
    is not blocked — it simply passes through as an ordinary message that
    the agent loop (and the trust ceiling) still governs.
    """
    if _SIGNING_SECRET is None:
        return (
            "SMD_WEBHOOK_SIGNING_SECRET not configured; webhook routing is "
            "disabled until a per-customer signing secret is provisioned"
        )
    headers = kwargs.get("headers")
    signature = _header(headers, _SIGNATURE_HEADER)
    timestamp = _header(headers, _TIMESTAMP_HEADER)
    raw_body = _raw_body_for(kwargs, payload)
    try:
        verify.verify_signature(
            secret=_SIGNING_SECRET,
            raw_body=raw_body,
            signature=signature,
            timestamp=timestamp,
        )
    except verify.WebhookVerificationError as exc:
        return f"signature verification failed: {exc}"

    event_id = _header(headers, _EVENT_ID_HEADER) or _event_id_from_payload(payload)
    if not event_id:
        return "missing event id required for replay protection"
    if not _REPLAY.check_and_record(event_id):
        return f"replayed event id {event_id!r}"
    return None


def on_pre_gateway_dispatch(**kwargs: Any) -> dict | None:
    """Inspect each inbound dispatch for webhook markers and rewrite if matched.

    Expected kwargs:
        payload: dict | Any - the inbound (parsed) message body.
        raw_body: bytes | str - the verbatim request body the provider
            signed. REQUIRED for real third-party webhooks; see
            ``_raw_body_for``.
        headers: dict - inbound request headers carrying the provider
            signature, timestamp, and event id.

    Returns a rewrite directive on a matched AND verified webhook, or
    ``None`` to pass through unchanged. The rewrite directive shape:

        {"action": "route_to_skill",
         "persona": "<persona-slug>",
         "skill": "<skill-name>",
         "payload": <original payload>}

    Hermes' gateway dispatcher consumes this contract to invoke the
    named skill on the named persona.

    A matched route is verified (HMAC signature + timestamp freshness +
    event-ID dedupe) before it fires (issue #13). Verification failure
    does NOT block the dispatch — it declines to auto-invoke the skill,
    so a forged or replayed event cannot drive skill actions.

    Exception-safe: any failure logs at warning level and returns
    ``None``. Per AGENTS.md hard rule #3, the callback never raises.
    """
    if _TABLE.size() == 0:
        return None

    payload = kwargs.get("payload")
    try:
        decision = router.decide_route(_TABLE, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes-smd-webhook-router: decide_route failed (%s); passing through",
            exc,
        )
        return None

    if decision.trigger is None:
        return None

    # Verify the inbound webhook before routing (issue #13). An attacker
    # who learns the dispatch URL must not be able to drive skill actions
    # with forged or replayed events.
    try:
        failure = _verification_failure(kwargs, payload)
    except Exception as exc:  # noqa: BLE001 - never let verification raise
        logger.warning(
            "hermes-smd-webhook-router: verification raised (%s); refusing to route",
            exc,
        )
        return None
    if failure is not None:
        logger.warning(
            "hermes-smd-webhook-router: refusing to route %s — %s",
            decision.matched_key,
            failure,
        )
        return None

    # ADR 0027 — attach an inbound provenance envelope to the dispatched
    # content and record it for the pre_llm_call quarantine chokepoint. The
    # router only reaches here on a VERIFIED route (the verification gate
    # above already declined forged/replayed events), so verification is
    # "verified"; trust_class stays unknown_external (a verified webhook is
    # still untrusted third-party data — positive evidence is required to
    # raise the trust class). Building the envelope must never break the
    # route, so it is wrapped in its own try/except.
    envelope: inbound.InboundEnvelope | None = None
    try:
        content = _inbound_content_for(payload)
        envelope = inbound.make_envelope(
            content=content,
            source=decision.trigger.source,
            surface="webhook",
            verification="verified",
            trust_class=inbound.TRUST_CLASS_UNKNOWN_EXTERNAL,
        )
        # Record the item for the inbound plugin's pre_llm_call chokepoint to
        # fence. Keyed by the dispatch session so the fence applies to the
        # right turn. session_id may be absent on some gateway shapes; an
        # empty key still enqueues (the chokepoint drains by the same key).
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str):
            session_id = ""
        inbound.PENDING.enqueue(
            inbound.InboundItem(session_id=session_id, content=content, envelope=envelope)
        )
        # Recipient-lock anchor (hermes-smd-demo-relay). Record WHO opened this
        # session — the verified inbound sender + the inbox/message to reply
        # into — so the demo relay can only send a governed draft back to the
        # address that emailed in. FIRST inbound wins (SessionInboundOrigin), so
        # a later injected "inbound" cannot move the lock. Recording the origin
        # grants NO send capability on its own; the relay is fail-closed on the
        # demo.reply_relay flag and re-checks the content/fabrication floors. A
        # payload without a resolvable sender/message-id records nothing (the
        # relay then finds no origin and refuses to send). Never breaks routing.
        origin = _inbound_origin_from(payload, content=content)
        if origin is not None:
            inbound.SESSION_INBOUND_ORIGIN.record(session_id, origin)
    except Exception as exc:  # noqa: BLE001 — provenance must not break routing
        logger.warning(
            "hermes-smd-webhook-router: inbound envelope/enqueue failed (%s); "
            "route still applied (the trust gate remains the enforcing wall)",
            exc,
        )

    # Emit audit rows. Failure is logged but does NOT block the route -
    # the dispatch rewrite is the load-bearing action; the audit rows
    # are observability.
    if _D1_CLIENT is not None and _CUSTOMER_SLUG is not None:
        try:
            _emit_webhook_routed(
                client=_D1_CLIENT,
                customer=_CUSTOMER_SLUG,
                trigger=decision.trigger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes-smd-webhook-router: WEBHOOK_ROUTED emission failed (%s); "
                "route still applied",
                exc,
            )
        if envelope is not None:
            try:
                _emit_inbound_received(
                    client=_D1_CLIENT,
                    customer=_CUSTOMER_SLUG,
                    envelope=envelope,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes-smd-webhook-router: INBOUND_RECEIVED emission failed (%s); "
                    "route still applied",
                    exc,
                )

    directive: dict[str, Any] = {
        "action": "route_to_skill",
        "persona": decision.trigger.persona,
        "skill": decision.trigger.skill,
        "payload": payload,
    }
    if envelope is not None:
        # Attach provenance to the dispatch directive so downstream consumers
        # (and the dashboard) can trace the action back to the inbound item.
        directive["inbound_envelope"] = envelope.as_dict()
    return directive


def register(ctx) -> None:
    """Plugin entry point. Wires pre_gateway_dispatch.

    Reads customer.yaml at register time and builds the routing table.
    Any failure during table build leaves the table empty; the hook
    stays registered so Hermes' dispatcher contract holds, but
    dispatches pass through unchanged.
    """
    global _TABLE, _CUSTOMER_SLUG, _D1_CLIENT, _SIGNING_SECRET

    try:
        secrets_map = require("SMD_CUSTOMER_SLUG", "SMD_D1_AUDIT_BINDING")
        _CUSTOMER_SLUG = secrets_map["SMD_CUSTOMER_SLUG"]
        # Broker-aware audit transport (OP-P1-4): routes WEBHOOK_ROUTED /
        # INBOUND_RECEIVED rows through the append-only broker when
        # SMD_AUDIT_BROKER_SOCKET is set; direct D1Client otherwise.
        _D1_CLIENT = audit_client_from_env(customer_slug=_CUSTOMER_SLUG)
    except KeyError as exc:
        _CUSTOMER_SLUG = None
        _D1_CLIENT = None
        logger.warning(
            "hermes-smd-webhook-router: env not configured (%s); routing will work "
            "without audit emission",
            exc,
        )

    # Per-customer webhook signing secret (issue #13). Optional at register
    # time: when absent, the router still registers but refuses to route any
    # webhook (it cannot verify it). Provisioned alongside the webhook
    # subscription that captures the provider's signing secret.
    try:
        _SIGNING_SECRET = get_secret("SMD_WEBHOOK_SIGNING_SECRET")
    except KeyError:
        _SIGNING_SECRET = None
        logger.warning(
            "hermes-smd-webhook-router: SMD_WEBHOOK_SIGNING_SECRET not set; inbound "
            "webhooks cannot be verified and will NOT be routed until it is provisioned"
        )

    customer_yaml_path = Path(
        os.environ.get("SMD_CUSTOMER_YAML_PATH") or _DEFAULT_CUSTOMER_YAML_PATH
    )
    _TABLE = router.build_routing_table(customer_yaml_path)
    logger.info(
        "hermes-smd-webhook-router registered (customer=%s, routing_table_size=%d)",
        _CUSTOMER_SLUG,
        _TABLE.size(),
    )

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
