"""Per-session identifier provenance register (A1 runtime register)."""

from __future__ import annotations

from shared import provenance
from shared.identifier_filter import check


def setup_function() -> None:
    provenance._reset_for_tests()


def test_record_read_makes_a_draft_identifier_verify():
    provenance.record_read("sess-1", "Matter note: filing deadline 6/8/26, A# 123-456-789.")
    reg = provenance.register_for("sess-1")
    # A draft that reuses those identifiers (reformatted) verifies — nothing unverified.
    result = check("Your hearing is June 8, 2026 (A123456789).", reg)
    assert not result.has_unverified


def test_unknown_session_yields_empty_register():
    reg = provenance.register_for("never-seen")
    assert not bool(reg)
    # Everything flags against an empty register (report-only over-reports — safe).
    result = check("A999999999 on 2026-12-01.", reg)
    assert result.register_was_empty is True
    assert result.has_unverified


def test_record_read_is_session_scoped():
    provenance.record_read("sess-a", "A111111111 on file.")
    # sess-b never read it → unverified there.
    result_b = check("ref A111111111", provenance.register_for("sess-b"))
    assert result_b.has_unverified
    # sess-a read it → verified there.
    result_a = check("ref A111111111", provenance.register_for("sess-a"))
    assert not result_a.has_unverified


def test_drop_forgets_a_session():
    provenance.record_read("sess-x", "A222222222 read.")
    assert bool(provenance.register_for("sess-x"))
    provenance.drop("sess-x")
    assert not bool(provenance.register_for("sess-x"))


def test_record_read_is_best_effort_on_bad_input():
    # None/empty/non-str must not raise.
    provenance.record_read("", "ignored")
    provenance.record_read("s", "")
    provenance.record_read("s", None)  # type: ignore[arg-type]
    assert not bool(provenance.register_for("s"))


def test_eviction_bounds_the_register_count():
    for i in range(provenance._MAX_SESSIONS + 10):
        provenance.record_read(f"sess-{i}", "A333333333 read.")
    assert len(provenance._registers) <= provenance._MAX_SESSIONS
    # The oldest sessions were evicted; the newest survives.
    assert bool(provenance.register_for(f"sess-{provenance._MAX_SESSIONS + 9}"))
