"""Shared test fixtures for the hermes-smd-overlay test suite.

Provides:
  - A minimal fake ``PluginContext`` that records every ``register_hook`` call,
    so tests can assert which hooks a plugin attaches to.
  - A ``load_plugin`` helper that imports a plugin package by its directory
    name. Plugin directory names contain hyphens (e.g. ``hermes-smd-audit``)
    which are not valid Python identifiers, so we load each plugin's
    ``__init__.py`` directly via ``importlib.util``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

import pytest


class FakePluginContext:
    """Minimal stand-in for Hermes' ``PluginContext``.

    Records every ``register_hook(name, callback)`` invocation into
    ``self.registered``, a dict mapping hook name to list of callbacks.
    Tests inspect ``registered`` to assert what a plugin's ``register``
    entry point attached.
    """

    def __init__(self) -> None:
        self.registered: dict[str, list[Callable[..., Any]]] = {}

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.registered.setdefault(name, []).append(callback)


@pytest.fixture
def fake_ctx() -> FakePluginContext:
    """Return a fresh ``FakePluginContext`` for each test."""
    return FakePluginContext()


def load_plugin(plugin_name: str) -> ModuleType:
    """Load a plugin module by its directory name (handles hyphens).

    Plugin directories use hyphens (``hermes-smd-audit``) which are not
    valid in dotted Python module paths. We bypass the normal import
    machinery by loading the plugin's ``__init__.py`` directly via
    ``importlib.util.spec_from_file_location``. The resulting module is
    registered under a sanitized name (``plugin_hermes_smd_audit``).
    """
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(
        f"plugin_{sanitized}", init_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
