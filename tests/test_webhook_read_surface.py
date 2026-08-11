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
    WEBHOOK_EXPECTED_TOOLS,
    WEBHOOK_READ_TOOLS,
    WEBHOOK_READ_TOOLSET,
    WEBHOOK_SAFE_TOOLSETS,
    WebhookReadSurfaceError,
    assert_read_file_on_webhook,
    expected_tool_report,
    missing_expected_tools,
    read_webhook_surface_status,
    register_webhook_read_toolset,
    resolve_webhook_tool_names,
    webhook_platform_enabled,
    webhook_platform_toolsets,
    write_webhook_surface_status,
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


# --- the WARN tier (ss-console#2222) ----------------------------------------
#
# A second tier over the SAME resolved surface, with the opposite boot posture:
# absence logs CRITICAL and lands in a heartbeat field, and the seat keeps
# serving. The tiers answer different orders of harm — no ``read_file`` means
# every voice-gated delivery refuses (the seat cannot work), while a missing
# ``operator_seat_facts`` means one class of answer is improvised. The second is
# bad; refusing to serve the paid client over it is worse.


def _offer_expected_tools(toolsets_state, config):
    """Simulate the initiation plugin's toolset reaching the webhook surface.

    Plugin toolsets are NOT named in ``platform_toolsets`` — they resolve onto
    the surface through the plugin registry, which the live pilot probe
    confirmed by returning three ``establish_*`` names. The fake models that as a
    toolset the enabled list carries.
    """
    toolsets_state["initiation"] = list(WEBHOOK_EXPECTED_TOOLS)
    config.setdefault("platform_toolsets", {})
    config["platform_toolsets"]["webhook"] = [*webhook_platform_toolsets(), "initiation"]
    return config


def test_expected_tier_is_exactly_the_seat_facts_tool():
    """The warn tier is deliberately small. Widening it silently converts a
    "this class of answer degrades" signal into noise."""
    assert WEBHOOK_EXPECTED_TOOLS == ("operator_seat_facts",)
    assert set(WEBHOOK_EXPECTED_TOOLS).isdisjoint(WEBHOOK_READ_TOOLS)


def test_platform_toolsets_is_unchanged_by_the_expected_tier(hermes):
    """T1. The live probe closed the question the Layer 0 design opened: plugin
    wrapped tools ALREADY reach webhook turns (three ``establish_*`` names in the
    resolved 15, and ``establish_stage_document`` executed on a real inbound
    email turn). So no new toolset name is emitted and no custom toolset is
    created for the warn tier.

    Falsifier: add a toolset name to the emitted list. This test exists to stop a
    future author from helpfully re-adding the half the probe retired."""
    assert webhook_platform_toolsets() == [*WEBHOOK_SAFE_TOOLSETS, WEBHOOK_READ_TOOLSET]
    assert all(t not in webhook_platform_toolsets() for t in WEBHOOK_EXPECTED_TOOLS)


def test_expected_tools_report_carries_both_sides(hermes):
    """An all-clear that also fires when the expectation was DELETED is the same
    defect class the alert exists to catch, so each entry ships ``expected`` and
    ``offered`` and a consumer can say which way a recovery went."""
    register_webhook_read_toolset()
    config = _offer_expected_tools(hermes, _config(webhook=True, fixed=True))
    assert expected_tool_report(config) == {
        "operator_seat_facts": {"expected": True, "offered": True}
    }
    assert missing_expected_tools(config) == []


def test_expected_tools_missing_when_the_plugin_did_not_register(hermes):
    """T2, warn half + THE NEGATIVE CONTROL. Same config, same fakes, the plugin
    toolset simply resolves to nothing — the ``vision_analyze`` shape live on the
    pilot today, where a tool named in the config is dropped from the surface by
    a failing check with nothing in the logs."""
    register_webhook_read_toolset()
    config = _config(webhook=True, fixed=True)
    config["platform_toolsets"]["webhook"] = [*webhook_platform_toolsets(), "initiation"]
    # "initiation" resolves to [] — never registered.
    assert missing_expected_tools(config) == ["operator_seat_facts"]
    assert expected_tool_report(config) == {
        "operator_seat_facts": {"expected": True, "offered": False}
    }


def test_the_fatal_tier_is_unchanged_by_the_warn_tier(hermes):
    """T2, fatal half. ``read_file``'s assertion still raises on a surface that
    omits it AND still passes on one that carries it, whether or not the warn
    tier is satisfied. A boot posture change that quietly relaxed the fatal check
    would trade a loud failure for a silent one."""
    register_webhook_read_toolset()
    both = _offer_expected_tools(hermes, _config(webhook=True, fixed=True))
    assert_read_file_on_webhook(both)  # warn tier satisfied

    read_only = _config(webhook=True, fixed=True)
    assert_read_file_on_webhook(read_only)  # warn tier NOT satisfied, still fine
    assert missing_expected_tools(read_only) == ["operator_seat_facts"]

    # The warn tier satisfied and the fatal one NOT: the surface offers
    # operator_seat_facts and no read_file. Still fatal, exactly as before.
    hermes["initiation"] = list(WEBHOOK_EXPECTED_TOOLS)
    seat_facts_only = _config(webhook=True, fixed=False)
    seat_facts_only["platform_toolsets"] = {
        "webhook": [*WEBHOOK_SAFE_TOOLSETS, "initiation"],
    }
    assert missing_expected_tools(seat_facts_only) == []
    with pytest.raises(WebhookReadSurfaceError):
        assert_read_file_on_webhook(seat_facts_only)


def test_resolver_docstring_records_that_it_under_reports_mcp_tools():
    """The cheapest thing in the design, and the one most likely to be dropped.
    The live probe returned 15 names with zero ``mcp_*`` entries while
    ``mcp_agentmail_create_draft`` was executing on that same channel — anyone
    reading this function's output as the complete turn surface draws a wrong
    conclusion, so the function says so where they will be looking."""
    doc = resolve_webhook_tool_names.__doc__ or ""
    assert "NOT THE COMPLETE TURN SURFACE" in doc
    assert "mcp_servers" in doc


# --- the boot sentinel (agent process writes, gate process reads) ------------


def test_surface_sentinel_round_trips(tmp_path):
    assert write_webhook_surface_status(
        ok=True,
        tools={"operator_seat_facts": {"expected": True, "offered": True}},
        hermes_home=str(tmp_path),
    )
    status = read_webhook_surface_status(str(tmp_path))
    assert status is not None
    assert status["ok"] is True
    assert status["tools"] == {"operator_seat_facts": {"expected": True, "offered": True}}
    assert isinstance(status["pid"], int) and status["pid"] > 0


def test_a_broken_check_writes_no_map_it_cannot_trust(tmp_path):
    """``ok`` is the health of the CHECK, not of any tool, and a broken check
    ships ``tools=None``. Emitting an empty map instead would read as "checked,
    everything offered" — our blindness rendered as the firm's all-clear."""
    write_webhook_surface_status(ok=False, tools=None, hermes_home=str(tmp_path))
    status = read_webhook_surface_status(str(tmp_path))
    assert status["ok"] is False
    assert status["tools"] is None


def test_absent_or_foreign_sentinel_reads_as_none(tmp_path):
    assert read_webhook_surface_status(str(tmp_path)) is None
    path = tmp_path / ".smd" / "webhook_surface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": "something.else/9"}', encoding="utf-8")
    assert read_webhook_surface_status(str(tmp_path)) is None
    path.write_text("{ not json", encoding="utf-8")
    assert read_webhook_surface_status(str(tmp_path)) is None
