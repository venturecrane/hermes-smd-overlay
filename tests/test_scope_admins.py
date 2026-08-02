"""``scope.admins`` — the Operator-admin allow list (ss ADR 0085 §2).

Three surfaces, one authored list: the runtime accessor (fail-closed to ``[]``),
the bootstrap validator (persons only, no domain grants), and the live-apply
allow-list (an admins edit must apply on the next message, and must not reject
a bundled live diff — the 2026-07-14 grain bug shape).

Every test here can FAIL on the old code (Law 12): before this PR the accessor
did not exist, the validator accepted any shape under ``scope``, and a
``scope.admins`` diff rejected the whole live apply.
"""

from __future__ import annotations

from textwrap import dedent

from bootstrap.validate import validate_customer_yaml
from config_applier.safety import live_writable, non_live_writable_changes
from shared.customer_config import CustomerConfig
from tests.test_customer_config import VALID_YAML


def _cfg(admins) -> CustomerConfig:
    return CustomerConfig({"customer_id": "acme", "scope": {"admins": admins}})


# ---------------------------------------------------------------------------
# Accessor — normalization + fail-closed
# ---------------------------------------------------------------------------


def test_admins_normalizes_and_dedupes():
    cfg = _cfg(["Chris@Firm.com", "  christa@firm.com ", "chris@firm.com"])
    assert cfg.admins == ["chris@firm.com", "christa@firm.com"]


def test_admins_drops_domain_grants_and_garbage():
    """An admin is a PERSON. A ``@domain`` entry that somehow survived authoring
    must not make every colleague an establishment authority."""
    cfg = _cfg(
        ["@firm.com", "chris@firm.com", 7, "", "not-an-address", "a@b@c.com", "@", "x@nodot"]
    )
    assert cfg.admins == ["chris@firm.com"]


def test_admins_fails_closed_to_empty_on_malformed_shapes():
    assert CustomerConfig({"customer_id": "acme"}).admins == []
    assert CustomerConfig({"customer_id": "acme", "scope": "oops"}).admins == []
    assert _cfg("not-a-list").admins == []
    assert _cfg(None).admins == []


def test_sender_is_admin_exact_match_only():
    """No domain widening: being on the firm's domain makes you a colleague,
    not an admin (unlike sender_on_roster, deliberately)."""
    cfg = _cfg(["chris@firm.com"])
    assert cfg.sender_is_admin("Chris@Firm.com") is True
    assert cfg.sender_is_admin("sarah@firm.com") is False
    assert cfg.sender_is_admin("") is False
    assert cfg.sender_is_admin(None) is False


def test_sender_is_admin_empty_list_matches_no_one():
    assert _cfg([]).sender_is_admin("chris@firm.com") is False


# ---------------------------------------------------------------------------
# Validator — persons only
# ---------------------------------------------------------------------------


def _with_admins(block: str) -> str:
    return VALID_YAML + dedent(block)


def _validate(tmp_path, body: str) -> list[str]:
    path = tmp_path / "customer.yaml"
    path.write_text(body)
    return validate_customer_yaml(path)


def test_validator_accepts_exact_person_addresses(tmp_path):
    body = VALID_YAML.replace(
        "scope:\n",
        "scope:\n  admins:\n    - chris@firm.com\n    - christa@firm.com\n",
    )
    assert _validate(tmp_path, body) == []


def test_validator_rejects_domain_grants(tmp_path):
    body = VALID_YAML.replace("scope:\n", "scope:\n  admins:\n    - '@firm.com'\n")
    errors = _validate(tmp_path, body)
    assert any("domain grant" in e and "scope.admins[0]" in e for e in errors)


def test_validator_rejects_malformed_and_duplicate_entries(tmp_path):
    body = VALID_YAML.replace(
        "scope:\n",
        "scope:\n  admins:\n    - not-an-address\n    - chris@firm.com\n    - CHRIS@firm.com\n",
    )
    errors = _validate(tmp_path, body)
    assert any("scope.admins[0]" in e and "exact person address" in e for e in errors)
    assert any("scope.admins[2]" in e and "duplicate" in e for e in errors)


def test_validator_rejects_non_list(tmp_path):
    body = VALID_YAML.replace("scope:\n", "scope:\n  admins: chris@firm.com\n")
    errors = _validate(tmp_path, body)
    assert any("scope.admins must be a list" in e for e in errors)


def test_validator_accepts_absent_admins(tmp_path):
    """Unauthored is a legitimate state — no admins means no firm-level
    establishment, ever, which is fail-closed and valid."""
    assert _validate(tmp_path, VALID_YAML) == []


# ---------------------------------------------------------------------------
# Live-writability — the applier grain
# ---------------------------------------------------------------------------


def test_scope_admins_is_live_writable():
    assert live_writable("scope.admins") is True
    assert live_writable("scope.admins.0") is True


def test_admins_only_diff_applies_live():
    """Before this PR, an admins edit rejected the WHOLE diff — authoring an
    admin would have required a re-provision, and an admins edit bundled with
    any live change would have silently blocked both (the 2026-07-14 grain
    bug, in a new costume)."""
    old = {"scope": {"admins": []}}
    new = {"scope": {"admins": ["chris@firm.com"]}}
    assert non_live_writable_changes(old, new) == []
