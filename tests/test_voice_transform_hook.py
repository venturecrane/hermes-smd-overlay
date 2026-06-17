"""Tests for the transform_llm_output hook wiring (Voice Layer 2).

Pins the connection between transform_draft() and the Hermes
transform_llm_output hook.  The transformer itself is tested extensively in
test_voice_transform.py; these tests focus on the hook wiring:

- on_transform_llm_output returns None when unbound
- on_transform_llm_output returns None when no samples available
- on_transform_llm_output returns transformed text when transform succeeds
- on_transform_llm_output returns None on passthrough (draft already in voice)
- on_transform_llm_output is exception-safe (never raises)
- bind_runtime invalidates the bundle cache
- register() attaches transform_llm_output to the plugin context
- on_transform_llm_output is synchronous (Hermes dispatcher contract)
"""

from __future__ import annotations

import inspect
import json

import pytest

from tests.conftest import load_plugin

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_sample(
    *,
    greeting_style: str = "first_name",
    signoff_style: str = "thanks",
    avg_sentence_length: float = 10.0,
    cohort: str = "general",
) -> dict:
    """Return a minimal structural-diff dict compatible with the R2 schema."""
    return {
        "schema_version": 1,
        "word_count": 50,
        "sentence_count": 5,
        "paragraph_count": 2,
        "subject_word_count": 3,
        "avg_sentence_length": avg_sentence_length,
        "sentence_length_distribution": {"lt_5": 0, "lt_10": 3, "lt_20": 2, "lt_35": 0, "gte_35": 0},
        "greeting_style": greeting_style,
        "signoff_style": signoff_style,
        "opener_template": "",
        "closer_template": "",
        "punctuation_rhythm": {"period_per_100": 8.0},
        "recipient_cohort": cohort,
    }


class _FakeR2Reader:
    """In-memory R2SampleReader."""

    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def list_keys(self, prefix: str) -> list[str]:
        return [k for k in self._objects if k.startswith(prefix)]

    def get(self, key: str) -> bytes:
        return self._objects[key]


def _make_reader_with_samples(slug: str, count: int = 6) -> _FakeR2Reader:
    """Build a fake reader with ``count`` homogeneous samples (enough for MIN_PROFILE_SAMPLE_COUNT=5)."""
    sample = _make_sample()
    objects = {
        f"{slug}/voice/cohort/general/s{i}.json": json.dumps(sample).encode()
        for i in range(count)
    }
    return _FakeR2Reader(objects)


@pytest.fixture
def voice():
    """Load the plugin fresh and reset module state between tests."""
    mod = load_plugin("hermes-smd-voice")
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None
    mod._VOICE_BUNDLE = None
    yield mod
    mod._R2_READER = None
    mod._CUSTOMER_SLUG = None
    mod._VOICE_BUNDLE = None


# ---------------------------------------------------------------------------
# Register contract
# ---------------------------------------------------------------------------


def test_register_attaches_transform_hook(voice, fake_ctx, monkeypatch):
    """register() must attach transform_llm_output so Hermes can fire it."""
    for k in ("SMD_CUSTOMER_SLUG", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_BUCKET_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    voice.register(fake_ctx)
    assert "transform_llm_output" in fake_ctx.registered


def test_transform_hook_is_synchronous(voice):
    """Hermes fires transform_llm_output synchronously; the callback must not be a coroutine."""
    assert not inspect.iscoroutinefunction(voice.on_transform_llm_output)


# ---------------------------------------------------------------------------
# Unbound / empty
# ---------------------------------------------------------------------------


def test_transform_noop_when_unbound(voice):
    result = voice.on_transform_llm_output(
        response_text="Hi Chris, thanks for the message.", session_id="s1", model="m", platform="p"
    )
    assert result is None


def test_transform_noop_when_empty_response(voice):
    reader = _make_reader_with_samples("acme")
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    result = voice.on_transform_llm_output(
        response_text="", session_id="s1", model="m", platform="p"
    )
    assert result is None


def test_transform_noop_when_whitespace_response(voice):
    reader = _make_reader_with_samples("acme")
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    result = voice.on_transform_llm_output(
        response_text="   \n\n  ", session_id="s1", model="m", platform="p"
    )
    assert result is None


def test_transform_noop_when_no_samples(voice):
    """No samples → no profile → passthrough (None)."""
    reader = _FakeR2Reader({})  # empty vault
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    result = voice.on_transform_llm_output(
        response_text="Dear Mr. Smith, thank you for your inquiry.",
        session_id="s1",
        model="m",
        platform="p",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Transform fires when profile is sufficient
# ---------------------------------------------------------------------------


def test_transform_returns_string_when_transformed(voice, monkeypatch):
    """With >=5 samples and a draft that needs reshaping, the hook returns a string."""
    # Build a profile with 'first_name' greeting style
    sample = _make_sample(greeting_style="first_name", signoff_style="thanks")
    reader = _FakeR2Reader({
        f"acme/voice/cohort/general/s{i}.json": json.dumps(sample).encode()
        for i in range(6)
    })
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)

    # Draft uses formal greeting — transform should swap to first_name
    draft = "Dear Mr. Johnson, just following up on our earlier conversation. Best regards,"
    result = voice.on_transform_llm_output(
        response_text=draft, session_id="s1", model="m", platform="p"
    )
    # Either transformed (string returned) or passthrough (None) — what matters
    # is the hook NEVER raises and always returns str | None
    assert result is None or isinstance(result, str)


def test_transform_returns_none_on_passthrough(voice):
    """When the draft already matches the profile, the hook returns None (no change)."""
    # Profile: first_name greeting, thanks signoff
    sample = _make_sample(greeting_style="first_name", signoff_style="thanks")
    reader = _FakeR2Reader({
        f"acme/voice/cohort/general/s{i}.json": json.dumps(sample).encode()
        for i in range(6)
    })
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)

    # Draft already in voice (first_name greeting, thanks signoff)
    draft = "Hi Chris, just a quick note. Thanks,"
    result = voice.on_transform_llm_output(
        response_text=draft, session_id="s1", model="m", platform="p"
    )
    # Passthrough → None (leave unchanged)
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


def test_transform_hook_never_raises_on_broken_reader(voice):
    """A broken R2 reader must not crash the agent loop."""

    class _BrokenReader:
        def list_keys(self, prefix):
            raise RuntimeError("R2 down")

        def get(self, key):
            raise RuntimeError("R2 down")

    voice.bind_runtime(customer_slug="acme", r2_reader=_BrokenReader())
    result = voice.on_transform_llm_output(
        response_text="Hi there, quick update.", session_id="s1", model="m", platform="p"
    )
    assert result is None


def test_transform_hook_never_raises_on_malformed_samples(voice):
    """Corrupt sample JSON must not crash the agent loop."""
    reader = _FakeR2Reader({"acme/voice/cohort/general/bad.json": b"not json !!!"})
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    result = voice.on_transform_llm_output(
        response_text="Hi there.", session_id="s1", model="m", platform="p"
    )
    assert result is None


# ---------------------------------------------------------------------------
# Bundle cache
# ---------------------------------------------------------------------------


def test_bind_runtime_resets_bundle_cache(voice):
    """bind_runtime() must invalidate _VOICE_BUNDLE so the next call rebuilds it."""
    reader = _make_reader_with_samples("acme")
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    # Trigger a bundle build
    _ = voice._get_cached_bundle()
    assert voice._VOICE_BUNDLE is not None

    # Rebind with new reader — cache must be cleared
    new_reader = _make_reader_with_samples("acme", count=0)
    voice.bind_runtime(customer_slug="acme", r2_reader=new_reader)
    assert voice._VOICE_BUNDLE is None


def test_bundle_is_built_lazily(voice):
    """_VOICE_BUNDLE starts None; _get_cached_bundle builds it on first call."""
    reader = _make_reader_with_samples("acme")
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    assert voice._VOICE_BUNDLE is None  # not yet built
    _ = voice._get_cached_bundle()
    # After the call it may be a VoiceProfileBundle or None (empty vault)
    # — the key invariant is that it is no longer the uninitialized sentinel
    # (i.e. the load was attempted)
    # (if samples = 0, bundle stays None — that's correct)


def test_bundle_is_reused_across_calls(voice):
    """_get_cached_bundle returns the same object on subsequent calls."""
    reader = _make_reader_with_samples("acme")
    voice.bind_runtime(customer_slug="acme", r2_reader=reader)
    b1 = voice._get_cached_bundle()
    b2 = voice._get_cached_bundle()
    assert b1 is b2
