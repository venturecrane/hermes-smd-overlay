"""hermes-smd-voice — sample-driven voice transformation.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):

- ``pre_llm_call`` (run_agent.py:12447-12457) — injects relevant voice
  samples from the customer's R2 vault into the user-message context
  BEFORE the model sees the turn. Per Hermes' contract, this preserves
  the system-prompt cache.

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
raising callback can still create context churn. Both hooks below catch
every exception, log a warning, and return a safe value (``None`` for
pre_llm_call, nothing for post_llm_call). A voice-transformation
failure on one turn never breaks the agent loop.
"""

from __future__ import annotations

import logging
from typing import Any

from . import samples, transform  # noqa: F401 — surface module imports for tests

logger = logging.getLogger(__name__)


# Module-level R2 reader binding. The Machine boot path wires a real
# R2 client into this slot before the agent loop starts; tests can
# patch it directly. ``None`` means "no binding available" — the hook
# degrades to a no-op rather than raising.
_R2_READER: samples.R2SampleReader | None = None

# Module-level customer slug. Resolved at register-time from
# ``SMD_CUSTOMER_SLUG``. Missing slug means "single-tenant Machine boot
# misconfigured" — the hook degrades to a no-op rather than raising so
# the agent still responds.
_CUSTOMER_SLUG: str | None = None


def bind_runtime(*, customer_slug: str, r2_reader: samples.R2SampleReader) -> None:
    """Bind runtime collaborators after register() returns.

    The Machine boot sequence:

      1. Loads plugin entry points (calls ``register(ctx)``).
      2. Constructs the customer-scoped R2 client.
      3. Calls ``bind_runtime`` with the resolved slug + R2 binding.

    Step 3 is what makes the hooks active. Without it, both hooks
    return ``None`` and the plugin behaves like a no-op.
    """
    global _R2_READER, _CUSTOMER_SLUG
    _R2_READER = r2_reader
    _CUSTOMER_SLUG = customer_slug
    logger.info(
        "hermes-smd-voice: runtime bound customer=%s r2=%s",
        customer_slug,
        type(r2_reader).__name__,
    )


async def on_pre_llm_call(**kwargs: Any) -> dict | None:
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
        if _R2_READER is None or not _CUSTOMER_SLUG:
            logger.debug("hermes-smd-voice: pre_llm_call no-op (runtime unbound)")
            return None

        query_context: dict = {}
        sender_id = kwargs.get("sender_id")
        if sender_id:
            query_context["sender_id"] = sender_id

        sample_dicts = await samples.retrieve_relevant_samples_async(
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


async def on_post_llm_call(**kwargs: Any) -> None:
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

        sample_dicts = await samples.retrieve_relevant_samples_async(
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
    """Plugin entry point. Wires both hooks.

    The Machine boot path resolves ``SMD_CUSTOMER_SLUG`` from env and
    calls :func:`bind_runtime` after register returns. If the slug is
    missing here, register still succeeds — the hook callbacks fall
    back to no-ops.
    """
    import os

    global _CUSTOMER_SLUG
    _CUSTOMER_SLUG = os.environ.get("SMD_CUSTOMER_SLUG") or None

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info(
        "hermes-smd-voice registered (customer=%s, awaiting bind_runtime)",
        _CUSTOMER_SLUG or "<unset>",
    )


__all__ = [
    "bind_runtime",
    "on_post_llm_call",
    "on_pre_llm_call",
    "register",
]
