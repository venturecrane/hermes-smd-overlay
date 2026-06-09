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

# CONTRACT TEST: shared/identifier_filter.py is a vendored copy of
# ss-console/operator/safety-substrate/identifier_filter.py. These behavioral
# cases are the same as ss-console's; passing here asserts the vendored copy is
# in parity (not a byte hash — code, per shared/inbound.py rationale).
from shared.identifier_filter import (
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
# Boot self-check
# ---------------------------------------------------------------------------


def test_run_self_check_passes() -> None:
    ok, msg = run()
    assert ok, msg
