"""Authored webhook-trigger exceptions (ss-console #1766 lineage: the ops-matter
and principal-actor cases) — enforced at the GATE, before any agent wake.

customer.yaml authors, per webhook_triggers[] entry, an optional ``exclude``
block::

    webhook_triggers:
      - source: smokeball
        event_type: matter.updated
        skill: matter-memo-on-update
        persona: quinn
        exclude:
          matters:            # matter GUIDs this trigger never fires for
            - 3c191bed-...    #   (e.g. the internal ops/digest-home matter)
          actors:             # Smokeball user GUIDs whose own changes are exempt
            - <userId>        #   (e.g. the supervising principal's edits)

Semantics:

- The exclusion is TRIGGER-scoped: (source, event_type) must match an authored
  entry; other routes/events are untouched.
- Matter is read from the verified payload's ``id``/``matterId``; actor from
  ``userId``. Field names are the ones the matter-memo skill's grounded
  contract documents (operator/skills/matter-memo-on-update/SKILL.md).
- A suppressed delivery is acknowledged 202 (suppression is our authored
  policy, not a vendor error), NEVER forwarded (zero agent turns), and ALWAYS
  audited (``WEBHOOK_SUPPRESSED``) — the L2 round's "a halt is never silent"
  lesson, generalized. Mirrors the ADR 0062 inbound wake guard's park shape.
- FAIL-OPEN TO FORWARD: any read/parse failure (missing customer.yaml,
  malformed exclude block, non-JSON payload, absent fields) forwards the
  delivery normally. Suppression is hygiene, never load-bearing safety; the
  dangerous failure mode is an authoring typo silently killing a live chain.
  Unauthored (no exclude blocks) is a no-op (ADR 0035, no imposed defaults).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_TYPE_KEYS = ("event_type", "type", "event")
_MATTER_KEYS = ("id", "matterId")
_ACTOR_KEYS = ("userId",)


def resolve_exclusions(config: Any) -> dict[tuple[str, str], dict[str, frozenset[str]]]:
    """Extract ``{(source, event_type): {"matters": ids, "actors": ids}}`` from a
    customer config mapping. Tolerant: malformed entries are skipped (fail-open),
    ids are compared case-insensitively (GUIDs)."""
    out: dict[tuple[str, str], dict[str, frozenset[str]]] = {}
    if not isinstance(config, dict):
        return out
    triggers = config.get("webhook_triggers")
    if not isinstance(triggers, list):
        return out
    for entry in triggers:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        event_type = entry.get("event_type")
        exclude = entry.get("exclude")
        if (
            not isinstance(source, str)
            or not isinstance(event_type, str)
            or not isinstance(exclude, dict)
        ):
            continue

        matters = _ids(exclude, "matters")
        actors = _ids(exclude, "actors")
        if matters or actors:
            key = (source.strip().lower(), event_type.strip().lower())
            merged = out.setdefault(key, {"matters": frozenset(), "actors": frozenset()})
            merged["matters"] |= matters
            merged["actors"] |= actors
    return out


def _ids(exclude: dict[str, Any], key: str) -> frozenset[str]:
    raw = exclude.get(key)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(s.strip().lower() for s in raw if isinstance(s, str) and s.strip())


def check_excluded(
    *, route: str, body: bytes, exclusions: dict[tuple[str, str], dict[str, frozenset[str]]]
) -> str | None:
    """Return a suppression reason when the verified delivery matches an authored
    exclusion, else None (forward). Never raises; any surprise -> None."""
    if not exclusions:
        return None
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        event_type = next(
            (
                payload[k].strip().lower()
                for k in _EVENT_TYPE_KEYS
                if isinstance(payload.get(k), str) and payload[k].strip()
            ),
            None,
        )
        if event_type is None:
            return None
        rule = exclusions.get((route.strip().lower(), event_type))
        if rule is None:
            return None
        matter = next(
            (
                payload[k].strip().lower()
                for k in _MATTER_KEYS
                if isinstance(payload.get(k), str) and payload[k].strip()
            ),
            None,
        )
        if matter is not None and matter in rule["matters"]:
            return f"excluded-matter:{matter}"
        actor = next(
            (
                payload[k].strip().lower()
                for k in _ACTOR_KEYS
                if isinstance(payload.get(k), str) and payload[k].strip()
            ),
            None,
        )
        if actor is not None and actor in rule["actors"]:
            return f"excluded-actor:{actor}"
        return None
    except Exception:  # noqa: BLE001 — fail open to forward, never break the gate
        logger.warning("trigger-exclusions: check failed; forwarding", exc_info=True)
        return None


def live_exclusions() -> dict[tuple[str, str], dict[str, frozenset[str]]]:
    """Live-read the authored exclusions from customer.yaml per delivery (the
    ADR 0044 read-fresh posture, mirroring default_cap_resolver); any failure
    yields no exclusions (fail-open to forward)."""
    try:
        from shared.customer_config import CustomerConfig

        return resolve_exclusions(CustomerConfig.from_volume()._data)  # noqa: SLF001 — raw-dict seam; webhook_triggers has no typed accessor
    except Exception:  # noqa: BLE001
        return {}


__all__ = ["check_excluded", "live_exclusions", "resolve_exclusions"]
