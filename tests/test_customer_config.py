"""Tests for ``shared.customer_config`` and ``bootstrap.validate``.

Covers:

* :class:`CustomerConfig` loads a valid file from a path and exposes
  typed accessors (slug, personas, scope, connectors, voice_library).
* The loader rejects missing files, empty files, and YAML that does
  not parse as a mapping.
* Schema validation catches the required-field set, vertical
  enumeration, persona shape, connector backend prefixes, and the
  memory namespace isolation invariants.
"""

from pathlib import Path
from textwrap import dedent

import pytest

from bootstrap.validate import validate_customer_yaml
from shared.customer_config import (
    CustomerConfig,
    CustomerConfigError,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


VALID_YAML = dedent(
    """\
    schema_version: 1
    customer_id: acme
    customer_name: Acme Corp
    vertical: law-firm
    fly_region: iad
    model: claude-opus-4-7
    hermes_ref: v2026.5.16-smd.0

    personas:
      - slug: marcus
        status: active
        name: Marcus
        title: AI Associate
        tone:
          - plainspoken
          - concise
        entitlements:
          exposure:
            internal_write: autonomous
            external_send: draft_for_review
        skills:
          - name: inbox-triage
            version: pending
            initiation:
              manual: true
              scheduled: false
              webhook: false
            enabled: true

    connectors:
      Email:
        adapter: gmail
        backend: mcp:gmail
        enabled: true

    scope:
      email_folders_visible:
        - Inbox
      email_folders_blind: []
      email_keyword_blocks: []
      domain_blocks: []

    voice_library:
      samples_path: 'r2://vaults/acme/voice/samples/'

    memory:
      d1_namespace: acme
      r2_vault_path: 'vaults/acme/'
      vectorize_index: 'hermes-acme-vault'
    """
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "customer.yaml"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# CustomerConfig — happy path
# ---------------------------------------------------------------------------


def test_customer_config_loads_valid_yaml(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    assert cfg.slug == "acme"
    assert cfg.customer_name == "Acme Corp"
    assert cfg.vertical == "law-firm"


def test_customer_config_personas(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    personas = cfg.personas
    assert len(personas) == 1
    assert personas[0]["slug"] == "marcus"
    assert personas[0]["name"] == "Marcus"


def test_customer_config_scope(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    scope = cfg.scope
    assert scope["email_folders_visible"] == ["Inbox"]
    assert scope["email_folders_blind"] == []


def test_customer_config_connectors(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    connectors = cfg.connectors
    assert "Email" in connectors
    assert connectors["Email"]["backend"] == "mcp:gmail"


def test_customer_config_voice_library(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    voice = cfg.voice_library
    assert voice["samples_path"] == "r2://vaults/acme/voice/samples/"


def test_customer_config_raw(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    cfg = CustomerConfig.from_volume(str(path))
    raw = cfg.raw
    assert raw["customer_id"] == "acme"
    assert raw["memory"]["vectorize_index"] == "hermes-acme-vault"


# ---------------------------------------------------------------------------
# CustomerConfig — failure modes
# ---------------------------------------------------------------------------


def test_from_volume_missing_file_raises(tmp_path):
    with pytest.raises(CustomerConfigError, match="not found"):
        CustomerConfig.from_volume(str(tmp_path / "missing.yaml"))


def test_from_volume_empty_file_raises(tmp_path):
    path = _write(tmp_path, "")
    with pytest.raises(CustomerConfigError, match="empty"):
        CustomerConfig.from_volume(str(path))


def test_from_volume_invalid_yaml_raises(tmp_path):
    path = _write(tmp_path, "::: not valid yaml :::")
    with pytest.raises(CustomerConfigError, match="not valid YAML"):
        CustomerConfig.from_volume(str(path))


def test_from_volume_non_mapping_raises(tmp_path):
    path = _write(tmp_path, "- not\n- a\n- mapping\n")
    with pytest.raises(CustomerConfigError, match="must be a mapping"):
        CustomerConfig.from_volume(str(path))


def test_slug_accessor_raises_when_missing():
    cfg = CustomerConfig({"customer_name": "Anonymous"})
    with pytest.raises(CustomerConfigError, match="customer_id"):
        _ = cfg.slug


# ---------------------------------------------------------------------------
# validate_customer_yaml — happy path
# ---------------------------------------------------------------------------


def test_validate_returns_empty_for_valid_file(tmp_path):
    path = _write(tmp_path, VALID_YAML)
    errors = validate_customer_yaml(path)
    assert errors == []


# ---------------------------------------------------------------------------
# validate_customer_yaml — failure modes
# ---------------------------------------------------------------------------


def test_validate_reports_missing_file(tmp_path):
    errors = validate_customer_yaml(tmp_path / "missing.yaml")
    assert any("not found" in e for e in errors)


def test_validate_reports_missing_top_level_fields(tmp_path):
    path = _write(
        tmp_path,
        "customer_id: acme\npersonas:\n  - slug: m\n    name: M\n    status: active\n",
    )
    errors = validate_customer_yaml(path)
    assert any("customer_name" in e for e in errors)
    assert any("vertical" in e for e in errors)
    assert any("fly_region" in e for e in errors)
    assert any("hermes_ref" in e for e in errors)
    assert any("model" in e for e in errors)


def test_validate_rejects_unknown_vertical(tmp_path):
    bad = VALID_YAML.replace("vertical: law-firm", "vertical: snake-charming")
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("vertical must be one of" in e for e in errors)


def test_validate_requires_at_least_one_persona(tmp_path):
    # Author a minimal valid-shape doc with an explicit empty personas list.
    minimal = dedent(
        """\
        schema_version: 1
        customer_id: acme
        customer_name: Acme Corp
        vertical: law-firm
        fly_region: iad
        model: claude-opus-4-7
        hermes_ref: v2026.5.16-smd.0
        personas: []
        memory:
          d1_namespace: acme
          r2_vault_path: 'vaults/acme/'
          vectorize_index: 'hermes-acme-vault'
        """
    )
    path = _write(tmp_path, minimal)
    errors = validate_customer_yaml(path)
    assert any("at least one persona" in e for e in errors)


def test_validate_rejects_duplicate_persona_slugs(tmp_path):
    # Author a doc with two personas sharing one slug, top-level shape valid.
    bad = dedent(
        """\
        schema_version: 1
        customer_id: acme
        customer_name: Acme Corp
        vertical: law-firm
        fly_region: iad
        model: claude-opus-4-7
        hermes_ref: v2026.5.16-smd.0
        personas:
          - slug: marcus
            status: active
            name: Marcus
          - slug: marcus
            status: active
            name: Marcus (dup)
        memory:
          d1_namespace: acme
          r2_vault_path: 'vaults/acme/'
          vectorize_index: 'hermes-acme-vault'
        """
    )
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("duplicate slug" in e for e in errors)


def test_validate_rejects_invalid_exposure_ceiling(tmp_path):
    bad = VALID_YAML.replace("internal_write: autonomous", "internal_write: yolo")
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("must be one of" in e and "exposure" in e for e in errors)


def test_validate_rejects_legacy_skill_trust_ceiling(tmp_path):
    """ADR 0056: a retired skill trust_ceiling is rejected with no shim."""
    bad = VALID_YAML.replace(
        "        version: pending\n",
        "        version: pending\n        trust_ceiling: draft_for_review\n",
    )
    assert "trust_ceiling: draft_for_review" in bad  # guard: the replace landed
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("trust_ceiling" in e and "retired" in e for e in errors)


def test_validate_rejects_invalid_connector_backend(tmp_path):
    bad = VALID_YAML.replace("backend: mcp:gmail", "backend: http://example.com")
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("backend" in e and "must start with" in e for e in errors)


def test_validate_rejects_memory_namespace_mismatch(tmp_path):
    bad = VALID_YAML.replace("d1_namespace: acme", "d1_namespace: other")
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("d1_namespace" in e and "must match" in e for e in errors)


def test_validate_rejects_memory_r2_path_mismatch(tmp_path):
    bad = VALID_YAML.replace("r2_vault_path: 'vaults/acme/'", "r2_vault_path: 'vaults/other/'")
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("r2_vault_path" in e for e in errors)


def test_validate_rejects_memory_vectorize_index_mismatch(tmp_path):
    bad = VALID_YAML.replace(
        "vectorize_index: 'hermes-acme-vault'",
        "vectorize_index: 'hermes-other-vault'",
    )
    path = _write(tmp_path, bad)
    errors = validate_customer_yaml(path)
    assert any("vectorize_index" in e for e in errors)


# ---------------------------------------------------------------------------
# Live-read config block accessors (ADR 0044)
# ---------------------------------------------------------------------------


_BLOCKS_YAML = VALID_YAML + dedent(
    """\

    escalation:
      red_flag_recipients:
        - team@acme.test
      failure_recipients:
        - ops@acme.test

    google_auth:
      mode: dwd
      subject: agent@acme.test
      scopes:
        - https://www.googleapis.com/auth/gmail.modify

    telegram:
      enabled: true
      allow_from:
        - '7367659986'
      require_mention: false
    """
)


# Relationship — authored behavioral lane (ADR 0048)
# ---------------------------------------------------------------------------


_RELATIONSHIP_YAML = VALID_YAML + dedent(
    """\

    relationship:
      people:
        - id: scott-durgan
          name: Scott Durgan
          role: Principal
          prefers:
            - Lead with the material change
          avoid:
            - Inventing estimates
          extra_field: should-be-dropped
        - id: no-name
        - name: no-id person
    """
)


def test_escalation_accessor_reads_block(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, _BLOCKS_YAML)))
    assert cfg.escalation == {
        "red_flag_recipients": ["team@acme.test"],
        "failure_recipients": ["ops@acme.test"],
    }


def test_memory_accessor_reads_block(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, VALID_YAML)))
    assert cfg.memory["d1_namespace"] == "acme"
    assert cfg.memory["r2_vault_path"] == "vaults/acme/"


def test_google_auth_accessor_reads_block(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, _BLOCKS_YAML)))
    assert cfg.google_auth["mode"] == "dwd"
    assert cfg.google_auth["subject"] == "agent@acme.test"


def test_telegram_accessor_reads_block(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, _BLOCKS_YAML)))
    assert cfg.telegram["enabled"] is True
    assert cfg.telegram["allow_from"] == ["7367659986"]


def test_live_read_blocks_absent_default_to_empty(tmp_path):
    # VALID_YAML carries memory but no escalation/google_auth/telegram —
    # absent blocks must read {} (fail-soft), never raise.
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, VALID_YAML)))
    assert cfg.escalation == {}
    assert cfg.google_auth == {}
    assert cfg.telegram == {}


def test_live_read_block_non_mapping_raises(tmp_path):
    bad = VALID_YAML + "\nescalation: not-a-mapping\n"
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, bad)))
    with pytest.raises(CustomerConfigError):
        _ = cfg.escalation


def test_relationship_people_normalizes_and_drops_unknown_keys(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, _RELATIONSHIP_YAML)))
    people = cfg.relationship_people()
    # Entries missing id or name are skipped (defensive parse).
    assert [p["id"] for p in people] == ["scott-durgan"]
    # Closed-set normalization: only id/name/role/prefers/avoid survive.
    assert people[0] == {
        "id": "scott-durgan",
        "name": "Scott Durgan",
        "role": "Principal",
        "prefers": ["Lead with the material change"],
        "avoid": ["Inventing estimates"],
    }
    assert "extra_field" not in people[0]


def test_relationship_absent_block_is_empty(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, VALID_YAML)))
    assert cfg.relationship == {}
    assert cfg.relationship_people() == []


def test_relationship_non_mapping_raises(tmp_path):
    bad = VALID_YAML + "\nrelationship:\n  - not\n  - a\n  - map\n"
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, bad)))
    with pytest.raises(CustomerConfigError):
        _ = cfg.relationship
