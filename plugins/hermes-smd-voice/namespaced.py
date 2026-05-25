"""Factory helper that wires `VoiceIngestionRunner` against namespaced R2.

Ported from ss-console/ai-employee/adapter/voice/namespaced.py with the
audit-log and cross-module namespace-assertion dependencies stripped
out — those live elsewhere in the overlay and aren't required by the
runtime voice path.

The voice pipeline's `R2Client` Protocol exposes
`put(key, body, content_type)` and `delete(key)` plus a `customer_slug`
attribute. The namespace-assertion wrapper uses
`put_object(key, body, *, content_type)` and `delete_object(key)` —
close but not identical, so a thin bridge adapter glues them together
without touching the pipeline.

`build_namespaced_voice_runner` is the public entry point. The Hermes
fork's per-customer Machine boot path calls this instead of constructing
`VoiceIngestionRunner` directly with raw R2 — every put + delete from
the runner then routes through the namespace assertion before hitting
the raw client.

TODO(shared): When shared/namespace_assertion lands in this overlay,
re-import the real ``NamespacedR2Client`` here and drop the inline
``_AssertingR2Wrapper``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from .filter import AuditDigestLookup
from .pipeline import (
    CohortResolver,
    CursorStore,
    EmailSource,
    VoiceIngestionRunner,
)
from .state import VoiceSourceStateStore

log = logging.getLogger("aie.voice.namespaced")


class NamespaceAssertionError(RuntimeError):
    """Raised when an R2 key crosses the customer-slug boundary.

    The voice pipeline names every key with the bound customer slug as
    the leading path segment. If a foreign slug ever appears, that is a
    fatal isolation breach — the runner refuses the write rather than
    silently leaking data into the wrong tenant's vault.
    """


class RawR2Client(Protocol):
    """The raw R2 client interface — exactly what the namespace wrapper wraps.

    The fork's overlay constructs one of these per customer Machine. The
    factory below wraps it with the asserting wrapper and then bridges
    that wrapper onto the pipeline's `R2Client.put` / `R2Client.delete`
    shape.
    """

    async def put_object(self, key: str, body: bytes, *, content_type: str) -> None: ...
    async def get_object(self, key: str) -> bytes: ...
    async def delete_object(self, key: str) -> None: ...


class _AssertingR2Wrapper:
    """Wraps a raw R2 client with a per-customer slug assertion.

    Inline mirror of ``ss-console/ai-employee/adapter/namespace_assertion.NamespacedR2Client``;
    kept narrow so the voice plugin can run without dragging the entire
    namespace-assertion module into the overlay. When the shared module
    lands here, this class is deleted in favor of the import.
    """

    def __init__(self, *, expected_slug: str, inner: RawR2Client) -> None:
        if not expected_slug:
            raise ValueError("expected_slug must be a non-empty string")
        self._expected_slug = expected_slug
        self._inner = inner

    def _assert_key(self, key: str) -> None:
        if not key:
            raise NamespaceAssertionError("empty R2 key")
        leading = key.split("/", 1)[0]
        if leading != self._expected_slug:
            log.error(
                "voice.namespace_breach expected=%s saw=%s key=%s",
                self._expected_slug,
                leading,
                key,
            )
            raise NamespaceAssertionError(
                f"R2 key {key!r} does not match expected customer slug {self._expected_slug!r}"
            )

    async def put_object(self, key: str, body: bytes, *, content_type: str) -> None:
        self._assert_key(key)
        await self._inner.put_object(key, body, content_type=content_type)

    async def get_object(self, key: str) -> bytes:
        self._assert_key(key)
        return await self._inner.get_object(key)

    async def delete_object(self, key: str) -> None:
        self._assert_key(key)
        await self._inner.delete_object(key)


class _NamespacedVoiceR2Bridge:
    """Implements the voice pipeline's `R2Client` Protocol via the namespace wrapper.

    `customer_slug` is required by the voice pipeline's R2Client Protocol;
    it is read from the wrapper's bound slug so the two cannot drift.
    """

    def __init__(self, *, r2: _AssertingR2Wrapper, customer_slug: str) -> None:
        self._r2 = r2
        self.customer_slug = customer_slug

    async def put(self, key: str, body: bytes, content_type: str) -> None:
        await self._r2.put_object(key, body, content_type=content_type)

    async def delete(self, key: str) -> None:
        await self._r2.delete_object(key)


def build_namespaced_voice_runner(
    *,
    customer_slug: str,
    source: EmailSource,
    cohort_resolver: CohortResolver,
    raw_r2: RawR2Client,
    state_store: VoiceSourceStateStore,
    cursor_store: CursorStore,
    audit_lookup: AuditDigestLookup,
    source_kind: str = "email",
    clock: Callable[[], datetime] | None = None,
) -> VoiceIngestionRunner:
    """Return a `VoiceIngestionRunner` wired through namespace-asserting R2.

    The Hermes fork's per-customer Machine boot path should call this
    factory instead of constructing `VoiceIngestionRunner` directly with
    raw R2. Every R2 put + delete from the runner is routed through the
    namespace assertion before it hits the raw client, so a foreign-slug
    key refuses at the boundary.
    """
    r2 = _AssertingR2Wrapper(expected_slug=customer_slug, inner=raw_r2)
    bridge = _NamespacedVoiceR2Bridge(r2=r2, customer_slug=customer_slug)
    return VoiceIngestionRunner(
        source=source,
        cohort_resolver=cohort_resolver,
        r2_client=bridge,  # type: ignore[arg-type]  # bridge implements R2Client
        state_store=state_store,
        cursor_store=cursor_store,
        audit_lookup=audit_lookup,
        source_kind=source_kind,
        _clock=clock,
    )


__all__ = [
    "NamespaceAssertionError",
    "RawR2Client",
    "build_namespaced_voice_runner",
]
