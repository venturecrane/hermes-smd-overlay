"""Validator rule: on a seat with people:, the roster is exactly the people.

Mirror of ss-console validator rule 8 (#3289). A seat whose
``scope.inbound_allow_from`` drifts from the couple's emails refuses to boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bootstrap.validate import validate_customer_yaml
from tests.test_bootstrap_translate import VALID_YAML

A = "ana@example.com"
B = "ben@example.com"


def _couple(roster: list) -> dict:
    doc = yaml.safe_load(VALID_YAML)
    doc["people"] = [
        {"id": "p1", "name": "Ana", "telegram_id": "1001", "email": A},
        {"id": "p2", "name": "Ben", "telegram_id": "2002", "email": B},
    ]
    doc["spaces"] = {"shared_telegram_chat": "-1001234567890"}
    doc["scope"]["inbound_allow_from"] = roster
    return doc


def _errors(tmp_path: Path, doc: dict) -> list[str]:
    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump(doc))
    return [e for e in validate_customer_yaml(path) if "inbound_allow_from" in e or "people[" in e]


def test_roster_equal_to_the_people_passes(tmp_path):
    assert _errors(tmp_path, _couple([A, B.upper()])) == []


@pytest.mark.parametrize(
    "roster, fragment",
    [
        ([A, B, "scott@smd.services"], "not a person: scott@smd.services"),
        ([A, B, "@smd.services"], "domain or wildcard"),
        ([A, B, "*"], "domain or wildcard"),
        ([A], f"missing: {B}"),
        ([], "missing"),
    ],
)
def test_a_roster_that_drifts_from_the_people_refuses_boot(tmp_path, roster, fragment):
    """FALSIFIER: drop the extra/missing/wildcard checks in _validate_people_roster."""
    errors = _errors(tmp_path, _couple(roster))
    assert any(fragment in e for e in errors), errors


def test_a_missing_roster_or_person_email_refuses(tmp_path):
    doc = _couple([A, B])
    del doc["scope"]["inbound_allow_from"]
    assert any("required on a seat with people" in e for e in _errors(tmp_path, doc))
    doc = _couple([A])
    doc["people"][1]["email"] = ""
    assert any("people[1].email" in e for e in _errors(tmp_path, doc))


def test_a_seat_without_people_is_unchanged(tmp_path):
    """REGRESSION: any roster, SMD address and domain grants included, is not
    this rule's business when people: is absent."""
    doc = yaml.safe_load(VALID_YAML)
    doc["scope"]["inbound_allow_from"] = [A, "scott@smd.services", "@example.com"]
    assert _errors(tmp_path, doc) == []
