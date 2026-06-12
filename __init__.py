"""hermes-smd-overlay umbrella plugin — fan-out registrar.

WHY THIS EXISTS (read first): the overlay packages seven sub-plugins under this
one directory, declared in ``plugin.yaml``'s ``plugins:`` list. The original
design ASSUMED Hermes would read that list and load each sub-plugin. It does
not: Hermes' plugin contract is "a plugin is a directory with ``plugin.yaml`` +
``__init__.py`` exposing ``register(ctx)``", and its manifest parser has no
``plugins:`` (aggregation) field — so the list was silently ignored, this
umbrella loaded as a manifest-only plugin with no ``register()``, and **none of
the five functional plugins (audit, trust, voice, webhook-router, memory-mirror)
ever registered** on a live gateway. The overlay was inert: no audit, and —
worse — no trust/ceiling enforcement. (ss-console#1285.)

This module makes the umbrella a real Hermes plugin whose ``register(ctx)`` fans
out to each declared sub-plugin, loading it the SAME way Hermes loads a
directory plugin (``spec_from_file_location`` under the ``hermes_plugins.<slug>``
namespace with ``submodule_search_locations`` so the sub-plugins' relative
imports resolve), then calling its ``register(ctx)`` with the SAME context. The
result is identical to Hermes loading each sub-plugin directly, and is
order-independent (each sub-plugin only registers its own hooks).

Activation is asserted as a HARD boot gate by the safety-substrate invariant
``invariant_8_overlay_activation`` (ss-console operator/safety-substrate), so a
future Hermes re-pin that breaks this contract fails loudly at boot instead of
silently shipping an ungoverned operator.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("hermes_smd.overlay")

# Mirror Hermes' own directory-plugin namespace parent (hermes_cli/plugins.py:
# ``_NS_PARENT = "hermes_plugins"``) so a sub-plugin loaded here is
# indistinguishable from one Hermes loaded directly.
_NS_PARENT = "hermes_plugins"


def declared_subplugins(base: Path | None = None) -> list[str]:
    """The sub-plugin dirs declared in the umbrella ``plugin.yaml`` ``plugins:``
    list (relative paths like ``plugins/hermes-smd-audit``). The manifest stays
    the single source of truth — adding/removing a sub-plugin there is all it
    takes."""
    base = base or Path(__file__).resolve().parent
    manifest = yaml.safe_load((base / "plugin.yaml").read_text(encoding="utf-8"))
    subs = manifest.get("plugins", []) if isinstance(manifest, dict) else []
    return [str(s) for s in subs]


def _load_subplugin_module(plugin_dir: Path) -> types.ModuleType:
    """Load a sub-plugin's ``__init__.py`` exactly as Hermes loads a directory
    plugin: a package module under ``hermes_plugins.<slug>`` with
    ``submodule_search_locations`` set so its ``from .x import y`` relative
    imports resolve. Returns the loaded module (caller invokes ``register``)."""
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"no __init__.py in {plugin_dir}")

    if _NS_PARENT not in sys.modules:
        ns_pkg = types.ModuleType(_NS_PARENT)
        ns_pkg.__path__ = []  # type: ignore[attr-defined]
        ns_pkg.__package__ = _NS_PARENT
        sys.modules[_NS_PARENT] = ns_pkg

    slug = plugin_dir.name.replace("/", "__").replace("-", "_")
    module_name = f"{_NS_PARENT}.{slug}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(plugin_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for {init_file}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_and_register_subplugins(ctx: Any, base: Path | None = None) -> list[str]:
    """Fan out: load + ``register(ctx)`` every declared sub-plugin, with the
    SAME ctx. Best-effort — a sub-plugin that fails to load/register is logged
    and skipped so one bad plugin can't take down the rest; completeness is the
    boot invariant's job to enforce (it fails the boot gate if any of the five
    functional plugins didn't register). Returns the slugs that registered."""
    base = base or Path(__file__).resolve().parent
    registered: list[str] = []
    for rel in declared_subplugins(base):
        plugin_dir = base / rel
        name = plugin_dir.name
        try:
            module = _load_subplugin_module(plugin_dir)
            register_fn = getattr(module, "register", None)
            if register_fn is None:
                logger.warning("overlay fan-out: %s has no register(); skipping", name)
                continue
            register_fn(ctx)
            registered.append(name)
            logger.info("overlay fan-out: registered %s", name)
        except Exception:  # noqa: BLE001 — one bad sub-plugin must not abort the rest
            logger.exception("overlay fan-out: %s failed to register", name)
    return registered


def _warn_on_undeclared_env_reads() -> None:
    """WARN-only env-consumption conformance at boot.

    Logs any env var the overlay reads with a string literal that is NOT
    declared in ``contracts/consumes.yaml`` — the OP-P0-2 voice-break class,
    surfaced loudly and early. It NEVER raises and NEVER fails boot: per the
    Phase A reframe, boot fails only on a liveness check, never on a conformance
    check (a boot-time scan gate is itself a brick vector). The fail-closed
    enforcement is the CI test; this is the early-warning twin sharing the same
    discovery code. The stale-declaration half is CI-only (it needs the full
    source tree, which a stripped install layout may not carry)."""
    try:
        from shared import consumes_conformance as cc

        read = cc.discover_static_env_reads()
        if not read:
            return  # source tree not scannable in this layout — CI owns conformance
        undeclared = read - set(cc.declared_vars())
        if undeclared:
            logger.warning(
                "env-consumption drift: overlay reads %s with a string literal but they are NOT "
                "declared in contracts/consumes.yaml (the OP-P0-2 voice-break class). "
                "CI enforces this; boot continues.",
                sorted(undeclared),
            )
    except Exception:  # noqa: BLE001 — a checker hiccup must never affect boot
        logger.debug("consumes.yaml conformance WARN check skipped", exc_info=True)


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Fans out to every declared sub-plugin."""
    registered = load_and_register_subplugins(ctx)
    logger.info(
        "hermes-smd-overlay registered %d sub-plugin(s): %s",
        len(registered),
        ", ".join(registered),
    )
    _warn_on_undeclared_env_reads()
