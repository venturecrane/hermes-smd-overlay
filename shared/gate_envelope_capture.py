"""Verbatim envelope capture at the gate — the instrument for measuring what a
vendor delivery actually carries.

WHY THIS EXISTS (ss-console, 2026-08-13). The gate could not answer what is in a
delivery it suppressed. Its structural log is ``keys/markers only — never body
content`` (``_stamp_source``), and ``_audit_suppression`` records reason/route/
request_id and nothing else. The only verbatim Smokeball envelope either repo
has came from a *forwarded* delivery's session transcript — so a **suppressed**
delivery has always been unobservable.

That gap is not academic. To decide whether the seat's own writes can be told
apart from a human's in-app edit, you must compare two envelopes: the echo of an
Operator write (suppressed by the throttle) and a human's edit (also suppressed,
if it lands inside the same window). The one measurement that matters is the one
the instrument could not see — the venture's own "a check that cannot fail has
measured nothing", applied to an instrument rather than a control.

## Two independent conditions, both required

1. ``SMD_GATE_ENVELOPE_CAPTURE_DIR`` is set. Unset (the default, everywhere)
   means this module does nothing at all and costs one dict lookup.
2. The live ``customer.yaml`` authors ``seat.kind: proving``.

Condition 2 is read fresh per delivery and **fails CLOSED** — the inverse of the
suppression modules beside it, deliberately. There, a failed config read forwards
the delivery, because the dangerous outcome is silently killing a live chain.
Here, the dangerous outcome is writing verified vendor bodies — client content —
to a new location on a seat nobody meant to instrument. So an unreadable config,
an unauthored ``seat`` block, or any value other than ``proving`` captures
nothing. ``CustomerConfig.seat`` documents exactly this posture: *"Callers
deciding blast radius should treat an unauthored seat with customer-grade caution
rather than assuming it is a proving rig."*

## What it never does

- **Never captures an unverified body.** The call site sits after signature
  verification. An attacker who cannot forge a signature cannot write here.
- **Never grows without bound.** ``max_files`` stops the run; oversize bodies are
  truncated with an explicit marker rather than dropped silently, so a truncated
  capture can never be mistaken for a short envelope.
- **Never raises.** Every failure path returns quietly. An instrument that can
  break the gate it observes is worse than no instrument.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

CAPTURE_DIR_ENV = "SMD_GATE_ENVELOPE_CAPTURE_DIR"
MAX_FILES_ENV = "SMD_GATE_ENVELOPE_CAPTURE_MAX_FILES"
MAX_BYTES_ENV = "SMD_GATE_ENVELOPE_CAPTURE_MAX_BYTES"

_DEFAULT_MAX_FILES = 50
_DEFAULT_MAX_BYTES = 65_536

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _int_env(name: str, default: int) -> int:
    """Read a positive int from the environment; any surprise yields the default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value > 0 else default


def _safe(token: object, fallback: str) -> str:
    text = _UNSAFE.sub("-", str(token or "")).strip("-")
    return text[:64] or fallback


def seat_is_proving(config: Any) -> bool:
    """Is this seat authored ``seat.kind: proving``?

    Fails CLOSED on every ambiguity: a non-mapping config, an absent or
    non-mapping ``seat`` block, a missing ``kind``, or any value that is not
    exactly ``proving`` (case-insensitively, whitespace-trimmed) returns False.
    """
    if not isinstance(config, dict):
        return False
    seat = config.get("seat")
    if not isinstance(seat, dict):
        return False
    kind = seat.get("kind")
    if not isinstance(kind, str):
        return False
    return kind.strip().lower() == "proving"


def _live_seat_is_proving() -> bool:
    """Read ``seat.kind`` fresh from the volume config (ADR 0044 read-fresh, the
    posture ``live_exclusions`` uses). Any failure fails CLOSED — no capture."""
    try:
        from shared.customer_config import CustomerConfig

        return seat_is_proving(CustomerConfig.from_volume()._data)  # noqa: SLF001 — raw-dict seam, as live_exclusions
    except Exception:  # noqa: BLE001 — an unreadable config must not enable capture
        logger.warning("envelope-capture: live config read failed; not capturing", exc_info=True)
        return False


def build_record(
    *, route: str, request_id: str, body: bytes, max_bytes: int, now: float
) -> dict[str, Any]:
    """The captured record. Verbatim body as text when it decodes as UTF-8, else
    base64 — never a lossy transcription. Truncation is always marked."""
    truncated = len(body) > max_bytes
    kept = body[:max_bytes] if truncated else body
    record: dict[str, Any] = {
        "captured_at": now,
        "route": route,
        "request_id": request_id,
        "body_bytes": len(body),
        "truncated": truncated,
    }
    try:
        record["body"] = kept.decode("utf-8")
        record["body_encoding"] = "utf-8"
    except UnicodeDecodeError:
        record["body"] = base64.b64encode(kept).decode("ascii")
        record["body_encoding"] = "base64"
    return record


def capture(*, route: str, request_id: str, body: bytes, now: float | None = None) -> str | None:
    """Write one verbatim envelope, or return None having done nothing.

    Returns the written path (tests and gate logs use it); None whenever capture
    is disabled, the seat is not a proving rig, the cap is reached, or anything
    at all goes wrong.
    """
    try:
        directory = os.environ.get(CAPTURE_DIR_ENV)
        if not directory or not directory.strip():
            return None
        if not _live_seat_is_proving():
            return None

        directory = directory.strip()
        os.makedirs(directory, mode=0o700, exist_ok=True)

        max_files = _int_env(MAX_FILES_ENV, _DEFAULT_MAX_FILES)
        existing = [n for n in os.listdir(directory) if n.endswith(".json")]
        if len(existing) >= max_files:
            logger.info(
                "envelope-capture: cap reached (%d files); not capturing %s/%s",
                max_files,
                route,
                request_id,
            )
            return None

        reference = time.time() if now is None else now
        record = build_record(
            route=route,
            request_id=request_id,
            body=body,
            max_bytes=_int_env(MAX_BYTES_ENV, _DEFAULT_MAX_BYTES),
            now=reference,
        )

        name = f"{reference:.6f}-{_safe(route, 'route')}-{_safe(request_id, 'req')}.json"
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(record, sort_keys=True).encode("utf-8"))
        finally:
            os.close(fd)
        logger.info("envelope-capture: wrote %s (%d bytes)", name, record["body_bytes"])
        return path
    except Exception:  # noqa: BLE001 — an instrument must never break the gate it observes
        logger.warning("envelope-capture: failed; continuing", exc_info=True)
        return None


__all__ = [
    "CAPTURE_DIR_ENV",
    "MAX_BYTES_ENV",
    "MAX_FILES_ENV",
    "build_record",
    "capture",
    "seat_is_proving",
]
