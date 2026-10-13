"""The session auto-title must never ask an Opus 5.5 seat for thinking off.

Sentry SMD-OPERATOR-1M: Hermes titles every new session with a side-call on the
main model and always asks for thinking off; its Anthropic adapter renders that
as ``thinking: {"type": "disabled"}``, which Opus 5.5 answers with HTTP 400.
translate.py pins the title call to Haiku 4.5, which the adapter renders with no
thinking block. These tests build the title request the seat would send.

The real-adapter test runs against the pinned Hermes source the CI job checks
out (HERMES_AGENT_SRC). It is the falsifier: delete the pin in translate.py and
it fails on claude-opus-5-5, because the request falls back to the main model.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

from bootstrap.translate import _TITLE_GENERATION_MODEL, translate_customer_yaml
from tests.test_bootstrap_translate import _seed_repo

_HERMES_SRC = os.environ.get("HERMES_AGENT_SRC", "")


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


def _title_request_model(config: dict) -> str:
    """The model Hermes sends the title call to: auxiliary.title_generation.model,
    else the main model (agent/auxiliary_client.py _resolve_task_provider_model,
    provider "auto" routes onto the main runtime)."""
    pinned = ((config.get("auxiliary") or {}).get("title_generation") or {}).get("model")
    return str(pinned or config["model"])


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-4-6", "claude-fable-5-1"])
def test_claude_seats_pin_the_title_model(tmp_path, model):
    config = _profile_config(tmp_path, model)
    assert config["auxiliary"] == {"title_generation": {"model": _TITLE_GENERATION_MODEL}}
    # provider/credential stay inherited from the main runtime.
    assert "provider" not in config["auxiliary"]["title_generation"]


def test_a_non_claude_seat_gets_no_title_pin(tmp_path):
    config = _profile_config(tmp_path, "gpt-5.5")
    assert "auxiliary" not in config


@pytest.mark.skipif(
    not _HERMES_SRC and not os.environ.get("CI"),
    reason="HERMES_AGENT_SRC not set (CI checks out the pinned Hermes and never skips this)",
)
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-4-6"])
def test_the_title_request_for_the_seat_carries_no_disabled_thinking(tmp_path, model):
    assert _HERMES_SRC, "CI must check out the pinned Hermes and set HERMES_AGENT_SRC"
    sys.path.insert(0, _HERMES_SRC)
    try:
        from agent.anthropic_adapter import build_anthropic_kwargs
    finally:
        sys.path.remove(_HERMES_SRC)

    # Sanity: the pinned adapter still renders thinking-off as "disabled" for
    # Opus 5.5. If upstream fixes that, this test should be revisited, not
    # left passing for the wrong reason.
    bare = build_anthropic_kwargs(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "x"}],
        tools=None,
        max_tokens=64,
        reasoning_config={"enabled": False},
    )
    assert bare.get("thinking") == {"type": "disabled"}

    config = _profile_config(tmp_path, model)
    kwargs = build_anthropic_kwargs(
        model=_title_request_model(config),
        messages=[{"role": "user", "content": "Draft the reply about the deposition date"}],
        tools=None,
        max_tokens=64,
        # agent/title_generator.py generate_title's exact reasoning_config.
        reasoning_config={"enabled": False},
    )
    assert (kwargs.get("thinking") or {}).get("type") != "disabled"
