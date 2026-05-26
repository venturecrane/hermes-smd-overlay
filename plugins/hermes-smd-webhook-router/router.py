"""Routing-table construction and pre_gateway_dispatch handler.

The router reads ``customer.yaml.webhook_triggers[]`` once at plugin
register time and builds an in-memory ``(source, event_type)`` →
``(persona_slug, skill_name)`` mapping. On each ``pre_gateway_dispatch``
fire, the inbound message's metadata is inspected for webhook markers
(``source`` and ``event_type`` keys); when both match a routing-table
entry, the dispatch is rewritten to invoke the configured skill.

Per ADR 0021 Stream E + ADR 0016 mirror-don't-gate: a successful route
emits one ``WEBHOOK_ROUTED`` audit row. Routes that fail to match the
routing table pass through unchanged.

The routing-table build is intentionally lenient — missing
customer.yaml, missing ``webhook_triggers`` block, malformed entries
all log a warning and produce an empty table rather than raising. The
plugin then no-ops on every dispatch (the safer default for an
observer plugin).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import-time guard
    raise ImportError(
        "PyYAML is required by hermes-smd-webhook-router; install with `pip install pyyaml`"
    ) from exc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookTrigger:
    """One entry from customer.yaml.webhook_triggers[].

    Mirrors the schema landed in ss-console PR #1052. The router
    treats the four fields as opaque strings; cross-field validation
    (e.g., ``source`` matches a connector adapter, ``persona`` matches
    a declared persona slug) happens upstream at customer.yaml
    validation time.
    """

    source: str
    event_type: str
    skill: str
    persona: str


@dataclass(frozen=True)
class RoutingTable:
    """In-memory routing table. Keys are ``(source, event_type)`` tuples;
    values are the WebhookTrigger record carrying the resolved skill +
    persona."""

    entries: dict[tuple[str, str], WebhookTrigger]

    @classmethod
    def empty(cls) -> RoutingTable:
        return cls(entries={})

    def lookup(self, source: str, event_type: str) -> WebhookTrigger | None:
        return self.entries.get((source, event_type))

    def size(self) -> int:
        return len(self.entries)


def build_routing_table(customer_yaml_path: Path) -> RoutingTable:
    """Build the routing table by reading customer.yaml.

    Returns an empty RoutingTable on any failure - missing file,
    YAML parse error, missing ``webhook_triggers`` key, malformed
    entries. Each failure logs a warning; the plugin no-ops at
    dispatch time but the hook stays registered so Hermes' dispatcher
    contract holds.
    """
    if not customer_yaml_path.exists():
        logger.warning(
            "hermes-smd-webhook-router: customer.yaml not found at %s; router will no-op",
            customer_yaml_path,
        )
        return RoutingTable.empty()

    try:
        with customer_yaml_path.open() as handle:
            cfg = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        logger.warning(
            "hermes-smd-webhook-router: customer.yaml parse failed (%s); router will no-op",
            exc,
        )
        return RoutingTable.empty()

    triggers = cfg.get("webhook_triggers") or []
    if not isinstance(triggers, list):
        logger.warning(
            "hermes-smd-webhook-router: webhook_triggers must be a list; got %s",
            type(triggers).__name__,
        )
        return RoutingTable.empty()

    entries: dict[tuple[str, str], WebhookTrigger] = {}
    for i, raw in enumerate(triggers):
        if not isinstance(raw, dict):
            logger.warning(
                "hermes-smd-webhook-router: webhook_triggers[%d] not a mapping; skipped",
                i,
            )
            continue
        source = raw.get("source")
        event_type = raw.get("event_type")
        skill = raw.get("skill")
        persona = raw.get("persona")
        if not (
            isinstance(source, str)
            and isinstance(event_type, str)
            and isinstance(skill, str)
            and isinstance(persona, str)
        ):
            logger.warning(
                "hermes-smd-webhook-router: webhook_triggers[%d] missing required string fields; skipped",
                i,
            )
            continue
        key = (source, event_type)
        if key in entries:
            logger.warning(
                "hermes-smd-webhook-router: duplicate (source=%s, event_type=%s); "
                "first wins, later entry skipped",
                source,
                event_type,
            )
            continue
        entries[key] = WebhookTrigger(
            source=source,
            event_type=event_type,
            skill=skill,
            persona=persona,
        )

    return RoutingTable(entries=entries)


# ---------------------------------------------------------------------------
# pre_gateway_dispatch payload inspection
# ---------------------------------------------------------------------------


def detect_webhook_markers(payload: Any) -> tuple[str, str] | None:
    """Return ``(source, event_type)`` if the inbound payload carries
    webhook markers, else ``None``.

    The router accepts two payload shapes (the gateway can wrap the
    inbound webhook either way depending on the AgentMail/Composio
    fan-in pattern):

    1. Top-level keys: ``{"source": "...", "event_type": "..."}``
       - bare webhook payload.
    2. Nested under ``metadata``:
       ``{"metadata": {"source": "...", "event_type": "..."}}`` -
       payload wrapped by the AgentMail/Composio webhook ingress.

    Any other shape returns ``None`` - the router does not route on
    inferred markers.
    """
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    event_type = payload.get("event_type")
    if isinstance(source, str) and isinstance(event_type, str):
        return (source, event_type)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        m_source = metadata.get("source")
        m_event_type = metadata.get("event_type")
        if isinstance(m_source, str) and isinstance(m_event_type, str):
            return (m_source, m_event_type)
    return None


@dataclass(frozen=True)
class RouteDecision:
    """Returned by ``decide_route`` - pure logic, no I/O.

    A ``trigger`` of ``None`` means the dispatch passes through
    unchanged. A non-None ``trigger`` carries the resolved skill +
    persona to invoke.
    """

    trigger: WebhookTrigger | None
    matched_key: tuple[str, str] | None


def decide_route(table: RoutingTable, payload: Any) -> RouteDecision:
    """Pure routing decision. No side effects."""
    markers = detect_webhook_markers(payload)
    if markers is None:
        return RouteDecision(trigger=None, matched_key=None)
    trigger = table.lookup(*markers)
    if trigger is None:
        return RouteDecision(trigger=None, matched_key=markers)
    return RouteDecision(trigger=trigger, matched_key=markers)
