"""hermes-smd-hook-probe - runtime verification of the documented hook surface.

Attaches to all six hooks the SMD overlay depends on at the pinned Hermes ref
(v2026.5.16) and emits one structured JSON log line per firing. The probe is
the belt-and-suspenders companion to the static-analysis citations recorded in
docs/hook-surface.md - if the probe runs against a stock Hermes container and
the expected log lines appear in the expected order, the citations are
load-bearing.

Hooks attached (firing sites at v2026.5.16):
- pre_tool_call           hermes_cli/plugins.py:1419-1426 from model_tools.py:778
- post_tool_call          model_tools.py:826-836
- pre_llm_call            run_agent.py:12447-12457
- post_llm_call           run_agent.py:15901-15910
- transform_tool_result   model_tools.py:847-857
- on_session_end          run_agent.py:16016-16024 (primary) + cli.py:13831-13839 (safety net)

Observer-only. Every callback returns None and swallows its own exceptions.
Logs ONLY kwarg key names and Python type names - never values - because
user content and secrets pass through these seams.
"""

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_NAME = "hermes_smd_hook_probe"

_sequence_lock = threading.Lock()
_sequence_counter = 0


def _next_sequence() -> int:
    """Return a monotonically-incrementing sequence number, thread-safe."""
    global _sequence_counter
    with _sequence_lock:
        _sequence_counter += 1
        return _sequence_counter


def _kwargs_digest(kwargs: dict[str, Any]) -> dict[str, str]:
    """Return a {key: type_name} mapping for safe logging.

    Never includes values - those may contain user content or secrets.
    """
    return {key: type(value).__name__ for key, value in kwargs.items()}


def _kwargs_seen(kwargs: dict[str, Any]) -> list[str]:
    """Return the sorted list of kwarg names received."""
    return sorted(kwargs.keys())


def _emit(hook_name: str, kwargs: dict[str, Any]) -> None:
    """Emit one JSON log line for a single hook firing."""
    try:
        payload = {
            "event": _EVENT_NAME,
            "hook_name": hook_name,
            "sequence": _next_sequence(),
            "timestamp_iso": datetime.now(UTC).isoformat(),
            "kwargs_digest": _kwargs_digest(kwargs),
            "kwargs_seen": _kwargs_seen(kwargs),
        }
        logger.info(json.dumps(payload))
    except Exception as exc:  # noqa: BLE001 - probe must never raise into Hermes
        logger.warning("hermes-smd-hook-probe: failed to emit %s: %s", hook_name, exc)


def on_pre_tool_call(**kwargs: Any) -> None:
    """Probe callback for pre_tool_call.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, task_id, session_id, tool_call_id
    """
    try:
        _emit("pre_tool_call", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-hook-probe: pre_tool_call handler error: %s", exc)


def on_post_tool_call(**kwargs: Any) -> None:
    """Probe callback for post_tool_call.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms
    """
    try:
        _emit("post_tool_call", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-hook-probe: post_tool_call handler error: %s", exc)


def on_pre_llm_call(**kwargs: Any) -> None:
    """Probe callback for pre_llm_call.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, conversation_history, is_first_turn, model, platform, sender_id
    """
    try:
        _emit("pre_llm_call", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-hook-probe: pre_llm_call handler error: %s", exc)


def on_post_llm_call(**kwargs: Any) -> None:
    """Probe callback for post_llm_call.

    Expected kwargs per docs/hook-surface.md:
        session_id, user_message, assistant_response, conversation_history, model, platform

    Only fires on completed, non-interrupted turns.
    """
    try:
        _emit("post_llm_call", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-hook-probe: post_llm_call handler error: %s", exc)


def on_transform_tool_result(**kwargs: Any) -> None:
    """Probe callback for transform_tool_result.

    Expected kwargs per docs/hook-surface.md (same shape as post_tool_call):
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms

    Fires after post_tool_call on the same execution path.
    """
    try:
        _emit("transform_tool_result", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes-smd-hook-probe: transform_tool_result handler error: %s", exc
        )


def on_session_end(**kwargs: Any) -> None:
    """Probe callback for on_session_end.

    Expected kwargs per docs/hook-surface.md:
        session_id, completed, interrupted, model, platform

    Fires from run_agent.py:16016-16024 (primary, per-turn) or from
    cli.py:13831-13839 (safety net on interrupted CLI exit). Never both for
    the same turn.
    """
    try:
        _emit("on_session_end", kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-smd-hook-probe: on_session_end handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires all six hooks the SMD overlay depends on."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("hermes-smd-hook-probe registered (all six hooks armed)")
