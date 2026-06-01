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

import asyncio

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


class _FakeR2Reader:
    """In-memory R2SampleReader for end-to-end hook exercise."""

    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    async def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._objects if k.startswith(prefix)]

    async def get(self, key: str) -> bytes:
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

    result = asyncio.run(voice.on_pre_llm_call(session_id="s", user_message="hi"))
    assert result is not None
    assert "context" in result
    assert "voice samples" in result["context"]


def test_pre_llm_call_noop_when_unbound(voice):
    """Unbound → None (no context). The no-op path still exists; the fix is
    that register() now BINDS when it can, so this path is the genuine
    misconfigured-Machine case, not the default."""
    result = asyncio.run(voice.on_pre_llm_call(session_id="s", user_message="hi"))
    assert result is None
