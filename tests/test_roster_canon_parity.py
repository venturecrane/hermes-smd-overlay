"""The RUNTIME half of the ss#2284 cross-language parity contract.

This copy of ``_canonicalize_roster_entry`` is the one that actually decides who
a roster line names when a send is classified on a seat. The console's validator
(``src/lib/operator/customer-yaml/sections-scope.ts::canonRosterAddress``)
decides whether two authored spellings are "the same address" for its collision
rules -- no address under two outbound classes, no address in both the outbound
roster and ``inbound_allow_from``.

When the two disagree, a config passes validation as two addresses and resolves
here to one: one human holding two silent exposure classes. They DID disagree
until 2026-09-01. The validator did a bare ``trim().toLowerCase()`` where this
module NFC-normalizes, so NFD- and NFC-spelled ``jose'@firm.example`` were two
addresses there and one here; and the whitespace sets differed, JavaScript's
``\\s`` covering NBSP, the ideographic space and the BOM where this module's
explicit character list did not.

``tests/fixtures/roster-canon-cases.json`` is copied VERBATIM from
``ss-console/operator/contracts/fixtures/roster-canon-cases.json`` in the same
change whenever it moves -- mirrored-artifact discipline, the same rule the
seat/gate snapshot and body-canon-vectors.json follow. Three suites load it (the
console's vitest, ss-console's pytest, this one); none of the three can move the
rule alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.recipient_classifier import _canonicalize_roster_entry

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "roster-canon-cases.json").read_text()
)["cases"]

# One code point, spelled out. This file is about characters that render as
# nothing; an invisible literal in a test is a defect waiting to be "cleaned up"
# by an editor that strips whitespace.
CP = chr


def test_fixture_carries_the_cases_that_caught_the_divergence() -> None:
    """A fixture stripped of its adversarial rows would leave every assertion
    below passing while measuring nothing (Law 12)."""
    assert len(_CASES) >= 15
    inputs = [c["input"] for c in _CASES]
    assert any(CP(0x0301) in i for i in inputs), "no NFD (combining acute) case"
    assert any(CP(0x00A0) in i for i in inputs), "no NBSP case"
    assert any(CP(0xFEFF) in i for i in inputs), "no BOM case"
    assert any(CP(0x0085) in i for i in inputs), "no NEL (C1 control) case"
    # The @domain grant is the only path the roster-entry check guards alone:
    # it returns before _canonicalize_address is ever reached. Without a case
    # here, reverting that check leaves the suite green.
    assert any(i.startswith("@") and c["expected"] is None for i, c in
               ((c["input"], c) for c in _CASES)), "no refused @domain grant case"
    assert any(c["expected"] is not None for c in _CASES), "no acceptance case"


@pytest.mark.parametrize("case", _CASES, ids=[c["name"][:60] for c in _CASES])
def test_canonicalization_matches_the_arbiter(case: dict) -> None:
    assert _canonicalize_roster_entry(case["input"]) == case["expected"]


def test_nfd_and_nfc_spellings_are_one_address() -> None:
    """The defect in one line: the same human, spelled two ways."""
    nfd = _canonicalize_roster_entry("jose" + CP(0x0301) + "@firm.example")
    nfc = _canonicalize_roster_entry("jos" + CP(0x00E9) + "@firm.example")
    assert nfd is not None
    assert nfd == nfc
