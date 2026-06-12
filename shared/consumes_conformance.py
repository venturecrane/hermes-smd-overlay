"""Reconcile the env vars the overlay STATICALLY reads against contracts/consumes.yaml.

One discovery function, two callers (derive-don't-duplicate):
  - tests/test_consumes_conformance.py  — fail-closed CI gate.
  - the umbrella __init__.register()      — WARN-only at boot (never fails).

Discovery is AST-based, not regex, so comments/docstrings/multi-line calls
never produce a false positive — a flaky conformance gate gets disabled, which
would defeat the purpose. We match only STRING-LITERAL env reads:

    os.environ["X"] / os.environ.get("X") / os.getenv("X")
    require("A", "B", ...) / get_secret("X")     (the shared/secrets.py helpers)

Reads whose name is a variable (os.environ.get(binding), os.environ[name],
f"WEBHOOK_SECRET_{route}") are plumbing/indirection — they carry no literal, so
they are simply never matched here, and are documented under `dynamic_reads`
in consumes.yaml instead.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import yaml

# Overlay code scanned for env reads (excludes tests/ and __pycache__).
_SCAN_DIRS = ("plugins", "shared", "bootstrap", "hooks")
_SCAN_FILES = ("webhook_gate.py", "__init__.py")
# Helper functions in shared/secrets.py whose string-literal args name env vars.
_SECRET_FUNCS = frozenset({"require", "get_secret"})
# An env-var-name-shaped literal.
_ENV_NAME = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


def overlay_root() -> Path:
    """The overlay repo/package root (where plugin.yaml + contracts/ live)."""
    return Path(__file__).resolve().parent.parent


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _const_env_name(node: ast.AST | None) -> str | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _ENV_NAME.match(node.value)
    ):
        return node.value
    return None


def _literal_env_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        # os.environ["X"]
        if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            name = _const_env_name(node.slice)
            if name:
                names.add(name)
            continue
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # os.environ.get("X")
        if isinstance(fn, ast.Attribute) and fn.attr == "get" and _is_os_environ(fn.value):
            name = _const_env_name(node.args[0]) if node.args else None
            if name:
                names.add(name)
        # os.getenv("X")
        elif (
            isinstance(fn, ast.Attribute)
            and fn.attr == "getenv"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "os"
        ):
            name = _const_env_name(node.args[0]) if node.args else None
            if name:
                names.add(name)
        # require("A", "B", ...) / get_secret("X")  — the shared/secrets.py helpers
        elif (isinstance(fn, ast.Name) and fn.id in _SECRET_FUNCS) or (
            isinstance(fn, ast.Attribute) and fn.attr in _SECRET_FUNCS
        ):
            for arg in node.args:
                name = _const_env_name(arg)
                if name:
                    names.add(name)
    return names


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files.extend((root / d).rglob("*.py"))
    for f in _SCAN_FILES:
        p = root / f
        if p.is_file():
            files.append(p)
    out: list[Path] = []
    for f in files:
        parts = set(f.parts)
        if "tests" in parts or "__pycache__" in parts or f.name.startswith("test_"):
            continue
        # shared/secrets.py (the generic accessor) is scanned too, but its only
        # reads are os.environ[name]/.get(name) with a VARIABLE name, so the AST
        # scan finds nothing there — the helper itself contributes no literal.
        out.append(f)
    return out


def discover_static_env_reads(root: Path | None = None) -> set[str]:
    """Every string-literal env var name the overlay reads, by AST."""
    root = root or overlay_root()
    names: set[str] = set()
    for f in _python_files(root):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names |= _literal_env_names(tree)
    return names


def declared_vars(root: Path | None = None) -> dict[str, dict]:
    """The `vars` map from contracts/consumes.yaml."""
    root = root or overlay_root()
    data = yaml.safe_load((root / "contracts" / "consumes.yaml").read_text(encoding="utf-8")) or {}
    return data.get("vars", {}) or {}


@dataclass(frozen=True)
class Reconciliation:
    undeclared: frozenset[str]  # read with a literal but absent from consumes.yaml `vars`
    stale_static: frozenset[str]  # declared `discovery: static` but never read with a literal

    @property
    def ok(self) -> bool:
        return not self.undeclared and not self.stale_static


def reconcile(root: Path | None = None) -> Reconciliation:
    root = root or overlay_root()
    read = discover_static_env_reads(root)
    declared = declared_vars(root)
    undeclared = read - set(declared)
    stale_static = {
        name
        for name, spec in declared.items()
        if isinstance(spec, dict) and spec.get("discovery") == "static" and name not in read
    }
    return Reconciliation(frozenset(undeclared), frozenset(stale_static))
