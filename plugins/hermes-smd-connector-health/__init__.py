"""hermes-smd-connector-health — per-server call-outcome capture (ss#1990).

A Smokeball API outage or a dead Graph token fails every tool call while
every liveness signal stays green — ADR 0079's named accepted gap. This
plugin is the agent-side half of closing it: it observes every MCP tool
call's outcome and maintains the per-server failure ledger
(:mod:`shared.connector_ledger`) that the gate's heartbeat emitter reads
and ships to the console, where the fleet alerter evaluates
``connector_down:<server>`` / ``connector_check_error`` conditions
(ADR 0080).

Attaches to ONE hook at the pinned Hermes ref (v2026.7.1 @ 7c1a0295):

- ``post_tool_call`` (``model_tools.py:853``, emitted at :1178/:1078) —
  observer-only, fires per tool invocation with ``tool_name``, ``status``
  ("ok"|"error"|"blocked"), ``error_type`` (None|"tool_error"|
  "plugin_block") and ``error_message``. MCP failures never raise; they
  arrive here as ``status="error"`` (Hermes converts them to
  ``{"error": ...}`` results, including server-not-connected and
  transport-down).

What counts (ADR 0080 failure semantics):

* Only tools that resolve to an MCP server via Hermes'
  ``tools.mcp_tool._mcp_tool_server_names`` — the authoritative mapping
  populated at tool registration. There is deliberately NO prefix-parse
  fallback: sanitized server names contain underscores, so parsing
  ``mcp_{server}_{tool}`` is ambiguous, and a misparse would mint a
  phantom-key alert with no path to RECOVERED. Unmapped ``mcp_*`` names
  are logged once and not counted (undercount is the doctrinally safe
  failure). If the mapping import itself breaks (pin bump moved it), the
  ledger is flagged ``mapping_ok=False`` so the console PAGES the dark
  window instead of the alert class dying silently.
* ``status="ok"`` → success (resets the server's failure run).
* ``status="error"`` with ``error_type="tool_error"`` → failure, tagged
  conn-class when the message matches
  :mod:`shared.connector_signatures`.
* ``status="blocked"`` / ``error_type="plugin_block"`` → ignored: our own
  trust plugin refusing a call is policy, not outage.

Observer-only and exception-safe per AGENTS.md hard rule #3: the callback
always returns None and swallows its own exceptions — health capture must
never break the agent turn.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.connector_ledger import ledger_path, mark_mapping_broken, record_call
from shared.connector_signatures import is_conn_class

logger = logging.getLogger(__name__)

# One-shot warning latches (module state; per agent process). The mapping
# flag also lands in the ledger so the GATE can report it on the wire —
# a log line alone would be exactly the invisible failure this system
# exists to kill.
_MAPPING_BROKEN = False
_UNMAPPED_WARNED: set[str] = set()


def _resolve_server(tool_name: str) -> str | None:
    """Sanitized MCP server name for ``tool_name``, or None to not count.

    Imports the mapping at call time (not register time): MCP servers
    register after plugin load, and the dict is module state that fills as
    they do.
    """
    global _MAPPING_BROKEN
    try:
        from tools.mcp_tool import _mcp_tool_server_names
    except Exception as exc:  # noqa: BLE001 — pin bump may move/remove it
        if not _MAPPING_BROKEN:
            _MAPPING_BROKEN = True
            logger.error(
                "hermes-smd-connector-health: cannot import "
                "tools.mcp_tool._mcp_tool_server_names (%s) — connector "
                "health is NOT being counted; flagging ledger so the "
                "console pages",
                exc,
            )
            mark_mapping_broken()
        return None
    server = _mcp_tool_server_names.get(tool_name)
    if server is None and tool_name.startswith("mcp_") and tool_name not in _UNMAPPED_WARNED:
        _UNMAPPED_WARNED.add(tool_name)
        logger.warning(
            "hermes-smd-connector-health: %s looks like an MCP tool but is "
            "not in the server mapping; not counted",
            tool_name,
        )
    return server if isinstance(server, str) and server else None


def on_post_tool_call(**kwargs: Any) -> None:
    """Record one MCP tool-call outcome into the connector ledger."""
    try:
        status = kwargs.get("status")
        if status not in ("ok", "error"):
            return  # "blocked" and anything unrecognized: not a health signal
        if kwargs.get("error_type") == "plugin_block":
            return  # our own policy layer, not the connector
        tool_name = kwargs.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return
        server = _resolve_server(tool_name)
        if server is None:
            return
        if status == "ok":
            record_call(server, ok=True)
        else:
            message = kwargs.get("error_message")
            message = message if isinstance(message, str) else None
            record_call(
                server,
                ok=False,
                error_message=message,
                conn_class=is_conn_class(message),
            )
    except Exception as exc:  # noqa: BLE001 — never raise out of a hook
        logger.warning("hermes-smd-connector-health: post_tool_call handler error: %s", exc)


def register(ctx) -> None:
    """Plugin entry point. Wires ``post_tool_call`` unconditionally.

    No env to resolve and no authorization flag: this is a structural,
    content-agnostic observer whose only side effect is a local tmpfs file.
    """
    ctx.register_hook("post_tool_call", on_post_tool_call)
    logger.info(
        "hermes-smd-connector-health registered (per-server call outcomes → %s)",
        ledger_path(),
    )
