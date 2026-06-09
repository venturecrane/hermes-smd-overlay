"""Tests for the umbrella fan-out registrar (hermes-smd-overlay/__init__.py).

The umbrella's register(ctx) must load + register every declared sub-plugin the
same way Hermes loads a directory plugin, with the same ctx, so the overlay
actually governs a live gateway (ss-console#1285 — the umbrella was inert
because Hermes ignores the plugin.yaml `plugins:` aggregation list).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the umbrella package the way Hermes does (by path), so this test exercises
# the exact module the gateway loads.
_REPO = Path(__file__).resolve().parent.parent


def _load_umbrella():
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.hermes_smd_overlay",
        _REPO / "__init__.py",
        submodule_search_locations=[str(_REPO)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "hermes_plugins.hermes_smd_overlay"
    module.__path__ = [str(_REPO)]
    sys.modules["hermes_plugins.hermes_smd_overlay"] = module
    spec.loader.exec_module(module)
    return module


class RecordingCtx:
    """Records what each sub-plugin registers — faithful to the subset of the
    Hermes PluginContext API the sub-plugins use (register_hook), tolerant of
    anything else via no-op fallbacks."""

    def __init__(self):
        self.hooks: dict[str, list] = {}
        self.tools: list = []
        self.commands: list = []

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_tool(self, *a, **k):
        self.tools.append((a, k))

    def register_command(self, *a, **k):
        self.commands.append((a, k))

    def __getattr__(self, _name):
        # Any other ctx call a sub-plugin might make at register time is a no-op
        # here — we only assert on hook registration.
        return lambda *a, **k: None


@pytest.fixture
def env(monkeypatch, tmp_path):
    # The sub-plugins resolve their per-customer namespace + audit binding from
    # env at register time; set them so register() runs the full path (incl.
    # the audit plugin's ensure_schema), not its degraded branch.
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", str(tmp_path / "audit.db"))
    return tmp_path


# The five functional plugins whose activation is the governance guarantee, plus
# the two auxiliaries the manifest also declares.
_FUNCTIONAL = {
    "hermes-smd-audit",
    "hermes-smd-trust",
    "hermes-smd-voice",
    "hermes-smd-memory-mirror",
    "hermes-smd-webhook-router",
}


def test_declared_subplugins_matches_manifest():
    mod = _load_umbrella()
    declared = [Path(p).name for p in mod.declared_subplugins()]
    assert _FUNCTIONAL.issubset(set(declared))


def test_fanout_registers_all_functional_plugins_and_their_hooks(env):
    mod = _load_umbrella()
    ctx = RecordingCtx()
    registered = mod.load_and_register_subplugins(ctx)

    # Every functional plugin registered (governance is complete).
    assert _FUNCTIONAL.issubset(set(registered)), f"missing: {_FUNCTIONAL - set(registered)}"

    # The hooks each functional plugin attaches actually landed on the ctx.
    assert "pre_tool_call" in ctx.hooks  # trust ceiling gate — the safety-critical one
    assert "post_tool_call" in ctx.hooks  # audit + trust
    assert "post_llm_call" in ctx.hooks  # audit + voice
    assert "subagent_stop" in ctx.hooks  # audit
    assert "pre_llm_call" in ctx.hooks  # voice + inbound
    assert "on_session_end" in ctx.hooks  # memory-mirror
    assert "pre_gateway_dispatch" in ctx.hooks  # webhook-router


def test_audit_ensure_schema_ran_during_fanout(env):
    # Activation isn't just "register_hook was called" — the audit plugin's
    # register() must have run ensure_schema() and created the table.
    import sqlite3

    mod = _load_umbrella()
    mod.load_and_register_subplugins(RecordingCtx())
    db = env / "audit.db"
    assert db.exists(), "audit.db was not created — ensure_schema did not run in the fan-out"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    assert "audit_log" in tables
