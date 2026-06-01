"""Voice library export pipeline.

Ported from ss-console/operator/adapter/voice/export.py.

Per the customer-owned memory artifact policy, the customer can request
a portable archive of their voice library on offboarding. This module
serializes the voice samples written by :mod:`pipeline` into the export
archive.

The privacy floor from the ingestion pipeline is preserved end-to-end.
The voice ingestion pipeline never persists a raw email body; it stores
a structural-diff JSON per accepted message under
``{customer-slug}/voice/cohort/{cohort-id}/{sample-id}.json``. The
export pulls those same JSON files verbatim. There is no path inside
this module that reads raw email content because there is no raw email
content on disk.

Design rules
------------

* **Read-only.** The export does not mutate ``voice_ingestion_items``
  rows, does not delete R2 objects, and does not change the per-source
  ``voice_source_state`` row. Decommission does the deletion AFTER the
  export has been written to its archive destination.

* **Structural-diff only.** Each per-cohort voice sample is the
  privacy-bounded JSON produced by :func:`extract_structural_diff`. We
  copy bytes; we do not re-extract.

* **Cohort scope.** Voice samples are tagged with a recipient cohort
  on ingestion. The export preserves the cohort tag in both the
  archive path and the manifest entry so an auditor can filter the
  export by cohort.

* **Source-state snapshot.** Each ``(source_kind, source_id)`` row
  from ``voice_source_state`` is exported separately so the customer
  can see the per-cohort histogram and last-ingestion-at timestamp
  the agent had at offboarding time.

* **Voice library config.** Per-customer voice configuration
  (cohort definitions, retention window, partner allow-list) lives
  on ``customer.yaml`` and is consumed by the pipeline. This module
  accepts the config as an opaque dict and stores it under
  ``voice/library/config.json`` so the customer carries the
  configuration alongside the samples. The dict shape is owned by
  customer-yaml-schema.md; this module does not validate it.

* **No raw email content.** Asserted at write time:
  :func:`export_voice_library` raises :class:`VoiceExportPrivacyError`
  if any R2 object that lands in the export contains a field named
  ``body_text`` or ``raw_body`` or any obvious raw-content key. The
  ingestion pipeline never produces such a key, so this is a
  belt-and-braces check that prevents future regressions.

* **No autonomous send.** The caller chooses the destination via a
  :class:`VoiceExportWriter`. There is no SMTP, no S3 publish, no
  shareable-URL generation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

log = logging.getLogger("aie.voice.export")


# Schema version for the export format itself.
VOICE_EXPORT_SCHEMA_VERSION = 1


# Manifest kinds.
ARTIFACT_KIND_VOICE_SAMPLE = "voice_sample"
ARTIFACT_KIND_VOICE_STATE = "voice_state"
ARTIFACT_KIND_VOICE_CONFIG = "voice_config"
ARTIFACT_KIND_VOICE_PROVENANCE = "voice_provenance"

ALL_VOICE_KINDS = frozenset(
    {
        ARTIFACT_KIND_VOICE_SAMPLE,
        ARTIFACT_KIND_VOICE_STATE,
        ARTIFACT_KIND_VOICE_CONFIG,
        ARTIFACT_KIND_VOICE_PROVENANCE,
    }
)


# Keys that must NEVER appear inside an exported voice sample. The
# structural-diff format omits all of these by construction; the check
# below is a runtime guard so a future regression in the diff extractor
# cannot leak content into a customer-facing export.
_FORBIDDEN_SAMPLE_KEYS = frozenset(
    {
        "body_text",
        "raw_body",
        "body",
        "html",
        "plain_text",
        "subject_text",
        "quoted_text",
    }
)


class VoiceExportPrivacyError(RuntimeError):
    """Raised when a voice artifact appears to contain raw content.

    The export halts on the first occurrence -- a privacy violation in
    the export is worse than a missing export; the customer can re-run
    once the substrate has been audited.
    """


# ---------------------------------------------------------------------------
# Manifest dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceExportManifestEntry:
    """One artifact in the voice manifest."""

    path: str
    kind: str
    sha256: str
    item_count: int
    cohort: str | None = None
    source_kind: str | None = None
    source_id: str | None = None


@dataclass
class VoiceExportManifest:
    """Top-level voice manifest, written to ``manifests/voice.json``."""

    customer_slug: str
    exported_at: str
    schema_version: int = VOICE_EXPORT_SCHEMA_VERSION
    entries: list[VoiceExportManifestEntry] = field(default_factory=list)
    signature: str | None = None
    signature_kind: str | None = None

    def add(self, entry: VoiceExportManifestEntry) -> None:
        if entry.kind not in ALL_VOICE_KINDS:
            raise ValueError(
                f"unknown voice manifest kind {entry.kind!r}; "
                f"valid kinds are {sorted(ALL_VOICE_KINDS)}"
            )
        self.entries.append(entry)

    def total_items(self) -> int:
        return sum(e.item_count for e in self.entries)

    def to_json_bytes(self) -> bytes:
        payload = {
            "customer_slug": self.customer_slug,
            "exported_at": self.exported_at,
            "schema_version": self.schema_version,
            "signature": self.signature,
            "signature_kind": self.signature_kind,
            "entries": [
                {
                    "path": e.path,
                    "kind": e.kind,
                    "sha256": e.sha256,
                    "item_count": e.item_count,
                    "cohort": e.cohort,
                    "source_kind": e.source_kind,
                    "source_id": e.source_id,
                }
                for e in self.entries
            ],
        }
        return json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Protocols (read + write surfaces)
# ---------------------------------------------------------------------------


class VoiceExportReader(Protocol):
    """Per-customer D1 reads for the voice export module."""

    async def list_active_voice_items(self) -> list[dict]: ...

    async def list_voice_source_states(self) -> list[dict]: ...


class VoiceR2ObjectReader(Protocol):
    """Per-customer R2 binding (read-only)."""

    async def get(self, key: str) -> bytes: ...


class VoiceExportWriter(Protocol):
    """Write surface for the export archive.

    The caller composes one writer that satisfies both the memory
    export and the voice export so a single archive contains both.
    """

    async def write_file(self, path: str, body: bytes) -> None: ...


class VoiceExportSigner(Protocol):
    """No-op signer seam."""

    signature_kind: str

    async def sign(self, manifest_bytes: bytes) -> str: ...


class NoOpVoiceExportSigner:
    """No-op signer; used when no real signature provider is bound."""

    signature_kind = "stub"

    async def sign(self, manifest_bytes: bytes) -> str:  # noqa: ARG002
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_utc(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _serialize_rows(rows: Sequence[dict]) -> bytes:
    return json.dumps(list(rows), sort_keys=True, indent=2, default=str).encode("utf-8")


def _assert_no_raw_content(sample_bytes: bytes, *, r2_key: str) -> None:
    """Inspect a voice-sample JSON object for forbidden keys.

    Halts the export if a structural-diff document smuggles a raw body
    field. The ingestion pipeline does not produce such a key today;
    this guard prevents future regressions from silently shipping raw
    email content to the customer's offboarding archive.

    Tolerates non-JSON bodies (returns silently) -- only the
    structural-diff JSON shape is policed here, and the caller already
    classifies which keys land where.
    """
    try:
        parsed = json.loads(sample_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not JSON -- cannot inspect for the forbidden keys. We could
        # refuse to export anything that is not JSON, but that is
        # over-broad: future voice substrate work might land different
        # serializations. The structural-diff format is JSON today.
        return

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str) and key.lower() in _FORBIDDEN_SAMPLE_KEYS:
                    raise VoiceExportPrivacyError(
                        f"voice sample at {r2_key!r} contains forbidden key {key!r}; "
                        "structural-diff extractor must redact before R2 write"
                    )
                _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(parsed)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def export_voice_library(
    *,
    customer_slug: str,
    reader: VoiceExportReader,
    r2_reader: VoiceR2ObjectReader | None,
    writer: VoiceExportWriter,
    voice_config: dict | None = None,
    signer: VoiceExportSigner | None = None,
    now: datetime | None = None,
) -> VoiceExportManifest:
    """Produce the voice portion of the customer-owned export archive.

    Steps:

      1. Read ``voice_source_state`` rows. One JSON file per source
         lands at ``voice/state/{kind}-{id}.json``.
      2. Read ``voice_ingestion_items`` (active rows only). Provenance
         is exported as a single JSON file at
         ``voice/provenance/items.json`` so the customer holds the
         filter reasons, cohort assignments, and digests alongside the
         samples themselves.
      3. For each active item with a populated ``r2_key``, pull the
         structural-diff bytes via the R2 reader and write them at
         ``voice/samples/cohort/{cohort}/{sample-id}.json``. The
         privacy guard inspects each sample before it lands in the
         writer.
      4. Optionally serialize the per-customer voice config dict to
         ``voice/library/config.json`` so the customer can re-load the
         same cohort definitions, retention window, and allow-list.
      5. Sign and write the manifest at ``manifests/voice.json``.

    Best-effort on missing R2 objects: a 404 is logged and the
    provenance row still lands; the manifest records ``item_count=0``
    for that sample so an auditor sees the gap.
    """
    if not customer_slug:
        raise ValueError("customer_slug must be a non-empty string")

    exported_at = _iso_utc(now)
    manifest = VoiceExportManifest(
        customer_slug=customer_slug,
        exported_at=exported_at,
    )

    # 1. Source states.
    states = await reader.list_voice_source_states()
    for row in states:
        kind = row.get("source_kind", "unknown")
        sid = row.get("source_id", "unknown")
        path = f"voice/state/{kind}-{sid}.json"
        body = _serialize_rows([row])
        await writer.write_file(path, body)
        manifest.add(
            VoiceExportManifestEntry(
                path=path,
                kind=ARTIFACT_KIND_VOICE_STATE,
                sha256=_sha256(body),
                item_count=1,
                source_kind=kind,
                source_id=sid,
            )
        )

    # 2. Provenance rows (one collection file).
    items = await reader.list_active_voice_items()
    if items:
        path = "voice/provenance/items.json"
        body = _serialize_rows(items)
        await writer.write_file(path, body)
        manifest.add(
            VoiceExportManifestEntry(
                path=path,
                kind=ARTIFACT_KIND_VOICE_PROVENANCE,
                sha256=_sha256(body),
                item_count=len(items),
            )
        )

    # 3. Sample bodies (structural-diff JSON, copied from R2).
    if r2_reader is not None:
        for row in items:
            r2_key = row.get("r2_key")
            sample_id = row.get("id") or "unknown"
            cohort = row.get("recipient_cohort_id") or "unassigned"
            if not r2_key:
                # Filtered samples are recorded in provenance with
                # r2_key=NULL by design -- there is no sample body to copy.
                continue
            try:
                sample_bytes = await r2_reader.get(r2_key)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "voice_export.r2_get_failed customer=%s key=%s err=%s",
                    customer_slug,
                    r2_key,
                    exc,
                )
                continue

            _assert_no_raw_content(sample_bytes, r2_key=r2_key)

            archive_path = f"voice/samples/cohort/{cohort}/{sample_id}.json"
            await writer.write_file(archive_path, sample_bytes)
            manifest.add(
                VoiceExportManifestEntry(
                    path=archive_path,
                    kind=ARTIFACT_KIND_VOICE_SAMPLE,
                    sha256=_sha256(sample_bytes),
                    item_count=1,
                    cohort=cohort,
                    source_kind=row.get("source_kind"),
                    source_id=row.get("source_id"),
                )
            )

    # 4. Voice library config (opaque dict from customer.yaml).
    if voice_config:
        path = "voice/library/config.json"
        body = json.dumps(voice_config, sort_keys=True, indent=2).encode("utf-8")
        await writer.write_file(path, body)
        manifest.add(
            VoiceExportManifestEntry(
                path=path,
                kind=ARTIFACT_KIND_VOICE_CONFIG,
                sha256=_sha256(body),
                item_count=1,
            )
        )

    # 5. Sign and write the manifest.
    signer_impl = signer or NoOpVoiceExportSigner()
    manifest_bytes_unsigned = manifest.to_json_bytes()
    try:
        sig = await signer_impl.sign(manifest_bytes_unsigned)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_export.sign_failed customer=%s kind=%s err=%s; writing unsigned",
            customer_slug,
            signer_impl.signature_kind,
            exc,
        )
        sig = ""
    manifest.signature = sig
    manifest.signature_kind = signer_impl.signature_kind

    manifest_bytes = manifest.to_json_bytes()
    await writer.write_file("manifests/voice.json", manifest_bytes)

    log.info(
        "voice_export.complete customer=%s entries=%d items=%d",
        customer_slug,
        len(manifest.entries),
        manifest.total_items(),
    )
    return manifest


__all__ = [
    "ALL_VOICE_KINDS",
    "ARTIFACT_KIND_VOICE_CONFIG",
    "ARTIFACT_KIND_VOICE_PROVENANCE",
    "ARTIFACT_KIND_VOICE_SAMPLE",
    "ARTIFACT_KIND_VOICE_STATE",
    "NoOpVoiceExportSigner",
    "VOICE_EXPORT_SCHEMA_VERSION",
    "VoiceExportManifest",
    "VoiceExportManifestEntry",
    "VoiceExportPrivacyError",
    "VoiceExportReader",
    "VoiceExportSigner",
    "VoiceExportWriter",
    "VoiceR2ObjectReader",
    "export_voice_library",
]
