"""hermes-smd-voice — sample-driven voice transformation.

Attaches to three hooks at the pinned Hermes ref (v2026.5.16):

- ``pre_llm_call`` (run_agent.py:12447-12457) — injects relevant voice
  samples from the customer's R2 vault into the user-message context
  BEFORE the model sees the turn. Per Hermes' contract, this preserves
  the system-prompt cache.

- ``transform_llm_output`` (run_agent.py:15874-15893) — structurally
  reshapes the model's response to match the customer's authored voice.
  Layer 1 (pre_llm_call) sets the register; Layer 2 (this hook) reshapes
  whatever the model produced. First non-empty string returned wins; None
  leaves the response unchanged. Exception-safe: a failed transform never
  breaks the agent loop.

- ``post_llm_call`` (run_agent.py:15901-15910) — evaluates the draft
  response for voice fidelity (observational; the real fidelity gate
  lives in the voice-gate blind-test harness in ss-console, NOT in the
  runtime plugin).

The runtime is sample-driven (not rule-based). The agent is shown
examples of the customer's own writing and matches the style. Voice
transformation logic lives in :mod:`transform`; R2 retrieval in
:mod:`samples`. Both modules expose pure-Python helpers that the hooks
below invoke through exception-safe wrappers.

Per AGENTS.md hard rule #3: a noisy callback creates log spam but a
raising callback can still create context churn. All hooks below catch
every exception, log a warning, and return a safe value (``None`` for
pre_llm_call and transform_llm_output, nothing for post_llm_call). A
voice-transformation failure on one turn never breaks the agent loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from shared import provenance
from shared.voice_status import VOICE_STATUS

from . import samples, transform  # noqa: F401 — surface module imports for tests
from .diff import SCHEMA_VERSION as _DIFF_SCHEMA_VERSION
from .diff import StructuralDiff
from .transform import (
    GENERAL_VOICE_COHORT,
    TransformStatus,
    VoiceProfileBundle,
    build_voice_profile,
    transform_draft,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level runtime state
# ---------------------------------------------------------------------------

# R2 reader and customer slug. Wired by bind_runtime(); None until then.
_R2_READER: samples.R2SampleReader | None = None
_CUSTOMER_SLUG: str | None = None

# Cached VoiceProfileBundle built from all available samples.  Built lazily
# on the first transform_llm_output call and reused for the lifetime of the
# process (samples change only when the Machine reboots and re-syncs the
# voice vault).  A new bind_runtime() call (e.g. in tests) resets the cache.
_VOICE_BUNDLE: VoiceProfileBundle | None = None
_VOICE_BUNDLE_LOCK = threading.Lock()

# Max samples to load when building the structural profile.  Higher than
# MAX_SAMPLES_PER_TURN (which caps prompt context); profile accuracy improves
# with more samples and the cost is paid once at cache-build time, not per turn.
_PROFILE_SAMPLE_LIMIT = 256


def bind_runtime(*, customer_slug: str, r2_reader: samples.R2SampleReader) -> None:
    """Bind runtime collaborators that make the hooks active.

    Called by :func:`register` itself (which constructs the reader from the
    Machine's R2 env via ``samples.reader_from_env``), and directly by tests
    that inject a fake reader. Until this runs, ``_R2_READER`` is ``None`` and
    all hooks no-op; register() emits a WARNING in that case so an unbound
    plugin is never mistaken for a healthy one.

    Resets the cached VoiceProfileBundle so the next transform call rebuilds
    it from the newly-bound reader.
    """
    global _R2_READER, _CUSTOMER_SLUG, _VOICE_BUNDLE
    _R2_READER = r2_reader
    _CUSTOMER_SLUG = customer_slug
    _VOICE_BUNDLE = None  # invalidate on rebind (tests, reprovision)
    # Publish the samples probe the trust plugin's voice live-gate consults
    # (ADR 0028 §2). The probe reads the live globals, so a rebind takes effect
    # without republishing.
    VOICE_STATUS.publish_samples_probe(_probe_samples_available)
    logger.info(
        "hermes-smd-voice: runtime bound customer=%s r2=%s",
        customer_slug,
        type(r2_reader).__name__,
    )


def _probe_samples_available() -> bool:
    """Zero-arg probe published to ``shared.voice_status`` for the trust plugin's
    voice live-gate: are ≥1 voice samples retrievable for this seat right now?

    Reads the live bound reader / slug so a rebind is reflected without
    republishing. Returns ``False`` when the runtime is unbound.
    ``retrieve_relevant_samples`` is itself exception-safe (empty list on R2
    error), and ``VOICE_STATUS.samples_available`` wraps this call in a
    fail-closed try — so a probe fault degrades to "no samples" (draft), never a
    crash."""
    if _R2_READER is None or not _CUSTOMER_SLUG:
        return False
    hits = samples.retrieve_relevant_samples(
        customer_slug=_CUSTOMER_SLUG,
        r2_reader=_R2_READER,
        query_context=None,
        limit=1,
    )
    return len(hits) >= 1


# ---------------------------------------------------------------------------
# Voice bundle helpers
# ---------------------------------------------------------------------------


def _dicts_to_structural_diffs(raw_dicts: list[dict]) -> list[StructuralDiff]:
    """Convert a list of structural-diff dicts (from R2) into StructuralDiff objects.

    Mirrors the conversion logic in evaluate_draft_voice_fidelity — both
    surfaces read from the same R2 JSON schema.  Malformed rows are silently
    skipped so one corrupt sample never aborts the profile build.
    """
    result: list[StructuralDiff] = []
    for raw in raw_dicts:
        if not isinstance(raw, dict):
            continue
        try:
            result.append(
                StructuralDiff(
                    schema_version=int(raw.get("schema_version", _DIFF_SCHEMA_VERSION)),
                    word_count=int(raw.get("word_count", 0)),
                    sentence_count=int(raw.get("sentence_count", 0)),
                    paragraph_count=int(raw.get("paragraph_count", 0)),
                    subject_word_count=int(raw.get("subject_word_count", 0)),
                    avg_sentence_length=float(raw.get("avg_sentence_length", 0.0)),
                    sentence_length_distribution=dict(raw.get("sentence_length_distribution", {})),
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
    return result


def _build_bundle() -> VoiceProfileBundle | None:
    """Load all available samples and aggregate into a VoiceProfileBundle.

    Builds only the general (customer-wide) profile — per-user and
    per-cohort profiling are Phase 2 (requires authored voice_profile_id
    assignments in customer.yaml and sufficient per-reviewer sample counts).

    Returns None when no samples are available or the reader is unbound.
    """
    if _R2_READER is None or not _CUSTOMER_SLUG:
        return None

    raw_diffs = samples.retrieve_relevant_samples(
        customer_slug=_CUSTOMER_SLUG,
        r2_reader=_R2_READER,
        query_context=None,
        limit=_PROFILE_SAMPLE_LIMIT,
    )
    if not raw_diffs:
        logger.warning(
            "hermes-smd-voice: no voice samples found for customer=%s; "
            "transform will passthrough until samples are ingested",
            _CUSTOMER_SLUG,
        )
        return None

    struct_diffs = _dicts_to_structural_diffs(raw_diffs)
    if not struct_diffs:
        return None

    general_profile = build_voice_profile(
        cohort_id=GENERAL_VOICE_COHORT,
        samples=struct_diffs,
    )
    logger.info(
        "hermes-smd-voice: built general voice profile customer=%s samples=%d",
        _CUSTOMER_SLUG,
        general_profile.sample_count,
    )
    return VoiceProfileBundle(general=general_profile, per_user={}, per_user_cohort={})


def _get_cached_bundle() -> VoiceProfileBundle | None:
    """Return the cached VoiceProfileBundle, building it lazily on first call."""
    global _VOICE_BUNDLE
    if _VOICE_BUNDLE is not None:
        return _VOICE_BUNDLE
    with _VOICE_BUNDLE_LOCK:
        if _VOICE_BUNDLE is None:
            _VOICE_BUNDLE = _build_bundle()
        return _VOICE_BUNDLE


# ---------------------------------------------------------------------------
# Hook implementations
# ---------------------------------------------------------------------------


def on_pre_llm_call(**kwargs: Any) -> dict | None:
    """Inject relevant voice samples into the user message context.

    Expected kwargs per docs/hook-surface.md §3 (pre_llm_call):

        session_id, user_message, conversation_history, is_first_turn,
        model, platform, sender_id

    Returns ``{"context": "<sample block>"}`` when samples are
    available; ``None`` otherwise (which Hermes treats as "no context
    contribution from this plugin"). Multi-plugin context is joined
    with ``\\n\\n`` per the firing-site contract — the block produced
    here is self-contained and safe to concatenate.

    Exception-safe per AGENTS.md hard rule #3. Any failure (missing
    binding, R2 outage, malformed sample, surprise exception) yields
    ``None`` and a logged warning.
    """
    try:
        # Per-turn voice live-gate marker (ADR 0028 §2): clear at the start of
        # EVERY turn so a prior turn's successful transform can never certify
        # THIS turn's send. Keyed by the resolved session id (the same value the
        # trust gate reads under). Runs before the unbound check — the clear is
        # cheap and keeps the marker strictly per-turn regardless of bind state.
        try:
            VOICE_STATUS.clear_turn(provenance.resolve_session(kwargs.get("session_id") or ""))
        except Exception:  # noqa: BLE001 — marker bookkeeping must never break the hook
            logger.debug("hermes-smd-voice: per-turn marker clear failed", exc_info=True)

        if _R2_READER is None or not _CUSTOMER_SLUG:
            logger.debug("hermes-smd-voice: pre_llm_call no-op (runtime unbound)")
            return None

        query_context: dict = {}
        sender_id = kwargs.get("sender_id")
        if sender_id:
            query_context["sender_id"] = sender_id

        sample_dicts = samples.retrieve_relevant_samples(
            customer_slug=_CUSTOMER_SLUG,
            r2_reader=_R2_READER,
            query_context=query_context,
        )
        block = samples.render_sample_block(sample_dicts)
        if not block:
            return None
        return {"context": block}
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-voice: pre_llm_call failed", exc_info=True)
        return None


def on_transform_llm_output(**kwargs: Any) -> str | None:
    """Structurally reshape the model's response to match the customer's voice.

    Expected kwargs per run_agent.py:15882 (transform_llm_output):

        response_text, session_id, model, platform

    This is Layer 2 of the voice stack. Layer 1 (pre_llm_call) sets the
    register by injecting style examples; Layer 2 reshapes whatever the model
    produced so the structural signature (sentence length distribution,
    paragraph density, greeting/signoff style) matches the customer's authored
    voice profile.

    Returns the reshaped text when ``TransformStatus.TRANSFORMED``; returns
    ``None`` (Hermes leaves the response unchanged) for every passthrough case:

    - No samples / insufficient profile (< MIN_PROFILE_SAMPLE_COUNT=5)
    - Draft already matches profile within tolerance
    - Empty response
    - Fabrication guard fired (transform proposed an illegal token)
    - Any exception

    Exception-safe per AGENTS.md hard rule #3.
    """
    try:
        if _R2_READER is None or not _CUSTOMER_SLUG:
            return None

        response_text = kwargs.get("response_text") or ""
        if not response_text.strip():
            return None

        bundle = _get_cached_bundle()
        if bundle is None:
            return None

        result = transform_draft(draft=response_text, profile=bundle)

        logger.info(
            "voice.transform status=%s changes=%s profile_n=%d session=%s",
            result.status.value,
            result.changes_applied,
            result.profile_sample_count,
            kwargs.get("session_id", ""),
        )

        if result.status == TransformStatus.TRANSFORMED:
            # Voice live-gate marker (ADR 0028 §2): the transform demonstrably
            # reshaped this turn's output, so mark THIS turn as voice-applied.
            # Set ONLY on a successful transform — a passthrough (below) or a
            # swallowed exception (the except clause) leaves the mark clear, so a
            # send on such a turn fails the gate (draft). Keyed by the resolved
            # session id (matches the trust gate's read key).
            VOICE_STATUS.mark_applied(provenance.resolve_session(kwargs.get("session_id") or ""))
            return result.transformed_draft
        return None
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-voice: transform_llm_output failed", exc_info=True)
        return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Observe per-turn draft fidelity for the voice samples in play.

    Expected kwargs per docs/hook-surface.md §4 (post_llm_call):

        session_id, user_message, assistant_response,
        conversation_history, model, platform

    Observer only — the firing site discards return values
    (run_agent.py:15901-15910). The hook computes a coarse fidelity
    score against the customer's R2 samples and logs it; the real
    fidelity bar lives in the ss-console voice-gate harness.

    Exception-safe per AGENTS.md hard rule #3.
    """
    try:
        if _R2_READER is None or not _CUSTOMER_SLUG:
            return

        draft = kwargs.get("assistant_response") or ""
        if not draft.strip():
            return

        sample_dicts = samples.retrieve_relevant_samples(
            customer_slug=_CUSTOMER_SLUG,
            r2_reader=_R2_READER,
            query_context=None,
        )
        if not sample_dicts:
            return

        fidelity = transform.evaluate_draft_voice_fidelity(draft, sample_dicts)
        session_id = kwargs.get("session_id", "")
        logger.info(
            "voice.fidelity session=%s model=%s fidelity=%.4f samples=%d",
            session_id,
            kwargs.get("model", ""),
            fidelity,
            len(sample_dicts),
        )
    except Exception:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-voice: post_llm_call failed", exc_info=True)


def register(ctx) -> None:
    """Plugin entry point. Wires all three hooks AND binds the runtime.

    Self-sufficient binding: register resolves ``SMD_CUSTOMER_SLUG`` and
    constructs the R2 voice-sample reader from the Machine's R2 env
    (``samples.reader_from_env``), then calls :func:`bind_runtime`. There is no
    separate out-of-band boot step — the previous design *defined*
    ``bind_runtime`` but nothing ever called it, so both hooks silently
    no-op'd forever (the plugin reported a healthy register while doing
    nothing — the exact fail-silent anti-pattern this overlay guards against).

    If the customer slug or the R2 credentials are absent, the plugin does NOT
    pretend to be active: it logs a WARNING naming what is missing and leaves
    the runtime unbound (hooks no-op, but loudly and explained, not silently).
    Tests may call :func:`bind_runtime` directly to inject a fake reader.
    """
    import os

    global _CUSTOMER_SLUG
    slug = os.environ.get("SMD_CUSTOMER_SLUG") or None

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("post_llm_call", on_post_llm_call)

    reader = samples.reader_from_env()
    if slug and reader is not None:
        bind_runtime(customer_slug=slug, r2_reader=reader)
        logger.info("hermes-smd-voice registered and ACTIVE (customer=%s)", slug)
        return

    # Not active — be explicit about WHY. Never a silent healthy-looking no-op.
    _CUSTOMER_SLUG = slug
    missing = []
    if not slug:
        missing.append("SMD_CUSTOMER_SLUG")
    if reader is None:
        missing.append(
            "voice vault (SMD_VOICE_VAULT_DIR boot-sync dir, or R2 env "
            "R2_ENDPOINT_URL/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET_CONFIG)"
        )
    logger.warning(
        "hermes-smd-voice registered but INACTIVE — voice transformation will NOT run. "
        "Missing: %s. Hooks will no-op until the runtime is bound.",
        "; ".join(missing),
    )


__all__ = [
    "bind_runtime",
    "on_post_llm_call",
    "on_pre_llm_call",
    "on_transform_llm_output",
    "register",
]
