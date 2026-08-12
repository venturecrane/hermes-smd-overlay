"""Outbound matter-identity gate (ss#2167).

The P0 shape under test: a letter composed from case A's material, addressed to
someone attached to case B. Every test that asserts a withhold is paired with a
control asserting the correct pairing passes — a gate that withholds everything
would satisfy the first half alone and would have measured nothing.
"""

from __future__ import annotations

import pytest

from shared import matter_binding, matter_gate

SID = "s1"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
M_B = "bbbbbbbb-1111-2222-3333-444444444444"
# What a letter actually cites.
NUM_A = "2026-PI-101"
CLIENT_A = "alvarez@example.com"
CLIENT_B = "okafor@example.com"


@pytest.fixture(autouse=True)
def _clean():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


def _closed(matter_id: str, *emails: str) -> None:
    """The matter's own complete party list was read."""
    matter_binding.membership_for(SID).add(matter_id, emails, complete=True)


def _open(matter_id: str, *emails: str) -> None:
    """Contact-keyed: this person is a party; the full set is unknown."""
    matter_binding.membership_for(SID).add(matter_id, emails, complete=False)


# ---- the P0 -----------------------------------------------------------------


def test_case_a_body_to_case_b_client_is_a_mismatch() -> None:
    _closed(M_A, CLIENT_A)
    _closed(M_B, CLIENT_B)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A}, please find the deposition summary attached.",
        recipients={CLIENT_B},
    )
    assert v.is_mismatch and v.should_withhold
    assert CLIENT_B in v.reason


def test_control_correct_pairing_passes() -> None:
    # The half that makes the test above mean something.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A}, please find the deposition summary attached.",
        recipients={CLIENT_A},
    )
    assert v.status == "ok"
    assert not v.should_withhold


# ---- unresolved must never masquerade as non-membership ---------------------


def test_open_party_set_yields_unresolved_not_mismatch() -> None:
    # Contact-keyed capture proves CLIENT_A is on M_A; it says nothing about
    # whether CLIENT_B is. Absence from an open set is not evidence.
    _open(M_A, CLIENT_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_B})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_unresolved_does_not_withhold() -> None:
    # A control that blocks correct work gets removed rather than fixed.
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_open_set_still_passes_a_proven_party() -> None:
    _open(M_A, CLIENT_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A})
    assert v.status == "ok"


def test_closed_set_upgrade_is_monotonic() -> None:
    # An open read after a closed one must not reopen the set.
    _closed(M_A, CLIENT_A)
    _open(M_A, CLIENT_B)
    assert matter_binding.membership_for(SID).is_closed(M_A)


# ---- scope ------------------------------------------------------------------


def test_body_citing_no_matter_is_not_applicable() -> None:
    v = matter_gate.evaluate(session_id=SID, body="Thanks, will do.", recipients={CLIENT_B})
    assert v.status == "not_applicable"


def test_exempt_recipient_class_is_skipped() -> None:
    # Firm staff and records vendors are not expected to be parties; the roster
    # that says so is the client's, not ours.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"matter {M_A}",
        recipients={"records@vendor.example"},
        recipient_is_exempt=True,
    )
    assert v.status == "not_applicable"


def test_matter_never_read_is_unresolved_not_mismatch() -> None:
    # A number nobody read is not evidence about anybody.
    v = matter_gate.evaluate(session_id=SID, body="matter 2026-PI-999", recipients={CLIENT_A})
    assert v.status == "unresolved"


def test_mixed_recipients_one_offender_is_a_mismatch() -> None:
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {M_A}", recipients={CLIENT_A, CLIENT_B})
    assert v.is_mismatch


# ---- the citation form real correspondence uses (ss#2167 second pass) --------
#
# Every test above cites the matter by its connector UUID. No letter does that.
# The gate resolved a cited token by looking it up in the membership map, which
# is keyed by id, so a body citing "2026-PI-101" produced *unresolved* even
# against a closed party set — the control was blind to its own subject matter
# and the whole suite passed. These pin the number->id join.


def test_number_cited_body_to_the_wrong_client_is_a_mismatch() -> None:
    _closed(M_A, CLIENT_A)
    matter_binding.membership_for(SID).add_alias(NUM_A, M_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Re: matter {NUM_A}. Please find the deposition summary attached.",
        recipients={CLIENT_B},
    )
    assert v.is_mismatch and v.should_withhold
    # The refusal must name the matter the way the human reading it saw it.
    assert NUM_A in v.reason
    assert NUM_A in v.matters


def test_control_number_cited_correct_pairing_passes() -> None:
    # The half that makes the test above mean something.
    _closed(M_A, CLIENT_A)
    matter_binding.membership_for(SID).add_alias(NUM_A, M_A)
    v = matter_gate.evaluate(session_id=SID, body=f"Re: matter {NUM_A}.", recipients={CLIENT_A})
    assert v.status == "ok"
    assert not v.should_withhold


def test_number_citation_is_case_insensitive() -> None:
    # This test previously asserted the OPPOSITE — that a lower-case citation was
    # not extracted at all — and documented that as a known gap (ss#2262). The
    # gap is closed: the extractor is IGNORECASE, which is safe here because
    # _resolve_cited keeps only tokens that resolve to a matter this session
    # read, so a false positive cannot manufacture a verdict. Extraction detail
    # lives in tests/test_matter_number_extraction.py.
    _closed(M_A, CLIENT_A)
    matter_binding.membership_for(SID).add_alias(NUM_A, M_A)
    v = matter_gate.evaluate(session_id=SID, body=f"re: {NUM_A.lower()}", recipients={CLIENT_B})
    assert v.is_mismatch and v.should_withhold
    # And a matching-case citation with odd surrounding whitespace still binds.
    v2 = matter_gate.evaluate(session_id=SID, body=f"re:  {NUM_A} ", recipients={CLIENT_B})
    assert v2.is_mismatch


def test_id_citation_is_case_insensitive() -> None:
    # The ID path's version of the test above, and it was the asymmetry in
    # ss#2290: _MATTER_ID_RE carries IGNORECASE and returns the match VERBATIM,
    # but resolve() compared that token against the raw connector ids with `in`.
    # A body citing an uppercased GUID therefore resolved to nothing and the
    # verdict came back *unresolved* against a CLOSED party set. Every test in
    # this file seeded a lower-case UUID body, so nothing covered it.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A.upper()}, see attached.",
        recipients={CLIENT_B},
    )
    assert v.is_mismatch and v.should_withhold
    # The refusal names the matter the way the human reading it saw it.
    assert M_A.upper() in v.reason


def test_control_uppercase_id_correct_pairing_passes() -> None:
    # The half that makes the test above mean something: case-folding must not
    # turn every uppercased citation into a withhold.
    _closed(M_A, CLIENT_A)
    v = matter_gate.evaluate(
        session_id=SID,
        body=f"Regarding matter {M_A.upper()}, see attached.",
        recipients={CLIENT_A},
    )
    assert v.status == "ok"
    assert not v.should_withhold


def test_mixed_case_id_resolves_to_the_stored_id() -> None:
    # Folding is a lookup convenience, not a rewrite: the canonical id the rest
    # of the module keys on (parties, is_closed, audit reasons) is unchanged.
    _closed(M_A, CLIENT_A)
    m = matter_binding.membership_for(SID)
    assert m.resolve("AaAaAaAa-1111-2222-3333-444444444444") == M_A
    assert m.known_matters() == {M_A}


def test_case_variant_ids_are_ambiguous_and_withdrawn() -> None:
    # Two distinct ids differing only by case: neither may claim the folded key.
    # Same doctrine the alias path already follows — withdraw, never guess —
    # because guessing here would call a legitimate client an outsider.
    upper = M_A.upper()
    _closed(M_A, CLIENT_A)
    _closed(upper, CLIENT_B)
    m = matter_binding.membership_for(SID)
    # Exact matches still resolve; only the folded lookup is withdrawn.
    assert m.resolve(M_A) == M_A
    assert m.resolve(upper) == upper
    assert m.resolve("AaAaAaAa-1111-2222-3333-444444444444") == ""


def test_number_path_still_normalizes_to_upper() -> None:
    # The number path was already protected by _norm_matter; folding the id path
    # must not disturb it in either direction.
    _closed(M_A, CLIENT_A)
    m = matter_binding.membership_for(SID)
    m.add_alias(NUM_A.lower(), M_A)
    assert m.resolve(NUM_A) == M_A
    assert m.resolve(NUM_A.lower()) == M_A


def test_an_ambiguous_number_is_withdrawn_not_guessed() -> None:
    # Two matters claiming one number: neither binding may be used. Keeping
    # either would let the gate call a legitimate client an outsider on a
    # collision, which is the one verdict this module must never produce.
    _closed(M_A, CLIENT_A)
    _closed(M_B, CLIENT_B)
    m = matter_binding.membership_for(SID)
    m.add_alias(NUM_A, M_A)
    m.add_alias(NUM_A, M_B)
    assert m.resolve(NUM_A) == ""
    v = matter_gate.evaluate(session_id=SID, body=f"matter {NUM_A}", recipients={CLIENT_B})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_a_re_added_ambiguous_number_stays_withdrawn() -> None:
    # Without the blacklist the loser's next read would simply re-add it.
    m = matter_binding.membership_for(SID)
    m.add_alias(NUM_A, M_A)
    m.add_alias(NUM_A, M_B)
    m.add_alias(NUM_A, M_A)
    assert m.resolve(NUM_A) == ""


def test_number_alias_on_an_open_set_is_still_unresolved() -> None:
    # The alias answers "which matter is this", never "is the set closed".
    _open(M_A, CLIENT_A)
    matter_binding.membership_for(SID).add_alias(NUM_A, M_A)
    v = matter_gate.evaluate(session_id=SID, body=f"matter {NUM_A}", recipients={CLIENT_B})
    assert v.status == "unresolved"
    assert not v.should_withhold


def test_capture_learns_the_number_from_a_get_matter_payload() -> None:
    # The connector puts `number` in the same dict as `parties` (server.py:363).
    matter_binding.record_from_read(
        SID,
        {
            "id": M_A,
            "number": NUM_A,
            "parties": [{"contact_id": "c1", "email": CLIENT_A, "side": "client"}],
            "parties_complete": True,
        },
    )
    v = matter_gate.evaluate(session_id=SID, body=f"Re: matter {NUM_A}", recipients={CLIENT_B})
    assert v.is_mismatch and v.should_withhold


def test_capture_learns_the_number_from_a_contact_filtered_listing() -> None:
    # The reply lane's shape: list_matters fires on 34 of 86 reply turns against
    # get_matter's 8, so this is where the join usually comes from there.
    matter_binding.record_from_read(SID, {"id": "c9", "person": {"email": CLIENT_A}})
    matter_binding.record_from_read(
        SID,
        {"matters_for_contact": "c9", "value": [{"id": M_A, "number": NUM_A}]},
    )
    assert matter_binding.membership_for(SID).resolve(NUM_A) == M_A


# ---- posture ----------------------------------------------------------------


def test_mode_is_fail_closed_on_a_typo(monkeypatch) -> None:
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "repot")
    assert matter_gate.mode() == "block"
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "off")
    assert matter_gate.mode() == "block"
    monkeypatch.setenv("SMD_MATTER_GATE_MODE", "report")
    assert matter_gate.mode() == "report"


def test_evaluation_never_raises() -> None:
    v = matter_gate.evaluate(session_id=SID, body=None, recipients={CLIENT_A})  # type: ignore[arg-type]
    assert v.status in {"not_applicable", "unresolved"}


# ---- capture ----------------------------------------------------------------


def test_capture_reads_a_closed_party_list_from_get_matter() -> None:
    matter_binding.record_from_read(
        SID,
        {
            "id": M_A,
            "parties": [{"contact_id": "c1", "email": CLIENT_A, "side": "client"}],
            "parties_complete": True,
        },
    )
    assert matter_binding.membership_for(SID).is_closed(M_A)
    assert CLIENT_A in matter_binding.membership_for(SID).parties(M_A)


def test_capture_binds_contact_to_matters_across_two_reads() -> None:
    # The reply lane's shape: the contact is read first, the contact-filtered
    # matter listing second, as separate tool calls.
    matter_binding.record_from_read(SID, {"id": "c9", "person": {"email": CLIENT_A}})
    matter_binding.record_from_read(
        SID, {"matters_for_contact": "c9", "value": [{"id": M_A}, {"id": M_B}]}
    )
    m = matter_binding.membership_for(SID)
    assert m.matters_for(CLIENT_A) == {M_A, M_B}
    # Open by nature — it can prove membership, never non-membership.
    assert not m.is_closed(M_A)


def test_incomplete_party_list_is_captured_but_not_closed() -> None:
    matter_binding.record_from_read(
        SID,
        {"id": M_A, "parties": [{"email": CLIENT_A}], "parties_complete": False},
    )
    m = matter_binding.membership_for(SID)
    assert CLIENT_A in m.parties(M_A)
    assert not m.is_closed(M_A)
