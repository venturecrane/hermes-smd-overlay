"""The drafting lane's declared exit (ss ADR 0083, ss-console #2094).

What these tests pin, in priority order:

1. An internal-artifact class the seat declared is GATED — a work_product draft
   delivered on a turn that did not read the voice spec is refused. That is the
   row `spec_gate` alone could not reach, because `work_product` has no
   recipient and nothing in a tool call resolves it.
2. An explicit `output_class` cannot MANUFACTURE a declaration. This is the
   trust property that makes accepting the parameter safe at all, and it is the
   one an unrelated refactor is most likely to quietly break.
3. The four outbound classes cannot be declared here. They resolve from their
   recipient at the send site with better evidence, and a second resolver would
   be a second authority over an answered question.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from shared import spec_gate
from shared.spec_status import SPEC_STATUS

_ROOT = Path(__file__).parent.parent


def _load_drafting():
    """Import the hyphenated plugin package by path.

    `plugins/hermes-smd-drafting` is not a dotted module path, which is the same
    constraint that put `check_spec_gate` in `shared/`. The plugin loader
    resolves these by path at boot; the tests do the same rather than inventing
    an import shim that production never uses.
    """
    spec = importlib.util.spec_from_file_location(
        "smd_drafting_under_test",
        _ROOT / "plugins" / "hermes-smd-drafting" / "__init__.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drafting = _load_drafting()

SESSION = "sess-deliver-1"
BODY = "Your driver entered the intersection against a red signal.\n"


@pytest.fixture(autouse=True)
def _clean():
    SPEC_STATUS._reset_for_tests()
    spec_gate._AUDIT_WIRED = True  # audit wiring is not the subject here
    spec_gate._AUDIT_CLIENT = None
    spec_gate._AUDIT_CUSTOMER_SLUG = None
    yield
    SPEC_STATUS._reset_for_tests()


@pytest.fixture
def spec_tree(tmp_path, monkeypatch):
    """An installed work_product voice spec plus its root-owned manifest."""
    body = "Open on the operative fact.\n"
    rel = "classes/work_product/voice.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(body)
    import hashlib

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "customer": "pilot-smokeball",
                "source_digest": "deadbeef",
                "specs": {
                    rel: {
                        "output_class": "work_product",
                        "prop": "voice",
                        "sha256": hashlib.sha256(body.encode()).hexdigest(),
                    }
                },
            }
        )
    )
    monkeypatch.setenv("SMD_SPEC_DIR", str(tmp_path))
    return tmp_path


def _declare(monkeypatch, classes: dict) -> None:
    """Point the gate's config resolver at an authored declaration."""

    class FakeConfig:
        output_classes = classes

        @classmethod
        def from_volume(cls):
            return cls

    monkeypatch.setattr(spec_gate, "CustomerConfig", FakeConfig)


def _call(**over):
    args = {"output_class": "work_product", "body": BODY, "seam": "smokeball_memo"}
    args.update(over)
    return drafting._handle(args, session_id=SESSION)


# ---------------------------------------------------------------- the row


def test_declared_class_refuses_when_the_spec_was_not_read(spec_tree, monkeypatch):
    """The whole point: work_product is gated where spec_gate could not reach."""
    _declare(monkeypatch, {"work_product": {"voice_spec": "expected"}})
    out = _call()
    assert out.startswith("Refused")
    assert "work_product" in out
    # Pin the REASON, not just the refusal. `gate_error` also refuses and also
    # says "deliver again", so asserting only on the remedy would let this test
    # pass on an evaluation fault — green for the wrong reason, which is worse
    # than red.
    assert "did not read it" in out
    # An internal artifact must NOT be told to "create a draft for review" — it
    # already is one, and offering that reads as an escape hatch.
    assert "draft for review" not in out


def test_declared_class_authorizes_after_a_verified_read(spec_tree, monkeypatch):
    _declare(monkeypatch, {"work_product": {"voice_spec": "expected"}})
    SPEC_STATUS.mark_read(SESSION, "work_product", "voice")
    out = _call()
    assert out.startswith("Authorized")
    assert "smokeball_memo" in out


def test_a_read_of_a_different_class_does_not_certify_this_one(spec_tree, monkeypatch):
    _declare(monkeypatch, {"work_product": {"voice_spec": "expected"}})
    SPEC_STATUS.mark_read(SESSION, "staff", "voice")
    assert _call().startswith("Refused")


# ------------------------------------------------- the trust property


def test_an_explicit_class_cannot_manufacture_a_declaration(spec_tree, monkeypatch):
    """Naming a class the seat never declared leaves the gate SILENT.

    The parameter selects which authored declaration is consulted. It cannot
    create one, which is why accepting it from a tool handler is safe where
    accepting `_skill_name` from model-composed args would not be. If this ever
    fails, the parameter has become an entitlement input.
    """
    _declare(monkeypatch, {})  # nothing authored at all
    assert _call().startswith("Authorized")


def test_a_class_declared_none_is_not_gated(spec_tree, monkeypatch):
    """`none` is an authored choice, not an absence — the gate stays silent."""
    _declare(monkeypatch, {"work_product": {"voice_spec": "none"}})
    assert _call().startswith("Authorized")


# ------------------------------------------------------- the boundary


@pytest.mark.parametrize(
    "slug",
    ["staff", "outbound_client", "outbound_vendor", "outbound_external"],
)
def test_outbound_classes_cannot_be_declared_here(slug, spec_tree, monkeypatch):
    _declare(monkeypatch, {slug: {"voice_spec": "expected"}})
    out = _call(output_class=slug)
    assert out.startswith("Refused")
    assert "not a class this tool delivers" in out


def test_unknown_class_is_refused():
    assert "not a class this tool delivers" in _call(output_class="nonsense")


def test_empty_body_is_refused():
    assert _call(body="   ").startswith("Refused")


def test_record_is_deliverable():
    """`record` has no recipient either, so it is declarable for the same reason."""
    assert "record" in drafting.DELIVERABLE_CLASSES
