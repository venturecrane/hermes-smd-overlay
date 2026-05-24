"""Tests for ``bootstrap.translate``.

Covers:
  - The ``bootstrap.translate`` module imports successfully.
  - ``translate_customer_yaml`` is callable.
  - Calling the placeholder with bogus args raises ``NotImplementedError``
    (the §7 placeholder behavior — actual translation lands in a
    follow-on PR).
"""

from __future__ import annotations

import pytest

from bootstrap import translate


def test_translate_customer_yaml_is_callable() -> None:
    """``translate_customer_yaml`` must exist and be callable."""
    assert callable(translate.translate_customer_yaml)


def test_translate_customer_yaml_raises_not_implemented() -> None:
    """The §7 placeholder must raise NotImplementedError on any input."""
    with pytest.raises(NotImplementedError):
        translate.translate_customer_yaml({"bogus": "input"})
