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
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class FakePluginContext:
    """Minimal stand-in for Hermes' ``PluginContext``.

    Records every ``register_hook(name, callback)`` invocation into
    ``self.registered``, a dict mapping hook name to list of callbacks, and
    every ``register_tool(...)`` into ``self.tools`` keyed by tool name.
    Tests inspect both to assert what a plugin's ``register`` entry point
    attached.
    """

    def __init__(self) -> None:
        self.registered: dict[str, list[Callable[..., Any]]] = {}
        self.tools: dict[str, dict[str, Any]] = {}

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.registered.setdefault(name, []).append(callback)

    def register_tool(self, *, name: str, **kwargs: Any) -> None:
        self.tools[name] = {"name": name, **kwargs}


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

    The sanitized name is also registered in ``sys.modules`` BEFORE the
    loader executes so that relative imports inside ``__init__.py``
    (``from . import emit, schemas, ...``) resolve correctly. Without
    pre-registration, Python's import machinery can't find the parent
    package and the submodule imports raise ``ImportError``.
    """
    import sys

    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = f"plugin_{plugin_name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(
        sanitized,
        init_path,
        submodule_search_locations=[str(init_path.parent)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[sanitized] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(sanitized, None)
        raise
    return module
