"""Tests for hermes-smd-voice runtime binding (P0-1).

The plugin previously defined ``bind_runtime`` but nothing ever called it, so
both hooks silently no-op'd forever while register() reported success — the
fail-silent anti-pattern. These tests pin the fix:

  * register() with R2 env present → runtime BOUND, pre_llm_call actually
    injects a sample block (end-to-end, against a fake R2 reader).
  * register() with R2 env absent → hooks still registered, but runtime
    UNBOUND and a WARNING is emitted (never a silent healthy no-op).
  * reader_from_env() resolves only when all R2 vars are set.
"""

from __future__ import annotations

import inspect

import pytest

from tests.conftest import load_plugin

_R2_ENV = {
    "SMD_CUSTOMER_SLUG": "acme",
    "R2_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
    "R2_ACCESS_KEY_ID": "ak",
    "R2_SECRET_ACCESS_KEY": "sk",
    "R2_BUCKET_CONFIG": "acme-config",
}


@pytest.fixture
def voice():
    """Load the plugin fresh and reset its module-level binding between tests."""
    mod = load_plugin("hermes-smd-voice")
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None
    yield mod
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None


def test_reader_from_env_requires_all_vars(voice, monkeypatch):
    samples = voice.samples
    for k in _R2_ENV:
        monkeypatch.delenv(k, raising=False)
    assert samples.reader_from_env() is None
    # only some set → still None
    monkeypatch.setenv("R2_ENDPOINT_URL", "x")
    assert samples.reader_from_env() is None
    # all set → a reader
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_BUCKET_CONFIG", "b")
    reader = samples.reader_from_env()
    assert isinstance(reader, samples.R2VaultSampleReader)


def test_register_without_r2_env_is_inactive_but_loud(voice, fake_ctx, monkeypatch, caplog):
    for k in _R2_ENV:
        monkeypatch.delenv(k, raising=False)
    with caplog.at_level("WARNING"):
        voice.register(fake_ctx)
    # hooks ARE registered (so the surface is correct)...
    assert "pre_llm_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered
    # ...but the runtime is NOT bound, and that fact is logged loudly.
    assert voice._R2_READER is None
    assert any("INACTIVE" in r.message for r in caplog.records)


def test_register_with_r2_env_binds_runtime(voice, fake_ctx, monkeypatch):
    for k, v in _R2_ENV.items():
        monkeypatch.setenv(k, v)
    voice.register(fake_ctx)
    assert voice._CUSTOMER_SLUG == "acme"
    assert voice._R2_READER is not None  # actually bound — not a silent no-op


def test_registered_hooks_match_synchronous_hermes_dispatcher(voice):
    assert not inspect.iscoroutinefunction(voice.on_pre_llm_call)
    assert not inspect.iscoroutinefunction(voice.on_post_llm_call)


class _FakeR2Reader:
    """In-memory R2SampleReader for end-to-end hook exercise."""

    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._objects if k.startswith(prefix)]

    def get(self, key: str) -> bytes:
        return self._objects[key]


def test_pre_llm_call_injects_samples_when_bound(voice):
    """End-to-end: a bound plugin actually returns a context block — proving
    the hook does real work, not the old silent no-op."""
    import json

    sample = {
        "greeting_style": "first_name",
        "signoff_style": "thanks",
        "avg_sentence_length": 12.0,
        "recipient_cohort": "clients",
    }
    reader = _FakeR2Reader(
        {"acme/voice/cohort/clients/s1.json": json.dumps(sample).encode("utf-8")}
    )
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)

    result = voice.on_pre_llm_call(session_id="s", user_message="hi")
    assert result is not None
    assert "context" in result
    assert "author voice profile" in result["context"]


def test_pre_llm_call_noop_when_unbound(voice):
    """Unbound → None (no context). The no-op path still exists; the fix is
    that register() now BINDS when it can, so this path is the genuine
    misconfigured-Machine case, not the default."""
    result = voice.on_pre_llm_call(session_id="s", user_message="hi")
    assert result is None


# --- Local vault reader (OP-P0-2: agent holds no R2 credential for voice) ------


def test_reader_from_env_prefers_local_vault_dir(voice, monkeypatch, tmp_path):
    """SMD_VOICE_VAULT_DIR (the boot-synced mirror) wins over R2 env, so the
    agent reads voice samples without any R2 credential."""
    samples = voice.samples
    for k, v in _R2_ENV.items():
        monkeypatch.setenv(k, v)  # even with R2 env present...
    monkeypatch.setenv("SMD_VOICE_VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    reader = samples.reader_from_env()
    assert isinstance(reader, samples.LocalVaultSampleReader)


def test_reader_from_env_local_dir_must_exist(voice, monkeypatch, tmp_path):
    """A configured-but-missing vault dir does not shadow the R2 fallback."""
    samples = voice.samples
    for k, v in _R2_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SMD_VOICE_VAULT_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    reader = samples.reader_from_env()
    assert isinstance(reader, samples.R2VaultSampleReader)


def test_local_reader_round_trips_keys_and_cohort(voice, tmp_path):
    """list_keys/get return the SAME slug-relative key shape as the R2 reader,
    so _cohort_from_key and the retrieval path work unchanged."""
    import json

    samples = voice.samples
    # Mirror the boot-synced layout: <vault>/cohort/<cohort>/<id>.json
    cohort_dir = tmp_path / "cohort" / "clients"
    cohort_dir.mkdir(parents=True)
    sample = {"greeting_style": "first_name", "recipient_cohort": "clients"}
    (cohort_dir / "s1.json").write_text(json.dumps(sample), encoding="utf-8")

    reader = samples.LocalVaultSampleReader(base_dir=str(tmp_path), customer_slug="acme")
    keys = reader.list_keys("acme/voice/cohort/")
    assert keys == ["acme/voice/cohort/clients/s1.json"]
    assert samples._cohort_from_key(keys[0]) == "clients"
    assert json.loads(reader.get(keys[0])) == sample


def test_local_reader_empty_dir_returns_no_keys(voice, tmp_path):
    """An empty/absent vault (the common unconfigured-voice case) yields no
    keys rather than raising — the plugin stays quietly inactive."""
    samples = voice.samples
    reader = samples.LocalVaultSampleReader(base_dir=str(tmp_path), customer_slug="acme")
    assert reader.list_keys("acme/voice/cohort/") == []


def test_pre_llm_call_injects_samples_from_local_vault(voice, tmp_path):
    """End-to-end through the local reader: a bound plugin returns a context
    block — proving the OP-P0-2 no-R2-credential path actually drives voice."""
    import json

    cohort_dir = tmp_path / "cohort" / "clients"
    cohort_dir.mkdir(parents=True)
    sample = {
        "greeting_style": "first_name",
        "signoff_style": "thanks",
        "avg_sentence_length": 12.0,
        "recipient_cohort": "clients",
    }
    (cohort_dir / "s1.json").write_text(json.dumps(sample), encoding="utf-8")

    reader = voice.samples.LocalVaultSampleReader(
        base_dir=str(tmp_path), customer_slug="acme"
    )
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    result = voice.on_pre_llm_call(session_id="s", user_message="hi")
    assert result is not None
    assert "author voice profile" in result["context"]
