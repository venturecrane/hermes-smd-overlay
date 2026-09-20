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

It also carries a second, unrelated observation that happens to need the
same import. Because this file already holds Hermes' authoritative
registered-tool mapping, it is the one place that can see the whole
offered tool surface, so :func:`_sweep_tool_surface` names any registered
tool the action-class map does not classify — a vendor adding a verb
otherwise produces nothing but REFUSED calls nobody can explain. See that
function's docstring for why it warns instead of fixing.

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
_SURFACE_SWEPT = False


def _sweep_tool_surface(mapping: dict[str, Any]) -> None:
    """Name every REGISTERED MCP tool that the action-class map does not classify.

    Why this exists. ``TOOL_ACTION_CLASS_MAP`` is a hand-maintained snapshot of
    surfaces we do not vendor, and an unmapped tool fails closed — every call
    REFUSED, with a message the model cannot interpret or route around. A vendor
    ships new tools on its own schedule with no coordinated change on our side,
    so the snapshot rots silently. It has rotted three times now: the Brave
    single-name form (overlay#148), the v0.19 ``mcp__server__tool`` rename that
    unmapped EVERY connector tool (``shared/mcp_tool_names.py:11-21``), and the
    agentmail verbs this sweep shipped with. All three were caught by someone
    driving a live seat, never by CI — which is the whole argument for observing
    the registry at runtime instead of pinning it in a test.

    This does NOT make a drifted tool work, and deliberately so: auto-classifying
    an unknown vendor tool would be exactly the fail-open that the REFUSED
    terminal class exists to prevent. It converts a silent dead end into a named
    one, and a human lands the map entry.

    Severity is the signal, and the floor is higher than it looks.

    Drift logs at ERROR so it becomes a Sentry event in ``smd-operator``
    (``shared/sentry_init.py`` installs no ``LoggingIntegration``, so the SDK
    default ``event_level=ERROR`` applies and anything below it is a breadcrumb
    nobody reads).

    A CLEAN sweep logs at WARNING. It was INFO for exactly one pin, and that was
    wrong: ``hermes_plugins.*`` INFO does not reach the seat's log at all. The
    control is this plugin's own ``register()`` line, which is unconditional --
    a captured full boot of pilot-smokeball on 2026-09-19 contains sixteen boot
    markers and zero "hermes-smd-connector-health registered". So the clean line
    was invisible in production, which destroyed the very property it existed to
    provide: "swept and clean" and "never swept" were both silence.

    WARNING is the narrow band that works. It reaches the log, and it sits below
    Sentry's ERROR floor, so a healthy seat still pages nobody -- a safety signal
    that fires on every known-good boot is one people learn to ignore. Volume is
    one line per agent process, not per turn.

    The line is emitted either way, so a sweep that found nothing says ``0
    unclassified`` and a sweep that never ran says nothing at all. That
    distinction is the point of the whole function; if a future change makes the
    clean branch quiet again, it has removed the instrument's ability to prove it
    ran.

    Reads post-exclusion state by construction: ``blocked_tools`` never reaches
    Hermes' registry (``bootstrap/translate.py`` writes them as an ``exclude``
    list), so this sees exactly the tools the agent can actually call, which is
    exactly the set that needs classifying.

    Per-process and per-seat. One seat's registry reflects only the connectors
    that seat binds, so a clean line here is not a claim about the fleet.
    """
    global _SURFACE_SWEPT
    if _SURFACE_SWEPT or not mapping:
        return
    _SURFACE_SWEPT = True
    try:
        from shared.action_classes import BANNED_TOOLS, TOOL_ACTION_CLASS_MAP
        from shared.mcp_tool_names import canonical_tool_name
    except Exception as exc:  # noqa: BLE001 — observer, never raises
        logger.error(
            "SMD OVERLAY TOOL SURFACE SWEEP: cannot import the action-class "
            "map (%s) — drift is NOT being observed on this seat",
            exc,
        )
        return
    known = set(TOOL_ACTION_CLASS_MAP) | set(BANNED_TOOLS)
    unclassified = sorted({canonical_tool_name(wire) for wire in mapping} - known)
    if unclassified:
        logger.error(
            "SMD OVERLAY TOOL SURFACE SWEEP: %d registered, %d unclassified: %s "
            "— each REFUSES on every call until it is added to "
            "TOOL_ACTION_CLASS_MAP (a vendor tool needs the map entry; an "
            "author-built connector tool needs its manifest tool_classes entry "
            "in the SAME change, or the boot probe FATALs the seat)",
            len(mapping),
            len(unclassified),
            ", ".join(unclassified),
        )
    else:
        # WARNING, not INFO: plugin INFO does not reach the seat's log, so an
        # INFO clean line is indistinguishable from no sweep at all. See the
        # docstring. Below Sentry's ERROR floor, so this pages nobody.
        logger.warning(
            "SMD OVERLAY TOOL SURFACE SWEEP: %d registered, 0 unclassified",
            len(mapping),
        )


def _resolve_server(tool_name: str) -> str | None:
    """Sanitized MCP server name for ``tool_name``, or None to not count.

    Imports the mapping at call time (not register time): MCP servers
    register after plugin load, and the dict is module state that fills as
    they do.

    ``tool_name`` here MUST be the WIRE name (Hermes' own registry spelling —
    ``mcp__server__tool`` from v0.19, ``mcp_server_tool`` before it), because
    the dict this reads is Hermes'. Every other overlay consumer sees the
    canonical single-underscore form the umbrella fan-out rewrites to; this is
    the one place that deliberately does not (ss-console#2444).
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
    _sweep_tool_surface(_mcp_tool_server_names)
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
        # The fan-out canonicalizes ``tool_name`` for policy tables and hands
        # the runtime's own spelling through as ``tool_name_wire``. Hermes'
        # server-name dict is keyed by the latter, so prefer it and fall back
        # to ``tool_name`` (pre-v0.19 seats, or a direct non-fan-out load).
        wire_name = kwargs.get("tool_name_wire")
        lookup_name = wire_name if isinstance(wire_name, str) and wire_name else tool_name
        server = _resolve_server(lookup_name)
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
