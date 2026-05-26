"""Tests for ``bootstrap.translate``.

Covers:

* Translation materializes one ``config.yaml`` + ``SOUL.md`` per persona
  under ``$HERMES_HOME/profiles/<slug>/``.
* The generated ``config.yaml`` embeds the canonical Honcho block from
  ADR 0016.
* Translation is idempotent — re-running with the same inputs leaves
  file bytes unchanged (same content, no churn).
* Skill pin resolution refuses when an enabled skill's pin disagrees
  with the on-disk content hash.
* ``pending`` skill pins are tolerated and replaced with the resolved
  pin in the generated config.
* Validation failures are surfaced as :class:`TranslateError` before
  any disk write.
"""

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from bootstrap.translate import (
    TranslateError,
    translate_customer_yaml,
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
        skills:
          - name: inbox-triage
            version: pending
            trust_ceiling: draft_for_review
            enabled: true

    connectors:
      Email:
        adapter: gmail
        backend: composio:gmail
        enabled: true

    scope:
      email_folders_visible: [Inbox]
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


def _seed_repo(tmp_path: Path, customer_yaml_body: str = VALID_YAML) -> tuple[Path, Path, Path]:
    """Create a minimal workspace: customer.yaml + skills dir + hermes home."""
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(customer_yaml_body)
    skills_dir = tmp_path / "skills"
    (skills_dir / "inbox-triage").mkdir(parents=True)
    (skills_dir / "inbox-triage" / "SKILL.md").write_text("# inbox-triage skill\n")
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    return customer_yaml, skills_dir, hermes_home


# ---------------------------------------------------------------------------
# Happy-path translation
# ---------------------------------------------------------------------------


def test_translate_materializes_profile_directory(tmp_path):
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    slugs = translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert slugs == ["marcus"]
    profile_dir = hermes_home / "profiles" / "marcus"
    assert (profile_dir / "config.yaml").exists()
    assert (profile_dir / "SOUL.md").exists()


def test_translate_writes_persona_identity_into_soul_md(tmp_path):
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "Marcus" in soul
    assert "AI Associate" in soul
    assert "Acme Corp" in soul
    # Tone bullets render
    assert "- plainspoken" in soul
    assert "- concise" in soul


def test_translate_embeds_tuned_honcho_block(tmp_path):
    """The generated config.yaml MUST embed the ADR 0016 Honcho config."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    honcho = config.get("honcho")
    assert honcho is not None
    assert honcho["recallMode"] == "hybrid"
    assert honcho["dialecticCadence"] == "3-5"
    assert honcho["dialecticDepth"] == 1
    assert honcho["user_observe_me"] is True
    assert honcho["user_observe_others"] is False
    assert honcho["ai_observe_me"] is False
    assert honcho["ai_observe_others"] is False
    assert honcho["writeFrequency"] == "session"


def test_translate_carries_customer_identity_into_config(tmp_path):
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert config["customer_id"] == "acme"
    assert config["customer_name"] == "Acme Corp"
    assert config["vertical"] == "law-firm"
    assert config["profile_slug"] == "marcus"
    assert config["persona"]["name"] == "Marcus"
    assert config["persona"]["tone"] == ["plainspoken", "concise"]


# ---------------------------------------------------------------------------
# ADR 0016 — MEMORY.md / USER.md tombstones + local_memory_files block
# ---------------------------------------------------------------------------


def test_translate_writes_memory_md_tombstone(tmp_path):
    """Each profile dir must contain a tombstoned MEMORY.md so Hermes
    does not auto-populate from default template at profile boot."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    memory_md = hermes_home / "profiles" / "marcus" / "MEMORY.md"
    assert memory_md.exists()
    body = memory_md.read_text()
    # Tombstone must contain the ADR 0016 rationale comment marker.
    assert "ADR 0016" in body
    assert "Honcho is the memory provider" in body
    # No actual memory content — purely a comment.
    assert body.strip().startswith("<!--")
    assert body.strip().endswith("-->")


def test_translate_writes_user_md_tombstone(tmp_path):
    """Each profile dir must contain a tombstoned USER.md."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    user_md = hermes_home / "profiles" / "marcus" / "USER.md"
    assert user_md.exists()
    body = user_md.read_text()
    assert "ADR 0016" in body
    assert "Honcho is the memory provider" in body
    assert body.strip().startswith("<!--")
    assert body.strip().endswith("-->")


def test_translate_embeds_local_memory_files_block(tmp_path):
    """config.yaml MUST declare MEMORY.md and USER.md disabled with the
    ADR 0016 rationale, so operators inspecting the profile see intent
    even without opening the tombstone files."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    block = config.get("local_memory_files")
    assert block is not None, "config.yaml missing local_memory_files block"
    assert block["memory_md_enabled"] is False
    assert block["user_md_enabled"] is False
    assert block["provider"] == "honcho"
    assert "ADR 0016" in block["rationale"]


def test_translate_tombstones_are_idempotent(tmp_path):
    """Re-running translate must NOT rewrite unchanged tombstone files
    (matches the idempotency contract for config.yaml and SOUL.md)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    memory_md = hermes_home / "profiles" / "marcus" / "MEMORY.md"
    user_md = hermes_home / "profiles" / "marcus" / "USER.md"
    mtime_memory = memory_md.stat().st_mtime_ns
    mtime_user = user_md.stat().st_mtime_ns

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert memory_md.stat().st_mtime_ns == mtime_memory
    assert user_md.stat().st_mtime_ns == mtime_user


def test_translate_writes_tombstones_for_every_persona(tmp_path):
    """Multi-persona customers get tombstones in every profile dir."""
    # Append a second persona, same pattern as test_translate_handles_multiple_personas.
    body = VALID_YAML.replace(
        "        enabled: true\n",
        "        enabled: true\n"
        "  - slug: junie\n"
        "    status: active\n"
        "    name: Junie\n"
        "    title: AI Associate\n"
        "    tone:\n"
        "      - cheerful\n"
        "    skills: []\n",
    )
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    for slug in ("marcus", "junie"):
        assert (hermes_home / "profiles" / slug / "MEMORY.md").exists()
        assert (hermes_home / "profiles" / slug / "USER.md").exists()
        config = yaml.safe_load((hermes_home / "profiles" / slug / "config.yaml").read_text())
        assert config["local_memory_files"]["memory_md_enabled"] is False
        assert config["local_memory_files"]["user_md_enabled"] is False


def test_translate_resolves_pending_skill_pin_to_actual_hash(tmp_path):
    """A `version: pending` skill entry is replaced with the resolved pin."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert len(config["skills"]) == 1
    pin = config["skills"][0]["version"]
    assert pin != "pending"
    assert len(pin) == 6  # 6-char content hash


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_translate_is_idempotent(tmp_path):
    """Re-running with the same inputs leaves file bytes unchanged."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config_path = hermes_home / "profiles" / "marcus" / "config.yaml"
    soul_path = hermes_home / "profiles" / "marcus" / "SOUL.md"
    config_bytes_first = config_path.read_bytes()
    soul_bytes_first = soul_path.read_bytes()

    # Second run, same inputs.
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert config_path.read_bytes() == config_bytes_first
    assert soul_path.read_bytes() == soul_bytes_first


def test_translate_rewrites_when_input_changes(tmp_path):
    """Changing customer.yaml causes the next run to update the on-disk files."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul_path = hermes_home / "profiles" / "marcus" / "SOUL.md"
    first = soul_path.read_text()

    # Change persona tone — non-structural to the validator but visible in SOUL.md.
    # Indentation matches the dedented VALID_YAML (4-space tone:, 6-space list).
    new_body = VALID_YAML.replace(
        "    tone:\n      - plainspoken\n      - concise\n",
        "    tone:\n      - warm-but-professional\n",
    )
    customer_yaml.write_text(new_body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    second = soul_path.read_text()
    assert first != second
    assert "warm-but-professional" in second


# ---------------------------------------------------------------------------
# Multi-persona materialization
# ---------------------------------------------------------------------------


def test_translate_handles_multiple_personas(tmp_path):
    # Append a second persona under the existing personas: list. The VALID_YAML
    # personas block is dedented to 2-space list indent, so the new entry uses
    # the same 2-space dash and 4-space body.
    body = VALID_YAML.replace(
        "        enabled: true\n",
        "        enabled: true\n"
        "  - slug: junie\n"
        "    status: active\n"
        "    name: Junie\n"
        "    title: AI Associate\n"
        "    tone:\n"
        "      - cheerful\n"
        "    skills: []\n",
    )
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=body)
    slugs = translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert sorted(slugs) == ["junie", "marcus"]
    assert (hermes_home / "profiles" / "marcus" / "config.yaml").exists()
    assert (hermes_home / "profiles" / "junie" / "config.yaml").exists()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_translate_raises_on_validation_failure(tmp_path):
    bad = VALID_YAML.replace("vertical: law-firm", "vertical: bogus")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=bad)
    with pytest.raises(TranslateError, match="validation"):
        translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
        )
    assert not (hermes_home / "profiles").exists()


def test_translate_raises_on_missing_skill_directory(tmp_path):
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    # Wipe the seeded skill — pin resolution must refuse.
    import shutil

    shutil.rmtree(skills_dir / "inbox-triage")
    with pytest.raises(TranslateError, match="not found"):
        translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
        )


def test_translate_raises_on_pin_mismatch(tmp_path):
    # Pin the skill to a deliberate wrong value (6 chars, hex shape).
    pinned_bad = VALID_YAML.replace("version: pending", "version: deadbe")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=pinned_bad)
    with pytest.raises(TranslateError, match="pinned version"):
        translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
        )


def test_translate_skips_disabled_skills(tmp_path):
    """Disabled skills do not contribute to pin resolution or the config."""
    body = VALID_YAML.replace("enabled: true", "enabled: false")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=body)
    # Wipe the on-disk skill so any attempt to hash it would fail.
    import shutil

    shutil.rmtree(skills_dir / "inbox-triage")
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert config["skills"] == []


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_translate_customer_yaml_is_callable():
    """The ``translate_customer_yaml`` function must exist and be callable."""
    assert callable(translate_customer_yaml)
