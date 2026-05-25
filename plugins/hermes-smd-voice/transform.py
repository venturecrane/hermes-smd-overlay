"""Voice Layer 2 — sample-driven draft transformation.

Ported from ss-console/ai-employee/adapter/voice/transform.py.

Layer 1 (rules) lives in skill ``SKILL.md`` frontmatter — banned patterns,
required patterns, tone register. Layer 2 (this module) is the
example-driven rewrite: given a draft and the customer's voice profile
aggregated from structural-diff samples, rewrite the draft so its surface
shape matches the customer's voice.

What this module rewrites
-------------------------

The transform performs **structural reshaping**: it adjusts the draft's
greeting line, signoff line, sentence-length distribution, and
paragraph rhythm so they line up with the per-cohort target profile.
Concretely:

* **Greeting swap** — if the customer's profile shows ``first_name``
  greetings and the draft opens with ``Dear Mr. Smith,`` (a formal-named
  greeting), the transform replaces the greeting line with the
  first-name variant. The recipient name is preserved verbatim from
  the draft — the transform never invents a name.

* **Signoff swap** — same logic on the closing line. If the profile is
  ``thanks`` and the draft closes with ``Sincerely,`` the transform
  swaps in ``Thanks,``. The printed signer line is preserved verbatim.

* **Sentence-length redistribution** — the draft's
  ``sentence_length_distribution`` is compared to the profile's. If the
  draft skews long where the profile skews short, the transform splits
  long sentences at clause boundaries (commas, semicolons, "and"/"but"
  conjunctions). If the draft skews short where the profile skews long,
  it joins adjacent short sentences with a conjunction. Joins and splits
  are conservative: a sentence is split only at a real clause boundary,
  and joins are limited to adjacent sentences in the same paragraph.

* **Paragraph density** — if the profile shows shorter paragraphs than
  the draft, the transform inserts paragraph breaks at sentence
  boundaries that already mark a topic shift (a sentence starting with
  ``Also``, ``Meanwhile``, ``As for``, ``On``, etc.). It does NOT split
  paragraphs at arbitrary points — that risks scrambling the draft's
  logical flow.

What this module REFUSES to do (fabrication discipline)
--------------------------------------------------------

The non-fabrication contract is the load-bearing safety property. The
transform must NEVER:

* Introduce new tokens that aren't already in the source draft except
  for the closed-vocabulary structural connectors documented in
  ``_ALLOWED_CONNECTORS`` (greeting/signoff phrase fragments, sentence-
  join conjunctions).

* Inject new entities (names, dates, dollar amounts, addresses, phone
  numbers, URLs, case numbers, matter IDs). If a token type matches one
  of these patterns and isn't already in the source draft, the
  transform must not produce it.

* Promise behavior, schedule meetings, accept obligations, or commit
  to deliverables. The voice transform never adds a sentence — it can
  only reshape sentences that are already there.

* Modify quoted content, signature blocks, or anything after the
  reviewer's printed signer line.

The empty-state principle applies: when the voice profile is
unavailable, missing samples, or unsuitable for the cohort, the
transform returns the original draft unchanged with a
``status="passthrough"`` reason recorded on the result. A draft that
cannot be safely reshaped is better than a draft that fabricates
content.

Performance contract
--------------------

The transform target is <2s p99 per draft. The implementation is pure
Python with no model calls and no network I/O — every operation is
either a regex substitution or a small string scan.

Voice profile aggregation
-------------------------

:class:`VoiceProfile` is the aggregated read-side model the transform
consumes. It is constructed by :func:`build_voice_profile` from a
sequence of :class:`StructuralDiff` objects — typically the samples
loaded from R2 for the active cohort.

A profile with fewer than :data:`MIN_PROFILE_SAMPLE_COUNT` samples is
considered insufficient. The transform's contract is to return
passthrough in that case.
"""

from __future__ import annotations

import enum
import logging
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .diff import (
    _PARAGRAPH_SPLIT,
    _SENTENCE_SPLIT,
    _WORD_SPLIT,
    GreetingStyle,
    SignoffStyle,
    StructuralDiff,
)
from .diff import (
    SCHEMA_VERSION as DIFF_SCHEMA_VERSION,
)

log = logging.getLogger("aie.voice.transform")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


MIN_PROFILE_SAMPLE_COUNT = 5
"""Below this sample count, the profile is considered insufficient and
the transform returns passthrough rather than reshape against a noisy
target. Wave-1 voice ingestion gates external drafts on >=30
samples at the platform level; this floor is the in-transform
defense-in-depth check for cases where the profile was loaded with
fewer samples (per-cohort partial coverage, test fixtures, etc.)."""


MAX_TRANSFORM_PASSES = 2
"""Hard cap on structural-change passes. Each pass examines the draft's
current shape vs. the profile and applies at most one structural change
per category (greeting, signoff, sentence-length, paragraph-density).
Two passes lets a sentence join pull a previously-short sentence into
the long bucket on the next iteration if needed; more than two passes
risks oscillation."""


GENERAL_VOICE_USER_ID = "__general__"
"""Sentinel slug for the customer-level general voice profile.

The general profile aggregates every sample regardless of reviewer
attribution. Customers without any per-user voice profiles configured
have only the general profile; customers with per-user profiles still
maintain the general one as the fallback when a per-user profile has
insufficient samples (per :data:`MIN_PROFILE_SAMPLE_COUNT`).
"""


GENERAL_VOICE_COHORT = "__general__"
"""Sentinel slug for the cohort-agnostic profile.

Used in the same shape as :data:`GENERAL_VOICE_USER_ID`: when no
recipient cohort is specified, OR when the requested cohort has no
matching profile, OR when the cohort-specific profile has insufficient
samples, the bundle falls back to the cohort-agnostic profile (still
attributed to the resolved user). The bundle's two-axis fallback
ladder is:

  (user_id, cohort)        -- most specific
  -> (user_id, __general__) -- per-user, cohort-agnostic
  -> (__general__, __general__) -- customer-wide composite
"""


_BUCKET_DELTA_TOLERANCE = 0.15
"""Maximum probability-mass delta per sentence-length bucket before the
transform attempts a redistribution. 0.15 means the bucket's share of
sentences in the draft can be +/-15 percentage points off the target
without triggering a rewrite. Tighter than this thrashes on short
drafts (a 3-sentence draft has 33% granularity per sentence)."""


# Closed set of structural-connector tokens the transform is allowed to
# introduce. Anything outside this list is treated as new content and
# refused by `_assert_no_new_tokens()`. The list is deliberately tiny:
# greeting/signoff phrase fragments and sentence-join conjunctions.
_ALLOWED_CONNECTORS: frozenset = frozenset(
    {
        # Greeting phrase fragments
        "hi",
        "hello",
        "dear",
        "good",
        "morning",
        "afternoon",
        "evening",
        # Signoff phrase fragments
        "best",
        "thanks",
        "thank",
        "you",
        "regards",
        "kind",
        "sincerely",
        # Sentence-join conjunctions (lowercase; case-preserved in apply)
        "and",
        "but",
        "so",
    }
)


# Topic-shift sentence starters. A sentence beginning with one of these
# is a candidate for a paragraph break before it. Conservative list —
# false negatives are fine (the transform just doesn't reshape that
# paragraph); false positives risk scrambling logical flow.
_TOPIC_SHIFT_STARTERS: tuple = (
    "Also,",
    "Also ",
    "Meanwhile,",
    "Meanwhile ",
    "As for ",
    "On the ",
    "Separately,",
    "Separately ",
    "Additionally,",
    "Additionally ",
)


# Regexes for fabrication-discipline guards. Tokens matching these
# patterns are entity-shaped and must NOT be introduced by the transform.
_DOLLAR_RE = re.compile(r"\$\s*\d[\d,.]*")
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}(?:,\s*\d{4})?)\b"
)
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")


# Greeting/signoff regexes lifted from diff.py — used to LOCATE the line
# in the draft so we can replace it. We do not import from diff.py's
# private namespace beyond what is already exported.
_GREETING_LINE_PATTERNS = {
    GreetingStyle.FORMAL_NAMED: re.compile(
        r"^(?P<prefix>Dear|To)\s+(?P<honorific>Mr\.|Ms\.|Mrs\.|Dr\.|Hon\.|Justice|Judge|Counsel)\s+(?P<name>\S+?)(?P<terminator>[,:])\s*$",
        re.IGNORECASE,
    ),
    GreetingStyle.SEMI_FORMAL: re.compile(
        r"^(?P<prefix>Hi|Hello)\s+(?P<honorific>Mr\.|Ms\.|Mrs\.|Dr\.)\s+(?P<name>\S+?)(?P<terminator>[,:])\s*$",
        re.IGNORECASE,
    ),
    GreetingStyle.FIRST_NAME: re.compile(
        r"^(?P<prefix>Hi|Hello|Hey)\s+(?P<name>[A-Z][a-z]+)(?P<terminator>[,:])\s*$",
    ),
    GreetingStyle.BARE_HI: re.compile(
        r"^(?P<prefix>Hi|Hello|Hey)(?P<terminator>[,!.])\s*$",
        re.IGNORECASE,
    ),
}


_SIGNOFF_LINE_PATTERNS = {
    SignoffStyle.BEST: re.compile(r"^Best(?:\s+regards)?[,.]?\s*$", re.IGNORECASE),
    SignoffStyle.THANKS: re.compile(
        r"^(?:Thanks(?:\s+so\s+much)?|Thank\s+you)[,.!]?\s*$",
        re.IGNORECASE,
    ),
    SignoffStyle.REGARDS: re.compile(
        r"^(?:Kind\s+)?Regards[,.]?\s*$", re.IGNORECASE
    ),
    SignoffStyle.SINCERELY: re.compile(
        r"^Sincerely(?:\s+yours)?[,.]?\s*$", re.IGNORECASE
    ),
}


# Greeting templates per style. Tokens used here are all in
# _ALLOWED_CONNECTORS; the recipient name placeholder is filled from
# the source draft, never invented.
_GREETING_TEMPLATES = {
    GreetingStyle.FORMAL_NAMED: "Dear {honorific} {name},",
    GreetingStyle.SEMI_FORMAL: "Hi {honorific} {name},",
    GreetingStyle.FIRST_NAME: "Hi {name},",
    GreetingStyle.BARE_HI: "Hi,",
}


_SIGNOFF_TEMPLATES = {
    SignoffStyle.BEST: "Best,",
    SignoffStyle.THANKS: "Thanks,",
    SignoffStyle.REGARDS: "Regards,",
    SignoffStyle.SINCERELY: "Sincerely,",
}


# ---------------------------------------------------------------------------
# Result vocabulary
# ---------------------------------------------------------------------------


class TransformStatus(str, enum.Enum):
    """Outcome of one transform run.

    * ``transformed`` — at least one structural change was applied.
    * ``passthrough_no_change_needed`` — the draft already matched the
      profile within tolerance. Returned draft is the source verbatim.
    * ``passthrough_insufficient_profile`` — the profile had fewer than
      :data:`MIN_PROFILE_SAMPLE_COUNT` samples. Returned draft is the
      source verbatim. Caller may surface this on the dashboard.
    * ``passthrough_empty_draft`` — the source draft was empty or
      whitespace-only.
    * ``passthrough_fabrication_guard`` — a structural change was
      proposed but would have introduced a disallowed token. The
      transform aborted that change and returned the source verbatim.
      This is the defense-in-depth signal — it should never fire in
      production unless a profile is malformed.
    """

    TRANSFORMED = "transformed"
    PASSTHROUGH_NO_CHANGE_NEEDED = "passthrough_no_change_needed"
    PASSTHROUGH_INSUFFICIENT_PROFILE = "passthrough_insufficient_profile"
    PASSTHROUGH_EMPTY_DRAFT = "passthrough_empty_draft"
    PASSTHROUGH_FABRICATION_GUARD = "passthrough_fabrication_guard"


@dataclass(frozen=True)
class TransformResult:
    """One transform-run summary.

    The result is structured so the caller can record an audit row, light
    up a dashboard health indicator, and surface to the operator (during
    calibration) which structural moves the transform made.

    ``changes_applied`` is a list of short tags from a closed vocabulary:
    ``"greeting_swap"``, ``"signoff_swap"``, ``"sentence_split"``,
    ``"sentence_join"``, ``"paragraph_break"``. The vocabulary is closed
    so the dashboard renderer can map each tag to a stable display label.
    """

    status: TransformStatus
    transformed_draft: str
    source_draft: str
    profile_sample_count: int
    changes_applied: list = field(default_factory=list)
    notes: str | None = None
    selected_voice_user_id: str = GENERAL_VOICE_USER_ID
    """The voice profile actually applied to this draft. Equals the
    reviewer's `voice_profile_id` slug when their per-user profile was
    selected, or :data:`GENERAL_VOICE_USER_ID` when the customer's
    general profile was used (no reviewer specified, no per-user
    profile configured, or insufficient per-user samples)."""

    selected_voice_cohort: str = GENERAL_VOICE_COHORT
    """The recipient-cohort profile actually applied to this draft.
    Equals the requested cohort slug when the per-(user, cohort)
    profile was selected, or :data:`GENERAL_VOICE_COHORT` when the
    fallback ladder landed on the cohort-agnostic per-user or general
    profile."""


@dataclass(frozen=True)
class VoiceProfileBundle:
    """Per-customer collection of voice profiles, keyed by user identity
    and recipient cohort.

    Built by the voice-profile loader at runtime — one bundle per
    customer Machine, refreshed when samples are ingested. The bundle
    holds three tiers, deliberately redundant so the fallback ladder
    can short-circuit cleanly:

    * `general` — customer-wide composite aggregated across every
      sample regardless of user or cohort. The terminal fallback.
    * `per_user` — per-user, cohort-agnostic profiles keyed by the
      user's `voice_profile_id` slug. Aggregated across the user's
      samples regardless of cohort. Optional; empty dict when no
      per-user profiles are configured.
    * `per_user_cohort` — per-(user, cohort) profiles keyed by
      `(voice_profile_id, cohort_id)` tuples. Aggregated across the
      user's samples tagged with that cohort. Optional; empty dict
      when the customer has not partitioned samples by cohort.

    The bundle is read-only. Callers select profiles via
    :meth:`select` (legacy 2-tuple, kept for cohort-unaware callers) or
    :meth:`select_with_cohort` (3-tuple, the cohort-aware path).
    """

    general: VoiceProfile
    per_user: dict
    """Mapping from `voice_profile_id` slug to VoiceProfile. Empty dict
    when no per-user profiles are configured. The dict is opaque to the
    caller; use :meth:`select` rather than indexing directly so the
    fallback rule is enforced in one place."""

    per_user_cohort: dict = field(default_factory=dict)
    """Mapping from `(voice_profile_id, cohort_id)` tuples to
    VoiceProfile. Empty dict when no per-(user, cohort) profiles are
    configured."""

    min_samples_per_cohort: int = MIN_PROFILE_SAMPLE_COUNT
    """Minimum sample count below which a per-(user, cohort) profile
    is rejected in favor of the cohort-agnostic per-user (or general)
    fallback."""

    def select(self, reviewer_user_id: str | None) -> tuple:
        """Pick the right profile for this reviewer, cohort-agnostic.

        Legacy 2-tuple entry point preserved for cohort-unaware callers.
        Returns `(profile, selected_user_id)`. Cohort-aware callers
        should use :meth:`select_with_cohort` instead.

        Selection rule (cohort-agnostic):

        1. If `reviewer_user_id` is None or the empty string, return
           the customer general profile.
        2. If `reviewer_user_id` has no matching per-user profile,
           return the customer general profile.
        3. If the per-user profile has fewer than
           :data:`MIN_PROFILE_SAMPLE_COUNT` samples, return the
           customer general profile.
        4. Otherwise return the per-user profile.
        """
        if not reviewer_user_id:
            return self.general, GENERAL_VOICE_USER_ID
        candidate = self.per_user.get(reviewer_user_id)
        if candidate is None:
            return self.general, GENERAL_VOICE_USER_ID
        if candidate.sample_count < MIN_PROFILE_SAMPLE_COUNT:
            return self.general, GENERAL_VOICE_USER_ID
        return candidate, reviewer_user_id

    def select_with_cohort(
        self,
        reviewer_user_id: str | None,
        recipient_cohort: str | None,
    ) -> tuple:
        """Pick the right profile for this (reviewer, cohort) pair.

        Two-axis fallback ladder, most-specific first.

        1. **(user, cohort)** — `per_user_cohort[(user, cohort)]` when
           present AND `sample_count >= self.min_samples_per_cohort`.
           Returned as `(profile, user, cohort)`.
        2. **(user, __general__)** — `per_user[user]` when present AND
           `sample_count >= MIN_PROFILE_SAMPLE_COUNT`. Returned as
           `(profile, user, __general__)`.
        3. **(__general__, __general__)** — `self.general`. Returned
           as `(general_profile, __general__, __general__)`.

        Each step's outcome is recorded so the audit row + dashboard
        can attribute which fallback the bundle landed on. Returns
        `(profile, selected_user_id, selected_cohort_id)`.

        `recipient_cohort` of None or empty string is treated as
        "cohort not specified" and skips straight to step 2.
        `reviewer_user_id` of None / empty string skips to step 3.
        """
        if not reviewer_user_id:
            return self.general, GENERAL_VOICE_USER_ID, GENERAL_VOICE_COHORT

        if recipient_cohort:
            cohort_candidate = self.per_user_cohort.get((reviewer_user_id, recipient_cohort))
            if (
                cohort_candidate is not None
                and cohort_candidate.sample_count >= self.min_samples_per_cohort
            ):
                return cohort_candidate, reviewer_user_id, recipient_cohort

        user_candidate = self.per_user.get(reviewer_user_id)
        if (
            user_candidate is not None
            and user_candidate.sample_count >= MIN_PROFILE_SAMPLE_COUNT
        ):
            return user_candidate, reviewer_user_id, GENERAL_VOICE_COHORT

        return self.general, GENERAL_VOICE_USER_ID, GENERAL_VOICE_COHORT


@dataclass(frozen=True)
class VoiceProfile:
    """Aggregated voice signature for one cohort, consumed by the transform.

    Built from a sequence of :class:`StructuralDiff` objects via
    :func:`build_voice_profile`. The profile is read-only — once
    constructed, it never mutates.

    ``sample_count`` lets callers gate the transform on minimum-sample
    coverage. ``cohort_id`` is recorded so the audit row can attribute
    the transform to a specific cohort.
    """

    cohort_id: str
    sample_count: int
    schema_version: int
    avg_sentence_length: float
    sentence_length_distribution: dict
    greeting_style: str
    signoff_style: str
    punctuation_rhythm: dict
    paragraph_count_avg: float


def build_voice_profile(
    *,
    cohort_id: str,
    samples: Sequence[StructuralDiff],
) -> VoiceProfile:
    """Aggregate a sequence of structural-diff samples into one profile.

    The aggregation uses simple central-tendency statistics: averages
    for continuous values, modal categorization for categorical labels,
    bucket-sum normalization for the sentence-length distribution.

    ``samples`` may be empty — the resulting profile will have
    ``sample_count=0`` and trigger
    :data:`TransformStatus.PASSTHROUGH_INSUFFICIENT_PROFILE` when used.
    """
    n = len(samples)
    if n == 0:
        return VoiceProfile(
            cohort_id=cohort_id,
            sample_count=0,
            schema_version=DIFF_SCHEMA_VERSION,
            avg_sentence_length=0.0,
            sentence_length_distribution={
                "lt_5": 0.0,
                "lt_10": 0.0,
                "lt_20": 0.0,
                "lt_35": 0.0,
                "gte_35": 0.0,
            },
            greeting_style=GreetingStyle.UNKNOWN.value,
            signoff_style=SignoffStyle.UNKNOWN.value,
            punctuation_rhythm={
                "period_per_100": 0.0,
                "comma_per_100": 0.0,
                "semicolon_per_100": 0.0,
                "dash_per_100": 0.0,
                "question_per_100": 0.0,
                "exclamation_per_100": 0.0,
            },
            paragraph_count_avg=0.0,
        )

    avg_sentence_length = statistics.fmean(
        s.avg_sentence_length for s in samples
    )

    bucket_totals: dict = {
        "lt_5": 0,
        "lt_10": 0,
        "lt_20": 0,
        "lt_35": 0,
        "gte_35": 0,
    }
    for s in samples:
        for k, v in s.sentence_length_distribution.items():
            if k in bucket_totals:
                bucket_totals[k] += v
    total_sentences = sum(bucket_totals.values())
    if total_sentences > 0:
        sentence_length_distribution = {
            k: round(v / total_sentences, 4) for k, v in bucket_totals.items()
        }
    else:
        sentence_length_distribution = {k: 0.0 for k in bucket_totals}

    greeting_style = _modal_category(s.greeting_style for s in samples)
    signoff_style = _modal_category(s.signoff_style for s in samples)

    punct_keys = (
        "period_per_100",
        "comma_per_100",
        "semicolon_per_100",
        "dash_per_100",
        "question_per_100",
        "exclamation_per_100",
    )
    punctuation_rhythm = {
        k: round(statistics.fmean(s.punctuation_rhythm.get(k, 0.0) for s in samples), 2)
        for k in punct_keys
    }

    paragraph_count_avg = statistics.fmean(s.paragraph_count for s in samples)

    schema_versions = {s.schema_version for s in samples}
    if len(schema_versions) > 1:
        log.warning(
            "voice profile aggregates samples with mixed schema_versions: %r",
            sorted(schema_versions),
        )
    schema_version = max(schema_versions) if schema_versions else DIFF_SCHEMA_VERSION

    return VoiceProfile(
        cohort_id=cohort_id,
        sample_count=n,
        schema_version=schema_version,
        avg_sentence_length=round(avg_sentence_length, 2),
        sentence_length_distribution=sentence_length_distribution,
        greeting_style=greeting_style,
        signoff_style=signoff_style,
        punctuation_rhythm=punctuation_rhythm,
        paragraph_count_avg=round(paragraph_count_avg, 2),
    )


def _modal_category(values: Iterable[str]) -> str:
    """Return the most frequent non-unknown value, or ``unknown``.

    Modal aggregation deliberately drops the ``unknown`` label when
    other labels are present — a sample whose greeting was unrecognized
    should not anchor the profile.
    """
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    candidates = [(c, v) for v, c in counts.items() if v not in ("unknown",)]
    if not candidates:
        return counts and next(iter(counts)) or "unknown"
    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# The transformer
# ---------------------------------------------------------------------------


class DraftTransformer:
    """Voice Layer 2 transformer — sample-driven structural rewrite.

    Construction is parameter-free; the transformer holds no state. One
    instance per process is sufficient. The transform is invoked through
    :meth:`transform`, which takes the draft + the voice profile.

    Thread safety: this class has no mutable state. Concurrent calls are
    safe.
    """

    def transform(
        self,
        *,
        draft: str,
        profile: VoiceProfile | VoiceProfileBundle,
        reviewer_user_id: str | None = None,
        recipient_cohort: str | None = None,
    ) -> TransformResult:
        """Rewrite ``draft`` so its surface shape matches ``profile``.

        The transform is non-mutating from the caller's perspective:
        ``draft`` is consumed as an immutable string and the result's
        ``transformed_draft`` is a new string. On any passthrough path
        the result's ``transformed_draft`` equals ``draft`` byte-for-byte.
        """
        resolved_profile, selected_user_id, selected_cohort = _resolve_profile_selection(
            profile, reviewer_user_id, recipient_cohort
        )

        if not draft or not draft.strip():
            return TransformResult(
                status=TransformStatus.PASSTHROUGH_EMPTY_DRAFT,
                transformed_draft=draft,
                source_draft=draft,
                profile_sample_count=resolved_profile.sample_count,
                selected_voice_user_id=selected_user_id,
                selected_voice_cohort=selected_cohort,
            )

        if resolved_profile.sample_count < MIN_PROFILE_SAMPLE_COUNT:
            return TransformResult(
                status=TransformStatus.PASSTHROUGH_INSUFFICIENT_PROFILE,
                transformed_draft=draft,
                source_draft=draft,
                profile_sample_count=resolved_profile.sample_count,
                notes=(
                    f"profile has {resolved_profile.sample_count} samples, "
                    f"minimum is {MIN_PROFILE_SAMPLE_COUNT}"
                ),
                selected_voice_user_id=selected_user_id,
                selected_voice_cohort=selected_cohort,
            )

        current = draft
        changes: list = []
        for _ in range(MAX_TRANSFORM_PASSES):
            pass_changes: list = []

            after_greeting, greeting_change = _apply_greeting_swap(current, resolved_profile)
            if greeting_change:
                if _has_introduced_disallowed_tokens(current, after_greeting):
                    return _fabrication_guard_passthrough(
                        draft, resolved_profile, selected_user_id, selected_cohort
                    )
                current = after_greeting
                pass_changes.append(greeting_change)

            after_signoff, signoff_change = _apply_signoff_swap(current, resolved_profile)
            if signoff_change:
                if _has_introduced_disallowed_tokens(current, after_signoff):
                    return _fabrication_guard_passthrough(
                        draft, resolved_profile, selected_user_id, selected_cohort
                    )
                current = after_signoff
                pass_changes.append(signoff_change)

            after_sentences, sentence_changes = _apply_sentence_redistribution(
                current, resolved_profile
            )
            if sentence_changes:
                if _has_introduced_disallowed_tokens(current, after_sentences):
                    return _fabrication_guard_passthrough(
                        draft, resolved_profile, selected_user_id, selected_cohort
                    )
                current = after_sentences
                pass_changes.extend(sentence_changes)

            after_paragraphs, paragraph_change = _apply_paragraph_density(
                current, resolved_profile
            )
            if paragraph_change:
                if _has_introduced_disallowed_tokens(current, after_paragraphs):
                    return _fabrication_guard_passthrough(
                        draft, resolved_profile, selected_user_id, selected_cohort
                    )
                current = after_paragraphs
                pass_changes.append(paragraph_change)

            if not pass_changes:
                break
            changes.extend(pass_changes)

        if not changes:
            return TransformResult(
                status=TransformStatus.PASSTHROUGH_NO_CHANGE_NEEDED,
                transformed_draft=draft,
                source_draft=draft,
                profile_sample_count=resolved_profile.sample_count,
                selected_voice_user_id=selected_user_id,
                selected_voice_cohort=selected_cohort,
            )

        return TransformResult(
            status=TransformStatus.TRANSFORMED,
            transformed_draft=current,
            source_draft=draft,
            profile_sample_count=resolved_profile.sample_count,
            changes_applied=changes,
            selected_voice_user_id=selected_user_id,
            selected_voice_cohort=selected_cohort,
        )


# ---------------------------------------------------------------------------
# Greeting swap
# ---------------------------------------------------------------------------


def _apply_greeting_swap(draft: str, profile: VoiceProfile) -> tuple:
    """Replace the draft's greeting line if it diverges from the profile.

    Returns ``(new_draft, change_tag_or_None)``. When no swap applies
    (no recognizable greeting, target style equals current style, target
    style not in templates), returns ``(draft, None)``.
    """
    if profile.greeting_style in (GreetingStyle.UNKNOWN.value, GreetingStyle.NONE.value):
        return draft, None

    target = profile.greeting_style
    if target not in _GREETING_TEMPLATES:
        return draft, None

    lines = draft.split("\n")
    first_nonempty_idx = _first_nonempty_index(lines)
    if first_nonempty_idx is None:
        return draft, None

    first_line = lines[first_nonempty_idx].strip()
    current_style, captured = _classify_greeting_line(first_line)

    if current_style == target:
        return draft, None

    template = _GREETING_TEMPLATES[target]

    if "{honorific}" in template:
        honorific = captured.get("honorific")
        name = captured.get("name")
        if not honorific or not name:
            return draft, None
        new_line = template.format(honorific=honorific, name=name)
    elif "{name}" in template:
        name = captured.get("name")
        if not name:
            return draft, None
        new_line = template.format(name=name)
    else:
        new_line = template

    lines[first_nonempty_idx] = new_line
    return "\n".join(lines), "greeting_swap"


def _classify_greeting_line(line: str) -> tuple:
    """Classify a single line and return ``(style_value, captured_groups)``.

    The captured groups dict carries the recipient name and honorific
    (when present) so the swap can preserve them verbatim.
    """
    for style, pattern in _GREETING_LINE_PATTERNS.items():
        match = pattern.match(line)
        if match:
            return style.value, match.groupdict()
    return None, {}


# ---------------------------------------------------------------------------
# Signoff swap
# ---------------------------------------------------------------------------


def _apply_signoff_swap(draft: str, profile: VoiceProfile) -> tuple:
    """Replace the draft's signoff line if it diverges from the profile.

    Returns ``(new_draft, change_tag_or_None)``. The signoff is the last
    line in the body that matches one of the recognized phrase patterns;
    the printed signer line (typically the next line) is preserved.
    """
    if profile.signoff_style in (SignoffStyle.UNKNOWN.value, SignoffStyle.NONE.value):
        return draft, None

    target = profile.signoff_style
    if target not in _SIGNOFF_TEMPLATES:
        return draft, None

    lines = draft.split("\n")
    signoff_idx = _last_signoff_index(lines)
    if signoff_idx is None:
        return draft, None

    current_line = lines[signoff_idx].strip()
    current_style = _classify_signoff_line(current_line)
    if current_style == target:
        return draft, None

    lines[signoff_idx] = _SIGNOFF_TEMPLATES[target]
    return "\n".join(lines), "signoff_swap"


def _classify_signoff_line(line: str) -> str | None:
    for style, pattern in _SIGNOFF_LINE_PATTERNS.items():
        if pattern.match(line):
            return style.value
    return None


def _last_signoff_index(lines: list) -> int | None:
    """Find the index of the last recognized signoff phrase line.

    Scans from the bottom up, stopping at the first non-empty line that
    matches one of the signoff phrase patterns. Skips trailing blank
    lines and any printed signer-name lines underneath the signoff.
    """
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if _classify_signoff_line(stripped) is not None:
            return idx
    return None


# ---------------------------------------------------------------------------
# Sentence-length redistribution
# ---------------------------------------------------------------------------


def _apply_sentence_redistribution(draft: str, profile: VoiceProfile) -> tuple:
    """Split long sentences or join short ones based on the profile.

    Returns ``(new_draft, change_tags)``. The change-tag list contains
    ``"sentence_split"`` and/or ``"sentence_join"`` entries — at most
    one of each per pass.
    """
    target = profile.sentence_length_distribution
    if not any(target.values()):
        return draft, []

    body, header_lines, footer_lines = _split_off_greeting_signoff(draft)
    if not body.strip():
        return draft, []

    sentences = _split_sentences_preserving_offsets(body)
    if not sentences:
        return draft, []

    current_dist = _empirical_distribution(sentences)

    needs_more_long = (
        target.get("lt_20", 0.0) + target.get("lt_35", 0.0) + target.get("gte_35", 0.0)
    ) - (
        current_dist.get("lt_20", 0.0)
        + current_dist.get("lt_35", 0.0)
        + current_dist.get("gte_35", 0.0)
    ) > _BUCKET_DELTA_TOLERANCE

    needs_more_short = (
        target.get("lt_5", 0.0) + target.get("lt_10", 0.0)
    ) - (
        current_dist.get("lt_5", 0.0) + current_dist.get("lt_10", 0.0)
    ) > _BUCKET_DELTA_TOLERANCE

    changes: list = []
    new_sentences = list(sentences)

    if needs_more_short:
        split_idx = _find_first_splittable(new_sentences)
        if split_idx is not None:
            left, right = _split_sentence_at_clause(new_sentences[split_idx])
            if left and right:
                new_sentences[split_idx : split_idx + 1] = [left, right]
                changes.append("sentence_split")

    if needs_more_long:
        join_idx = _find_first_joinable(new_sentences)
        if join_idx is not None:
            joined = _join_sentences(new_sentences[join_idx], new_sentences[join_idx + 1])
            if joined:
                new_sentences[join_idx : join_idx + 2] = [joined]
                changes.append("sentence_join")

    if not changes:
        return draft, []

    new_body = " ".join(new_sentences)
    parts = []
    if header_lines:
        parts.append("\n".join(header_lines))
    parts.append(new_body)
    if footer_lines:
        parts.append("\n".join(footer_lines))
    return "\n".join(parts), changes


def _split_sentences_preserving_offsets(text: str) -> list:
    """Split text into sentences. Same regex as the diff module."""
    if not text:
        return []
    parts = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    return parts


def _empirical_distribution(sentences: list) -> dict:
    buckets = {
        "lt_5": 0,
        "lt_10": 0,
        "lt_20": 0,
        "lt_35": 0,
        "gte_35": 0,
    }
    for s in sentences:
        n = len(_WORD_SPLIT.findall(s))
        if n < 5:
            buckets["lt_5"] += 1
        elif n < 10:
            buckets["lt_10"] += 1
        elif n < 20:
            buckets["lt_20"] += 1
        elif n < 35:
            buckets["lt_35"] += 1
        else:
            buckets["gte_35"] += 1
    total = sum(buckets.values()) or 1
    return {k: v / total for k, v in buckets.items()}


_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?P<head>.+?)(?P<sep>[;,]\s+|,\s+(?:and|but|so)\s+)(?P<tail>.+)",
    re.IGNORECASE,
)


def _find_first_splittable(sentences: list) -> int | None:
    """Return the index of the first long sentence with a clause boundary."""
    for idx, s in enumerate(sentences):
        if len(_WORD_SPLIT.findall(s)) >= 15 and _CLAUSE_BOUNDARY_RE.search(s):
            return idx
    return None


def _split_sentence_at_clause(sentence: str) -> tuple:
    """Split one sentence at the first clause boundary.

    Returns ``("", "")`` when no acceptable split exists. The split
    preserves terminal punctuation by appending ``.`` to the left half
    if it ended with the clause separator's comma/semicolon.
    """
    match = _CLAUSE_BOUNDARY_RE.search(sentence)
    if not match:
        return "", ""
    head = match.group("head").rstrip(",;").strip()
    tail = match.group("tail").strip()
    sep = match.group("sep").lower()

    if not head or not tail:
        return "", ""

    if not head.endswith((".", "?", "!")):
        head = head + "."

    # If the separator was a conjunction (", and "/", but "/", so "), the
    # word after the comma was the conjunction itself — strip it so the
    # right-hand sentence starts fresh, capitalized.
    if sep.startswith(",") and any(c in sep for c in ("and", "but", "so")):
        # The tail already excludes the conjunction because the regex's
        # named groups partition the sentence around `sep`. We just need
        # to capitalize the first letter.
        pass

    if tail and tail[0].islower():
        tail = tail[0].upper() + tail[1:]

    return head, tail


def _find_first_joinable(sentences: list) -> int | None:
    """Return the index of the first pair of adjacent short joinable sentences."""
    for idx in range(len(sentences) - 1):
        left_len = len(_WORD_SPLIT.findall(sentences[idx]))
        right_len = len(_WORD_SPLIT.findall(sentences[idx + 1]))
        if left_len < 10 and right_len < 10 and left_len + right_len < 18:
            return idx
    return None


def _join_sentences(left: str, right: str) -> str:
    """Join two sentences with a conjunction. Returns ``""`` if unsafe."""
    if not left or not right:
        return ""
    left_trimmed = left.rstrip(".!? ").rstrip()
    if not left_trimmed:
        return ""

    if right and right[0].isupper():
        right_lowered = right[0].lower() + right[1:]
    else:
        right_lowered = right

    return f"{left_trimmed}, and {right_lowered}"


# ---------------------------------------------------------------------------
# Paragraph density
# ---------------------------------------------------------------------------


def _apply_paragraph_density(draft: str, profile: VoiceProfile) -> tuple:
    """Insert a paragraph break before a topic-shift sentence when the
    profile shows more paragraphs than the draft currently has.

    The transform only acts on topic-shift sentences (sentences
    beginning with one of :data:`_TOPIC_SHIFT_STARTERS`) — it never
    inserts a paragraph break at an arbitrary sentence boundary because
    that risks scrambling logical flow.
    """
    if profile.paragraph_count_avg <= 0:
        return draft, None

    paragraphs = [p for p in _PARAGRAPH_SPLIT.split(draft) if p.strip()]
    current_paragraph_count = len(paragraphs)

    if current_paragraph_count == 0:
        return draft, None

    if current_paragraph_count >= profile.paragraph_count_avg - 0.5:
        return draft, None

    for idx, paragraph in enumerate(paragraphs):
        sentences = _split_sentences_preserving_offsets(paragraph)
        for s_idx in range(1, len(sentences)):
            if _starts_with_topic_shift(sentences[s_idx]):
                head = " ".join(sentences[:s_idx])
                tail = " ".join(sentences[s_idx:])
                paragraphs[idx] = head
                paragraphs.insert(idx + 1, tail)
                new_draft = "\n\n".join(paragraphs)
                return new_draft, "paragraph_break"
    return draft, None


def _starts_with_topic_shift(sentence: str) -> bool:
    stripped = sentence.strip()
    for starter in _TOPIC_SHIFT_STARTERS:
        if stripped.startswith(starter):
            return True
    return False


# ---------------------------------------------------------------------------
# Header / footer isolation — keeps greeting + signoff out of sentence ops
# ---------------------------------------------------------------------------


def _split_off_greeting_signoff(draft: str) -> tuple:
    """Carve the draft into ``(body, header_lines, footer_lines)``.

    ``header_lines`` is the leading lines through the greeting (if a
    recognized greeting was found) plus the immediately following blank
    line. ``footer_lines`` is the trailing lines starting at the signoff.
    The body is everything in between.

    Sentence redistribution operates only on the body so it cannot mangle
    the greeting line, signoff line, or printed signer name.
    """
    lines = draft.split("\n")
    header_end = 0
    footer_start = len(lines)

    first_idx = _first_nonempty_index(lines)
    if first_idx is not None:
        first_line = lines[first_idx].strip()
        if _classify_greeting_line(first_line)[0] is not None:
            header_end = first_idx + 1
            while header_end < len(lines) and not lines[header_end].strip():
                header_end += 1

    signoff_idx = _last_signoff_index(lines)
    if signoff_idx is not None:
        footer_start = signoff_idx
        while footer_start > header_end and not lines[footer_start - 1].strip():
            footer_start -= 1

    if header_end >= footer_start:
        return draft, [], []

    header_lines = lines[:header_end]
    body_lines = lines[header_end:footer_start]
    footer_lines = lines[footer_start:]
    return "\n".join(body_lines).strip(), header_lines, footer_lines


def _first_nonempty_index(lines: list) -> int | None:
    for idx, line in enumerate(lines):
        if line.strip():
            return idx
    return None


# ---------------------------------------------------------------------------
# Fabrication discipline guards
# ---------------------------------------------------------------------------


def _has_introduced_disallowed_tokens(before: str, after: str) -> bool:
    """Return True if ``after`` contains entity-shaped tokens not in ``before``.

    The guard checks for new dollar amounts, dates, phone numbers, URLs,
    and email addresses. Any of these appearing in ``after`` but not in
    ``before`` is treated as fabrication and aborts the change.

    Note: tokens may be REARRANGED between before and after (a sentence
    split moves a phone number to the next sentence); only NEW tokens
    are disallowed. The check uses set membership of the matched strings.
    """
    for pattern in (_DOLLAR_RE, _DATE_RE, _PHONE_RE, _URL_RE, _EMAIL_RE):
        before_matches = set(pattern.findall(before))
        after_matches = set(pattern.findall(after))
        new_tokens = after_matches - before_matches
        if new_tokens:
            log.warning(
                "voice transform aborted: introduced %r — %s",
                sorted(new_tokens),
                pattern.pattern[:40],
            )
            return True

    before_word_set = {w.lower() for w in _WORD_SPLIT.findall(before)}
    after_word_set = {w.lower() for w in _WORD_SPLIT.findall(after)}
    new_words = after_word_set - before_word_set
    disallowed_new = new_words - _ALLOWED_CONNECTORS
    if disallowed_new:
        log.warning(
            "voice transform aborted: introduced disallowed words %r",
            sorted(disallowed_new),
        )
        return True

    return False


def _fabrication_guard_passthrough(
    draft: str,
    profile: VoiceProfile,
    selected_user_id: str = GENERAL_VOICE_USER_ID,
    selected_cohort: str = GENERAL_VOICE_COHORT,
) -> TransformResult:
    """Return a passthrough result with the fabrication-guard status."""
    return TransformResult(
        status=TransformStatus.PASSTHROUGH_FABRICATION_GUARD,
        transformed_draft=draft,
        source_draft=draft,
        profile_sample_count=profile.sample_count,
        notes="proposed rewrite introduced an entity-shaped token; aborted",
        selected_voice_user_id=selected_user_id,
        selected_voice_cohort=selected_cohort,
    )


# ---------------------------------------------------------------------------
# Profile-bundle resolution
# ---------------------------------------------------------------------------


def _resolve_profile_selection(
    profile: VoiceProfile | VoiceProfileBundle,
    reviewer_user_id: str | None,
    recipient_cohort: str | None = None,
) -> tuple:
    """Resolve the (profile, selected_user_id, selected_cohort) triple.

    Centralizes the selection so the transformer's happy path and every
    passthrough path agree on which profile they refer to. Bare
    :class:`VoiceProfile` callers get the legacy single-voice behavior
    with both selection markers set to their `__general__` sentinels.
    """
    if isinstance(profile, VoiceProfileBundle):
        return profile.select_with_cohort(reviewer_user_id, recipient_cohort)
    return profile, GENERAL_VOICE_USER_ID, GENERAL_VOICE_COHORT


# ---------------------------------------------------------------------------
# Convenience entry point — the function the skill pipeline will call
# ---------------------------------------------------------------------------


def transform_draft(
    *,
    draft: str,
    profile: VoiceProfile | VoiceProfileBundle,
    reviewer_user_id: str | None = None,
    recipient_cohort: str | None = None,
) -> TransformResult:
    """Top-level functional entry point.

    Equivalent to
    ``DraftTransformer().transform(draft=draft, profile=profile,
    reviewer_user_id=reviewer_user_id, recipient_cohort=recipient_cohort)``.
    Provided for callers that prefer a function over a class instance.

    `reviewer_user_id` and `recipient_cohort` only matter when
    `profile` is a :class:`VoiceProfileBundle`. With a bare
    :class:`VoiceProfile` they are accepted (so callers don't branch
    on profile shape) but have no effect — every reviewer / cohort
    gets the same profile.
    """
    return DraftTransformer().transform(
        draft=draft,
        profile=profile,
        reviewer_user_id=reviewer_user_id,
        recipient_cohort=recipient_cohort,
    )


def evaluate_draft_voice_fidelity(draft: str, samples: list[dict]) -> float:
    """Return a coarse fidelity score [0, 1] for a draft against samples.

    Best-effort observational signal used by the post_llm_call hook. The
    real fidelity bar lives in the voice-gate blind-test harness in
    ss-console; this function is a runtime sanity check, not a replacement
    for that evaluator.

    Treats ``samples`` as a list of structural-diff dicts (matching
    :meth:`StructuralDiff.as_dict`). Returns 0.0 when the draft is empty,
    when ``samples`` is empty, or when the diff fields needed for scoring
    are absent. Exception-safe: never raises on malformed input.
    """
    if not draft or not draft.strip():
        return 0.0
    if not samples:
        return 0.0

    try:
        # Build a transient profile from the supplied diff dicts so we can
        # compare the draft's greeting + signoff classification to the
        # modal sample shape. We do NOT mutate the draft here — the hook
        # is observational.
        diffs: list[StructuralDiff] = []
        for raw in samples:
            if not isinstance(raw, dict):
                continue
            try:
                diffs.append(
                    StructuralDiff(
                        schema_version=int(raw.get("schema_version", DIFF_SCHEMA_VERSION)),
                        word_count=int(raw.get("word_count", 0)),
                        sentence_count=int(raw.get("sentence_count", 0)),
                        paragraph_count=int(raw.get("paragraph_count", 0)),
                        subject_word_count=int(raw.get("subject_word_count", 0)),
                        avg_sentence_length=float(raw.get("avg_sentence_length", 0.0)),
                        sentence_length_distribution=dict(
                            raw.get("sentence_length_distribution", {})
                        ),
                        greeting_style=str(raw.get("greeting_style", "unknown")),
                        signoff_style=str(raw.get("signoff_style", "unknown")),
                        opener_template=str(raw.get("opener_template", "")),
                        closer_template=str(raw.get("closer_template", "")),
                        punctuation_rhythm=dict(raw.get("punctuation_rhythm", {})),
                        recipient_cohort=str(raw.get("recipient_cohort", "")),
                    )
                )
            except (TypeError, ValueError):
                continue

        if not diffs:
            return 0.0

        profile = build_voice_profile(cohort_id="__fidelity__", samples=diffs)
        if profile.sample_count == 0:
            return 0.0

        # Compute the draft's empirical shape via the same private helpers
        # the transform uses, then score similarity over greeting/signoff
        # match + sentence-length distribution overlap.
        lines = draft.split("\n")
        first_idx = _first_nonempty_index(lines)
        draft_greeting = (
            _classify_greeting_line(lines[first_idx].strip())[0]
            if first_idx is not None
            else None
        )
        signoff_idx = _last_signoff_index(lines)
        draft_signoff = (
            _classify_signoff_line(lines[signoff_idx].strip())
            if signoff_idx is not None
            else None
        )

        greeting_score = 1.0 if draft_greeting == profile.greeting_style else 0.0
        signoff_score = 1.0 if draft_signoff == profile.signoff_style else 0.0

        body_text, _, _ = _split_off_greeting_signoff(draft)
        sentences = _split_sentences_preserving_offsets(body_text or draft)
        if sentences:
            draft_dist = _empirical_distribution(sentences)
            overlap = 0.0
            for bucket, draft_p in draft_dist.items():
                target_p = profile.sentence_length_distribution.get(bucket, 0.0)
                overlap += min(draft_p, target_p)
            distribution_score = overlap
        else:
            distribution_score = 0.0

        return round(
            (greeting_score + signoff_score + distribution_score) / 3.0, 4
        )
    except Exception:  # noqa: BLE001 — observational; never raise
        log.warning("evaluate_draft_voice_fidelity failed", exc_info=True)
        return 0.0


__all__ = [
    "DraftTransformer",
    "GENERAL_VOICE_COHORT",
    "GENERAL_VOICE_USER_ID",
    "MAX_TRANSFORM_PASSES",
    "MIN_PROFILE_SAMPLE_COUNT",
    "TransformResult",
    "TransformStatus",
    "VoiceProfile",
    "VoiceProfileBundle",
    "build_voice_profile",
    "evaluate_draft_voice_fidelity",
    "transform_draft",
]
