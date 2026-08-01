"""Identifier-integrity filter — provenance discipline for asserted identifiers.

Covers the two halves that matter:
  - **False-positive avoidance** (the design-review correction): a *composed but
    correct* identifier — "6/8/26" written as "June 8, 2026", an A-number
    reformatted, "Robert J. Smith" addressed as "Robert Smith" — must VERIFY
    against the register, not flag. A gate that flags polished drafts trains the
    reviewer to ignore it.
  - **Fabrication detection**: an identifier the agent never read (a wrong
    A-number, an invented date/case number) is surfaced.

Plus the posture guarantees: never raises, never blocks (returns hits + mode),
audit metadata redacts values, money is out of scope (content floor's domain).
"""

from __future__ import annotations

import pytest

# CONTRACT TEST: shared/identifier_filter.py is a vendored copy of
# ss-console/operator/safety-substrate/identifier_filter.py. These behavioral
# cases are the same as ss-console's; passing here asserts the vendored copy is
# in parity (not a byte hash — code, per shared/inbound.py rationale).
from shared.identifier_filter import (
    _CASE_RE,
    IdKind,
    Mode,
    ProvenanceRegister,
    check,
    refusal_message,
    run,
)


def _reg(*read_blobs: str) -> ProvenanceRegister:
    reg = ProvenanceRegister()
    for blob in read_blobs:
        reg.add_read_text(blob)
    return reg


# ---------------------------------------------------------------------------
# False-positive avoidance — composed-but-correct identifiers VERIFY
# ---------------------------------------------------------------------------


def test_date_reformatted_to_long_form_verifies() -> None:
    """The crux FP case: agent read "6/8/26", wrote "June 8, 2026". Same date,
    canonicalized — must NOT flag."""
    reg = _reg("Hearing scheduled 6/8/26.")
    result = check("Your hearing is on June 8, 2026.", reg)
    assert not result.has_unverified


def test_iso_and_slash_dates_canonicalize_equal() -> None:
    reg = _reg("Filed 2026-06-08.")
    assert not check("We filed it 6/8/2026.", reg).has_unverified


def test_a_number_punctuation_variants_verify() -> None:
    """ "A 123 456 789" read, "A123456789" written — punctuation-insensitive."""
    reg = _reg("Alien number A 123 456 789 on file.")
    assert not check("Your case references A123456789.", reg).has_unverified


def test_name_with_middle_initial_verifies_without_it() -> None:
    """Source "Robert J. Smith", draft addresses "Robert Smith" — last+initial
    match, no false flag. Names are seeded from structured metadata."""
    reg = ProvenanceRegister()
    reg.add_name("Robert J. Smith")
    assert not check("Dear Robert Smith,\n\nThanks for your patience.", reg).has_unverified


def test_clean_draft_with_all_verified_identifiers_passes() -> None:
    reg = _reg("A#123-456-789, consult 03/14/2026, matter 1:24-cv-01234.")
    reg.add_name("Maria Diaz")
    body = (
        "Dear Maria Diaz,\n\n"
        "Confirming your consult on March 14, 2026 for case 1:24-cv-01234 "
        "(A123456789).\n\nRegards,\nThe Firm"
    )
    assert not check(body, reg).has_unverified


# ---------------------------------------------------------------------------
# Fabrication detection — unread identifiers are flagged
# ---------------------------------------------------------------------------


def test_fabricated_a_number_is_flagged() -> None:
    reg = _reg("Client Maria Diaz, A123456789.")
    result = check("Your alien number is A999999999.", reg)
    assert result.has_unverified
    assert result.unverified[0].kind is IdKind.A_NUMBER


def test_unread_date_is_flagged() -> None:
    reg = _reg("Consult on 6/8/26.")
    result = check("Your deadline is December 1, 2026.", reg)
    assert result.has_unverified
    assert any(h.kind is IdKind.DATE for h in result.unverified)


def test_unread_case_number_is_flagged() -> None:
    reg = _reg("Matter opened.")
    result = check("Re: case 1:24-cv-09999.", reg)
    assert result.has_unverified
    assert any(h.kind is IdKind.CASE_NUMBER for h in result.unverified)


def test_empty_register_flags_but_records_emptiness() -> None:
    """An empty register (nothing read) flags every identifier — but records
    register_was_empty so the caller can distinguish it from 'all verified'.
    Still report/flag, never block."""
    reg = ProvenanceRegister()
    result = check("Your A-number is A123456789, hearing 6/8/2026.", reg)
    assert result.has_unverified
    assert result.register_was_empty is True


# ---------------------------------------------------------------------------
# Posture — never blocks, never raises, mode carried through
# ---------------------------------------------------------------------------


def test_default_mode_is_report() -> None:
    assert check("A123456789", ProvenanceRegister()).mode is Mode.REPORT


def test_flag_mode_is_carried_through() -> None:
    result = check("A123456789", ProvenanceRegister(), mode=Mode.FLAG)
    assert result.mode is Mode.FLAG


def test_check_is_total_on_empty_and_garbage() -> None:
    reg = ProvenanceRegister()
    for body in ("", "   ", "no identifiers here at all"):
        assert not check(body, reg).has_unverified


def test_nickname_is_not_silently_verified_documented_limitation() -> None:
    """ "Bob" is not normalized to "Robert" (v1 limitation). In REPORT mode this
    is a surfaced signal, not a block — which is the correct, honest behavior:
    we cannot verify "Bob" against "Robert Smith", so we say so."""
    reg = _reg("Client Robert Smith.")
    result = check("Dear Bob,\n\nUpdate attached.", reg)
    assert result.has_unverified
    assert result.unverified[0].kind is IdKind.NAME
    assert result.mode is Mode.REPORT  # surfaced, not blocked


# ---------------------------------------------------------------------------
# Scope + redaction
# ---------------------------------------------------------------------------


def test_money_is_not_an_identifier_kind() -> None:
    """A dollar amount is the content floor's domain (ADR 0031), not this
    filter's — it must not be flagged here even if unread."""
    reg = ProvenanceRegister()
    result = check("Please remit $5,000 by Friday.", reg)
    assert not result.has_unverified


def test_names_only_extracted_from_greeting_slot() -> None:
    """A capitalized name in mid-prose is NOT treated as an identifier (FP
    control) — only the greeting recipient slot is checked. The sign-off (the
    firm's own name) is deliberately not checked."""
    reg = ProvenanceRegister()
    # "Judge Wilson" in prose is not a greeting slot -> not extracted.
    result = check("We appeared before Judge Wilson yesterday.", reg)
    assert not any(h.kind is IdKind.NAME for h in result.unverified)


def test_signoff_name_is_not_checked() -> None:
    """The sender's sign-off name (the firm / attorney) is authored by the firm,
    not a recipient identifier — it must not be flagged."""
    reg = ProvenanceRegister()
    reg.add_name("Maria Diaz")
    body = "Dear Maria Diaz,\n\nUpdate attached.\n\nSincerely,\nJane Attorney"
    result = check(body, reg)
    assert not result.has_unverified


def test_audit_metadata_redacts_values() -> None:
    reg = ProvenanceRegister()
    result = check("Your A-number is A123456789 and SSN 123-45-6789.", reg)
    meta = result.audit_metadata()
    blob = repr(meta)
    assert "A123456789" not in blob
    assert "123-45-6789" not in blob
    assert meta["gate_tier"] == "tier3_identifier"
    assert meta["mode"] == "report"
    assert meta["unverified_counts"].get("a_number") == 1


def test_annotations_include_value_for_human_reviewer() -> None:
    """FLAG-mode annotations DO include the value — the firm's reviewer needs to
    see it to judge it. (Distinct from the audit row, which redacts.)"""
    reg = ProvenanceRegister()
    result = check("Your A-number is A999999999.", reg, mode=Mode.FLAG)
    notes = " ".join(result.annotations())
    assert "A999999999" in notes
    assert "A999999999" in refusal_message(result)


# ---------------------------------------------------------------------------
# Matter numbers (added 2026-07-31)
#
# Before this, _CASE_RE could not see a practice-management matter number at
# all. Every IDENTIFIER_UNVERIFIED row on the pilot seat carried only date
# shapes, which reads as "no identifier problems" when the truth was "this
# filter is blind to the identifiers this firm uses." Silence from a gate that
# cannot see a class of value means nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2026-PI-101", "2026-PI-107", "PI-2026-0001"])
def test_case_re_sees_matter_numbers(value: str) -> None:
    assert _CASE_RE.search(value), f"{value} must be visible to the identifier gate"


@pytest.mark.parametrize("value", ["1:24-cv-01234", "No. 24-12345"])
def test_case_re_still_sees_federal_dockets(value: str) -> None:
    """The matter alternation is additive; the original shapes must not regress."""
    assert _CASE_RE.search(value)


@pytest.mark.parametrize("value", ["2026-08-12", "2026-08-12T09:00:00Z"])
def test_case_re_does_not_claim_dates_are_case_numbers(value: str) -> None:
    assert not _CASE_RE.search(value)


# ---------------------------------------------------------------------------
# ISO datetimes (added 2026-08-01)
#
# `\b\d{4}-\d{2}-\d{2}\b` could not match the date inside an ISO *datetime*:
# between the final "2" and the "T" there is no word boundary. Smokeball events
# carry ISO datetimes, so a digest that correctly read a hearing and wrote its
# date was flagged unverified — a false positive at daily volume, and one that
# would have been measured as the model's fabrication rate.
# ---------------------------------------------------------------------------


def test_iso_datetime_read_verifies_a_correctly_written_date() -> None:
    """The regression this fix exists for: the agent reads an event whose start
    time is an ISO datetime and writes the date in prose. That is correct
    behaviour and must not be flagged."""
    reg = ProvenanceRegister()
    reg.add_read_text('{"subject": "Deposition", "start_time": "2026-08-06T10:00:00Z"}')
    result = check("The deposition is set for August 6, 2026.", reg)
    assert not result.has_unverified, result.annotations()


def test_iso_datetime_with_offset_also_verifies() -> None:
    reg = ProvenanceRegister()
    reg.add_read_text('{"start_time": "2026-07-25T14:00:00-07:00"}')
    assert not check("Response was due 2026-07-25.", reg).has_unverified


def test_unread_date_is_still_flagged_after_the_iso_fix() -> None:
    """Widening extraction must not widen *verification* — a date the agent
    never read stays flagged."""
    reg = ProvenanceRegister()
    reg.add_read_text('{"start_time": "2026-08-06T10:00:00Z"}')
    result = check("The hearing is October 13, 2026.", reg)
    assert result.has_unverified
    assert result.unverified[0].kind is IdKind.DATE


def test_iso_pattern_declines_a_longer_digit_run() -> None:
    """The ISO branch must not read "2026-08-12" out of "2026-08-12-99".

    Narrowly about the ISO branch: a separate `\\d{1,2}-\\d{1,2}-\\d{2,4}` pattern
    still reads "08-12-99" here as 1999-08-12, which is its own business. What
    this pins is that the ISO branch stops claiming a date the run does not
    carry — the old trailing \\b matched it.
    """
    reg = ProvenanceRegister()
    canonicals = {h.canonical for h in check("ref 2026-08-12-99 attached.", reg).unverified}
    assert "2026-08-12" not in canonicals


# ---------------------------------------------------------------------------
# Pair-keyed provenance (added 2026-08-01)
#
# These cases are a transcript, not a hypothetical. On 2026-08-01, after the
# matter-number join shipped (#2115), the Operator's delivered escalation mail
# still carried two wrong references out of seven. Atom-level provenance would
# have caught one of them and missed the other, because both of its values had
# been legitimately read — just never together.
# ---------------------------------------------------------------------------


def _seeded_from_the_2026_08_01_tenant() -> ProvenanceRegister:
    """The records the Operator actually read on the run that produced the
    mispairing: the Alvarez deposition event and the Okafor trial-binder tasks."""
    reg = ProvenanceRegister()
    reg.add_record("2026-PI-101", ["2026-08-06T10:00:00Z"])  # Alvarez deposition
    reg.add_record("2026-PI-105", ["2026-07-15"])  # Okafor exhibit list
    reg.add_record("PI-2026-0001", ["2026-06-30"])  # Johnson / Sutter records
    return reg


def test_the_live_mispairing_is_caught() -> None:
    """THE regression. Delivered 2026-08-01T19:08:57Z:

        "matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026"

    The deposition event carried matterNumber=2026-PI-101. Both 2026-PI-105 and
    2026-08-06 were read that session, so every atom verifies and the line passed
    clean. The pair never existed on any record.
    """
    reg = _seeded_from_the_2026_08_01_tenant()
    body = "- matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026 (due in 5 days)"

    atoms = [h for h in check(body, reg).unverified if h.kind is not IdKind.PAIR]
    assert not atoms, f"atom-level check should pass — that is the whole problem: {atoms}"

    pairs = [h for h in check(body, reg).unverified if h.kind is IdKind.PAIR]
    assert pairs, "the mispairing must be caught"
    assert pairs[0].canonical == "2026PI105|2026-08-06"


def test_the_correctly_paired_line_is_not_flagged() -> None:
    """From the same delivered mail, and the control for the test above: this row
    WAS right, and must stay quiet."""
    reg = _seeded_from_the_2026_08_01_tenant()
    body = "- matter 2026-PI-105, trial binder exhibit list missing (authored due 2026-07-15)"
    assert not check(body, reg).has_unverified


def test_the_mangled_matter_number_is_caught_as_an_atom() -> None:
    """The other live error: the record says PI-2026-0001, the mail said
    2026-PI-001 — a number belonging to no matter in the tenant. This one atom
    provenance does catch."""
    reg = _seeded_from_the_2026_08_01_tenant()
    result = check("- matter 2026-PI-001, Sutter Roseville records overdue", reg)
    assert any(h.kind is IdKind.CASE_NUMBER for h in result.unverified)


def test_pairs_are_line_scoped_not_document_scoped() -> None:
    """Two correct rows must not cross-verify each other's values."""
    reg = _seeded_from_the_2026_08_01_tenant()
    good = (
        "- matter 2026-PI-101, deposition 2026-08-06\n- matter 2026-PI-105, exhibit list 2026-07-15"
    )
    assert not check(good, reg).has_unverified

    swapped = (
        "- matter 2026-PI-101, exhibit list 2026-07-15\n- matter 2026-PI-105, deposition 2026-08-06"
    )
    assert [h for h in check(swapped, reg).unverified if h.kind is IdKind.PAIR]


def test_unseeded_register_reports_no_pairs_at_all() -> None:
    """A register with no associations cannot judge one. Reporting pairs here
    would flag every line of every deadline digest — marking everything is how a
    reader learns to ignore the mark."""
    reg = ProvenanceRegister()
    reg.add_read_text("matter 2026-PI-105 and a date 2026-08-06 appear in this read")
    assert not reg.has_pairs
    result = check("- matter 2026-PI-105, something on 2026-08-06", reg)
    assert not [h for h in result.unverified if h.kind is IdKind.PAIR]


def test_add_read_text_never_registers_the_cross_product() -> None:
    """A tool result is a collection of records. Pairing everything in the blob
    would verify precisely the mispairings this exists to catch."""
    reg = ProvenanceRegister()
    reg.add_read_text('[{"m":"2026-PI-101","d":"2026-08-06"},{"m":"2026-PI-105","d":"2026-07-15"}]')
    assert not reg.has_pairs


def test_add_record_canonicalizes_so_seeder_and_checker_agree() -> None:
    """The seeder passes raw values; a key shaped differently from the one check()
    looks up would silently verify nothing."""
    reg = ProvenanceRegister()
    reg.add_record("2026-PI-101", ["2026-08-06T10:00:00Z"])
    assert not check("- matter 2026-PI-101, deposition August 6, 2026", reg).has_unverified


def test_pair_audit_metadata_redacts_the_values() -> None:
    reg = _seeded_from_the_2026_08_01_tenant()
    body = "- matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026"
    blob = repr(check(body, reg).audit_metadata())
    assert "2026-PI-105" not in blob
    assert "August 6, 2026" not in blob


def test_pair_annotation_explains_the_distinction() -> None:
    """A reviewer must not read "unverified" as "fabricated" here — both values
    are real, the association is not."""
    reg = _seeded_from_the_2026_08_01_tenant()
    body = "- matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026"
    notes = " ".join(check(body, reg, mode=Mode.FLAG).annotations())
    assert "never together" in notes


# ---------------------------------------------------------------------------
# Boot self-check
# ---------------------------------------------------------------------------


def test_run_self_check_passes() -> None:
    ok, msg = run()
    assert ok, msg
