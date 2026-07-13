"""Per-(trigger, matter) cooldown at the webhook gate — the deterministic
self-sustainment break for write-then-echo loops (ss-console #1781).

The live incident (pilot-smokeball, 2026-07-06→07): the seat's own
``create_memo`` write echoes back ~12 min later as a ``matter.updated``
delivery, which wakes the memo skill, which writes another memo — a
self-sustaining (and, under close-arriving deliveries, branching) loop that
only Smokeball's webhook latency throttled. The skill's assumed structural
break ("a memo write never emits ``matter.updated``") is disproven by the
audit ledger, and the authored actor exclusion
(:mod:`shared.gate_trigger_exclusions`) cannot match when the vendor omits
``userId`` (Smokeball documents that case). This module is the layer that
holds regardless: after a delivery for a matter forwards, further deliveries
for the SAME (source, event_type, matter) within the cooldown window are
parked — acknowledged 202, audited ``WEBHOOK_SUPPRESSED``, never forwarded,
zero agent turns. The echo therefore never wakes the agent, so it never
writes the next memo, and the chain terminates after at most one wake per
window.

Config: per ``webhook_triggers[]`` entry, an optional block::

    webhook_triggers:
      - source: smokeball
        event_type: matter.updated
        skill: matter-memo-on-update
        throttle:
          cooldown_minutes: 30    # 0 disables the throttle for this trigger

Unauthored triggers get the platform default (30 min) — an integrity
control in the ADR 0035 sense, same footing as the inbound daily cap's
platform default (:mod:`shared.gate_inbound_cap`): it only narrows wakes,
never widens capability, and defaulting it OFF would let every future seat
rediscover the loop on a client's production matter. Malformed authored
values fail toward the default, never toward "disabled".

Semantics and posture:

- The throttle keys on the same matter-id extraction as the exclusions
  module (``id``/``matterId``, top level and nested under ``payload`` —
  the verbatim live Smokeball envelope). A delivery with NO extractable
  matter id is never throttled (there is nothing to key a window on; the
  route-level ADR 0062 daily cap bounds that path).
- Only deliveries that actually FORWARD start a window: the caller records
  after every other gate check has passed, so a suppressed/parked delivery
  never extends its own window.
- Suppression is never silent: the gate writes the same
  ``WEBHOOK_SUPPRESSED`` audit row as authored exclusions, reason
  ``trigger-cooldown:<matter-id>``.
- State is in-memory per gate process (mirrors the replay guard). A gate
  restart clears windows; the worst case is one extra wake per restart
  before the loop re-parks — bounded, and vastly simpler than persisting
  sub-hour state.
- FAIL-OPEN on parse/config surprises (malformed payload, config read
  failure): the delivery forwards. The throttle is a loop-breaker, not
  load-bearing safety; the dangerous failure mode is a typo silently
  killing a live chain (same posture as gate_trigger_exclusions).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 30 * 60

_EVENT_TYPE_KEYS = ("event_type", "type", "event")
_MATTER_KEYS = ("id", "matterId")


def resolve_throttles(config: Any) -> dict[tuple[str, str], int]:
    """Extract ``{(source, event_type): cooldown_seconds}`` for every authored
    ``webhook_triggers[]`` entry. Entries without a ``throttle`` block (or with
    a malformed one) get :data:`DEFAULT_COOLDOWN_SECONDS`; an authored
    ``cooldown_minutes: 0`` disables the throttle for that trigger. Triggers
    that are not authored at all are absent from the map (an unauthored
    (source, event_type) never wakes the agent anyway — the router has no
    target)."""
    out: dict[tuple[str, str], int] = {}
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
        if not isinstance(source, str) or not isinstance(event_type, str):
            continue
        key = (source.strip().lower(), event_type.strip().lower())
        cooldown = _cooldown_seconds(entry.get("throttle"))
        # Two authored entries for the same (source, event_type) keep the
        # LARGER window — the conservative merge (narrowing, never widening).
        out[key] = max(out.get(key, 0), cooldown) if key in out else cooldown
    return out


def _cooldown_seconds(throttle: Any) -> int:
    if throttle is None:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        if not isinstance(throttle, dict):
            raise TypeError(f"throttle must be a mapping; got {type(throttle).__name__}")
        raw = throttle.get("cooldown_minutes")
        if raw is None:
            return DEFAULT_COOLDOWN_SECONDS
        if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
            raise TypeError("cooldown_minutes must be an integer")
        minutes = int(raw)
        if minutes < 0:
            raise ValueError("cooldown_minutes must be >= 0")
        return minutes * 60
    except Exception as exc:  # noqa: BLE001 — malformed fails toward the default
        logger.warning(
            "trigger-throttle: invalid throttle block; using default %ss: %s",
            DEFAULT_COOLDOWN_SECONDS,
            exc,
        )
        return DEFAULT_COOLDOWN_SECONDS


def _extract(body: bytes) -> tuple[str | None, str | None]:
    """(event_type, matter_id) from the verified payload, or Nones. Same
    envelope handling as gate_trigger_exclusions: matter id may live at the
    top level or nested under ``payload`` (the verbatim live Smokeball
    envelope); first present candidate wins for KEYING (unlike exclusion
    matching, a window needs exactly one key)."""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None, None
        event_type = next(
            (
                payload[k].strip().lower()
                for k in _EVENT_TYPE_KEYS
                if isinstance(payload.get(k), str) and payload[k].strip()
            ),
            None,
        )
        candidates: list[dict[str, Any]] = [payload]
        nested = payload.get("payload")
        if isinstance(nested, dict):
            # Nested first: the live envelope's top-level ``id`` is the
            # DELIVERY id, not the matter (proven by signed probes
            # 2026-07-07); ``payload.id`` is the matter.
            candidates.insert(0, nested)
        matter = next(
            (
                obj[k].strip().lower()
                for obj in candidates
                for k in _MATTER_KEYS
                if isinstance(obj.get(k), str) and obj[k].strip()
            ),
            None,
        )
        return event_type, matter
    except Exception:  # noqa: BLE001 — fail open (no key, no throttle)
        return None, None


class TriggerThrottle:
    """Per-gate-process cooldown windows. Thread-safe; windows prune lazily."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str, str], float] = {}

    def check(
        self,
        *,
        route: str,
        body: bytes,
        throttles: dict[tuple[str, str], int],
        now: float | None = None,
    ) -> str | None:
        """Return a suppression reason when the delivery falls inside an open
        cooldown window, else None (forward). A None return RECORDS the new
        window — call this only on the final pre-forward check so parked or
        excluded deliveries never extend a window. Never raises."""
        try:
            if not throttles:
                return None
            event_type, matter = _extract(body)
            if event_type is None or matter is None:
                return None
            cooldown = throttles.get((route.strip().lower(), event_type))
            if not cooldown:  # unauthored trigger or authored 0 → disabled
                return None
            reference = time.time() if now is None else now
            key = (route.strip().lower(), event_type, matter)
            with self._lock:
                expired = [k for k, exp in self._windows.items() if exp <= reference]
                for k in expired:
                    del self._windows[k]
                open_until = self._windows.get(key)
                if open_until is not None and open_until > reference:
                    return f"trigger-cooldown:{matter}"
                self._windows[key] = reference + cooldown
                return None
        except Exception:  # noqa: BLE001 — fail open to forward, never break the gate
            logger.warning("trigger-throttle: check failed; forwarding", exc_info=True)
            return None


def live_throttles() -> dict[tuple[str, str], int]:
    """Live-read the authored throttles from customer.yaml per delivery (the
    ADR 0044 read-fresh posture, mirroring live_exclusions). A failed config
    read yields no throttles (fail-open to forward) and logs loudly."""
    try:
        from shared.customer_config import CustomerConfig

        return resolve_throttles(CustomerConfig.from_volume()._data)  # noqa: SLF001 — raw-dict seam; webhook_triggers has no typed accessor
    except Exception:  # noqa: BLE001
        logger.warning("trigger-throttle: live config read failed; no throttles", exc_info=True)
        return {}


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "TriggerThrottle",
    "live_throttles",
    "resolve_throttles",
]
