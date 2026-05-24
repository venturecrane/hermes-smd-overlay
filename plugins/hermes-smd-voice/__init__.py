"""hermes-smd-voice — sample-driven voice transformation.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):
- pre_llm_call (run_agent.py:12447-12457) — injects relevant voice samples
  from the customer's R2 vault into the user-message context BEFORE the model
  sees the turn. Per Hermes' contract, this preserves the system-prompt cache.
- post_llm_call (run_agent.py:15901-15910) — transforms the draft response
  in voice-fidelity-critical paths. (Used sparingly; over-aggressive
  post-transform reads as inauthentic.)

Real implementation ports from ss-console/ai-employee/adapter/voice/ in §7
of the build plan.
"""

import logging
from typing import Any

from . import samples, transform  # noqa: F401

logger = logging.getLogger(__name__)


def on_pre_llm_call(**kwargs: Any) -> dict | None:
    """Stub. Real impl returns {"context": "<sample block>"} injecting
    customer-voice samples relevant to this turn.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, conversation_history, is_first_turn, model,
        platform, sender_id
    """
    logger.debug("hermes-smd-voice: pre_llm_call stub (port logic in §7)")
    return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Stub. Real impl evaluates draft against voice samples for fidelity
    measurement and optional rewrite. (Mostly observational — post-transform
    is used sparingly per voice-gate findings.)

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, assistant_response, conversation_history,
        model, platform
    """
    logger.debug("hermes-smd-voice: post_llm_call stub (port logic in §7)")


def register(ctx) -> None:
    """Plugin entry point. Wires both hooks."""
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info("hermes-smd-voice registered (stub mode; awaiting §7 logic port)")
