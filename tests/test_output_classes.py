"""A firm rule may only attach to an output class that EXISTS (ss-console#2546).

THE LIVE DEFECT (pilot, 2026-08-22, 20:29Z to 20:52Z). Four firm rules were
recorded against classes that are not in the registry: ``b91c239c`` on
``demand_letter``, and ``0685fc1f`` / ``234d57ea`` / ``c0a5ada6`` on ``letter``.
The last was explicitly about "internal emails to our own staff", which is the
``staff`` class. Every one of them was accepted, installed into a directory
nothing reads, and reported to the firm as in effect.

WHY NOTHING CAUGHT IT, and why the fix belongs on the seat. The broker checks
that a slug is well-formed and the intake writes wherever the slug points;
neither can read the seat's contracts, so neither can ask whether a well-formed
slug names anything. The registry ships to the seat, so the seat is the layer
that can answer, and the answer is a refusal that names the six classes in
words a model can act on.

WHAT THESE TESTS PIN:

* the list is the registry's, checked against the registry file itself wherever
  it is reachable, and ``workspace`` is NOT one of them -- it is a key under
  ``skill_bindings:`` naming a skill, and reading it as a class is the same
  error as ``letter``, one level up;
* the exact slugs from the incident are refused, on propose and on submit;
* a real class passes, which is the falsifier for a gate that just says no;
* the refusal names what each class IS, because a refusal listing six slugs
  teaches six slugs and the model's wrong guess was already slug-shaped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.output_classes import (
    OUTPUT_CLASS_MEANINGS,
    OUTPUT_CLASSES,
    catalogue,
    describe,
    is_output_class,
)

#: The registry, if this machine has an ss-console checkout beside the overlay or
#: is the seat itself (the Dockerfile copies operator/contracts/ to /app/contracts).
_REGISTRY_CANDIDATES = (
    Path("/app/contracts/output-classes.yaml"),
    Path.home() / "dev/ss-console/operator/contracts/output-classes.yaml",
)


def _registry_path() -> Path | None:
    return next((p for p in _REGISTRY_CANDIDATES if p.is_file()), None)


def test_the_six_classes_are_the_registrys_six():
    """The pinned list, stated flat. A change here is a change to what the
    Operator is able to produce, and it should read like one in the diff."""
    assert OUTPUT_CLASSES == {
        "staff",
        "work_product",
        "record",
        "outbound_client",
        "outbound_vendor",
        "outbound_external",
    }


def test_workspace_is_a_skill_and_not_a_class():
    """THE SEVENTH THAT ISN'T. ``workspace`` appears in output-classes.yaml under
    ``skill_bindings:``, naming the workspace SKILL. Reading the file's keys
    without reading its structure produces a seventh class that does not exist,
    which is the same mistake as ``letter`` made one level up."""
    assert "workspace" not in OUTPUT_CLASSES


def test_the_pinned_list_matches_the_registry_file():
    """The drift check, run wherever the registry is on disk. It is skipped in
    the overlay's own CI, which has no ss-console checkout -- the guard that runs
    on every change to the registry is ss-console's
    test_output_class_conformance.py, on the side the file lives on."""
    path = _registry_path()
    if path is None:
        pytest.skip("no output-classes.yaml reachable; ss-console CI holds this side")
    yaml = pytest.importorskip("yaml")
    registry = yaml.safe_load(path.read_text())
    assert set(registry["classes"]) == OUTPUT_CLASSES
    assert "workspace" in registry["skill_bindings"]


@pytest.mark.parametrize("slug", sorted(OUTPUT_CLASSES))
def test_every_class_says_what_it_is(slug):
    """A slug with no plain-words meaning would fall out of the refusal silently
    and be the one the model keeps guessing wrong."""
    assert describe(slug)
    assert slug in catalogue()
    assert describe(slug) in catalogue()


@pytest.mark.parametrize(
    "value", ["letter", "demand_letter", "email", "outbound", "workspace", "", None, 7]
)
def test_what_is_not_a_class(value):
    """The four from the incident, the fixture slug this repo used to use, the
    plausible seventh, and two shapes that are not strings at all."""
    assert not is_output_class(value)
    assert describe(value) == ""


def test_a_slug_is_matched_exactly_not_repaired():
    """Case and whitespace are normalized, because those are the same slug typed
    untidily. Nothing else is: a near miss is a rule that would attach to
    nothing, and guessing which class was meant is how ``letter`` became a
    directory."""
    assert is_output_class("  Staff ")
    assert not is_output_class("staff_")
    assert not is_output_class("staffs")
    assert not is_output_class("out_bound_client")


def test_the_catalogue_carries_all_six_with_their_meanings():
    text = catalogue()
    for slug, words in OUTPUT_CLASS_MEANINGS.items():
        assert f"{slug} ({words})" in text
