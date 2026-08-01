"""Per-session identifier provenance register (A1 runtime register)."""

from __future__ import annotations

import json

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


# ---------------------------------------------------------------------------
# Associations (2026-08-01)
#
# A transcript, not a hypothetical. After the matter-number join shipped
# (ss-console #2115) the Operator's delivered mail still mispaired a matter with
# a date, and every atom in that line verified.
# ---------------------------------------------------------------------------

_TASKS_BLOB = json.dumps(
    {
        "value": [
            {
                "id": "e1",
                "subject": "Deposition of Plaintiff Maria Alvarez",
                "startTime": "2026-08-06T10:00:00Z",
                "matterNumber": "2026-PI-101",
                "matterCaption": "Alvarez v. Draper",
            },
            {
                "id": "t1",
                "subject": "GAP: Exhibit list missing from matter",
                "dueDate": "2026-07-15",
                "matterNumber": "2026-PI-105",
                "matterCaption": "Okafor v. Grand Valley Market",
            },
        ]
    }
)


def test_the_live_mispairing_is_caught_end_to_end():
    """The 2026-08-01T19:08:57Z line, from a real read blob through the register."""
    provenance.record_read("sess-pair", _TASKS_BLOB)
    reg = provenance.register_for("sess-pair")
    body = "- matter 2026-PI-105, deposition of plaintiff Alvarez, August 6, 2026 (due in 5 days)"
    unverified = check(body, reg).unverified
    assert any(h.canonical == "2026PI105|2026-08-06" for h in unverified), unverified


def test_correctly_paired_lines_are_silent():
    """The control, and the one that decides whether this can ever leave
    report-only: a gate that flags correct lines is worse than no gate."""
    provenance.record_read("sess-pair-ok", _TASKS_BLOB)
    reg = provenance.register_for("sess-pair-ok")
    body = (
        "- matter 2026-PI-101, deposition of plaintiff Alvarez, August 6, 2026\n"
        "- matter 2026-PI-105, trial binder exhibit list missing, authored due 2026-07-15"
    )
    assert not check(body, reg).has_unverified, check(body, reg).annotations()


def test_a_listing_does_not_seed_the_cross_product():
    """Two records in one blob must not license each other's pairings."""
    provenance.record_read("sess-cross", _TASKS_BLOB)
    reg = provenance.register_for("sess-cross")
    swapped = "- matter 2026-PI-101, exhibit list, authored due 2026-07-15"
    assert [h for h in check(swapped, reg).unverified if h.kind.value == "pair"]


def test_records_without_a_matter_number_seed_no_pairs():
    """Enrichment can fail to attach matterNumber; that must produce no
    association rather than a guessed one."""
    provenance.record_read(
        "sess-nonum",
        json.dumps({"value": [{"id": "t9", "dueDate": "2026-07-15", "subject": "orphan"}]}),
    )
    assert not provenance.register_for("sess-nonum").has_pairs


def test_non_json_read_still_records_atoms():
    """Prose reads keep working exactly as before — associations are additive."""
    provenance.record_read("sess-prose", "Filing deadline 6/8/26 on this matter.")
    reg = provenance.register_for("sess-prose")
    assert not reg.has_pairs
    assert not check("The deadline is June 8, 2026.", reg).has_unverified


def test_seeding_is_bounded():
    many = json.dumps(
        {
            "value": [
                {"matterNumber": f"2026-PI-{i:03d}", "dueDate": "2026-07-15"} for i in range(500)
            ]
        }
    )
    provenance.record_read("sess-many", many)
    assert provenance.register_for("sess-many").has_pairs


def test_malformed_json_is_best_effort():
    provenance.record_read("sess-bad", '{"value": [ this is not json')
    provenance.record_read("sess-bad2", "[]")
    provenance.record_read("sess-bad3", "null")
