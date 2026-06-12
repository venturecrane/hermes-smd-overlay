"""Fail-closed CI gate: the overlay's literal env reads ⊆ contracts/consumes.yaml.

This is the consumer-side guard against the OP-P0-2 voice-break class — an env
var the overlay reads but nobody declared. The reconciliation logic lives in
shared/consumes_conformance.py and is shared with the boot-time WARN path.
"""

from __future__ import annotations

import ast

from shared import consumes_conformance as cc


def test_no_undeclared_static_env_reads() -> None:
    """Every string-literal env read in the overlay is declared in consumes.yaml."""
    rec = cc.reconcile()
    assert not rec.undeclared, (
        "overlay reads these env vars with a string literal but they are NOT declared in "
        f"contracts/consumes.yaml `vars`: {sorted(rec.undeclared)}. "
        "Declare them (the voice-break guard) — this is the same drift that silently broke voice."
    )


def test_no_stale_static_declarations() -> None:
    """Every var declared `discovery: static` is actually read with a literal."""
    rec = cc.reconcile()
    assert not rec.stale_static, (
        "these vars are declared `discovery: static` in consumes.yaml but no literal read was "
        f"found — stale or misclassified (use `discovery: indirect` if name is held in a constant): "
        f"{sorted(rec.stale_static)}"
    )


def test_discovery_has_teeth() -> None:
    """A scanner that silently finds nothing makes the undeclared check vacuous.

    Guard against that: the real overlay must yield a non-trivial set including
    known-present reads."""
    read = cc.discover_static_env_reads()
    assert len(read) >= 10, f"discovery suspiciously small ({len(read)}): scanner may be broken"
    for known in (
        "SMD_CUSTOMER_SLUG",
        "SMD_VOICE_VAULT_DIR",
        "SMD_D1_AUDIT_BINDING",
        "SMD_TRUST_CEILING",
    ):
        assert known in read, f"expected discovery to find {known}; scanner regressed"


def test_scanner_matches_each_literal_form_and_ignores_dynamic() -> None:
    """Unit-prove the AST scanner: it catches every literal access form and
    ignores variable-named (dynamic) reads — the property the whole gate rests on."""
    snippet = """
import os
from shared.secrets import require, get_secret
A = os.environ["LITERAL_SUBSCRIPT"]
B = os.environ.get("LITERAL_ENVIRON_GET")
C = os.getenv("LITERAL_GETENV")
D = require("LITERAL_REQUIRE_1", "LITERAL_REQUIRE_2")
E = get_secret("LITERAL_GET_SECRET")
name = "x"
F = os.environ.get(name)            # dynamic — must be ignored
G = os.environ[name]                # dynamic — must be ignored
H = os.environ.get(f"WEBHOOK_SECRET_{route}")  # constructed — must be ignored
"""
    found = cc._literal_env_names(ast.parse(snippet))
    assert found == {
        "LITERAL_SUBSCRIPT",
        "LITERAL_ENVIRON_GET",
        "LITERAL_GETENV",
        "LITERAL_REQUIRE_1",
        "LITERAL_REQUIRE_2",
        "LITERAL_GET_SECRET",
    }, f"scanner mismatch: {sorted(found)}"
