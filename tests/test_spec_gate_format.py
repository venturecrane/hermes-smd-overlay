"""A non-conforming output does not go out (ss ADR 0083 criterion 7/8).

The checker decides whether text has the authored shape. This file is about the
half that matters to a reader: that a failing output is REFUSED rather than
noted, and that the refusal names which rule broke.

Two failures that must stay distinct, because their fixes differ:

* ``spec_not_read`` — the model never consulted its spec.
* ``format_violation`` — it consulted it and produced something else.

Collapsing them would tell a writer to go read a document they already read.

The fixture builds a REAL on-disk spec tree rather than monkeypatching
``entries_for_class``. It used to patch the helper, which bypassed ``spec_dir``,
``load_entries`` and ``verify`` — the exact seam that decides whether a control
is installed, tampered, or unprovable (ss-console #2234). A test that skips it
cannot tell a working format spec from a missing one.
"""

from __future__ import annotations

import hashlib
import json
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
def gate(monkeypatch, tmp_path):
    g = spec_gate

    # Declared: format expected, voice not — so only the shape rules bind and
    # the assertions below are what decides.
    monkeypatch.setattr(g, "_spec_expected", lambda _c: False)
    monkeypatch.setattr(g, "_declared", lambda _c, prop: prop == "format")

    # A real INSTALLED format spec: real bytes, a real digest, and the four
    # rules recorded the way root records them. `verify` has to pass here or the
    # gate reads the control as tampered and refuses before any shape is checked.
    # The VOICE spec is installed too, though `_spec_expected` is False by
    # default so it does not bind here. It matters for the one test that flips
    # voice on: since #2234, "declared voice with nothing installed" is a broken
    # control that a staff send passes through, so a test about an UNREAD spec
    # has to have a spec there to leave unread.
    specs = {
        "classes/staff/format.md": ("format", FOUR_RULES),
        "classes/staff/voice.md": ("voice", {}),
    }
    entries = {}
    for rel, (prop, assertions) in specs.items():
        body = f"The authored {prop} for staff mail.\n"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        entries[rel] = {
            "class": "staff",
            "property": prop,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "bytes": len(body),
            "assertions": assertions,
        }
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "smd-staging",
                "source_digest": "deadbeef",
                "specs": entries,
            }
        )
    )
    monkeypatch.setenv(spec_manifest.SPEC_DIR_ENV, str(tmp_path))
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
    """Same class, same send, two causes — and they must not read alike.

    Both halves need their spec INSTALLED (the fixture installs both): a
    declared spec that was never installed is a broken control, and a staff send
    passes through those rather than refusing (ss-console #2234).
    """
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
