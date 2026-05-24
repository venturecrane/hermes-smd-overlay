"""hermes-smd-audit — per-tool and per-LLM-call audit emission to per-customer D1.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):
- post_tool_call (model_tools.py:826-836) — emits one D1 row per tool invocation
- post_llm_call (run_agent.py:15901-15910) — emits one D1 row per completed turn

Real implementation ports from ss-console/ai-employee/adapter/audit_log.py +
audit_emit_points.py + audit_log_immutability.py in §7 of the build plan.
"""

import logging
from typing import Any

from . import emit, schemas  # noqa: F401 — surface module imports for downstream tests

logger = logging.getLogger(__name__)


def on_post_tool_call(**kwargs: Any) -> None:
    """Stub. Real impl writes one audit_log row per tool call.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms
    """
    logger.debug("hermes-smd-audit: post_tool_call stub (port logic in §7)")


def on_post_llm_call(**kwargs: Any) -> None:
    """Stub. Real impl writes one audit_log row per completed LLM turn.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, assistant_response, conversation_history, model, platform
    """
    logger.debug("hermes-smd-audit: post_llm_call stub (port logic in §7)")


def register(ctx) -> None:
    """Plugin entry point. Wires both hooks."""
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info("hermes-smd-audit registered (stub mode; awaiting §7 logic port)")
