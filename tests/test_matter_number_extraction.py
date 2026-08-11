"""Which matter-number spellings the gate can actually SEE (ss#2269, ss#2262).

Separate from test_matter_gate.py on purpose. That file tests the verdict logic
and was fully green while the extractor in front of it was silently truncating a
real matter number — because its fixture number ("2026-PI-101") happens to sit in
the one alternation branch that always worked. A suite cannot catch a defect in
the input it never varies, so the input is the subject here.

Every number below was read off the pilot's live Smokeball on 2026-08-11
(vfy_01KZRZH044CH4N5EEKHQ9A6KHW), not invented. PI-2026-0001 is the one matter of
nine on that seat with a COMPLETE party list — the only matter the gate could act
on at all — and it is exactly the spelling that was being mangled.
"""

from __future__ import annotations

import pytest

from shared import matter_binding, matter_gate

SID = "s-extract"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
CLIENT_A = "alvarez@example.com"
CLIENT_B = "okafor@example.com"

# Real pilot matter numbers, spanning both shapes the firm's data uses.
REAL_NUMBERS = ["2026-PI-101", "2026-PI-106", "2026-OPS-001", "PI-2026-0001"]


@pytest.fixture(autouse=True)
def _clean():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


def _closed_with_alias(number: str) -> None:
    m = matter_binding.membership_for(SID)
    m.add(M_A, [CLIENT_A], complete=True)
    m.add_alias(number, M_A)


@pytest.mark.parametrize("number", REAL_NUMBERS)
def test_real_matter_numbers_extract_whole(number: str) -> None:
    # Whole, not a prefix. "PI-2026-0001" -> {"PI-2026"} was ss#2269: first-match-
    # wins alternation truncated it. A silently WRONG token is worse than none —
    # it reads as "cites a matter I never saw" rather than as a defect.
    assert matter_gate.cited_matters(f"Re: matter {number}. Update attached.") == {number}


@pytest.mark.parametrize("number", REAL_NUMBERS)
def test_real_matter_numbers_extract_in_lower_case(number: str) -> None:
    # ss#2262. Safe here in a way it would not be in a reporting filter:
    # _resolve_cited keeps only tokens that resolve to a matter this session
    # read, so a false positive cannot manufacture a verdict.
    assert matter_gate.cited_matters(f"re: matter {number.lower()}.") == {number.lower()}


@pytest.mark.parametrize("number", REAL_NUMBERS)
def test_each_real_number_withholds_a_non_party(number: str) -> None:
    _closed_with_alias(number)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Re: matter {number}. Deposition set for Tuesday.",
        recipients={CLIENT_B},
    )
    assert v.is_mismatch and v.should_withhold
    assert number in v.matters


@pytest.mark.parametrize("number", REAL_NUMBERS)
def test_control_each_real_number_passes_a_party(number: str) -> None:
    # The half that makes the parametrised test above mean something: a gate that
    # withheld everything would satisfy it and would have measured nothing.
    _closed_with_alias(number)
    v = matter_gate.evaluate(session_id=SID, body=f"Re: matter {number}.", recipients={CLIENT_A})
    assert v.status == "ok"
    assert not v.should_withhold


def test_lower_case_citation_resolves_through_the_alias() -> None:
    # Extraction preserves the body's case and _norm_matter folds it at lookup.
    # If those two halves disagreed, a lower-case citation would extract and then
    # fail to resolve — indistinguishable from "matter never read".
    _closed_with_alias("2026-PI-101")
    v = matter_gate.evaluate(session_id=SID, body="re: matter 2026-pi-101.", recipients={CLIENT_B})
    assert v.is_mismatch and v.should_withhold


@pytest.mark.parametrize(
    "text",
    [
        "Please review the 2026-2027 budget and the follow-up notes.",
        "See sections 12-14 and the COVID-19 addendum.",
        "Call me at 602-555-0143 tomorrow.",
        "Our SLA-2 response window applies.",
        "Invoice 4500 is paid; PO 22-9 is not.",
    ],
)
def test_extractor_does_not_swallow_ordinary_prose(text: str) -> None:
    # The falsifier for widening the pattern and adding IGNORECASE. If either
    # made everyday hyphenated text look like a citation, the gate would start
    # naming matters nobody wrote, and every such body would route to review.
    assert matter_gate.cited_matters(text) == set()
