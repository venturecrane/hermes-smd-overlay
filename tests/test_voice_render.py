"""Tests for hermes-smd-voice render_sample_block (signal-surfacing fix).

The differ computes a rich style fingerprint — sentence-length distribution
and punctuation rhythm — and stores it in each R2 sample. The prior
render_sample_block dropped both, injecting only greeting/signoff/avg: a
near-empty label that could not shape a draft. These tests pin the fix:
the injected block MUST surface the rhythm signal, aggregate across samples,
and degrade gracefully on malformed dicts (they come from R2 JSON).
"""

from __future__ import annotations

import pytest

from tests.conftest import load_plugin


@pytest.fixture
def samples_mod():
    return load_plugin("hermes-smd-voice").samples


def _fingerprint(
    *,
    greeting="none",
    signoff="none",
    avg=8.0,
    dist=None,
    punct=None,
    cohort="unassigned",
):
    """A structural-diff dict shaped like diff.as_dict() (style only)."""
    return {
        "schema_version": 1,
        "word_count": 40,
        "avg_sentence_length": avg,
        "sentence_length_distribution": dist
        if dist is not None
        else {"lt_5": 2, "lt_10": 3, "lt_20": 1, "lt_35": 0, "gte_35": 0},
        "greeting_style": greeting,
        "signoff_style": signoff,
        "opener_template": "",
        "closer_template": "",
        "punctuation_rhythm": punct
        if punct is not None
        else {
            "period_per_100": 14.0,
            "comma_per_100": 5.0,
            "semicolon_per_100": 0.0,
            "dash_per_100": 3.0,
            "question_per_100": 1.0,
            "exclamation_per_100": 0.0,
        },
        "recipient_cohort": cohort,
    }


def test_empty_samples_render_empty(samples_mod):
    assert samples_mod.render_sample_block([]) == ""


def test_block_surfaces_rhythm_signal(samples_mod):
    """The regression guard: distribution AND punctuation must appear.

    This is the whole point of the fix — the prior version discarded both.
    """
    block = samples_mod.render_sample_block([_fingerprint()])
    assert "author voice profile" in block
    assert "sentence-length mix:" in block
    assert "punctuation per 100 words:" in block
    # brevity + envelope still present
    assert "greeting: none" in block
    assert "sign-off: none" in block
    assert "~8.0 words" in block


def test_distribution_rendered_as_percentages(samples_mod):
    # all 10 sentences in the <5w bucket -> 100% short
    block = samples_mod.render_sample_block(
        [_fingerprint(dist={"lt_5": 10, "lt_10": 0, "lt_20": 0, "lt_35": 0, "gte_35": 0})]
    )
    assert "<5w 100%" in block


def test_aggregates_across_samples(samples_mod):
    # two 'none' greetings, one 'firstname' -> dominant is 'none'
    block = samples_mod.render_sample_block(
        [
            _fingerprint(greeting="none", avg=6.0),
            _fingerprint(greeting="none", avg=10.0),
            _fingerprint(greeting="firstname", avg=8.0),
        ]
    )
    assert "greeting: none" in block
    # mean of 6,10,8 = 8.0
    assert "~8.0 words" in block
    assert "derived from 3 of the author's own messages" in block


def test_low_frequency_punctuation_pruned(samples_mod):
    # semicolon/exclamation at 0.0 should not clutter the line
    block = samples_mod.render_sample_block([_fingerprint()])
    punct_line = next(ln for ln in block.splitlines() if "punctuation per 100" in ln)
    assert "semicolon" not in punct_line
    assert "exclamation" not in punct_line
    assert "period" in punct_line


def test_graceful_on_malformed_dicts(samples_mod):
    """Diffs come from R2 JSON; missing/wrong-typed fields must not crash."""
    block = samples_mod.render_sample_block(
        [
            {},  # nothing
            {"sentence_length_distribution": "not-a-dict", "punctuation_rhythm": None},
            {"avg_sentence_length": "oops"},
        ]
    )
    # No exception; still produces a header and the brevity line.
    assert "author voice profile" in block
    assert "typical sentence length" in block
