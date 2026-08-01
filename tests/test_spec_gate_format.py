"""A non-conforming output does not go out (ss ADR 0083 criterion 7/8).

The checker decides whether text has the authored shape. This file is about the
half that matters to a reader: that a failing output is REFUSED rather than
noted, and that the refusal names which rule broke.

Two failures that must stay distinct, because their fixes differ:

* ``spec_not_read`` — the model never consulted its spec.
* ``format_violation`` — it consulted it and produced something else.

Collapsing them would tell a writer to go read a document they already read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shared import spec_manifest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "hermes-smd-trust"))

from shared import spec_gate  # noqa: E402

FOUR_RULES = {
    "opening_line_prefix": "Bottom line:",
    "closing_line_prefix": "Next:",
    "single_closing_line": True,
    "forbid_bullets": True,
}

COMPLIANT = "Bottom line: it holds.\n\nThe body in prose.\n\nNext: confirm tomorrow."
TWO_CLOSERS = "Bottom line: it holds.\n\nNext: early.\n\nThe body.\n\nNext: confirm tomorrow."


@pytest.fixture
def gate(monkeypatch):
    g = spec_gate

    # Declared: format expected, voice not — so only the shape rules bind and
    # the assertions below are what decides.
    monkeypatch.setattr(g, "_spec_expected", lambda _c: False)
    monkeypatch.setattr(g, "_declared", lambda _c, prop: prop == "format")
    monkeypatch.setattr(
        spec_manifest,
        "entries_for_class",
        lambda _c, directory=None: [
            spec_manifest.SpecEntry(
                rel_path="classes/staff/format.md",
                output_class="staff",
                prop="format",
                sha256="0" * 64,
                assertions=FOUR_RULES,
            )
        ],
    )
    # Audit is best-effort and needs no transport in a unit test.
    monkeypatch.setattr(g, "_emit_spec_gate_audit", lambda **_k: None)
    return g


def test_a_conforming_output_is_allowed(gate):
    assert (
        gate.check_spec_gate(
            tool_name="email_send",
            action_class_value="external_send_internal",
            session_id="s1",
            body=COMPLIANT,
        )
        is None
    )


def test_a_non_conforming_output_is_REFUSED(gate):
    """The criterion, in one assertion: it does not go out."""
    result = gate.check_spec_gate(
        tool_name="email_send",
        action_class_value="external_send_internal",
        session_id="s1",
        body=TWO_CLOSERS,
    )
    assert result is not None
    assert result["action"] == "block"


def test_the_refusal_names_the_broken_rule(gate):
    """ "Failed a check" sends someone to read the whole spec. Name the rule."""
    result = gate.check_spec_gate(
        tool_name="email_send",
        action_class_value="external_send_internal",
        session_id="s1",
        body=TWO_CLOSERS,
    )
    assert "single_closing_line" in result["message"]


def test_format_and_not_read_are_different_refusals(gate, monkeypatch):
    """Same class, same send, two causes — and they must not read alike."""
    shape = gate.check_spec_gate(
        tool_name="email_send",
        action_class_value="external_send_internal",
        session_id="s1",
        body=TWO_CLOSERS,
    )
    monkeypatch.setattr(gate, "_spec_expected", lambda _c: True)
    monkeypatch.setattr(gate, "_declared", lambda _c, prop: False)
    unread = gate.check_spec_gate(
        tool_name="email_send",
        action_class_value="external_send_internal",
        session_id="s2",
        body=COMPLIANT,
    )
    assert shape["message"] != unread["message"]
    assert "did not read" in unread["message"] or "read the spec" in unread["message"].lower()


def test_an_undeclared_class_is_untouched(gate, monkeypatch):
    """A seat that authored nothing is not governed by anything."""
    monkeypatch.setattr(gate, "_declared", lambda _c, _p: False)
    monkeypatch.setattr(gate, "_spec_expected", lambda _c: False)
    assert (
        gate.check_spec_gate(
            tool_name="email_send",
            action_class_value="external_send_internal",
            session_id="s1",
            body=TWO_CLOSERS,
        )
        is None
    )


def test_a_class_the_gate_cannot_resolve_is_untouched(gate):
    """work_product and record never reach this gate; nothing invents a class."""
    assert (
        gate.check_spec_gate(
            tool_name="local_file_write",
            action_class_value="internal_write",
            session_id="s1",
            body=TWO_CLOSERS,
        )
        is None
    )
