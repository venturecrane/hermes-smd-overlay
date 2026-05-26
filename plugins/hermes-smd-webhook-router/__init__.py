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
from shared.secrets import require

from . import router  # noqa: F401 - surface for tests

logger = logging.getLogger(__name__)


# Module-level state - populated by ``register()``. Hook callbacks are
# fast paths that never re-read disk.
_TABLE: router.RoutingTable = router.RoutingTable.empty()
_CUSTOMER_SLUG: str | None = None
_D1_CLIENT: Any | None = None


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


def on_pre_gateway_dispatch(**kwargs: Any) -> dict | None:
    """Inspect each inbound dispatch for webhook markers and rewrite if matched.

    Expected kwargs:
        payload: dict | Any - the inbound message body.

    Returns a rewrite directive on match, or ``None`` to pass through
    unchanged. The rewrite directive shape:

        {"action": "route_to_skill",
         "persona": "<persona-slug>",
         "skill": "<skill-name>",
         "payload": <original payload>}

    Hermes' gateway dispatcher consumes this contract to invoke the
    named skill on the named persona.

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
    global _TABLE, _CUSTOMER_SLUG, _D1_CLIENT

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
