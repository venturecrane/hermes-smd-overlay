"""ULID, ISO-8601 UTC timestamps, and SHA-256 digesting — one source.

Consolidates the Crockford-base32 ULID generator, the millisecond-precision
ISO-8601 UTC timestamp formatter, and the payload digest helper that were
previously hand-copied into hermes-smd-audit/emit.py,
hermes-smd-webhook-router/__init__.py, and hermes-smd-trust/outbound.py.

A ULID is a 26-char string: 10 chars of millisecond timestamp + 16 chars of
randomness, Crockford-base32 encoded. Lexically sortable, no dashes, no
external dependencies.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import UTC, datetime

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def encode_crockford(value: int, length: int) -> str:
    """Encode ``value`` as a fixed-``length`` Crockford-base32 string."""
    out: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def ulid(now_ms: int | None = None) -> str:
    """Return a 26-char ULID. ``now_ms`` is injectable for deterministic tests."""
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(80)
    return encode_crockford(ts, 10) + encode_crockford(rand, 16)


def iso_utc(now: datetime | None = None) -> str:
    """ISO 8601 UTC with millisecond precision and explicit ``Z`` suffix.

    ``now`` is injectable for deterministic tests.
    """
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sha256(payload: bytes | None) -> str | None:
    """Hex SHA-256 of ``payload``, or ``None`` when there is nothing to digest."""
    if payload is None:
        return None
    return hashlib.sha256(payload).hexdigest()


__all__ = ["encode_crockford", "ulid", "iso_utc", "sha256"]
