"""hermes-smd-trust — content-class trust ceilings + Composio isolation guard.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):
- pre_tool_call (model_tools.py:778 via get_pre_tool_call_block_message helper) —
  blocks tools that exceed the per-customer trust ceiling by returning
  {"action": "block", "message": "<reason>"}.
- transform_tool_result (model_tools.py:847-857) — refuses a Composio tool result
  whose connection_id doesn't match the customer's expected value, returning a
  replacement result string.

Real implementation ports from:
- ss-console/ai-employee/adapter/trust_ceiling.py → enforce.py
- ss-console/ai-employee/adapter/connectors/composio_assertion.py → composio_guard.py
in §7 of the build plan.
"""

import logging
from typing import Any

from . import composio_guard, enforce  # noqa: F401

logger = logging.getLogger(__name__)


def on_pre_tool_call(**kwargs: Any) -> dict | None:
    """Stub. Real impl returns {"action": "block", "message": "..."} when the
    tool exceeds the per-customer trust ceiling. Returns None otherwise (allow).

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, task_id, session_id, tool_call_id
    """
    logger.debug("hermes-smd-trust: pre_tool_call stub (port logic in §7)")
    return None


def on_transform_tool_result(**kwargs: Any) -> str | None:
    """Stub. Real impl returns a replacement result string when a Composio call
    is missing or has a mismatched connection_id. Returns None to leave the
    result unchanged.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms
    """
    logger.debug("hermes-smd-trust: transform_tool_result stub (port logic in §7)")
    return None


def register(ctx) -> None:
    """Plugin entry point. Wires both hooks."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    logger.info("hermes-smd-trust registered (stub mode; awaiting §7 logic port)")
