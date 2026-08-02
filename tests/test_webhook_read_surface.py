"""Tests for ``read_file`` on webhook turns (ss-console#2145).

The fix is two halves in two processes plus a boot assertion, and each is
covered here:

* CONFIG half — ``bootstrap.translate`` emits ``platform_toolsets.webhook``
  naming the safe toolsets plus the overlay's read-only one, and emits it only
  for seats that actually serve the webhook platform.
* RUNTIME half — the webhook-router plugin's ``register`` creates that toolset
  at plugin load with exactly ``read_file`` (never the ``file`` toolset, whose
  ``write_file``/``patch``/``search_files`` are what the webhook-safe default
  exists to deny on untrusted inbound content).
* BOOT ASSERTION — the resolved surface is checked the way a real webhook turn
  resolves it, and the check FAILS on the pre-fix config and on the config-only
  config. That is the point of it: config-half-only is silent at runtime
  (``explicit_passthrough`` keeps the unknown name, ``validate_toolset`` returns
  False, gateway callers suppress the warning with ``quiet_mode=True``), so a
  check that could not fail on that exact input would measure nothing.

Hermes is not installed in this environment, so ``toolsets``,
``hermes_cli.tools_config`` and ``model_tools`` are faked — but faked
FAITHFULLY, reproducing the three behaviors measured against the pinned ref
(v2026.7.1@7c1a029) with a real hermes checkout:

    default config      -> clarify, vision_analyze, web_extract, web_search
    config half only    -> unchanged (the silent failure)
    both halves         -> + read_file, and nothing else

The negative control is ``test_assertion_fails_on_config_half_alone``: same
config, same fakes, toolset never registered.
"""

from __future__ import annotations

import sys
import types

import pytest

from shared.webhook_read_surface import (
    WEBHOOK_READ_TOOLS,
    WEBHOOK_READ_TOOLSET,
    WEBHOOK_SAFE_TOOLSETS,
    WebhookReadSurfaceError,
    assert_read_file_on_webhook,
    register_webhook_read_toolset,
    resolve_webhook_tool_names,
    webhook_platform_enabled,
    webhook_platform_toolsets,
)

# Hermes' real toolset contents at the pinned ref, for the names in play.
_HERMES_TOOLSETS: dict[str, list[str]] = {
    "hermes-webhook": ["web_search", "web_extract", "vision_analyze", "clarify"],
    "web": ["web_search", "web_extract"],
    "vision": ["vision_analyze"],
    "clarify": ["clarify"],
    "file": ["read_file", "write_file", "patch", "search_files"],
    "browser": ["browser_navigate", "browser_click", "web_search"],
}

# The webhook platform's default composite when no platform_toolsets entry
# exists (hermes_cli/platforms.py:41).
_WEBHOOK_DEFAULT_TOOLSET = "hermes-webhook"


@pytest.fixture
def hermes(monkeypatch):
    """Install faithful fakes for the three Hermes modules the contract touches.

    ``TOOLSETS`` is a fresh dict per test so a registration in one test cannot
    leak into another (and so the negative control genuinely has no toolset).
    """
    toolsets_state = dict(_HERMES_TOOLSETS)

    toolsets_mod = types.ModuleType("toolsets")

    def resolve_toolset(name: str) -> list[str]:
        # Hermes returns [] for an unknown name — the whole silent-failure mode.
        return list(toolsets_state.get(name, []))

    def create_custom_toolset(name, description, tools=None, includes=None):
        assert not includes, "the read-only webhook toolset must not include others"
        toolsets_state[name] = list(tools or [])

    toolsets_mod.resolve_toolset = resolve_toolset  # type: ignore[attr-defined]
    toolsets_mod.create_custom_toolset = create_custom_toolset  # type: ignore[attr-defined]
    toolsets_mod.TOOLSETS = toolsets_state  # type: ignore[attr-defined]

    tools_config_mod = types.ModuleType("hermes_cli.tools_config")

    def _get_platform_tools(config, platform, include_default_mcp_servers=True):
        """Mirror the branch that matters: an explicit list is used as given —
        including names Hermes does not know (``explicit_passthrough``,
        tools_config.py:1620) — and its absence falls back to the platform's
        default composite."""
        explicit = (config.get("platform_toolsets") or {}).get(platform)
        if isinstance(explicit, list) and explicit:
            return {str(t) for t in explicit}
        return {_WEBHOOK_DEFAULT_TOOLSET}

    tools_config_mod._get_platform_tools = _get_platform_tools  # type: ignore[attr-defined]

    model_tools_mod = types.ModuleType("model_tools")

    def get_tool_definitions(
        enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False, **_kw
    ):
        """Union the enabled toolsets, then subtract the disabled ones by TOOL
        (model_tools.py:395-424 — the subtraction is tool-level, which is the
        collateral defect filed separately for ``browser``/``web_search``)."""
        included: set[str] = set()
        for name in enabled_toolsets or []:
            included.update(resolve_toolset(name))
        for name in disabled_toolsets or []:
            included.difference_update(resolve_toolset(name))
        return [{"type": "function", "function": {"name": n}} for n in sorted(included)]

    model_tools_mod.get_tool_definitions = get_tool_definitions  # type: ignore[attr-defined]

    parent = sys.modules.get("hermes_cli") or types.ModuleType("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", tools_config_mod)
    monkeypatch.setitem(sys.modules, "toolsets", toolsets_mod)
    monkeypatch.setitem(sys.modules, "model_tools", model_tools_mod)
    return toolsets_state


def _config(*, webhook: bool, fixed: bool, disabled: list[str] | None = None) -> dict:
    """A generated config.yaml as it looks before (``fixed=False``) and after
    (``fixed=True``) the translate half of the fix."""
    config: dict = {"agent": {"disabled_toolsets": disabled or []}}
    if webhook:
        config["platforms"] = {"webhook": {"enabled": True, "extra": {"port": 8644}}}
        if fixed:
            config["platform_toolsets"] = {"webhook": webhook_platform_toolsets()}
    return config


# --- the contract itself ----------------------------------------------------


def test_toolset_carries_only_read_file():
    """Never the ``file`` toolset: write_file/patch/search_files on an untrusted
    inbound turn is the surface the webhook-safe default exists to deny."""
    assert WEBHOOK_READ_TOOLS == ("read_file",)
    assert set(WEBHOOK_READ_TOOLS).isdisjoint({"write_file", "patch", "search_files"})


def test_emitted_list_carries_the_safe_toolsets_and_the_custom_name():
    """Naming a platform REPLACES its default composite, so the safe toolsets
    must be carried forward or the fix trades read_file for the four tools
    webhook turns already had."""
    emitted = webhook_platform_toolsets()
    assert emitted == [*WEBHOOK_SAFE_TOOLSETS, WEBHOOK_READ_TOOLSET]
    assert set(WEBHOOK_SAFE_TOOLSETS) == {"web", "vision", "clarify"}


def test_safe_toolsets_equal_hermes_webhook_default(hermes):
    """The three safe toolset keys resolve to exactly Hermes' webhook-safe tool
    set — the property that makes replacing the composite lossless."""
    safe: set[str] = set()
    for name in WEBHOOK_SAFE_TOOLSETS:
        safe.update(_HERMES_TOOLSETS[name])
    assert safe == set(_HERMES_TOOLSETS[_WEBHOOK_DEFAULT_TOOLSET])


def test_webhook_platform_enabled_detection():
    assert webhook_platform_enabled(_config(webhook=True, fixed=True)) is True
    assert webhook_platform_enabled(_config(webhook=False, fixed=False)) is False
    assert webhook_platform_enabled({"platforms": {"webhook": {"enabled": False}}}) is False
    assert webhook_platform_enabled({"platforms": "not-a-dict"}) is False


# --- the three resolution states -------------------------------------------


def test_surface_before_the_fix_has_no_read_file(hermes):
    """The defect, reproduced: the default webhook surface is the four safe
    tools and read_file is absent."""
    names = resolve_webhook_tool_names(_config(webhook=True, fixed=False))
    assert names == {"web_search", "web_extract", "vision_analyze", "clarify"}
    assert "read_file" not in names


def test_surface_with_config_half_only_is_unchanged(hermes):
    """The silent failure: the custom name survives into the enabled list but
    resolves to nothing, so the surface is byte-identical to the broken one."""
    names = resolve_webhook_tool_names(_config(webhook=True, fixed=True))
    assert "read_file" not in names
    assert names == resolve_webhook_tool_names(_config(webhook=True, fixed=False))


def test_surface_with_both_halves_adds_read_file_and_nothing_else(hermes):
    register_webhook_read_toolset()
    before = resolve_webhook_tool_names(_config(webhook=True, fixed=False))
    after = resolve_webhook_tool_names(_config(webhook=True, fixed=True))
    assert after - before == {"read_file"}
    assert before - after == set()


def test_read_file_survives_the_seat_disabled_toolsets(hermes):
    """The generated config disables 14 toolsets for cost. None of them carries
    read_file, but the subtraction is tool-level, so assert it rather than
    assume it."""
    register_webhook_read_toolset()
    disabled = ["browser", "computer_use", "image_gen", "tts", "video", "workspace"]
    names = resolve_webhook_tool_names(_config(webhook=True, fixed=True, disabled=disabled))
    assert "read_file" in names


# --- the boot assertion, and its negative control ---------------------------


def test_assertion_passes_when_both_halves_shipped(hermes):
    register_webhook_read_toolset()
    assert_read_file_on_webhook(_config(webhook=True, fixed=True))


def test_assertion_fails_on_the_pre_fix_config(hermes):
    register_webhook_read_toolset()
    with pytest.raises(WebhookReadSurfaceError) as ei:
        assert_read_file_on_webhook(_config(webhook=True, fixed=False))
    assert "read_file" in str(ei.value)


def test_assertion_fails_on_config_half_alone(hermes):
    """THE negative control. Same config as the passing test, toolset never
    registered — this is the failure mode Hermes reports nowhere, so if this
    test could not fail the assertion would be measuring nothing."""
    with pytest.raises(WebhookReadSurfaceError) as ei:
        assert_read_file_on_webhook(_config(webhook=True, fixed=True))
    message = str(ei.value)
    assert WEBHOOK_READ_TOOLSET in message
    assert "Both halves are required" in message


def test_registration_is_idempotent(hermes):
    register_webhook_read_toolset()
    register_webhook_read_toolset()
    assert hermes[WEBHOOK_READ_TOOLSET] == list(WEBHOOK_READ_TOOLS)


def test_registered_toolset_is_exactly_read_file(hermes):
    register_webhook_read_toolset()
    assert hermes[WEBHOOK_READ_TOOLSET] == ["read_file"]
