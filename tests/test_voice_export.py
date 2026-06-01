"""Tests for the ``hermes-smd-voice`` plugin's export module.

Ported from ss-console/operator/adapter/voice/tests/test_export.py.

Covers:

* Voice samples + provenance + state + config land at the right paths.
* The structural-diff bytes from R2 are copied verbatim -- no
  re-extraction, no mutation.
* The privacy guard raises on forbidden keys; the well-formed
  structural-diff format passes through unmodified.
* Missing R2 objects are logged + skipped, not fatal.
* Manifest entries include the cohort, sha256, and source pair.
* The signer seam runs by default; signature defaults to empty,
  signature_kind to "stub".
* Re-running with the same snapshot produces identical archive bytes
  for everything except the timestamp.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports inside ``__init__.py`` resolve."""
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


load_plugin("hermes-smd-voice")
import plugin_hermes_smd_voice.export as _export_mod  # noqa: E402

ARTIFACT_KIND_VOICE_CONFIG = _export_mod.ARTIFACT_KIND_VOICE_CONFIG
ARTIFACT_KIND_VOICE_PROVENANCE = _export_mod.ARTIFACT_KIND_VOICE_PROVENANCE
ARTIFACT_KIND_VOICE_SAMPLE = _export_mod.ARTIFACT_KIND_VOICE_SAMPLE
ARTIFACT_KIND_VOICE_STATE = _export_mod.ARTIFACT_KIND_VOICE_STATE
NoOpVoiceExportSigner = _export_mod.NoOpVoiceExportSigner
VOICE_EXPORT_SCHEMA_VERSION = _export_mod.VOICE_EXPORT_SCHEMA_VERSION
VoiceExportManifest = _export_mod.VoiceExportManifest
VoiceExportManifestEntry = _export_mod.VoiceExportManifestEntry
VoiceExportPrivacyError = _export_mod.VoiceExportPrivacyError
export_voice_library = _export_mod.export_voice_library


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _FakeReader:
    def __init__(self, *, states: list[dict], items: list[dict]) -> None:
        self._states = states
        self._items = items

    async def list_active_voice_items(self) -> list[dict]:
        return list(self._items)

    async def list_voice_source_states(self) -> list[dict]:
        return list(self._states)


class _FakeR2Reader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects
        self.calls: list[str] = []

    async def get(self, key: str) -> bytes:
        self.calls.append(key)
        if key not in self._objects:
            raise FileNotFoundError(key)
        return self._objects[key]


class _RecordingWriter:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def write_file(self, path: str, body: bytes) -> None:
        if path in self.files:
            raise AssertionError(f"duplicate write to {path!r}")
        self.files[path] = body


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# A representative structural-diff JSON (privacy-safe shape). The
# extractor that produces this is in plugins/hermes-smd-voice/diff.py.
_SAMPLE_JSON_PARTNERS = json.dumps(
    {
        "word_count": 47,
        "sentence_count": 5,
        "paragraph_count": 2,
        "sentence_length_distribution": {"0-5": 1, "5-10": 2, "10-20": 2},
        "greeting_style": "first_name",
        "signoff_style": "best",
        "opener_template": "first_name",
        "closer_template": "best",
        "punctuation_rhythm": {"period": 5, "comma": 7},
        "recipient_cohort": "partners",
        "schema_version": 1,
    },
    sort_keys=True,
).encode("utf-8")


_SAMPLE_JSON_CLIENTS = json.dumps(
    {
        "word_count": 102,
        "sentence_count": 9,
        "paragraph_count": 4,
        "sentence_length_distribution": {"5-10": 3, "10-20": 5, "20-35": 1},
        "greeting_style": "formal_named",
        "signoff_style": "sincerely",
        "opener_template": "formal_named",
        "closer_template": "sincerely",
        "punctuation_rhythm": {"period": 9, "comma": 14},
        "recipient_cohort": "clients",
        "schema_version": 1,
    },
    sort_keys=True,
).encode("utf-8")


@pytest.fixture
def voice_snapshot():
    states = [
        {
            "source_kind": "email",
            "source_id": "gmail",
            "last_ingestion_at": "2026-05-20T10:00:00.000Z",
            "last_success_at": "2026-05-20T10:00:00.000Z",
            "last_error": None,
            "ingest_status": "ok",
            "items_last_run": 2,
            "samples_by_cohort_json": '{"partners":1,"clients":1}',
            "schema_version": 1,
        }
    ]
    items = [
        {
            "id": "01H1",
            "source_kind": "email",
            "source_id": "gmail",
            "source_message_digest": "a" * 64,
            "recipient_cohort_id": "partners",
            "partner_authored": 1,
            "filter_reason": "accept",
            "ingested_at": "2026-05-20T10:00:00.000Z",
            "sent_at": "2026-05-19T15:00:00.000Z",
            "r2_key": "smd/voice/cohort/partners/01H1.json",
            "structural_diff_digest": "0" * 64,
            "word_count": 47,
            "schema_version": 1,
        },
        {
            "id": "01H2",
            "source_kind": "email",
            "source_id": "gmail",
            "source_message_digest": "b" * 64,
            "recipient_cohort_id": "clients",
            "partner_authored": 1,
            "filter_reason": "accept",
            "ingested_at": "2026-05-20T10:00:00.000Z",
            "sent_at": "2026-05-19T16:00:00.000Z",
            "r2_key": "smd/voice/cohort/clients/01H2.json",
            "structural_diff_digest": "1" * 64,
            "word_count": 102,
            "schema_version": 1,
        },
        # Filtered row (partner_authored=0, no r2_key): provenance only.
        {
            "id": "01H3",
            "source_kind": "email",
            "source_id": "gmail",
            "source_message_digest": "c" * 64,
            "recipient_cohort_id": "unassigned",
            "partner_authored": 0,
            "filter_reason": "agent_drafted",
            "ingested_at": "2026-05-20T10:00:00.000Z",
            "sent_at": "2026-05-19T17:00:00.000Z",
            "r2_key": None,
            "structural_diff_digest": None,
            "word_count": None,
            "schema_version": 1,
        },
    ]
    r2_objects = {
        "smd/voice/cohort/partners/01H1.json": _SAMPLE_JSON_PARTNERS,
        "smd/voice/cohort/clients/01H2.json": _SAMPLE_JSON_CLIENTS,
    }
    voice_config = {
        "voice_retention_days": 365,
        "cohorts": {"partners": "internal", "clients": "external"},
    }
    return states, items, r2_objects, voice_config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_writes_state_provenance_samples_config(voice_snapshot):
    states, items, r2_objects, voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
            voice_config=voice_config,
        )
    )

    assert "voice/state/email-gmail.json" in writer.files
    assert "voice/provenance/items.json" in writer.files
    assert "voice/samples/cohort/partners/01H1.json" in writer.files
    assert "voice/samples/cohort/clients/01H2.json" in writer.files
    assert "voice/library/config.json" in writer.files
    assert "manifests/voice.json" in writer.files

    # Filtered row's R2 key was None -- no sample lands for it.
    assert all(not p.startswith("voice/samples/cohort/unassigned/01H3") for p in writer.files)

    kinds = {entry.kind for entry in manifest.entries}
    assert ARTIFACT_KIND_VOICE_STATE in kinds
    assert ARTIFACT_KIND_VOICE_SAMPLE in kinds
    assert ARTIFACT_KIND_VOICE_PROVENANCE in kinds
    assert ARTIFACT_KIND_VOICE_CONFIG in kinds


def test_sample_bytes_are_copied_verbatim_from_r2(voice_snapshot):
    states, items, r2_objects, _voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
        )
    )

    assert writer.files["voice/samples/cohort/partners/01H1.json"] == _SAMPLE_JSON_PARTNERS
    assert writer.files["voice/samples/cohort/clients/01H2.json"] == _SAMPLE_JSON_CLIENTS


def test_privacy_guard_rejects_forbidden_keys(voice_snapshot):
    states, items, _r2_objects, _voice_config = voice_snapshot
    # Smuggle a body_text key into one sample -- the export must abort.
    leaked_bytes = json.dumps(
        {"word_count": 10, "body_text": "this is private"}, sort_keys=True
    ).encode("utf-8")
    r2_objects = {
        "smd/voice/cohort/partners/01H1.json": leaked_bytes,
        "smd/voice/cohort/clients/01H2.json": _SAMPLE_JSON_CLIENTS,
    }
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    with pytest.raises(VoiceExportPrivacyError):
        _run(
            export_voice_library(
                customer_slug="smd",
                reader=reader,
                r2_reader=r2,
                writer=writer,
            )
        )


def test_privacy_guard_walks_nested_dicts():
    """A forbidden key buried inside a nested object should still trip the guard."""
    sneaky = json.dumps(
        {
            "word_count": 5,
            "metadata": {
                "nested": {"raw_body": "leak"},
            },
        },
        sort_keys=True,
    ).encode("utf-8")
    states = [
        {
            "source_kind": "email",
            "source_id": "gmail",
            "last_ingestion_at": "2026-05-20T10:00:00.000Z",
            "last_success_at": None,
            "last_error": None,
            "ingest_status": "ok",
            "items_last_run": 1,
            "samples_by_cohort_json": "{}",
            "schema_version": 1,
        }
    ]
    items = [
        {
            "id": "01H9",
            "source_kind": "email",
            "source_id": "gmail",
            "source_message_digest": "d" * 64,
            "recipient_cohort_id": "partners",
            "partner_authored": 1,
            "filter_reason": "accept",
            "ingested_at": "2026-05-20T10:00:00.000Z",
            "sent_at": "2026-05-20T09:00:00.000Z",
            "r2_key": "smd/voice/cohort/partners/01H9.json",
            "structural_diff_digest": "0" * 64,
            "word_count": 5,
            "schema_version": 1,
        }
    ]
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader({"smd/voice/cohort/partners/01H9.json": sneaky})
    writer = _RecordingWriter()
    with pytest.raises(VoiceExportPrivacyError):
        _run(
            export_voice_library(
                customer_slug="smd",
                reader=reader,
                r2_reader=r2,
                writer=writer,
            )
        )


def test_missing_r2_object_is_skipped_not_fatal(voice_snapshot):
    states, items, _r2_objects, _voice_config = voice_snapshot
    # Only one object available; the other key 404s.
    partial_r2 = {
        "smd/voice/cohort/partners/01H1.json": _SAMPLE_JSON_PARTNERS,
    }
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(partial_r2)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
        )
    )

    assert "voice/samples/cohort/partners/01H1.json" in writer.files
    assert "voice/samples/cohort/clients/01H2.json" not in writer.files

    sample_entries = [e for e in manifest.entries if e.kind == ARTIFACT_KIND_VOICE_SAMPLE]
    paths = {e.path for e in sample_entries}
    assert "voice/samples/cohort/partners/01H1.json" in paths
    assert "voice/samples/cohort/clients/01H2.json" not in paths


def test_manifest_sha_matches_written_bytes(voice_snapshot):
    states, items, r2_objects, voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
            voice_config=voice_config,
        )
    )

    for entry in manifest.entries:
        if entry.path == "manifests/voice.json":
            continue
        assert entry.sha256 == _sha256(writer.files[entry.path])


def test_cohort_tag_propagates_into_manifest(voice_snapshot):
    states, items, r2_objects, _voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
        )
    )

    partner_entry = next(
        e for e in manifest.entries if e.path == "voice/samples/cohort/partners/01H1.json"
    )
    assert partner_entry.cohort == "partners"
    client_entry = next(
        e for e in manifest.entries if e.path == "voice/samples/cohort/clients/01H2.json"
    )
    assert client_entry.cohort == "clients"


def test_signer_seam_runs_and_records_kind(voice_snapshot):
    states, items, r2_objects, _voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
            signer=NoOpVoiceExportSigner(),
        )
    )

    assert manifest.signature == ""
    assert manifest.signature_kind == "stub"


def test_schema_version_recorded(voice_snapshot):
    states, items, r2_objects, _voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    manifest = _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
        )
    )
    assert manifest.schema_version == VOICE_EXPORT_SCHEMA_VERSION


def test_voice_export_is_read_only(voice_snapshot):
    states, items, r2_objects, _voice_config = voice_snapshot
    items_snapshot = json.dumps(items, sort_keys=True)
    states_snapshot = json.dumps(states, sort_keys=True)
    reader = _FakeReader(states=states, items=items)
    r2 = _FakeR2Reader(r2_objects)
    writer = _RecordingWriter()

    _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=r2,
            writer=writer,
        )
    )

    assert json.dumps(items, sort_keys=True) == items_snapshot
    assert json.dumps(states, sort_keys=True) == states_snapshot


def test_re_export_is_deterministic_with_fixed_clock(voice_snapshot):
    states, items, r2_objects, voice_config = voice_snapshot
    reader = _FakeReader(states=states, items=items)
    writer_a = _RecordingWriter()
    writer_b = _RecordingWriter()
    fixed = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

    _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=_FakeR2Reader(r2_objects),
            writer=writer_a,
            voice_config=voice_config,
            now=fixed,
        )
    )
    _run(
        export_voice_library(
            customer_slug="smd",
            reader=reader,
            r2_reader=_FakeR2Reader(r2_objects),
            writer=writer_b,
            voice_config=voice_config,
            now=fixed,
        )
    )

    assert set(writer_a.files.keys()) == set(writer_b.files.keys())
    for path in writer_a.files:
        assert writer_a.files[path] == writer_b.files[path], path


def test_manifest_entry_rejects_unknown_kind():
    manifest = VoiceExportManifest(
        customer_slug="smd",
        exported_at="2026-05-21T00:00:00.000Z",
    )
    with pytest.raises(ValueError):
        manifest.add(
            VoiceExportManifestEntry(
                path="voice/state/x.json",
                kind="not-a-kind",
                sha256="0" * 64,
                item_count=0,
            )
        )


def test_export_rejects_empty_customer_slug():
    reader = _FakeReader(states=[], items=[])
    writer = _RecordingWriter()
    with pytest.raises(ValueError):
        _run(
            export_voice_library(
                customer_slug="",
                reader=reader,
                r2_reader=None,
                writer=writer,
            )
        )
