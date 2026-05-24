"""Tests for the ``hermes-smd-voice`` plugin's transform layer.

Ported from
ss-console/ai-employee/adapter/voice/tests/test_transform.py with the
register-hooks coverage from the placeholder retained.

Covers:

* Plugin import + ``register(ctx)`` wires ``pre_llm_call`` and
  ``post_llm_call``.
* Fabrication discipline: the transform never introduces dollar amounts,
  dates, phone numbers, URLs, emails, or any content tokens beyond the
  closed connector vocabulary. This is the load-bearing safety property.
* Voice matching: a draft against a profile of known greeting / signoff
  style is rewritten to match.
* Passthrough behavior: empty drafts, insufficient profiles, drafts that
  already match, and fabrication-guard trips all return the source
  verbatim with the right status code.
* Performance: the transform runs in well under the 2s p99 target for
  representative drafts. <500ms is asserted here as a tight floor.
* Idempotence: running twice on the same draft + profile is a no-op
  after the first pass.
* Multi-user / per-cohort bundle selection.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loaders for the voice plugin.
#
# The shared ``tests.conftest.load_plugin`` calls ``exec_module`` without
# first registering the parent module in ``sys.modules``; relative imports
# inside the plugin's ``__init__.py`` then fail with ``ModuleNotFoundError``.
# Tests in this file need both the package itself (for register-hooks
# coverage) AND individual submodules (transform, diff, ...), so we use a
# local loader that does the right sequencing — mirroring the pattern in
# tests/test_audit_emit.py.
# ---------------------------------------------------------------------------


def load_plugin(plugin_name: str):
    """Load a plugin package so submodule imports (``from . import x``) work.

    Mirrors the helper in tests/test_audit_integrity.py — pre-registers
    the parent module in ``sys.modules`` before ``exec_module`` so the
    plugin's own ``from . import diff, transform`` lines resolve.
    """
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


# Load the plugin once at module import. The parent's __init__.py does
# ``from . import samples, transform``; that binds `samples` and
# `transform` as attributes on the loaded module. For sibling submodules
# the parent doesn't explicitly import (`diff`, `export`, `filter`,
# `pipeline`, `state`), we trigger a real package import via the
# pre-registered sys.modules entry — this is the canonical Python path
# for sub-packages of a programmatically-loaded parent.
load_plugin("hermes-smd-voice")
import plugin_hermes_smd_voice.diff as _diff_mod  # noqa: E402
import plugin_hermes_smd_voice.transform as _transform_mod  # noqa: E402

GreetingStyle = _diff_mod.GreetingStyle
SignoffStyle = _diff_mod.SignoffStyle
StructuralDiff = _diff_mod.StructuralDiff
extract_structural_diff = _diff_mod.extract_structural_diff

DraftTransformer = _transform_mod.DraftTransformer
GENERAL_VOICE_COHORT = _transform_mod.GENERAL_VOICE_COHORT
GENERAL_VOICE_USER_ID = _transform_mod.GENERAL_VOICE_USER_ID
MIN_PROFILE_SAMPLE_COUNT = _transform_mod.MIN_PROFILE_SAMPLE_COUNT
TransformStatus = _transform_mod.TransformStatus
VoiceProfile = _transform_mod.VoiceProfile
VoiceProfileBundle = _transform_mod.VoiceProfileBundle
build_voice_profile = _transform_mod.build_voice_profile
transform_draft = _transform_mod.transform_draft


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def test_voice_registers_expected_hooks(fake_ctx) -> None:
    """hermes-smd-voice must attach to pre_llm_call and post_llm_call."""
    mod = load_plugin("hermes-smd-voice")
    assert callable(mod.register)

    mod.register(fake_ctx)

    assert "pre_llm_call" in fake_ctx.registered
    assert "post_llm_call" in fake_ctx.registered


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def _make_diff(
    *,
    greeting=GreetingStyle.FIRST_NAME,
    signoff=SignoffStyle.THANKS,
    avg_len=12.0,
    distribution=None,
    paragraph_count=2,
    punct=None,
) -> StructuralDiff:
    """Build a StructuralDiff with sensible defaults for tests."""
    return StructuralDiff(
        schema_version=1,
        word_count=60,
        sentence_count=5,
        paragraph_count=paragraph_count,
        subject_word_count=4,
        avg_sentence_length=avg_len,
        sentence_length_distribution=distribution
        or {"lt_5": 1, "lt_10": 2, "lt_20": 2, "lt_35": 0, "gte_35": 0},
        greeting_style=greeting.value if hasattr(greeting, "value") else greeting,
        signoff_style=signoff.value if hasattr(signoff, "value") else signoff,
        opener_template="",
        closer_template="",
        punctuation_rhythm=punct
        or {
            "period_per_100": 8.0,
            "comma_per_100": 6.0,
            "semicolon_per_100": 0.5,
            "dash_per_100": 0.2,
            "question_per_100": 1.0,
            "exclamation_per_100": 0.1,
        },
        recipient_cohort="to-client",
    )


def _profile(
    *,
    samples_count=10,
    greeting=GreetingStyle.FIRST_NAME,
    signoff=SignoffStyle.THANKS,
    distribution=None,
    paragraph_count=2,
) -> VoiceProfile:
    diffs = [
        _make_diff(
            greeting=greeting,
            signoff=signoff,
            distribution=distribution,
            paragraph_count=paragraph_count,
        )
        for _ in range(samples_count)
    ]
    return build_voice_profile(cohort_id="to-client", samples=diffs)


# ---------------------------------------------------------------------------
# Profile aggregation
# ---------------------------------------------------------------------------


def test_build_voice_profile_empty_returns_zero_count():
    profile = build_voice_profile(cohort_id="to-client", samples=[])
    assert profile.sample_count == 0
    assert profile.cohort_id == "to-client"
    assert profile.greeting_style == GreetingStyle.UNKNOWN.value


def test_build_voice_profile_modal_picks_majority():
    samples = [
        _make_diff(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS),
        _make_diff(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS),
        _make_diff(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS),
        _make_diff(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.BEST),
    ]
    profile = build_voice_profile(cohort_id="to-client", samples=samples)
    assert profile.greeting_style == GreetingStyle.FIRST_NAME.value
    assert profile.signoff_style == SignoffStyle.THANKS.value


def test_build_voice_profile_modal_skips_unknown():
    samples = [
        _make_diff(greeting=GreetingStyle.UNKNOWN),
        _make_diff(greeting=GreetingStyle.FIRST_NAME),
        _make_diff(greeting=GreetingStyle.FIRST_NAME),
    ]
    profile = build_voice_profile(cohort_id="to-client", samples=samples)
    assert profile.greeting_style == GreetingStyle.FIRST_NAME.value


def test_build_voice_profile_distribution_normalizes_to_probabilities():
    samples = [
        _make_diff(distribution={"lt_5": 1, "lt_10": 0, "lt_20": 0, "lt_35": 0, "gte_35": 0}),
        _make_diff(distribution={"lt_5": 0, "lt_10": 1, "lt_20": 0, "lt_35": 0, "gte_35": 0}),
    ]
    profile = build_voice_profile(cohort_id="to-client", samples=samples)
    assert profile.sentence_length_distribution["lt_5"] == pytest.approx(0.5, abs=0.001)
    assert profile.sentence_length_distribution["lt_10"] == pytest.approx(0.5, abs=0.001)


# ---------------------------------------------------------------------------
# Passthrough paths
# ---------------------------------------------------------------------------


def test_empty_draft_returns_passthrough():
    result = transform_draft(draft="", profile=_profile())
    assert result.status == TransformStatus.PASSTHROUGH_EMPTY_DRAFT
    assert result.transformed_draft == ""


def test_whitespace_draft_returns_passthrough():
    result = transform_draft(draft="   \n\n   ", profile=_profile())
    assert result.status == TransformStatus.PASSTHROUGH_EMPTY_DRAFT


def test_insufficient_profile_returns_passthrough_verbatim():
    draft = "Dear Mr. Smith,\n\nFollowing up on the matter.\n\nSincerely,\nMarcus"
    profile = _profile(samples_count=MIN_PROFILE_SAMPLE_COUNT - 1)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.PASSTHROUGH_INSUFFICIENT_PROFILE
    assert result.transformed_draft == draft
    assert result.notes is not None


def test_matching_draft_returns_no_change_needed():
    draft = "Hi Sarah,\n\nFollowing up here.\n\nThanks,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.PASSTHROUGH_NO_CHANGE_NEEDED
    assert result.transformed_draft == draft


# ---------------------------------------------------------------------------
# Greeting swap
# ---------------------------------------------------------------------------


def test_greeting_swap_formal_to_first_name():
    draft = "Dear Mr. Smith,\n\nFollowing up on the matter.\n\nThanks,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    assert "greeting_swap" in result.changes_applied
    assert result.transformed_draft.startswith("Hi Smith,")


def test_greeting_swap_first_name_to_formal():
    draft = "Hi Sarah,\n\nFollowing up on the matter.\n\nThanks,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    # Formal-named requires honorific captured from source; first-name
    # source has no honorific, so the swap declines gracefully.
    # This is by design — the transform must not invent an honorific.
    assert result.status in (
        TransformStatus.PASSTHROUGH_NO_CHANGE_NEEDED,
        TransformStatus.TRANSFORMED,
    )
    if result.status == TransformStatus.TRANSFORMED:
        assert "greeting_swap" not in result.changes_applied


def test_greeting_swap_preserves_recipient_name():
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nThanks,\nMarcus"
    profile = _profile(greeting=GreetingStyle.SEMI_FORMAL, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    assert "Smith" in result.transformed_draft
    assert "Mr." in result.transformed_draft
    assert result.transformed_draft.startswith("Hi Mr. Smith,")


def test_greeting_swap_to_bare_hi_does_not_invent_name():
    draft = "Hi Sarah,\n\nFollowing up.\n\nThanks,\nMarcus"
    profile = _profile(greeting=GreetingStyle.BARE_HI, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    assert result.transformed_draft.startswith("Hi,")
    # Critical: the rewritten line is "Hi," not "Hi Sarah," — the name
    # is dropped, not invented.
    assert "Hi Sarah" not in result.transformed_draft.split("\n")[0]


# ---------------------------------------------------------------------------
# Signoff swap
# ---------------------------------------------------------------------------


def test_signoff_swap_sincerely_to_thanks():
    draft = "Hi Sarah,\n\nFollowing up.\n\nSincerely,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    assert "signoff_swap" in result.changes_applied
    assert "Thanks," in result.transformed_draft
    assert "Sincerely" not in result.transformed_draft


def test_signoff_swap_preserves_printed_signer_name():
    draft = "Hi Sarah,\n\nFollowing up.\n\nSincerely,\nMarcus Thompson"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.BEST)
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    # Signer name preserved verbatim
    assert "Marcus Thompson" in result.transformed_draft


# ---------------------------------------------------------------------------
# Fabrication discipline (the load-bearing safety guarantee)
# ---------------------------------------------------------------------------


def test_fabrication_guard_no_new_dollar_amounts():
    # Profile pushes toward longer sentences, which would trigger a join.
    # Verify the join never introduces a $ amount.
    draft = (
        "Hi Sarah,\n\n"
        "The motion is filed. Opposing counsel will respond. We expect a hearing soon.\n\n"
        "Thanks,\nMarcus"
    )
    profile = _profile(
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
        distribution={"lt_5": 0, "lt_10": 0, "lt_20": 5, "lt_35": 5, "gte_35": 0},
    )
    result = transform_draft(draft=draft, profile=profile)
    # No matter what changes happen, no dollar amounts must appear in
    # the output that weren't in the input.
    assert "$" not in result.transformed_draft


def test_fabrication_guard_no_new_dates():
    draft = (
        "Hi Sarah,\n\n"
        "Following up on the matter. Let me know if you have questions.\n\n"
        "Thanks,\nMarcus"
    )
    profile = _profile()
    result = transform_draft(draft=draft, profile=profile)
    for month in (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ):
        # Any month name not in source must not appear in output.
        if month not in draft:
            assert month not in result.transformed_draft


def test_fabrication_guard_no_new_phone_numbers():
    draft = "Hi Sarah,\n\nFollowing up on the matter.\n\nThanks,\nMarcus"
    profile = _profile()
    result = transform_draft(draft=draft, profile=profile)
    import re
    phone = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
    assert not phone.findall(result.transformed_draft)


def test_fabrication_guard_no_new_urls():
    draft = "Hi Sarah,\n\nFollowing up on the matter.\n\nThanks,\nMarcus"
    profile = _profile()
    result = transform_draft(draft=draft, profile=profile)
    assert "http://" not in result.transformed_draft
    assert "https://" not in result.transformed_draft
    assert "www." not in result.transformed_draft


def test_fabrication_guard_preserves_existing_entities_through_transform():
    # The source has a dollar amount, date, and URL — the transform may
    # rearrange them but must not lose or duplicate them.
    draft = (
        "Dear Mr. Smith,\n\n"
        "The settlement offer is $50,000 dated March 15, 2026. "
        "Please review at https://example.com/offer.\n\n"
        "Sincerely,\nMarcus"
    )
    profile = _profile(
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
    )
    result = transform_draft(draft=draft, profile=profile)
    # Greeting/signoff swap may have happened, but entities preserved
    assert "$50,000" in result.transformed_draft
    assert "March 15, 2026" in result.transformed_draft
    assert "https://example.com/offer" in result.transformed_draft


def test_no_new_content_words_introduced():
    """The core fabrication-discipline test.

    Any word appearing in the OUTPUT that wasn't in the INPUT must be
    one of: a structural connector from the closed allowed list (greeting
    phrase fragment, signoff fragment, sentence-join conjunction).
    """
    import re
    draft = (
        "Hi Sarah,\n\n"
        "The motion is filed. Opposing counsel will respond.\n\n"
        "Sincerely,\nMarcus"
    )
    profile = _profile(
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
        distribution={"lt_5": 0, "lt_10": 0, "lt_20": 5, "lt_35": 5, "gte_35": 0},
    )
    result = transform_draft(draft=draft, profile=profile)
    word_re = re.compile(r"\b\w+\b")
    source_words = {w.lower() for w in word_re.findall(draft)}
    output_words = {w.lower() for w in word_re.findall(result.transformed_draft)}
    new_words = output_words - source_words
    allowed = {
        "hi", "hello", "dear", "good", "morning", "afternoon", "evening",
        "best", "thanks", "thank", "you", "regards", "kind", "sincerely",
        "and", "but", "so",
    }
    disallowed = new_words - allowed
    assert not disallowed, f"transform introduced disallowed words: {disallowed}"


# ---------------------------------------------------------------------------
# Sentence redistribution
# ---------------------------------------------------------------------------


def test_sentence_split_when_profile_skews_short():
    # Long draft sentence, profile wants short sentences.
    draft = (
        "Hi Sarah,\n\n"
        "Following up on the discovery requests we sent last week, "
        "and I would appreciate your response by end of day Friday.\n\n"
        "Thanks,\nMarcus"
    )
    profile = _profile(
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
        distribution={"lt_5": 5, "lt_10": 5, "lt_20": 0, "lt_35": 0, "gte_35": 0},
    )
    result = transform_draft(draft=draft, profile=profile)
    assert "sentence_split" in result.changes_applied


def test_sentence_join_when_profile_skews_long():
    draft = (
        "Hi Sarah,\n\n"
        "The motion is filed. Opposing counsel will respond.\n\n"
        "Thanks,\nMarcus"
    )
    profile = _profile(
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
        distribution={"lt_5": 0, "lt_10": 0, "lt_20": 5, "lt_35": 5, "gte_35": 0},
    )
    result = transform_draft(draft=draft, profile=profile)
    assert "sentence_join" in result.changes_applied
    # Joined output has the conjunction
    assert ", and" in result.transformed_draft


# ---------------------------------------------------------------------------
# Performance contract — <2s p99
# ---------------------------------------------------------------------------


def test_transform_under_500ms_for_typical_draft():
    """Per the voice-layer-2 AC: <2s p99. We assert <500ms as a tight floor."""
    draft = (
        "Dear Mr. Smith,\n\n"
        "Following up on the matter we discussed last week regarding the "
        "discovery responses. The deadline is approaching and I want to "
        "confirm we have everything we need. Please let me know if you have "
        "any questions or need additional time.\n\n"
        "I have attached the latest draft of our response for your review. "
        "Once you have signed off we will file with the court.\n\n"
        "Sincerely,\nMarcus Thompson"
    )
    profile = _profile(samples_count=30)
    t0 = time.perf_counter()
    for _ in range(20):
        transform_draft(draft=draft, profile=profile)
    elapsed_per_call_ms = (time.perf_counter() - t0) / 20 * 1000
    assert elapsed_per_call_ms < 500, (
        f"per-call latency {elapsed_per_call_ms:.1f}ms exceeds 500ms floor"
    )


def test_transform_under_2s_for_very_long_draft():
    """Backstop test against the documented 2s ceiling for outsized drafts."""
    body_sentence = (
        "The court has set a hearing for the motion to compel. "
        "We need to prepare our reply brief and supporting exhibits. "
    )
    draft = (
        "Hi Sarah,\n\n"
        + (body_sentence * 50)
        + "\n\nThanks,\nMarcus"
    )
    profile = _profile(samples_count=30)
    t0 = time.perf_counter()
    result = transform_draft(draft=draft, profile=profile)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 2000, (
        f"long-draft latency {elapsed_ms:.1f}ms exceeds 2000ms ceiling"
    )
    # And the result is still well-formed (didn't crash on the size)
    assert result.transformed_draft


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_transform_is_idempotent():
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    first_pass = transform_draft(draft=draft, profile=profile)
    assert first_pass.status == TransformStatus.TRANSFORMED
    second_pass = transform_draft(
        draft=first_pass.transformed_draft, profile=profile
    )
    assert second_pass.status == TransformStatus.PASSTHROUGH_NO_CHANGE_NEEDED
    assert second_pass.transformed_draft == first_pass.transformed_draft


# ---------------------------------------------------------------------------
# Integration: round-trip extract -> aggregate -> transform
# ---------------------------------------------------------------------------


def test_round_trip_with_real_extracted_diffs():
    """End-to-end: extract structural diffs from real sample bodies,
    build a profile, transform a draft against it."""
    sample_bodies = [
        "Hi Sarah,\n\nFollowing up here.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nQuick note on the matter.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nReviewed your draft. Looks good.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nOne question on this.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nConfirmed. Moving forward.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nGood to talk earlier.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nSent the docs over.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nReviewing now.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nWill follow up after.\n\nThanks,\nMarcus",
        "Hi Sarah,\n\nNoted, thanks.\n\nThanks,\nMarcus",
    ]
    diffs = [
        extract_structural_diff(
            body_text=body, subject="Re: matter", recipient_cohort="to-client"
        )
        for body in sample_bodies
    ]
    profile = build_voice_profile(cohort_id="to-client", samples=diffs)
    assert profile.sample_count == 10
    assert profile.greeting_style == GreetingStyle.FIRST_NAME.value
    assert profile.signoff_style == SignoffStyle.THANKS.value

    draft = "Dear Mr. Smith,\n\nFollowing up on the matter.\n\nSincerely,\nMarcus"
    result = transform_draft(draft=draft, profile=profile)
    assert result.status == TransformStatus.TRANSFORMED
    assert "Hi Smith," in result.transformed_draft
    assert "Thanks," in result.transformed_draft
    assert "Marcus" in result.transformed_draft


# ---------------------------------------------------------------------------
# Class-vs-function entry parity
# ---------------------------------------------------------------------------


def test_class_and_function_entry_points_produce_same_result():
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    via_function = transform_draft(draft=draft, profile=profile)
    via_class = DraftTransformer().transform(draft=draft, profile=profile)
    assert via_function.status == via_class.status
    assert via_function.transformed_draft == via_class.transformed_draft
    assert via_function.changes_applied == via_class.changes_applied


# ---------------------------------------------------------------------------
# Multi-user voice — VoiceProfileBundle
# ---------------------------------------------------------------------------


def test_bare_profile_records_general_user_id():
    """Legacy single-profile callers always get the GENERAL sentinel."""
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    result = transform_draft(draft=draft, profile=profile)
    assert result.selected_voice_user_id == GENERAL_VOICE_USER_ID


def test_bare_profile_ignores_reviewer_user_id_argument():
    """Bare VoiceProfile callers can pass reviewer_user_id; it has no effect."""
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    a = transform_draft(draft=draft, profile=profile)
    b = transform_draft(draft=draft, profile=profile, reviewer_user_id="partner-sarah")
    assert a.status == b.status
    assert a.transformed_draft == b.transformed_draft
    assert a.selected_voice_user_id == b.selected_voice_user_id == GENERAL_VOICE_USER_ID


def test_bundle_with_no_reviewer_id_falls_back_to_general():
    """Bundle + None reviewer_user_id selects the general profile."""
    general = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    sarah = _profile(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.SINCERELY)
    bundle = VoiceProfileBundle(general=general, per_user={"partner-sarah": sarah})

    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(draft=draft, profile=bundle)
    assert result.selected_voice_user_id == GENERAL_VOICE_USER_ID
    # General profile is first_name/thanks — the formal draft should swap.
    assert result.status == TransformStatus.TRANSFORMED
    assert result.transformed_draft.startswith("Hi Smith,")


def test_bundle_per_user_profile_selected_when_reviewer_id_matches():
    """Bundle + matching reviewer_user_id selects that user's profile."""
    general = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    sarah = _profile(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.SINCERELY)
    bundle = VoiceProfileBundle(general=general, per_user={"partner-sarah": sarah})

    formal_draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(
        draft=formal_draft, profile=bundle, reviewer_user_id="partner-sarah"
    )
    assert result.selected_voice_user_id == "partner-sarah"
    # Already matches the formal target — no greeting / signoff swap.
    assert "greeting_swap" not in result.changes_applied
    assert "signoff_swap" not in result.changes_applied


def test_bundle_falls_back_to_general_when_reviewer_id_unknown():
    """Bundle + reviewer_user_id with no entry -> general profile."""
    general = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    sarah = _profile(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.SINCERELY)
    bundle = VoiceProfileBundle(general=general, per_user={"partner-sarah": sarah})

    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(
        draft=draft, profile=bundle, reviewer_user_id="associate-mike-no-profile"
    )
    assert result.selected_voice_user_id == GENERAL_VOICE_USER_ID
    # General is first_name/thanks -> swap applies
    assert result.status == TransformStatus.TRANSFORMED


def test_bundle_falls_back_when_per_user_profile_is_insufficient():
    """Per-user profile with < MIN samples falls back to general."""
    general = _profile(samples_count=30)
    # Sarah's profile has only 4 samples — well under the floor
    sarah = _profile(
        samples_count=MIN_PROFILE_SAMPLE_COUNT - 1,
        greeting=GreetingStyle.FORMAL_NAMED,
        signoff=SignoffStyle.SINCERELY,
    )
    bundle = VoiceProfileBundle(general=general, per_user={"partner-sarah": sarah})

    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(
        draft=draft, profile=bundle, reviewer_user_id="partner-sarah"
    )
    # Per-user profile was rejected, general kicked in
    assert result.selected_voice_user_id == GENERAL_VOICE_USER_ID


def test_bundle_with_empty_per_user_dict_equivalent_to_bare_profile():
    """A bundle with no per-user profiles behaves like the legacy path."""
    profile = _profile(greeting=GreetingStyle.FIRST_NAME, signoff=SignoffStyle.THANKS)
    bundle = VoiceProfileBundle(general=profile, per_user={})

    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    via_bare = transform_draft(draft=draft, profile=profile)
    via_bundle = transform_draft(draft=draft, profile=bundle)
    via_bundle_with_id = transform_draft(
        draft=draft, profile=bundle, reviewer_user_id="anyone"
    )
    assert via_bare.transformed_draft == via_bundle.transformed_draft
    assert via_bare.transformed_draft == via_bundle_with_id.transformed_draft
    assert via_bundle.selected_voice_user_id == GENERAL_VOICE_USER_ID
    assert via_bundle_with_id.selected_voice_user_id == GENERAL_VOICE_USER_ID


def test_bundle_passthrough_paths_carry_selected_user_id():
    """Empty draft / insufficient profile / fabrication-guard all report
    the resolved user id so the dashboard knows whose voice was attempted."""
    general = _profile()
    sarah = _profile(greeting=GreetingStyle.FORMAL_NAMED, signoff=SignoffStyle.SINCERELY)
    bundle = VoiceProfileBundle(general=general, per_user={"partner-sarah": sarah})

    # Empty draft
    r = transform_draft(draft="", profile=bundle, reviewer_user_id="partner-sarah")
    assert r.status == TransformStatus.PASSTHROUGH_EMPTY_DRAFT
    assert r.selected_voice_user_id == "partner-sarah"

    # Tiny general also fails MIN.
    tiny_general = _profile(samples_count=MIN_PROFILE_SAMPLE_COUNT - 1)
    tiny_bundle = VoiceProfileBundle(general=tiny_general, per_user={})
    r2 = transform_draft(draft="Hi,\n\nfoo.\n\nThanks,\nA", profile=tiny_bundle)
    assert r2.status == TransformStatus.PASSTHROUGH_INSUFFICIENT_PROFILE
    assert r2.selected_voice_user_id == GENERAL_VOICE_USER_ID


def test_bundle_select_returns_tuple_directly():
    """VoiceProfileBundle.select is the testable contract for callers
    that want to inspect the selection without invoking the transform."""
    general = _profile(samples_count=20)
    sarah = _profile(samples_count=15, greeting=GreetingStyle.FORMAL_NAMED)
    mike_thin = _profile(samples_count=2)  # < MIN
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah, "associate-mike": mike_thin},
    )

    chosen, who = bundle.select("partner-sarah")
    assert who == "partner-sarah"
    assert chosen is sarah

    chosen2, who2 = bundle.select("associate-mike")
    assert who2 == GENERAL_VOICE_USER_ID
    assert chosen2 is general

    chosen3, who3 = bundle.select(None)
    assert who3 == GENERAL_VOICE_USER_ID
    assert chosen3 is general

    chosen4, who4 = bundle.select("")
    assert who4 == GENERAL_VOICE_USER_ID
    assert chosen4 is general

    chosen5, who5 = bundle.select("nobody-knows-me")
    assert who5 == GENERAL_VOICE_USER_ID
    assert chosen5 is general


# ---------------------------------------------------------------------------
# Per-cohort voice variation — VoiceProfileBundle.select_with_cohort
# ---------------------------------------------------------------------------


def test_bundle_select_with_cohort_full_match():
    """All three tiers populated; the most-specific (user, cohort) wins."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20, greeting=GreetingStyle.FORMAL_NAMED)
    sarah_for_client = _profile(samples_count=15, greeting=GreetingStyle.FIRST_NAME)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): sarah_for_client},
    )
    chosen, who, cohort = bundle.select_with_cohort("partner-sarah", "client")
    assert chosen is sarah_for_client
    assert who == "partner-sarah"
    assert cohort == "client"


def test_bundle_select_with_cohort_falls_back_to_per_user_when_cohort_missing():
    """Reviewer has a per-user profile but no profile for this cohort ->
    use the cohort-agnostic per-user profile."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): _profile(samples_count=15)},
    )
    chosen, who, cohort = bundle.select_with_cohort("partner-sarah", "court")
    assert chosen is sarah
    assert who == "partner-sarah"
    assert cohort == GENERAL_VOICE_COHORT


def test_bundle_select_with_cohort_falls_back_when_cohort_profile_too_thin():
    """Per-(user, cohort) below min_samples_per_cohort -> step down to
    cohort-agnostic per-user profile."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20)
    thin_cohort = _profile(samples_count=3)  # below default MIN=5
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "court"): thin_cohort},
    )
    chosen, who, cohort = bundle.select_with_cohort("partner-sarah", "court")
    assert chosen is sarah
    assert who == "partner-sarah"
    assert cohort == GENERAL_VOICE_COHORT


def test_bundle_select_with_cohort_respects_custom_min_samples_per_cohort():
    """Customer override of min_samples_per_cohort tightens / relaxes the
    per-cohort floor."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20)
    cohort_profile = _profile(samples_count=8)
    # With default MIN=5, 8 passes — selected.
    bundle_default = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): cohort_profile},
    )
    chosen, _, cohort = bundle_default.select_with_cohort("partner-sarah", "client")
    assert chosen is cohort_profile
    assert cohort == "client"
    # With customer-tightened floor of 10, 8 fails — falls back to user.
    bundle_strict = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): cohort_profile},
        min_samples_per_cohort=10,
    )
    chosen2, _, cohort2 = bundle_strict.select_with_cohort("partner-sarah", "client")
    assert chosen2 is sarah
    assert cohort2 == GENERAL_VOICE_COHORT


def test_bundle_select_with_cohort_no_user_falls_back_to_general():
    """No reviewer id -> straight to customer general, regardless of cohort."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): _profile(samples_count=15)},
    )
    chosen, who, cohort = bundle.select_with_cohort(None, "client")
    assert chosen is general
    assert who == GENERAL_VOICE_USER_ID
    assert cohort == GENERAL_VOICE_COHORT


def test_bundle_select_with_cohort_no_cohort_skips_to_per_user():
    """Reviewer id but no cohort -> skip the (user, cohort) layer."""
    general = _profile(samples_count=30)
    sarah = _profile(samples_count=20, greeting=GreetingStyle.FORMAL_NAMED)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): _profile(samples_count=15)},
    )
    chosen, who, cohort = bundle.select_with_cohort("partner-sarah", None)
    assert chosen is sarah
    assert who == "partner-sarah"
    assert cohort == GENERAL_VOICE_COHORT


def test_bundle_select_with_cohort_user_only_in_cohort_table_falls_back():
    """If a reviewer has cohort-specific profiles but no cohort-agnostic
    per-user profile, an unmatched cohort falls all the way through to
    the general profile."""
    general = _profile(samples_count=30)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={},
        per_user_cohort={("partner-sarah", "client"): _profile(samples_count=15)},
    )
    # Asking for an uncovered cohort: no per-user fallback exists.
    chosen, who, cohort = bundle.select_with_cohort("partner-sarah", "court")
    assert chosen is general
    assert who == GENERAL_VOICE_USER_ID
    assert cohort == GENERAL_VOICE_COHORT


def test_transform_draft_threads_cohort_through_to_result():
    """End-to-end: transform_draft accepts recipient_cohort and the
    result records both selected_voice_user_id and selected_voice_cohort."""
    general = _profile(samples_count=30, greeting=GreetingStyle.SEMI_FORMAL)
    sarah = _profile(samples_count=20, greeting=GreetingStyle.FORMAL_NAMED)
    sarah_for_client = _profile(
        samples_count=15,
        greeting=GreetingStyle.FIRST_NAME,
        signoff=SignoffStyle.THANKS,
    )
    bundle = VoiceProfileBundle(
        general=general,
        per_user={"partner-sarah": sarah},
        per_user_cohort={("partner-sarah", "client"): sarah_for_client},
    )

    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(
        draft=draft,
        profile=bundle,
        reviewer_user_id="partner-sarah",
        recipient_cohort="client",
    )
    assert result.selected_voice_user_id == "partner-sarah"
    assert result.selected_voice_cohort == "client"
    assert result.status == TransformStatus.TRANSFORMED
    # Per-(user, cohort) is first_name/thanks -> swap from formal
    assert result.transformed_draft.startswith("Hi Smith,")


def test_transform_draft_records_general_cohort_on_legacy_path():
    """Bare VoiceProfile callers (no bundle) always see GENERAL cohort."""
    profile = _profile(samples_count=30)
    draft = "Dear Mr. Smith,\n\nFollowing up.\n\nSincerely,\nMarcus"
    result = transform_draft(
        draft=draft,
        profile=profile,
        reviewer_user_id="partner-sarah",
        recipient_cohort="client",
    )
    assert result.selected_voice_user_id == GENERAL_VOICE_USER_ID
    assert result.selected_voice_cohort == GENERAL_VOICE_COHORT


def test_transform_draft_records_cohort_on_passthrough_paths():
    """Empty draft + insufficient-profile passthroughs both surface
    selected_voice_cohort so the dashboard can attribute attempts."""
    general = _profile(samples_count=30)
    sarah_for_client = _profile(samples_count=15)
    bundle = VoiceProfileBundle(
        general=general,
        per_user={},
        per_user_cohort={("partner-sarah", "client"): sarah_for_client},
    )
    empty = transform_draft(
        draft="",
        profile=bundle,
        reviewer_user_id="partner-sarah",
        recipient_cohort="client",
    )
    assert empty.status == TransformStatus.PASSTHROUGH_EMPTY_DRAFT
    assert empty.selected_voice_user_id == "partner-sarah"
    assert empty.selected_voice_cohort == "client"
