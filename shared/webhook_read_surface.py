"""``read_file`` on webhook turns: the contract shared by its three processes.

THE DEFECT THIS CLOSES (ss-console#2145, probe-verified). ``read_file`` is absent
on webhook-platform turns and only there. Hermes resolves each platform's tool
surface through ``hermes_cli.tools_config._get_platform_tools``; with no
``platform_toolsets`` entry the webhook platform falls back to its default
composite ``hermes-webhook``, whose tools are ``_HERMES_WEBHOOK_SAFE_TOOLS``
(``toolsets.py:85``) — ``web_search``, ``web_extract``, ``vision_analyze``,
``clarify``. No ``file`` toolset, deliberately: a webhook payload is untrusted
third-party content and the constrained default is the injection defense.

The consequence is not "the agent reads one fewer file". The spec read-mark
(``plugins/hermes-smd-trust/spec_read.py`` ``_READ_TOOLS = {"read_file"}``) can
never be set on a webhook turn, so ``spec_gate`` refuses every voice-gated
delivery on the one platform inbound email arrives on.

THE FIX IS TWO HALVES IN TWO PROCESSES. Neither works alone:

1. CONFIG (``bootstrap/translate.py``, the provisioning process) emits
   ``platform_toolsets.webhook`` naming the safe toolsets PLUS
   :data:`WEBHOOK_READ_TOOLSET`. It can only write YAML.
2. RUNTIME (the webhook-router plugin's ``register``, the agent process) calls
   Hermes' ``create_custom_toolset`` so that name resolves to exactly
   ``read_file``. It must run at PLUGIN LOAD, before the first turn:
   ``get_tool_definitions`` memoizes on ``registry._generation``
   (``model_tools.py:251,321``) and ``create_custom_toolset`` mutates
   ``TOOLSETS`` without bumping it, so a late registration can land behind a
   stale memo and be invisible for the life of the process.

WHY THE BOOT ASSERTION EXISTS. Shipping half 1 without half 2 fails SILENTLY.
``_get_platform_tools`` keeps an unknown name via ``explicit_passthrough``
(``tools_config.py:1620``), ``validate_toolset`` returns False, and the one
warning Hermes would log is suppressed because gateway callers pass
``quiet_mode=True``. The surface stays ``read_file``-less with nothing in the
logs — the same shape as the original defect, now with a config block that
looks like the fix. So :func:`assert_read_file_on_webhook` re-derives the
surface the way a real webhook turn does (``gateway/run.py:12592-12599``:
``_get_platform_tools`` -> ``get_tool_definitions`` with the agent's
``disabled_toolsets``) and the activation gate ``_die``s when ``read_file`` is
absent. A check that cannot fail has measured nothing; this one fails on the
pre-fix config, which is what its test asserts.

WHY NOT THE ``file`` TOOLSET. ``file`` resolves to ``read_file``, ``write_file``,
``patch``, ``search_files``. Granting write/patch/search on a turn driven by
untrusted inbound content is precisely the surface the webhook-safe default
exists to deny. The custom toolset carries ONE tool, and
:data:`WEBHOOK_READ_TOOLS` is asserted to be exactly that in test.

TWO TIERS, AND THE SECOND IS DELIBERATELY NOT FATAL (ss-console#2222).
:data:`WEBHOOK_READ_TOOLS` stays fatal: without ``read_file`` the spec read-mark
can never be set, so every voice-gated delivery refuses and the seat cannot do
its job on its only channel. :data:`WEBHOOK_EXPECTED_TOOLS` is the warn tier —
tools whose absence degrades ONE class of answer rather than the seat. Its
absence logs CRITICAL and lands in a heartbeat field (via the sentinel below,
read gate-side by :mod:`shared.webhook_surface_check`), and boot CONTINUES.

The distinction is a harm judgment, not a confidence judgment. "This seat
improvises an introduction" and "this seat does not serve" are not the same
order of harm, and the second lands on the paid client. A crash-loop is the
right answer only when serving is worse than being down.

WHY A SENTINEL AND NOT A DIRECT FIELD SET. The check can only run in the AGENT
(gateway) process — that is the process whose resolved tool surface is the thing
in question — while the heartbeat emitter runs in the always-on GATE process,
which cannot see the agent's registry. So the agent writes the outcome to
``$HERMES_HOME/.smd/webhook_surface.json`` and the gate reads it, exactly the
crossing ``shared.audit_status`` already makes for audit wiring, pid-stamped for
the same reason: a handler cannot sentinel its own non-execution, so a previous
boot's file must be detectable as stale rather than served as current.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Name of the runtime-created custom toolset. Not a Hermes toolset, not an MCP
#: server, not a plugin toolset — it survives ``_get_platform_tools`` only via
#: the ``explicit_passthrough`` branch, which is why the config half is inert
#: until the runtime half creates it.
WEBHOOK_READ_TOOLSET = "smd_webhook_read"

#: The toolset's entire contents. One read verb. See the module docstring for
#: why this is not the ``file`` toolset.
WEBHOOK_READ_TOOLS: tuple[str, ...] = ("read_file",)

#: The WARN tier: tools a webhook turn is expected to be offered, whose absence
#: degrades one class of answer rather than the seat. Unlike
#: :data:`WEBHOOK_READ_TOOLS` these are NOT a boot-fatal condition — see the
#: module docstring for why the two tiers exist and which harm each answers.
#:
#: ``operator_seat_facts`` (ss-console#2222): the grounded self-description an
#: introduce ask on the mail channel composes from. Registered by the
#: hermes-smd-initiation plugin with no ``requires_env`` and no ``check_fn``
#: precisely so it cannot be dropped by a failing check — this entry is the
#: assertion that it was not dropped anyway.
WEBHOOK_EXPECTED_TOOLS: tuple[str, ...] = ("operator_seat_facts",)

#: Sentinel schema + location. Relative to HERMES_HOME, written by the agent
#: process, read by the gate. Mirrors ``shared.audit_status`` deliberately: an
#: operator should not have to learn two shapes for the same crossing.
SURFACE_STATUS_SCHEMA = "smd.webhook_surface/1"
_SURFACE_STATUS_RELPATH = Path(".smd") / "webhook_surface.json"
_DEFAULT_HERMES_HOME = "/opt/data"

#: Toolset-key spelling of Hermes' ``_HERMES_WEBHOOK_SAFE_TOOLS``. Naming a
#: platform explicitly REPLACES the default composite, so these must be carried
#: forward or the fix would trade ``read_file`` for the four tools webhook turns
#: already had. Probe-verified equal: ``resolve_toolset("hermes-webhook")`` ==
#: ``web | vision | clarify`` == {web_search, web_extract, vision_analyze,
#: clarify} at the pinned ref.
WEBHOOK_SAFE_TOOLSETS: tuple[str, ...] = ("web", "vision", "clarify")

_TOOLSET_DESCRIPTION = (
    "Read-only file access for webhook-platform turns. Exactly one tool "
    "(read_file) so an untrusted inbound payload cannot reach write_file, "
    "patch, or search_files (ss-console#2145)."
)


class WebhookReadSurfaceError(RuntimeError):
    """The resolved webhook tool surface does not carry ``read_file``."""


def webhook_platform_toolsets() -> list[str]:
    """The exact ``platform_toolsets.webhook`` list the config half must emit."""
    return [*WEBHOOK_SAFE_TOOLSETS, WEBHOOK_READ_TOOLSET]


def register_webhook_read_toolset() -> None:
    """Create the custom toolset in the live process. Idempotent.

    Hermes' ``create_custom_toolset`` assigns into the module-level ``TOOLSETS``
    dict, so re-running (the activation gate force-rediscovers plugins) yields
    the same definition rather than a duplicate.
    """
    from toolsets import create_custom_toolset

    create_custom_toolset(
        WEBHOOK_READ_TOOLSET,
        _TOOLSET_DESCRIPTION,
        tools=list(WEBHOOK_READ_TOOLS),
    )


def resolve_webhook_tool_names(config: dict[str, Any]) -> set[str]:
    """Names of the tools a webhook turn would be offered, on this config.

    Mirrors the turn path at ``gateway/run.py:12592-12599`` exactly — the same
    two calls, in the same order, with the same arguments — so what this returns
    is what the model would actually see, not a re-derivation that could agree
    with the code while disagreeing with the runtime.

    THIS IS NOT THE COMPLETE TURN SURFACE, and reading it as one draws a wrong
    conclusion. It is authoritative for CORE and PLUGIN toolsets only. MCP tools
    attach through ``config["mcp_servers"]`` (``bootstrap/translate.py``
    ``_materialize_mcp_servers``), never through ``platform_toolsets``, so they
    appear NOWHERE in this result even when they are demonstrably reachable: the
    live pilot-smokeball probe returned 15 names with zero ``mcp_*`` entries
    while ``mcp_agentmail_create_draft`` was executing on that same channel, as
    it had for months. Anyone answering "can a webhook turn call X" for an MCP
    tool must look at the MCP server config, not here.
    """
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    enabled = sorted(_get_platform_tools(config, "webhook"))
    disabled = (config.get("agent") or {}).get("disabled_toolsets") or None
    definitions = get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=disabled,
        quiet_mode=True,
    )
    names: set[str] = set()
    for definition in definitions:
        name = (definition.get("function") or {}).get("name")
        if name:
            names.add(str(name))
    return names


def webhook_platform_enabled(config: dict[str, Any]) -> bool:
    """True iff this seat actually serves the webhook platform.

    The assertion is scoped to seats that have it: a seat with no inbound
    webhooks has no webhook surface to be wrong about, and must not be refused
    a boot over a platform it never serves.
    """
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return False
    webhook = platforms.get("webhook")
    return isinstance(webhook, dict) and bool(webhook.get("enabled"))


def assert_read_file_on_webhook(config: dict[str, Any]) -> None:
    """Raise :class:`WebhookReadSurfaceError` unless ``read_file`` is offered.

    Raises rather than exits so the caller decides the posture — the activation
    gate turns this into ``os._exit(1)``; a test just catches it.
    """
    names = resolve_webhook_tool_names(config)
    missing = set(WEBHOOK_READ_TOOLS) - names
    if not missing:
        return
    raise WebhookReadSurfaceError(
        f"webhook tool surface is missing {sorted(missing)} "
        f"(offered: {sorted(names)}). Either config.yaml has no "
        f"platform_toolsets.webhook entry naming {WEBHOOK_READ_TOOLSET!r} "
        f"(the bootstrap/translate.py half), or nothing called "
        f"register_webhook_read_toolset() so that name resolves to no tools "
        f"(the plugin half). Both halves are required; shipping either alone "
        f"is silent (ss-console#2145)."
    )


def expected_tool_report(config: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """``{tool: {"expected": True, "offered": bool}}`` over the warn tier.

    BOTH SIDES OF THE COMPARISON SHIP, for the reason
    ``shared.spec_control_check`` states: an all-clear that also fires when the
    expectation was deleted is the same defect class the alert exists to catch.
    A consumer reading a RECOVERED transition must be able to say WHICH way it
    recovered — the tool came back, or we stopped expecting it.
    """
    names = resolve_webhook_tool_names(config)
    return {tool: {"expected": True, "offered": tool in names} for tool in WEBHOOK_EXPECTED_TOOLS}


def missing_expected_tools(config: dict[str, Any]) -> list[str]:
    """Warn-tier tools the resolved surface does not offer. Sorted, possibly empty."""
    return sorted(
        tool for tool, entry in expected_tool_report(config).items() if not entry["offered"]
    )


def _surface_status_path(hermes_home: str | None) -> Path:
    home = hermes_home or os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _SURFACE_STATUS_RELPATH


def write_webhook_surface_status(
    *,
    ok: bool,
    tools: dict[str, dict[str, bool]] | None,
    hermes_home: str | None = None,
) -> bool:
    """Persist this boot's warn-tier outcome for the gate. Never raises.

    ``ok`` is the health of the CHECK ITSELF (the surface was resolvable), not of
    any tool — the same split ``spec_control_check`` makes, and for the same
    reason: "the expected tool is missing" and "we could not look" want opposite
    responses, and identical emptiness would hide the second behind the first.
    ``ok=False`` therefore ships ``tools=None``: never emit a map you cannot
    trust. Atomic (tmp + rename) so the gate never reads a torn file.
    """
    path = _surface_status_path(hermes_home)
    payload: dict[str, Any] = {
        "schema": SURFACE_STATUS_SCHEMA,
        "ok": bool(ok),
        "tools": tools,
        "pid": os.getpid(),
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("webhook_read_surface: sentinel write failed (%s): %s", path, exc)
        return False


def read_webhook_surface_status(hermes_home: str | None = None) -> dict[str, Any] | None:
    """Read the sentinel. ``None`` when absent / unparseable / wrong shape."""
    path = _surface_status_path(hermes_home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SURFACE_STATUS_SCHEMA:
        return None
    return data


__all__ = [
    "SURFACE_STATUS_SCHEMA",
    "WEBHOOK_EXPECTED_TOOLS",
    "WEBHOOK_READ_TOOLS",
    "WEBHOOK_READ_TOOLSET",
    "WEBHOOK_SAFE_TOOLSETS",
    "WebhookReadSurfaceError",
    "assert_read_file_on_webhook",
    "expected_tool_report",
    "missing_expected_tools",
    "read_webhook_surface_status",
    "register_webhook_read_toolset",
    "resolve_webhook_tool_names",
    "webhook_platform_enabled",
    "webhook_platform_toolsets",
    "write_webhook_surface_status",
]
