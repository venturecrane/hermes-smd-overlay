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
"""

from __future__ import annotations

from typing import Any

#: Name of the runtime-created custom toolset. Not a Hermes toolset, not an MCP
#: server, not a plugin toolset — it survives ``_get_platform_tools`` only via
#: the ``explicit_passthrough`` branch, which is why the config half is inert
#: until the runtime half creates it.
WEBHOOK_READ_TOOLSET = "smd_webhook_read"

#: The toolset's entire contents. One read verb. See the module docstring for
#: why this is not the ``file`` toolset.
WEBHOOK_READ_TOOLS: tuple[str, ...] = ("read_file",)

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


__all__ = [
    "WEBHOOK_READ_TOOLS",
    "WEBHOOK_READ_TOOLSET",
    "WEBHOOK_SAFE_TOOLSETS",
    "WebhookReadSurfaceError",
    "assert_read_file_on_webhook",
    "register_webhook_read_toolset",
    "resolve_webhook_tool_names",
    "webhook_platform_enabled",
    "webhook_platform_toolsets",
]
