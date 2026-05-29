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

Hook callbacks are exception-safe per AGENTS.md hard rule #3.
"""

import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.d1_client import D1Client
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


# Minimal ULID generator. Duplicate of hermes-smd-audit/emit.py - the
# overlay does not yet have a shared/ulid module; consolidation is a
# follow-on cleanup. The contract matches: 10 chars timestamp + 16
# chars randomness, Crockford-base32.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def _ulid() -> str:
    ts = int(time.time() * 1000)
    rand = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


def _iso_utc() -> str:
    dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# The audit_log INSERT statement matches hermes-smd-audit/emit.py
# `_INSERT_SQL`. Schema lives in ss-console
# `docs/specs/ai-employee/d1-schema.md`; both plugins must agree.
_INSERT_SQL = (
    "INSERT INTO audit_log "
    "(id, ts, action_type, actor, actor_role, skill_name, matter_ref, "
    "input_digest, output_digest, diff_digest, trust_ceiling, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _emit_webhook_routed(
    *,
    client: Any,
    customer: str,
    trigger: router.WebhookTrigger,
) -> None:
    """Write one WEBHOOK_ROUTED row directly via D1Client.

    Sidesteps the dynamic-import dance of pulling AuditLogWriter from
    the sibling audit plugin. The INSERT SQL and the schema must agree
    with ``hermes-smd-audit/emit.py``; both reference the canonical
    audit_log schema in ss-console.
    """
    metadata = {
        "per_webhook_route": True,
        "customer": customer,
        "source": trigger.source,
        "event_type": trigger.event_type,
        "persona": trigger.persona,
        "skill": trigger.skill,
    }
    params = [
        _ulid(),
        _iso_utc(),
        "WEBHOOK_ROUTED",
        "agent",
        "agent",  # ActorRole.AGENT
        trigger.skill,
        None,  # matter_ref
        None,  # input_digest
        None,  # output_digest
        None,  # diff_digest
        None,  # trust_ceiling
        json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    ]
    client.execute(_INSERT_SQL, *params)


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

    # Emit audit row. Failure is logged but does NOT block the route -
    # the dispatch rewrite is the load-bearing action; the audit row
    # is observability.
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

    return {
        "action": "route_to_skill",
        "persona": decision.trigger.persona,
        "skill": decision.trigger.skill,
        "payload": payload,
    }


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
        _D1_CLIENT = D1Client(
            binding_name=secrets_map["SMD_D1_AUDIT_BINDING"],
            customer_slug=_CUSTOMER_SLUG,
        )
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
