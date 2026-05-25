"""hermes-smd-trust — content-class trust ceilings + Composio isolation guard.

Attaches to two hooks at the pinned Hermes ref (v2026.5.16):

- ``pre_tool_call`` (model_tools.py:778 via ``get_pre_tool_call_block_message``
  at hermes_cli/plugins.py:1396) — blocks tools that exceed the per-customer
  trust ceiling by returning ``{"action": "block", "message": "<reason>"}``.

- ``transform_tool_result`` (model_tools.py:847-857) — refuses a Composio
  tool result whose ``connection_id`` doesn't match the customer's expected
  value, returning a replacement result string.

Per AGENTS.md hard rule #3 both callbacks are exception-safe: a raise from
the policy or guard modules is caught at the hook boundary so a faulty
plugin cannot break the agent loop. Audit observation of refusals happens
downstream via the audit plugin's ``post_tool_call`` hook on the resulting
error result; this plugin does not cross-import the audit plugin.
"""

import logging
from typing import Any

from shared.secrets import get_secret

from . import composio_guard, enforce

logger = logging.getLogger(__name__)


# Env-var carrying the customer's bound Composio connection ID. Materialized
# from ``customer.yaml.connectors{composio}.composio_connection_id`` at
# provisioning. Absent in dev / pre-Composio environments — when missing,
# the transform_tool_result hook leaves non-Composio results alone and
# refuses Composio results loud.
_COMPOSIO_CONNECTION_ID_ENV = "SMD_COMPOSIO_CONNECTION_ID"


def _resolve_expected_connection_id() -> str | None:
    """Return the bound Composio connection ID or None if not provisioned."""
    try:
        return get_secret(_COMPOSIO_CONNECTION_ID_ENV)
    except KeyError:
        return None


def on_pre_tool_call(**kwargs: Any) -> dict | None:
    """Block a tool call that exceeds the per-customer trust ceiling.

    Returns ``{"action": "block", "message": "<reason>"}`` to refuse, or
    ``None`` to allow the call. Exception-safe: any raise is logged and
    swallowed, returning None (allow) so the agent loop continues. The
    pre_tool_call helper's contract (hermes_cli/plugins.py:1428-1437)
    only accepts the block-directive shape; other return values are
    ignored by the dispatcher, which means an exception-on-allow path
    is safe.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, task_id, session_id, tool_call_id
    """
    try:
        tool_name = kwargs.get("tool_name") or ""
        args = kwargs.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        customer_slug = kwargs.get("customer_slug")
        if not isinstance(customer_slug, str) or not customer_slug:
            # Fall back to the env var the Machine boots with. The slug is
            # not load-bearing for the policy decision itself (the customer
            # ceiling resolves from customer.yaml / env), but downstream
            # audit observation expects it.
            try:
                customer_slug = get_secret("SMD_CUSTOMER_SLUG")
            except KeyError:
                customer_slug = ""

        return enforce.evaluate_tool_call(tool_name, args, customer_slug)
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.exception(
            "hermes-smd-trust: pre_tool_call raised; allowing call to proceed "
            "(safety: trust-ceiling decision unavailable for this tool)"
        )
        return None


def on_transform_tool_result(**kwargs: Any) -> str | None:
    """Refuse a Composio tool result whose connection_id is foreign.

    Returns a replacement result string when the guard refuses, or
    ``None`` to leave the result unchanged. Exception-safe.

    Expected kwargs per docs/hook-surface.md:
        tool_name, args, result, task_id, session_id, tool_call_id, duration_ms
    """
    try:
        tool_name = kwargs.get("tool_name") or ""
        result = kwargs.get("result")
        expected = _resolve_expected_connection_id()

        # When no Composio binding exists for this Machine, only refuse
        # Composio-prefixed tool results; everything else passes through.
        # ``verify_composio_response`` itself handles the missing-expected
        # case by refusing Composio calls loud.
        return composio_guard.verify_composio_response(
            tool_name,
            result,
            expected or "",
        )
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.exception(
            "hermes-smd-trust: transform_tool_result raised; leaving result "
            "unchanged (safety: Composio guard unavailable for this call)"
        )
        return None


def register(ctx) -> None:
    """Plugin entry point. Wires both hooks."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)
    logger.info(
        "hermes-smd-trust registered: pre_tool_call + transform_tool_result"
    )
