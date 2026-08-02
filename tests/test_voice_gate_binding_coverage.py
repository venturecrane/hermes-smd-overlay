"""Coverage assertion for the ss#2086 step-1 voice-gate repoint: ADDITIVE, provably.

The repoint makes the voice gate resolve its binding regime per (seat × output
class): a class declared ``output_classes.<class>.voice_spec: expected`` is
governed by the authored-spec binding, every other class keeps the original
``voice_library`` / transform-ran binding. The hazard a substitutive variant
would create is concrete: a live client seat (ashton-price) authors
``voice_library`` and declares no outside class, so "seat declares specs
somewhere ⇒ spec logic only" — or "declared classes only, no fallback" — would
silently un-gate its autonomous client/vendor sends.

This module makes "additive" an enforced property rather than a review comment:
``resolve_binding_regime`` is a pure function, and a table-driven sweep over a
CHECKED-IN SNAPSHOT of every real seat's gate-relevant config asserts that the
new code never leaves a (seat × class) unbound where the OLD predicate
(``bool(voice_library)``) bound the gate.

FALSIFIER (verified during development, per Law 12 — a check that cannot fail
has measured nothing): temporarily rewriting ``resolve_binding_regime`` to the
substitutive form ``return REGIME_SPEC if isinstance(class_declaration, dict)
and declared-expected else REGIME_UNBOUND`` (dropping the voice_library
fallback) makes ``test_no_seat_class_loses_its_downgrade`` fail on every
voice-authored seat's undeclared classes — ashton-price × outbound_client
included. Restoring the additive form makes it pass.

The snapshot fixture (``tests/contract/seat_gate_binding_snapshot.json``) is
generated from ss-console ``operator/customers/*/customer.yaml`` with
underscore-prefixed template dirs excluded; the console side owns regeneration
(ss#2086 plan C3, following the ``validator_parity_fixtures.json`` precedent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import load_plugin

_SNAPSHOT_PATH = Path(__file__).parent / "contract" / "seat_gate_binding_snapshot.json"

#: Every output class the recipient-class map can resolve to, plus the two
#: internal-artifact classes and the unresolved case (``None``). The additive
#: property must hold across ALL of them — the gate only fires on the outside
#: classes today, but the pure function is class-agnostic and a future caller
#: must inherit the same invariant.
_CLASSES: tuple[str | None, ...] = (
    "staff",
    "outbound_client",
    "outbound_vendor",
    "outbound_external",
    "work_product",
    "record",
    None,
)


def _voice_gate():
    return load_plugin("hermes-smd-trust").enforce.voice_gate


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(_SNAPSHOT_PATH.read_text())


# ---------------------------------------------------------------------------
# Fixture hygiene — the snapshot is what the sweep's strength rests on
# ---------------------------------------------------------------------------


def test_snapshot_has_real_seats_and_no_templates(snapshot):
    """The sweep is only as strong as its table: it must cover real seats and
    must not have swallowed a template scaffold as if it were one."""
    seats = snapshot["seats"]
    assert len(seats) >= 1
    assert all(not slug.startswith("_") for slug in seats)


def test_snapshot_excluded_exactly_the_template_dirs(snapshot):
    """The generator's exclusion is asserted separately from the inclusion: the
    excluded list exists, and everything on it is underscore-prefixed — nothing
    real was dropped under the template rule."""
    excluded = snapshot["excluded_template_dirs"]
    assert excluded, "template dirs exist console-side; an empty list means the glob broke"
    assert all(slug.startswith("_") for slug in excluded)


def test_snapshot_reflects_the_fleetwide_binding_the_repoint_must_preserve(snapshot):
    """The premise ss#2086 states: every real seat authors voice_library, so the
    OLD predicate binds the gate fleet-wide. If a future seat legitimately drops
    voice_library, the console-side regeneration updates this fixture and this
    assertion is the prompt to re-review the sweep, not an error to delete."""
    assert all(s["voice_library_authored"] for s in snapshot["seats"].values())


# ---------------------------------------------------------------------------
# THE coverage assertion — never weaker than the old predicate
# ---------------------------------------------------------------------------


def test_no_seat_class_loses_its_downgrade(snapshot):
    """For every real (seat × class) — declared, undeclared, and unresolved —
    wherever the OLD predicate bound the gate, the NEW regime resolution binds
    it too. ``REGIME_UNBOUND`` where the old gate fired is the substitutive
    failure this test exists to make impossible to merge."""
    vg = _voice_gate()
    for slug, seat in snapshot["seats"].items():
        old_bound = seat["voice_library_authored"]
        for cls in _CLASSES:
            declaration = seat["output_classes"].get(cls) if cls else None
            regime = vg.resolve_binding_regime(
                voice_library_authored=old_bound,
                class_declaration=declaration,
            )
            if old_bound:
                assert regime != vg.REGIME_UNBOUND, (
                    f"{slug} × {cls}: the old voice_library binding fired here; "
                    f"the repointed gate resolved {regime} — the repoint went substitutive"
                )


def test_declared_classes_resolve_to_spec_and_undeclared_keep_the_fallback(snapshot):
    """The per-class split, on the real fleet: a declared-expected class is
    governed by the spec regime; every class the seat did NOT declare stays on
    the Mechanism-B fallback (a declaring seat does not flip wholesale)."""
    vg = _voice_gate()
    for slug, seat in snapshot["seats"].items():
        declared_expected = {
            cls
            for cls, decl in seat["output_classes"].items()
            if str(decl.get("voice_spec", "")).strip().lower() == "expected"
        }
        for cls in _CLASSES:
            declaration = seat["output_classes"].get(cls) if cls else None
            regime = vg.resolve_binding_regime(
                voice_library_authored=seat["voice_library_authored"],
                class_declaration=declaration,
            )
            if cls in declared_expected:
                assert regime == vg.REGIME_SPEC, f"{slug} × {cls}"
            elif seat["voice_library_authored"]:
                assert regime == vg.REGIME_MECHANISM_B, f"{slug} × {cls}"


def test_ashton_price_outside_sends_keep_the_fallback_downgrade(snapshot):
    """The narrowing hazard, named: the live client seat declares no outside
    class, so its autonomous client/vendor/outside sends MUST stay on the
    Mechanism-B binding. This is the row a substitutive repoint silently
    un-gates."""
    vg = _voice_gate()
    seat = snapshot["seats"]["ashton-price"]
    assert seat["voice_library_authored"]
    for cls in ("outbound_client", "outbound_vendor", "outbound_external"):
        regime = vg.resolve_binding_regime(
            voice_library_authored=True,
            class_declaration=seat["output_classes"].get(cls),
        )
        assert regime == vg.REGIME_MECHANISM_B


# ---------------------------------------------------------------------------
# The pure function's own table — synthetic corners the fleet doesn't exercise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("voice_authored", "declaration", "expected_regime"),
    [
        # Declared expected ⇒ spec regime, with and without voice_library.
        (True, {"voice_spec": "expected"}, "spec"),
        (False, {"voice_spec": "expected"}, "spec"),
        # Case/whitespace-normalized, matching shared.spec_gate._spec_expected.
        (True, {"voice_spec": " Expected "}, "spec"),
        # `none` is a legitimate authored choice ⇒ the fallback governs.
        (True, {"voice_spec": "none"}, "mechanism_b"),
        (False, {"voice_spec": "none"}, "unbound"),
        # Undeclared / malformed declaration states ⇒ the fallback governs.
        (True, None, "mechanism_b"),
        (True, {}, "mechanism_b"),
        (True, "expected", "mechanism_b"),  # non-mapping declaration is not a declaration
        (False, None, "unbound"),
        (False, {}, "unbound"),
    ],
)
def test_regime_table(voice_authored, declaration, expected_regime):
    vg = _voice_gate()
    assert (
        vg.resolve_binding_regime(
            voice_library_authored=voice_authored, class_declaration=declaration
        )
        == expected_regime
    )
