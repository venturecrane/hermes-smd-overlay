"""Client-side event throttling for Sentry (ADR 0023 Wave 1; incident 2026-07-16).

Why this exists
---------------
On 2026-07-16 a root-owned ``profiles/operator/cron/jobs.json`` locked the
hermes-uid scheduler out of its own job DB. The scheduler retried on a ~90s
tick and logged two errors per tick; Hermes core's logging->Sentry integration
turned each into an event. In 48 hours that single fault emitted ~3,800 events
and consumed the organization's entire monthly error budget (5,000). Sentry
then dropped **all** events fleet-wide for the remainder of the billing period
— one seat's stuck cron blinded monitoring for every seat.

The failure mode is structural, not specific to cron: *any* error raised inside
a fixed-interval retry loop reports once per attempt, forever, at a rate set by
the loop rather than by how much an operator needs to know.

Policy
------
**Logarithmic suppression per issue.** The 1st, 2nd, 4th, 8th, 16th ...
occurrence of a given event key is sent; the rest are dropped. A 3,800-event
storm becomes 12 events. Signal is preserved rather than sampled away: every
emitted event carries ``smd.occurrence`` (the running total) and
``smd.suppressed_since_last``, so the Sentry issue reports the true scale of
what it is suppressing.

Rare errors are never throttled — the first two occurrences always go. Only
repetition is penalized, and it is penalized in proportion to how repetitive
it is.

**Quiet reset.** A key unseen for ``QUIET_RESET_SECONDS`` starts over, so a
*new* burst after a calm period is reported promptly instead of being masked by
an old counter.

**Global backstop.** Per-key suppression assumes keys are stable. An error
whose text embeds an identifier this module fails to normalize would mint a new
key per occurrence and slip through. A process-wide hourly ceiling bounds that
case. It is a net, not the primary mechanism, and it is deliberately generous
so it never bites healthy traffic.

The authoritative ceiling is server-side (per-DSN-key rate limits in Sentry's
project settings), which survives any bug in this file. This module is the
first of two layers, not the only one.

Design constraints
------------------
* **Fails open.** A throttle that raises lets the event through. Losing one
  event to a bug here is worse than sending one extra: this module must never
  become the reason an outage went unreported.
* **Bounded memory.** Long-lived gateway process; the key table is an LRU
  capped at ``MAX_TRACKED_KEYS``.
* **Thread-safe.** The gate and gateway are threaded.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("hermes_smd.sentry.throttle")

#: A key unseen for this long resets to occurrence 1 (a new burst reports promptly).
QUIET_RESET_SECONDS = 3600.0

#: Max distinct keys tracked. LRU-evicted. Bounds memory in a long-lived process.
MAX_TRACKED_KEYS = 512

#: Process-wide hourly ceiling — the backstop for key-cardinality explosion.
#: Override with ``SMD_SENTRY_MAX_EVENTS_PER_HOUR`` (0 disables the backstop).
DEFAULT_MAX_EVENTS_PER_HOUR = 60

#: Volatile substrings normalized out of the key so the same fault groups
#: together across occurrences. Order matters: UUID before hex before integer,
#: so the broader patterns cannot chew up a narrower one's match first.
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\b\d{2,}\b"), "<n>"),
    # Addresses from repr()-style text (``<object at 0x7f...>``) vary per run.
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
)


def normalize(text: str) -> str:
    """Strip run-varying tokens so repeated occurrences share one key.

    Paths, exception types, and prose are preserved — those are the signal.
    Only identifiers that differ between two occurrences of the *same* fault
    are collapsed.
    """
    if not text:
        return ""
    out = text
    for rx, placeholder in _NORMALIZERS:
        out = rx.sub(placeholder, out)
    return out[:200]


def event_key(event: dict[str, Any]) -> str:
    """Derive a stable grouping key for one event.

    Approximates Sentry's server-side fingerprint using what is available
    client-side: the logger, the exception type, and the normalized message or
    exception value.
    """
    parts: list[str] = [
        str(event.get("logger") or ""),
        str(event.get("level") or ""),
    ]

    exc = event.get("exception")
    if isinstance(exc, dict):
        values = exc.get("values") or []
        if isinstance(values, list) and values:
            last = values[-1]
            if isinstance(last, dict):
                parts.append(str(last.get("type") or ""))
                parts.append(normalize(str(last.get("value") or "")))

    message = event.get("message")
    if isinstance(message, str) and message:
        parts.append(normalize(message))

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        # The formatted message varies per occurrence; the template does not.
        template = logentry.get("message")
        if isinstance(template, str) and template:
            parts.append(normalize(template))

    return "|".join(parts)


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


class EventThrottle:
    """Logarithmic per-key suppression with a process-wide hourly backstop."""

    def __init__(
        self,
        *,
        quiet_reset_seconds: float = QUIET_RESET_SECONDS,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
        max_events_per_hour: int | None = None,
    ) -> None:
        self._quiet_reset = quiet_reset_seconds
        self._max_keys = max_tracked_keys
        if max_events_per_hour is None:
            max_events_per_hour = _env_int(
                "SMD_SENTRY_MAX_EVENTS_PER_HOUR", DEFAULT_MAX_EVENTS_PER_HOUR
            )
        self._max_per_hour = max_events_per_hour
        self._lock = threading.Lock()
        # key -> [occurrences_since_reset, last_seen_monotonic, occurrence_at_last_send]
        self._keys: OrderedDict[str, list[float]] = OrderedDict()
        self._window_start = time.monotonic()
        self._window_sent = 0

    def should_send(self, event: dict[str, Any]) -> tuple[bool, dict[str, int]]:
        """Decide whether one event is sent.

        Returns ``(send, annotations)``. ``annotations`` carries ``occurrence``
        and ``suppressed_since_last`` for events that are sent, so the emitted
        event reports the volume it stands for.
        """
        key = event_key(event)
        now = time.monotonic()

        with self._lock:
            entry = self._keys.get(key)
            if entry is None or (now - entry[1]) > self._quiet_reset:
                entry = [1.0, now, 0.0]
                self._keys[key] = entry
            else:
                entry[0] += 1
                entry[1] = now
            self._keys.move_to_end(key)

            while len(self._keys) > self._max_keys:
                self._keys.popitem(last=False)

            occurrence = int(entry[0])
            if not _is_power_of_two(occurrence):
                return False, {}

            # Power-of-two occurrence: a send candidate. Charge the backstop.
            if self._max_per_hour > 0:
                if (now - self._window_start) >= 3600.0:
                    self._window_start = now
                    self._window_sent = 0
                if self._window_sent >= self._max_per_hour:
                    # Do NOT advance occurrence_at_last_send: the suppressed
                    # count keeps accruing so the next event that gets through
                    # still reports everything it stands for.
                    return False, {}
                self._window_sent += 1

            suppressed = occurrence - int(entry[2]) - 1
            entry[2] = float(occurrence)

        return True, {
            "occurrence": occurrence,
            "suppressed_since_last": max(suppressed, 0),
        }


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("sentry: %s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value >= 0 else default


#: Process-wide throttle. One per process; the SDK calls ``before_send`` from
#: whichever thread captured the event.
_throttle = EventThrottle()


def throttle_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Apply the throttle to one event, annotating it when it passes.

    Returns ``None`` to drop. Fails open: any internal error returns the event
    unchanged rather than risk silencing monitoring.
    """
    try:
        send, annotations = _throttle.should_send(event)
        if not send:
            return None
        if annotations:
            extra = event.setdefault("extra", {})
            if isinstance(extra, dict):
                extra["smd.occurrence"] = annotations["occurrence"]
                extra["smd.suppressed_since_last"] = annotations["suppressed_since_last"]
        return event
    except Exception:  # noqa: BLE001 — a throttle bug must never silence monitoring
        logger.exception("sentry: throttle raised; passing event through")
        return event


def reset_for_tests() -> None:
    """Reset process-wide throttle state. Test-support only."""
    global _throttle  # noqa: PLW0603
    _throttle = EventThrottle()
