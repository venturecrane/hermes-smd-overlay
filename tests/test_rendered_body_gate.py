"""In-turn rendered-body check (WS-RENDER): template + closed slots only."""

from __future__ import annotations

import pytest

from shared import rendered_body_gate


@pytest.fixture(autouse=True)
def _clean():
    rendered_body_gate._DEVIATIONS.clear()
    yield
    rendered_body_gate._DEVIATIONS.clear()


TEMPLATE = "The verification for {signer} is ready. Note: {note}"
SLOTS = {"note": ["signer ambiguous", "objections-only responses in question"]}
DECL = {
    "enforce": True,
    "templates": [{"name": "shape_a", "template": TEMPLATE, "slots": SLOTS}],
}


def test_exact_template_with_valid_slots_passes():
    body = "The verification for Ana Diaz is ready. Note: signer ambiguous"
    assert rendered_body_gate.check_body("s1", body, DECL) is None


def test_open_slot_accepts_any_short_value():
    body = "The verification for J. Q. Public Jr. is ready. Note: signer ambiguous"
    assert rendered_body_gate.check_body("s1", body, DECL) is None


def test_slot_outside_the_closed_phrase_list_blocks():
    body = "The verification for Ana is ready. Note: I think we should hurry"
    block = rendered_body_gate.check_body("s1", body, DECL)
    assert block is not None and block["action"] == "block"


def test_text_beyond_the_template_blocks():
    body = "The verification for Ana is ready. Note: signer ambiguous\nAlso, one more thing."
    assert rendered_body_gate.check_body("s1", body, DECL) is not None


def test_slot_length_cap():
    long_name = "x" * 200
    body = f"The verification for {long_name} is ready. Note: signer ambiguous"
    assert rendered_body_gate.check_body("s1", body, DECL) is not None


def test_zero_slot_template_is_exact_match_whitespace_tolerant():
    decl = {
        "enforce": True,
        "templates": [{"name": "note", "template": "The run failed.", "slots": {}}],
    }
    assert rendered_body_gate.check_body("s1", "The run failed.  \n", decl) is None
    assert rendered_body_gate.check_body("s1", "The run failed. Sorry!", decl) is not None


def test_second_deviation_switches_to_move_on():
    body = "free composition"
    first = rendered_body_gate.check_body("s1", body, DECL)
    second = rendered_body_gate.check_body("s1", body, DECL)
    assert "change nothing else" in first["message"]
    assert "move on" in second["message"]
    for message in (first["message"], second["message"]):
        for banned in ("gate", "rule", "ceiling", "template"):
            assert banned not in message.lower()


def test_no_declaration_or_enforce_false_passes_untouched():
    assert rendered_body_gate.check_body("s1", "anything", None) is None
    dark = {"enforce": False, "templates": [{"template": "The run failed.", "slots": {}}]}
    assert rendered_body_gate.check_body("s1", "anything", dark) is None


def test_any_declared_template_may_match():
    decl = {
        "enforce": True,
        "templates": [
            {"name": "a", "template": "Alpha {x}", "slots": {}},
            {"name": "b", "template": "Beta body.", "slots": {}},
        ],
    }
    assert rendered_body_gate.check_body("s1", "Beta body.", decl) is None
    assert rendered_body_gate.check_body("s1", "Alpha filled", decl) is None
