"""Every plugin directory carries a manifest, because Hermes discovers plugins
by manifest and a directory without one is INVISIBLE.

WHAT THIS COSTS WHEN IT IS MISSING (ss-console#2616, 2026-09-14). The
chronology-package plugin shipped with a complete `register()`, three correct
tool definitions, an action-class row for each, a passing unit suite, and no
`plugin.yaml`. Nothing anywhere failed. It simply never loaded, so the three
tools never existed on any surface.

The cost was paid on a client seat: an administrator emailed "build the
chronology package for matter <n>", the router classified it correctly, the
agent resolved the matter, sized the selection and read the procedure, then
could not submit. From outside, a tool that was never registered is
indistinguishable from a model that chose not to call one -- and the failure
survived a code review, a merge, a green CI run and a reprovision, because every
one of those looks at the code and none looks for the file that makes the code
reachable.

The warn tier (`WEBHOOK_EXPECTED_TOOLS`) named the two tools as `offered: false`
and is what made the diagnosis a one-liner. This test is the other half: it
fails on the cause instead of reporting the symptom.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _plugin_dirs() -> list[Path]:
    """Directories that are plugins: they define a package with a register()."""
    return sorted(
        d
        for d in PLUGINS.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_")) and (d / "__init__.py").is_file()
    )


def test_there_are_plugins_to_check():
    """The falsifier. If the discovery above stops matching, every assertion
    below passes vacuously and this file becomes decoration."""
    found = _plugin_dirs()
    assert len(found) >= 10, f"only found {len(found)} plugin dirs; the discovery is broken"


@pytest.mark.parametrize("plugin", _plugin_dirs(), ids=lambda p: p.name)
def test_every_plugin_dir_has_a_manifest(plugin: Path):
    manifest = plugin / "plugin.yaml"
    assert manifest.is_file(), (
        f"{plugin.name} defines register() but has no plugin.yaml, so Hermes will never "
        f"discover it and none of its tools or hooks will exist at runtime"
    )


@pytest.mark.parametrize("plugin", _plugin_dirs(), ids=lambda p: p.name)
def test_the_manifest_names_the_plugin_it_sits_in(plugin: Path):
    """A manifest naming a different plugin loads the wrong thing, or nothing."""
    doc = yaml.safe_load((plugin / "plugin.yaml").read_text(encoding="utf-8")) or {}
    assert doc.get("name") == plugin.name, (
        f"{plugin.name}/plugin.yaml declares name={doc.get('name')!r}"
    )
    assert str(doc.get("description") or "").strip(), f"{plugin.name} has no description"


def test_a_plugin_declaring_broker_tools_requires_the_broker_socket():
    """Tools that speak to the capability broker must say so in the manifest, so
    a seat without the socket does not load them and then fail per call."""
    for plugin in _plugin_dirs():
        src = (plugin / "__init__.py").read_text(encoding="utf-8", errors="replace")
        if 'requires_env=["SMD_WORKSPACE_BROKER_SOCKET"]' not in src and "[_SOCKET_ENV]" not in src:
            continue
        doc = yaml.safe_load((plugin / "plugin.yaml").read_text(encoding="utf-8")) or {}
        assert "SMD_WORKSPACE_BROKER_SOCKET" in (doc.get("requires_env") or []), (
            f"{plugin.name} registers broker-backed tools but its manifest does not require the socket"
        )
