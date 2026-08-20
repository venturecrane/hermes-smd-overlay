"""Canonical MCP tool names — the one place the overlay reconciles Hermes'
wire naming with the names its policy tables are written in.

WHY THIS EXISTS (ss-console#2444, Hermes v0.18.0 -> v0.20.4 promotion).
Hermes v0.19 changed how an MCP tool is registered. At v2026.7.1 the runtime
name was ``mcp_<server>_<tool>`` (single underscores, the form every overlay
policy table is written in); from v0.19 it is ``mcp__<server>__<tool>``
(``MCP_TOOL_NAME_PREFIX = "mcp__"`` / ``_MCP_NAME_DELIM = "__"`` in upstream
``tools/mcp_tool.py``, adopted to match the Claude Code / Codex / OpenCode
convention and to disambiguate the server/tool boundary).

That rename is not cosmetic here. The overlay's trust gate classifies by EXACT
name and fails closed on an unknown one, so on the first v0.20.4 seat every
connector tool arrived unmapped and was REFUSED: the agent could not read a
matter, could not reach its own mailbox, and told the firm "all connector
tools are failing closed" (observed on hermes-pilot-smokeball 2026-08-20,
crane_verify vfy_01M0E9XW8MBR2G1P9XK81K5B34). The same rename would silently
un-match the destructive-tool ban list, the matter-binding content-read set,
the outbound send/draft sets, and the spec-read marks — every table keyed by
tool name.

THE FIX IS A TRANSLATION AT THE BOUNDARY, NOT A SECOND VOCABULARY. The overlay
keeps ONE spelling in its tables (the legacy single-underscore form, which is
also what every historical audit row carries, so ledger queries stay valid
across the upgrade). The umbrella fan-out wraps every tool-hook callback and
rewrites the incoming ``tool_name`` through :func:`canonical_tool_name` before
any overlay code sees it, and passes the untouched wire name alongside as
``tool_name_wire`` for the one consumer that needs it (connector-health looks
the name up in Hermes' own ``_mcp_tool_server_names`` dict, which is keyed by
the wire form).

Both spellings are accepted forever. A seat can run either Hermes version
during a staged promotion, and a rollback must not need an overlay change.
"""

from __future__ import annotations

import re

__all__ = ["MCP_WIRE_PREFIX", "canonical_tool_name", "is_wire_mcp_name"]

MCP_WIRE_PREFIX = "mcp__"

# ``mcp__<server>__<tool>`` -> ``mcp_<server>_<tool>``.
#
# Non-greedy server capture so the FIRST ``__`` after the prefix is the
# server/tool boundary — which is exactly the boundary upstream builds with,
# since both components are sanitized to ``[A-Za-z0-9_]`` and then joined with
# ``__``. A tool component containing ``__`` therefore stays intact in the
# canonical form's tail, and a server containing ``__`` is not producible.
_WIRE_RE = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)$")


def is_wire_mcp_name(tool_name: str) -> bool:
    """True when *tool_name* is Hermes' v0.19+ ``mcp__server__tool`` form."""
    return bool(_WIRE_RE.match(tool_name or ""))


def canonical_tool_name(tool_name: str) -> str:
    """The spelling the overlay's policy tables are written in.

    ``mcp__smokeball__list_matters`` -> ``mcp_smokeball_list_matters``.
    Any other name (core Hermes tools, overlay tools, the pre-v0.19 MCP form,
    empty/None-ish input) is returned unchanged, so this is safe to apply
    unconditionally at a hook boundary.
    """
    if not tool_name:
        return tool_name
    match = _WIRE_RE.match(tool_name)
    if match is None:
        return tool_name
    return f"mcp_{match.group('server')}_{match.group('tool')}"
