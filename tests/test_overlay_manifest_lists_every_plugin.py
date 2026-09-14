"""Every plugin directory is LISTED in the overlay's own manifest.

THE DEFECT THIS CLOSES (2026-09-14). `hermes-smd-medchron` was never listed in
`plugin.yaml`. The overlay enumerates the plugins it loads, so `register()` was
never called and `medchron_job_submit` / `medchron_job_status` /
`medchron_allowance` never existed on any surface, on any platform, from the day
the plugin shipped.

Nothing failed anywhere. The plugin had a complete `register()`, three correct
tool definitions, an action-class row for each, a passing unit suite, and its own
`plugin.yaml`. Every one of those inspects the plugin; none asks whether anything
loads it.

WHAT IT COST. A law firm's administrator emailed the Operator for a chronology.
Twice, the router classified it correctly, the agent resolved the matter through
its dual probe, sized the selection, read the procedure -- and then replied that
the runner tools were not reachable, because they were not. Three separate
diagnoses were wrong first (the env requirement, toolset membership, and a
missing per-plugin manifest), each plausible, each eliminated only by finding a
sibling plugin that shares the property and works.

The elimination that finally settled it is the one this test encodes:
`establish_*` and `job_status` are broker-backed plugin tools with the identical
`requires_env`, in equally unlisted toolsets, and both have run on that seat. The
only thing medchron did not share with them was a line in this file.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin.yaml"


def _listed() -> set[str]:
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    return {str(p).split("/")[-1] for p in (doc.get("plugins") or [])}


def _dirs() -> set[str]:
    return {
        d.name
        for d in (ROOT / "plugins").iterdir()
        if d.is_dir() and not d.name.startswith((".", "_")) and (d / "__init__.py").is_file()
    }


def test_there_is_something_to_check():
    """The falsifier. If either side resolves to nothing, every assertion below
    passes vacuously and this file is decoration."""
    assert len(_dirs()) >= 10, f"only {len(_dirs())} plugin dirs found; discovery is broken"
    assert len(_listed()) >= 10, (
        f"only {len(_listed())} plugins listed; the manifest read is broken"
    )


def test_every_plugin_directory_is_loaded():
    unlisted = sorted(_dirs() - _listed())
    assert not unlisted, (
        f"these plugins define register() but are not listed in plugin.yaml, so they never load "
        f"and none of their tools or hooks exist at runtime: {unlisted}"
    )


def test_the_manifest_lists_nothing_that_does_not_exist():
    """A listed path that is gone fails the load, or silently loads nothing."""
    missing = sorted(_listed() - _dirs())
    assert not missing, f"plugin.yaml lists directories that do not exist: {missing}"
