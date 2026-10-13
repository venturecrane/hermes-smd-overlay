"""No request to a current Claude model carries thinking {"type": "disabled"}.

Sentry SMD-OPERATOR-1M: Hermes asks for thinking off on its side-calls (the
session auto-title always; any ``reasoning_effort: none`` main or auxiliary
task), and its Anthropic adapter rendered that as ``thinking: {"type":
"disabled"}``, which Opus 5.5 and Sonnet 5.5 answer with HTTP 400.
shared/anthropic_thinking_off.py maps thinking-off to adaptive thinking at
effort "low" on every Claude model on the adaptive contract (4.6 and later).

The adapter tests run against the REAL pinned Hermes source the CI job checks
out (HERMES_AGENT_SRC) and refuse to skip under CI. Remove the install from the
usage plugin, or the mapping from the shared module, and they fail on
claude-opus-5-5.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap.translate import _TITLE_GENERATION_MODEL, translate_customer_yaml
from shared import anthropic_thinking_off
from tests.conftest import load_plugin
from tests.test_bootstrap_translate import _seed_repo

_HERMES_SRC = os.environ.get("HERMES_AGENT_SRC", "")

# Current Claude models (claude-api model table, cached 2026-10-06): every one is
# on the adaptive contract, and a thinking-off request must never reach it as
# "disabled". Opus 4.6-4.8 / Sonnet 4.6 / Sonnet 5 still accept "disabled", but
# the mapping is uniform by design: one rule, no per-model table to go stale.
_ADAPTIVE_MODELS = (
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",
)
# Manual-thinking families: Hermes' rendering stays exactly as it was.
_LEGACY_MODELS = (
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5",
    "claude-opus-4-5",
)

_hermes_only = pytest.mark.skipif(
    not _HERMES_SRC and not os.environ.get("CI"),
    reason="HERMES_AGENT_SRC not set (CI checks out the pinned Hermes and never skips these)",
)


@pytest.fixture
def adapter():
    """The pinned Hermes Anthropic adapter, with the overlay's mapping installed
    the way a seat installs it (the usage plugin's register), restored after."""
    assert _HERMES_SRC, "CI must check out the pinned Hermes and set HERMES_AGENT_SRC"
    if _HERMES_SRC not in sys.path:
        sys.path.insert(0, _HERMES_SRC)
    from agent import anthropic_adapter

    original = anthropic_adapter._thinking_kwargs
    while getattr(original, "__wrapped__", None) is not None:
        original = original.__wrapped__
    anthropic_adapter._thinking_kwargs = original
    plugin = load_plugin("hermes-smd-usage")

    class _Ctx:
        def register_hook(self, *_a, **_k):
            pass

    plugin.register(_Ctx())
    yield anthropic_adapter
    anthropic_adapter._thinking_kwargs = original


def _build(adapter, model: str, reasoning_config: dict) -> dict:
    return adapter.build_anthropic_kwargs(
        model=model,
        messages=[{"role": "user", "content": "Draft the reply about the deposition date"}],
        tools=None,
        max_tokens=64,
        reasoning_config=reasoning_config,
    )


@_hermes_only
@pytest.mark.parametrize("model", _ADAPTIVE_MODELS)
def test_thinking_off_on_a_current_claude_model_is_adaptive_low(adapter, model):
    # agent/title_generator.py generate_title's exact reasoning_config.
    kwargs = _build(adapter, model, {"enabled": False})
    assert (kwargs.get("thinking") or {}).get("type") != "disabled"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}


@_hermes_only
def test_the_pinned_hermes_still_renders_disabled_without_the_overlay(adapter):
    """Guards the guard: if upstream stops sending "disabled", the mapping is
    dead weight and should be removed, not left passing for the wrong reason."""
    bare = adapter._thinking_kwargs.__wrapped__({"enabled": False}, "claude-opus-5-5", 64)
    assert bare == {"thinking": {"type": "disabled"}}


@_hermes_only
@pytest.mark.parametrize("model", _LEGACY_MODELS + ("kimi-k2-turbo-preview", "MiniMax-M2"))
def test_older_and_non_claude_models_keep_hermes_rendering(adapter, model):
    original = adapter._thinking_kwargs.__wrapped__
    assert adapter._thinking_kwargs({"enabled": False}, model, 64) == original(
        {"enabled": False}, model, 64
    )


@_hermes_only
@pytest.mark.parametrize("model", ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"))
@pytest.mark.parametrize("effort", ("low", "medium", "high", "xhigh"))
def test_thinking_on_is_untouched(adapter, model, effort):
    original = adapter._thinking_kwargs.__wrapped__
    cfg = {"enabled": True, "effort": effort}
    assert adapter._thinking_kwargs(cfg, model, 64000) == original(cfg, model, 64000)


@_hermes_only
def test_install_is_idempotent(adapter):
    wrapped = adapter._thinking_kwargs
    assert anthropic_thinking_off.install() is True
    assert anthropic_thinking_off.install() is True
    assert adapter._thinking_kwargs is wrapped


# Literal {"type": "disabled"} producers in the pinned Hermes outside the
# Anthropic adapter's _thinking_kwargs. Each is a non-Anthropic wire; a new one
# (or one moving onto the Anthropic wire) fails the scan below.
_KNOWN_NON_ANTHROPIC_DISABLE_SITES = {
    # Moonshot / DeepSeek chat_completions extra_body toggle.
    ("agent/reasoning_effort.py", "thinking_toggle_extras"),
    # MiniMax M3 on its OpenAI-compatible global endpoint (extra_body).
    ("plugins/model-providers/minimax/__init__.py", "build_api_kwargs_extras"),
    # The "actual" relay provider's chat_completions effort "none".
    ("plugins/model-providers/actual/__init__.py", "build_api_kwargs_extras"),
}


def _disabled_literal_sites(root: Path) -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("tests/", "evals/", "scripts/")) or "/tests/" in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if '"disabled"' not in text:
            continue
        tree = ast.parse(text)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=True):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "type"
                            and isinstance(value, ast.Constant)
                            and value.value == "disabled"
                        ):
                            sites.add((rel, func.name))
    return sites


@_hermes_only
def test_every_anthropic_disable_producer_is_the_wrapped_seam(adapter):
    """Every call site that asks for thinking off reaches the Anthropic wire
    through _thinking_kwargs; no other function in the pinned Hermes builds a
    {"type": "disabled"} thinking dict for it. Nested helpers report under
    their enclosing function too, so the seam is matched by name."""
    sites = _disabled_literal_sites(Path(_HERMES_SRC))
    anthropic_sites = {s for s in sites if s not in _KNOWN_NON_ANTHROPIC_DISABLE_SITES}
    assert anthropic_sites == {("agent/anthropic_adapter.py", "_thinking_kwargs")}, anthropic_sites


def _profile_config(tmp_path: Path, model: str) -> dict:
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    data = yaml.safe_load(customer_yaml.read_text())
    data["model"] = model
    customer_yaml.write_text(yaml.safe_dump(data, sort_keys=False))
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    return yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"])
def test_claude_seats_title_on_the_current_haiku(tmp_path, model):
    config = _profile_config(tmp_path, model)
    assert _TITLE_GENERATION_MODEL == "claude-haiku-5-5"
    assert config["auxiliary"] == {"title_generation": {"model": "claude-haiku-5-5"}}
    # provider/credential stay inherited from the main runtime.
    assert "provider" not in config["auxiliary"]["title_generation"]


def test_a_non_claude_seat_gets_no_title_pin(tmp_path):
    config = _profile_config(tmp_path, "gpt-5.5")
    assert "auxiliary" not in config


@_hermes_only
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_the_seat_title_request_carries_no_disabled_thinking(adapter, tmp_path, model):
    config = _profile_config(tmp_path, model)
    title_model = config["auxiliary"]["title_generation"]["model"]
    kwargs = _build(adapter, title_model, {"enabled": False})
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}
