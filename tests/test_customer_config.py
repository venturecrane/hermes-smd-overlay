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


# ---------------------------------------------------------------------------
# ADR 0075 — typed outbound roster (scope.outbound_roster)
# ---------------------------------------------------------------------------


def _with_scope_extra(extra: str) -> str:
    """Inject extra scope keys after domain_blocks in VALID_YAML."""
    return VALID_YAML.replace("  domain_blocks: []\n", "  domain_blocks: []\n" + extra)


_OUTBOUND_ROSTER_SCOPE = (
    "  inbound_allow_from:\n"
    "    - '@ashtonandprice.com'\n"
    "  outbound_roster:\n"
    "    - address: jane@gmail.com\n"
    "      class: client\n"
    "      note: PI client on gmail\n"
    "    - address: RECORDS@radiology.com\n"
    "      class: records_vendor\n"
)


def test_outbound_roster_normalizes_to_tuples(tmp_path):
    cfg = CustomerConfig.from_volume(
        str(_write(tmp_path, _with_scope_extra(_OUTBOUND_ROSTER_SCOPE)))
    )
    # Lowercased + typed; order preserved.
    assert cfg.outbound_roster == [
        ("jane@gmail.com", "client"),
        ("records@radiology.com", "records_vendor"),
    ]


def test_outbound_roster_drops_malformed_entries_fail_closed(tmp_path):
    scope = (
        "  outbound_roster:\n"
        "    - address: ok@client.com\n"
        "      class: client\n"
        "    - address: x@y.com\n"
        "      class: bogus\n"  # bad class → dropped
        "    - not-a-mapping\n"  # non-dict → dropped
        "    - class: client\n"  # missing address → dropped
    )
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, _with_scope_extra(scope))))
    assert cfg.outbound_roster == [("ok@client.com", "client")]


def test_outbound_roster_absent_is_empty(tmp_path):
    cfg = CustomerConfig.from_volume(str(_write(tmp_path, VALID_YAML)))
    assert cfg.outbound_roster == []


def test_outbound_roster_non_list_raises(tmp_path):
    cfg = CustomerConfig.from_volume(
        str(_write(tmp_path, _with_scope_extra("  outbound_roster: not-a-list\n")))
    )
    with pytest.raises(CustomerConfigError):
        _ = cfg.outbound_roster


# ---- validate_customer_yaml: outbound_roster accept/reject ----------------


def test_validate_accepts_valid_outbound_roster(tmp_path):
    path = _write(tmp_path, _with_scope_extra(_OUTBOUND_ROSTER_SCOPE))
    assert validate_customer_yaml(path) == []


def test_validate_rejects_bad_outbound_class(tmp_path):
    scope = "  outbound_roster:\n    - address: a@b.com\n      class: opposing_counsel\n"
    errors = validate_customer_yaml(_write(tmp_path, _with_scope_extra(scope)))
    assert any("outbound_roster" in e and "class" in e for e in errors)


def test_validate_accepts_exact_public_domain_client_rejects_domain_grant(tmp_path):
    # EXACT gmail address is a valid client (PI clients are consumers on gmail).
    ok_scope = "  outbound_roster:\n    - address: jane@gmail.com\n      class: client\n"
    assert validate_customer_yaml(_write(tmp_path, _with_scope_extra(ok_scope))) == []
    # A whole-@gmail.com grant is meaningless (shared by millions) → rejected.
    bad_scope = "  outbound_roster:\n    - address: '@gmail.com'\n      class: client\n"
    errors = validate_customer_yaml(_write(tmp_path, _with_scope_extra(bad_scope)))
    assert any("public-mail" in e for e in errors)


def test_validate_rejects_cross_class_collision(tmp_path):
    scope = (
        "  outbound_roster:\n"
        "    - address: x@firm-vendor.com\n"
        "      class: client\n"
        "    - address: x@firm-vendor.com\n"
        "      class: records_vendor\n"
    )
    errors = validate_customer_yaml(_write(tmp_path, _with_scope_extra(scope)))
    assert any("more than one outbound roster class" in e for e in errors)


def test_validate_accepts_reply_authorized_address_with_a_typed_class(tmp_path):
    """ss#2263 — this used to be REJECTED, and the rejection was the defect.

    "a recipient cannot be both internal and a typed outbound class" read
    ``inbound_allow_from`` as a statement of class. It is not one: it says who the
    Operator may autonomously REPLY to. Forbidding the overlap meant the only way
    to make a firm's own client reply-able was to leave them classified as staff —
    exempt from the content floor and the matter-identity gate — and it made the
    gate's reply-lane branch unreachable in every authorable config (ss#2271).
    """
    scope = (
        "  inbound_allow_from:\n"
        "    - client@example.com\n"
        "  outbound_roster:\n"
        "    - address: client@example.com\n"
        "      class: client\n"
    )
    assert validate_customer_yaml(_write(tmp_path, _with_scope_extra(scope))) == []


def test_validate_accepts_firm_staff_class(tmp_path):
    """``firm_staff`` is the authored form of "is firm staff" — the fact that used
    to be inferred from the reply list."""
    scope = "  outbound_roster:\n    - address: paralegal@firm.example\n      class: firm_staff\n"
    assert validate_customer_yaml(_write(tmp_path, _with_scope_extra(scope))) == []


def test_reply_authorized_client_classifies_client_not_internal(tmp_path):
    """The validator's acceptance and the classifier's verdict are asserted
    together: accepting the config would be pointless if the runtime still read
    the overlap as staff, and that pairing is the whole of ss#2263."""
    from shared.recipient_classifier import RecipientClass, classify_recipients_typed

    scope = (
        "  inbound_allow_from:\n"
        "    - client@example.com\n"
        "    - '@firm.example'\n"
        "  outbound_roster:\n"
        "    - address: client@example.com\n"
        "      class: client\n"
    )
    path = _write(tmp_path, _with_scope_extra(scope))
    assert validate_customer_yaml(path) == []
    cfg = CustomerConfig.from_volume(str(path))
    # The reply-authorized client is a CLIENT — floored and matter-gated.
    assert (
        classify_recipients_typed(["client@example.com"], cfg.inbound_roster, cfg.outbound_roster)
        is RecipientClass.CLIENT
    )
    # A colleague the typed roster says nothing about still resolves INTERNAL via
    # the reply list — the back-compat path every pre-split seat depends on.
    assert (
        classify_recipients_typed(
            ["paralegal@firm.example"], cfg.inbound_roster, cfg.outbound_roster
        )
        is RecipientClass.INTERNAL
    )


def test_validate_rejects_malformed_outbound_address(tmp_path):
    scope = "  outbound_roster:\n    - address: not-an-email\n      class: client\n"
    errors = validate_customer_yaml(_write(tmp_path, _with_scope_extra(scope)))
    assert any("outbound_roster" in e and "address" in e for e in errors)


# ---- validate_customer_yaml: new exposure keys + confirm ------------------


def test_validate_accepts_new_send_exposure_keys(tmp_path):
    bad = VALID_YAML.replace(
        "        external_send: draft_for_review\n",
        "        external_send: draft_for_review\n"
        "        external_send_client: autonomous\n"
        "        external_send_vendor: confirm\n",
    )
    assert "external_send_client: autonomous" in bad  # guard: replace landed
    assert validate_customer_yaml(_write(tmp_path, bad)) == []


def test_validate_rejects_confirm_on_non_send_class(tmp_path):
    bad = VALID_YAML.replace("internal_write: autonomous", "internal_write: confirm")
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("confirm" in e and "send classes" in e for e in errors)


def test_validate_accepts_confirm_on_commitment_in_exposure(tmp_path):
    """The shape that crash-looped pilot-smokeball at boot on 2026-08-21:
    `commitment: confirm` is what #303 gave a confirm branch for, and the
    validator refused it. A seat authoring the admin-confirmed act must boot."""
    good = VALID_YAML.replace(
        "internal_write: autonomous", "internal_write: autonomous\n        commitment: confirm"
    )
    assert "commitment: confirm" in good  # guard: replace landed
    assert validate_customer_yaml(_write(tmp_path, good)) == []


def test_validate_still_rejects_confirm_on_commitment_in_exposure_ceiling(tmp_path):
    """`confirm` on commitment is exposure-only: the entitlement dial's ceiling
    map is derived from send tiers and never carries a commitment entry."""
    bad = VALID_YAML.replace(
        "      exposure:\n", "      exposure_ceiling:\n        commitment: confirm\n      exposure:\n"
    )
    assert "exposure_ceiling:" in bad  # guard: replace landed
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("exposure_ceiling.commitment" in e and "confirm" in e for e in errors)


# ---- validate_customer_yaml: exposure_ceiling (ss#2003 Q7) ----------------


def _with_ceiling(block: str) -> str:
    """VALID_YAML with an exposure_ceiling block appended under entitlements."""
    return VALID_YAML.replace(
        "        external_send: draft_for_review\n",
        "        external_send: draft_for_review\n" + block,
    )


def test_validate_accepts_exposure_ceiling(tmp_path):
    good = _with_ceiling("      exposure_ceiling:\n        external_send: autonomous\n")
    assert "exposure_ceiling" in good  # guard: replace landed
    assert validate_customer_yaml(_write(tmp_path, good)) == []


def test_validate_rejects_bad_exposure_ceiling_key(tmp_path):
    bad = _with_ceiling("      exposure_ceiling:\n        read: autonomous\n")
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("read is always allowed" in e and "exposure_ceiling" in e for e in errors)


def test_validate_rejects_exposure_above_own_ceiling(tmp_path):
    # authored external_send: draft_for_review, ceiling refused → incoherent
    bad = _with_ceiling("      exposure_ceiling:\n        external_send: refused\n")
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("exceeds its own" in e for e in errors)


# ---------------------------------------------------------------------------
# Cross-module parity: the accepted-exposure vocabulary must agree between the
# validator, the translator filter, and the runtime ActionClass send members.
# A one-sided edit silently drops a key (validate rejects it, or translate omits
# it from the profile) — this pins the agreement.
# ---------------------------------------------------------------------------


def test_exposure_action_class_parity_validate_translate_enum():
    from bootstrap.translate import _AUTHORED_EXPOSURE_ACTION_CLASSES
    from bootstrap.validate import AUTHORED_EXPOSURE_ACTION_CLASSES, SEND_ACTION_CLASSES
    from shared.action_classes import ActionClass

    validate_keys = set(AUTHORED_EXPOSURE_ACTION_CLASSES)
    translate_keys = set(_AUTHORED_EXPOSURE_ACTION_CLASSES)
    # validator filter == translator filter
    assert validate_keys == translate_keys
    # both == every ActionClass value EXCEPT the never-authored read + the
    # fail-closed terminal refused.
    enum_authored = {ac.value for ac in ActionClass} - {"read", "refused"}
    assert validate_keys == enum_authored
    # the send classes the confirm ceiling + typed roster resolve to
    assert SEND_ACTION_CLASSES == {
        ActionClass.EXTERNAL_SEND.value,
        ActionClass.EXTERNAL_SEND_INTERNAL.value,
        ActionClass.EXTERNAL_SEND_CLIENT.value,
        ActionClass.EXTERNAL_SEND_VENDOR.value,
    }
