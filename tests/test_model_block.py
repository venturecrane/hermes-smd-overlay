"""The profile config's ``model`` names its provider, so Hermes offers vision.

A bare model string reads as provider "" to Hermes' image routing, which then
drops ``vision_analyze`` from every surface without a word (captain-mark,
2026-10-20: "I can't read image content on my end"). These tests pin the shape;
the seat probe that proved the shape flips the check is in the PR body.
"""

from __future__ import annotations

from bootstrap.translate import _model_block


def test_claude_model_names_anthropic():
    assert _model_block({"model": "claude-sonnet-4-6"}) == {
        "default": "claude-sonnet-4-6",
        "provider": "anthropic",
    }


def test_surrounding_whitespace_is_dropped():
    assert _model_block({"model": " claude-sonnet-5-5 "})["default"] == "claude-sonnet-5-5"


def test_non_claude_model_is_left_as_authored():
    # Naming a provider for a model we did not author one for would be a guess.
    assert _model_block({"model": "gpt-5"}) == "gpt-5"


def test_missing_model_stays_missing():
    assert _model_block({}) is None
