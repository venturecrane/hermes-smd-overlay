"""Tests for ``shared.person_prefs`` — per-person identity + manifest reader.

Law 12 framing: each check names the input the broken behavior would have
waved through — a domain accepted as a person, two addresses merging into one
slug, a forged/absent manifest read as installed preferences.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shared.person_prefs import (
    PREFS_MANIFEST_NAME,
    entry_for_sender,
    load_person_entries,
    normalize_person_address,
    person_slug,
)

# ---------------------------------------------------------------------------
# Address validation — a person, never a domain
# ---------------------------------------------------------------------------


def test_normalize_accepts_a_person_and_normalizes_case_and_space():
    assert normalize_person_address("  Dana@ExampleFirm.EXAMPLE ") == "dana@examplefirm.example"


@pytest.mark.parametrize(
    "bad",
    [
        "@firm.com",  # a domain grant is not a person
        "chris@firm",  # undotted domain
        "chris@.com",
        "chris@firm.",
        "chris",  # no @
        "a@b@c.com",  # two @
        "",
        None,
        42,
    ],
)
def test_normalize_refuses_non_person_shapes(bad):
    assert normalize_person_address(bad) is None


# ---------------------------------------------------------------------------
# Slug derivation — deterministic, path-safe, collision-free
# ---------------------------------------------------------------------------


def test_slug_is_deterministic_and_path_safe():
    slug = person_slug("Dana@ExampleFirm.example")
    assert slug == person_slug("dana@examplefirm.example")
    assert all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in slug)
    assert "/" not in slug and ".." not in slug


def test_slug_readable_prefix_names_the_person():
    assert person_slug("chris@firm.com").startswith("chris-firm-com-")


def test_addresses_that_sanitize_identically_get_distinct_slugs():
    """``a.b@x.com`` and ``a-b@x.com`` both sanitize to ``a-b-x-com``. Without
    the hash suffix they would MERGE two people's preferences — the correctness
    failure the suffix exists to prevent."""
    assert person_slug("a.b@x.com") != person_slug("a-b@x.com")


def test_slug_refuses_what_the_validator_refuses():
    with pytest.raises(ValueError):
        person_slug("@firm.com")


# ---------------------------------------------------------------------------
# Manifest reader — root-owned enforcement surface, empty on any doubt
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path, entries):
    (tmp_path / PREFS_MANIFEST_NAME).write_text(
        json.dumps({"schema_version": 1, "preferences": entries})
    )


def test_load_reads_wellformed_entries_and_skips_malformed_ones(tmp_path):
    slug = person_slug("sarah@firm.com")
    _write_manifest(
        tmp_path,
        {
            slug: {
                "person": "sarah@firm.com",
                "rel_path": f"preferences/{slug}.json",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "bytes": 1,
            },
            "broken": {"person": 42},
        },
    )
    entries = load_person_entries(tmp_path)
    assert set(entries) == {slug}
    assert entries[slug].person == "sarah@firm.com"


def test_load_is_empty_on_missing_or_malformed_manifest(tmp_path):
    assert load_person_entries(tmp_path) == {}
    (tmp_path / PREFS_MANIFEST_NAME).write_text("{not json")
    assert load_person_entries(tmp_path) == {}


def test_entry_for_sender_matches_on_the_normalized_address(tmp_path):
    slug = person_slug("sarah@firm.com")
    _write_manifest(
        tmp_path,
        {
            slug: {
                "person": "sarah@firm.com",
                "rel_path": f"preferences/{slug}.json",
                "sha256": "a" * 64,
            }
        },
    )
    entry = entry_for_sender("  Sarah@Firm.COM ", tmp_path)
    assert entry is not None and entry.slug == slug
    assert entry_for_sender("chris@firm.com", tmp_path) is None
    assert entry_for_sender("", tmp_path) is None
    assert entry_for_sender(None, tmp_path) is None
