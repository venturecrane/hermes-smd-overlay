"""Tests for ``bootstrap.translate``.

Covers:

* Translation materializes one ``config.yaml`` + ``SOUL.md`` per persona
  under ``$HERMES_HOME/profiles/<slug>/``.
* The generated ``config.yaml`` carries NO memory-provider block and NO
  ``local_memory_files`` block, and translation writes NO MEMORY.md /
  USER.md tombstones — Phase 1 runs on Hermes' always-on flat-file core
  (ADR 0016, revised 2026-05-30; the prior Honcho-as-sole-provider
  disposition is reversed).
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


def test_translate_installs_enabled_skill_body_into_profile(tmp_path):
    # Regression: the profile config.yaml referenced the skill but the body
    # was never placed in the profile's own skills dir, so the persona booted
    # skill-less (Hermes discovers skills per-profile by directory presence).
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    installed = hermes_home / "profiles" / "marcus" / "skills" / "inbox-triage" / "SKILL.md"
    assert installed.exists(), "enabled persona skill body must be installed into the profile"
    assert installed.read_text() == "# inbox-triage skill\n"


def test_translate_does_not_install_disabled_skills(tmp_path):
    body = VALID_YAML.replace("enabled: true", "enabled: false")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    profile_skills = hermes_home / "profiles" / "marcus" / "skills"
    assert not (profile_skills / "inbox-triage").exists(), (
        "a disabled skill must not be installed into the profile"
    )


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


_USERS_BLOCK = dedent(
    """\

    users:
      - full_name: Scott Durgan
        email: scott@smd.services
        role: principal
      - full_name: Pat Lee
        email: pat@smd.services
        role: staff
    """
)


def test_translate_renders_authored_exposure_into_soul_md(tmp_path):
    """ADR 0035 / ss ADR 0073: the persona's AUTHORED exposure reaches SOUL.md so
    the agent acts AT its ceiling instead of defaulting below it (the live-proof
    gap: an autonomous chase was drafted because the agent could not see its
    posture). Enforcement remains the trust plugin's job regardless of text."""
    autonomous = VALID_YAML.replace("external_send: draft_for_review", "external_send: autonomous")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, autonomous)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "## Authored autonomy (set by the firm)" in soul
    # the autonomous line is actionable: send, don't draft
    assert "**external_send**: autonomous" in soul
    assert "do not leave a draft" in soul
    # the authored internal_write renders too; nothing else is invented
    assert "**internal_write**: autonomous" in soul
    assert "**destructive**" not in soul  # unauthored class never rendered


def test_translate_renders_draft_for_review_exposure_plainly(tmp_path):
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "**external_send**: draft_for_review" in soul
    assert "a person reviews and sends" in soul


def test_translate_no_exposure_yields_no_autonomy_section(tmp_path):
    """No authored exposure ⇒ no section, no fabricated posture (ADR 0035)."""
    no_exposure = VALID_YAML.replace(
        """    entitlements:
      exposure:
        internal_write: autonomous
        external_send: draft_for_review
""",
        "",
    )
    assert "entitlements" not in no_exposure  # the surgery actually removed the block
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, no_exposure)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "Authored autonomy" not in soul


def test_translate_materializes_principal_into_soul_md(tmp_path):
    """#1326: the principal from the authored users[] list reaches SOUL.md so
    the running agent has a statement of whom it works for."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, VALID_YAML + _USERS_BLOCK)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "You work for Scott Durgan (scott@smd.services)." in soul
    # Only the principal is named — staff users do not produce a "work for" line.
    assert "Pat Lee" not in soul


def test_translate_omits_principal_line_when_no_principal(tmp_path):
    """No principal entry ⇒ no fabricated fallback name, byte-identical SOUL.md."""
    staff_only = _USERS_BLOCK.replace("role: principal", "role: staff", 1)
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, VALID_YAML + staff_only)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "You work for" not in soul


def test_translate_omits_principal_line_when_users_absent(tmp_path):
    """No users[] block at all ⇒ no principal line, no fabrication."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, no users[]
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "You work for" not in soul


_RELATIONSHIP_BLOCK = dedent(
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
    """
)


def test_translate_renders_relationship_into_soul_and_config(tmp_path):
    """ADR 0048: the authored behavioral lane reaches both the agent (SOUL.md)
    and the materialized config.yaml."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, VALID_YAML + _RELATIONSHIP_BLOCK)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "## Working relationships" in soul
    assert "### Scott Durgan — Principal" in soul
    assert "- Lead with the material change" in soul
    assert "- Inventing estimates" in soul
    # The preferences-not-permissions guardrail is rendered (ADR 0048 §2c).
    assert "preferences, not permissions" in soul

    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert config["relationship"]["people"][0]["id"] == "scott-durgan"


def test_translate_omits_relationship_soul_section_when_absent(tmp_path):
    """No `relationship:` block ⇒ SOUL.md has no Working-relationships section
    (byte-identical contract) and config carries an empty block, never dropped."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, no block
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "Working relationships" not in soul
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert config["relationship"] == {}


def test_translate_emits_delegation_block_from_escalation_model(tmp_path):
    """ADR 0049: an authored `escalation_model` materializes Hermes' native
    `delegation` block, so any skill calling delegate_task runs the heavy
    reasoning on the escalation tier while the seat's main model stays light.
    Provider/key are intentionally omitted — Hermes inherits the parent's."""
    body = VALID_YAML.replace(
        "model: claude-opus-4-7\n",
        "model: claude-sonnet-4-6\nescalation_model: claude-opus-4-8\n",
    )
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert config["model"] == "claude-sonnet-4-6"
    assert config["delegation"] == {"model": "claude-opus-4-8"}


def test_translate_omits_delegation_when_no_escalation_model(tmp_path):
    """No `escalation_model` ⇒ no `delegation` block. Single-tier seats stay
    byte-identical; delegated work inherits the main model (ADR 0049)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, no escalation_model
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "delegation" not in config


def test_translate_emits_disabled_toolsets_fleet_default(tmp_path):
    """Tool-surface trim (2026-07-15): every profile config disables the
    toolsets no Operator seat uses — their schemas otherwise ride every API
    call as prompt-cache write (measured 40,457-token first call on
    pilot-smokeball, 70% tool schemas). A seat without google_auth also drops
    the overlay `workspace` toolset (18 dead workspace_* schemas)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, no google_auth
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    disabled = config["agent"]["disabled_toolsets"]
    for ts in ("browser", "session_search", "tts", "computer_use", "workspace"):
        assert ts in disabled
    # Load-bearing surfaces must never appear here: terminal is called from
    # SKILL.md prose (ar-chaser, retainer-hours-reconciler), web is fenced-safe
    # in client-verification-tracker, and skills/file/memory/todo are core.
    for ts in (
        "terminal",
        "web",
        "skills",
        "file",
        "memory",
        "todo",
        "code_execution",
        "delegation",
        "escalation",
        "jobs",
    ):
        assert ts not in disabled


def test_translate_keeps_workspace_toolset_when_google_auth(tmp_path):
    """A seat with customer-owned Google Workspace authority (google_auth,
    e.g. smd/Crane) keeps the overlay `workspace` toolset — those 18 tools are
    its live capability, not dead weight."""
    body = VALID_YAML + "\ngoogle_auth:\n  mode: dwd\n  subject: crane@example.com\n"
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    disabled = config["agent"]["disabled_toolsets"]
    assert "workspace" not in disabled
    assert "browser" in disabled  # fleet default still applies


def test_translate_renders_escalation_soul_when_escalation_model(tmp_path):
    """ADR 0049: a seat with an escalation_model gets the standing 'Allocating
    heavy work' instruction in SOUL.md. The general escalation behavior lives
    once in identity — not in any skill — so authored, agent-created, and
    one-off work all inherit it, and skills stay tier-unaware."""
    body = VALID_YAML.replace(
        "model: claude-opus-4-7\n",
        "model: claude-sonnet-4-6\nescalation_model: claude-opus-4-8\n",
    )
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "## Allocating heavy work" in soul
    assert "Escalate first, before reading" in soul
    # The instruction consumes the skill marker, so weighted skills escalate
    # deterministically rather than on judgment alone.
    assert "weight: heavy" in soul
    # Roster-agnostic: the escalation model is never named in SOUL.
    assert "claude-opus-4-8" not in soul


def test_translate_omits_escalation_soul_when_single_tier(tmp_path):
    """No escalation_model ⇒ no 'Allocating heavy work' section. Single-tier
    seats stay byte-identical; the same pack runs on every roster (ADR 0049)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, single-tier
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "Allocating heavy work" not in soul


def test_translate_emits_no_memory_provider_block(tmp_path):
    """Phase 1: config.yaml carries NO honcho / memory-provider block.

    The flat-file core is the substrate; Honcho is deferred to Phase 2.
    A stray provider block would re-introduce the fictional, never-booted
    Honcho wiring (ADR 0016, revised 2026-05-30)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "honcho" not in config
    assert "local_memory_files" not in config
    # The customer-owned memory isolation block is still carried through.
    assert config["memory"]["d1_namespace"] == "acme"


def test_translate_emits_new_send_exposure_keys(tmp_path):
    # ADR 0075: external_send_client / external_send_vendor survive translation into
    # the per-profile exposure block (else they are silently DROPPED — the sneakiest
    # failure).
    body = VALID_YAML.replace(
        "        external_send: draft_for_review\n",
        "        external_send: draft_for_review\n"
        "        external_send_client: autonomous\n"
        "        external_send_vendor: draft_for_review\n",
    )
    assert "external_send_client: autonomous" in body  # guard: replace landed
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    exposure = config["entitlements"]["exposure"]
    assert exposure["external_send_client"] == "autonomous"
    assert exposure["external_send_vendor"] == "draft_for_review"


def test_translate_renders_scalar_skill_settings(tmp_path):
    # ADR 0075: a skill's authored scalar settings render into the profile skill
    # block; nested (non-scalar) values are dropped.
    body = VALID_YAML.replace(
        "        enabled: true\n",
        "        enabled: true\n"
        "        settings:\n"
        "          chase_cadence_days: 3\n"
        "          max_attempts: 5\n"
        "          explain_by_default: true\n"
        "          nested_dropped:\n"
        "            a: 1\n",
    )
    assert "chase_cadence_days: 3" in body  # guard: replace landed
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    settings = config["skills"][0]["settings"]
    assert settings == {"chase_cadence_days": 3, "max_attempts": 5, "explain_by_default": True}
    assert "nested_dropped" not in settings


def test_translate_omits_settings_key_when_skill_has_none(tmp_path):
    # Inertness: a skill without settings emits no `settings` key (byte-identical).
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "settings" not in config["skills"][0]


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
# ADR 0016 (revised 2026-05-30) — flat-file core stays on; NO tombstones
# ---------------------------------------------------------------------------


def test_translate_does_not_tombstone_memory_md(tmp_path):
    """Phase 1: translate must NOT write a MEMORY.md tombstone.

    Hermes' flat-file core is the substrate; it auto-creates MEMORY.md at
    profile boot. A tombstone here would suppress that and leave the agent
    with no working memory (the reversed prior Honcho-only disposition)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert not (hermes_home / "profiles" / "marcus" / "MEMORY.md").exists()


def test_translate_does_not_tombstone_user_md(tmp_path):
    """Phase 1: translate must NOT write a USER.md tombstone."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert not (hermes_home / "profiles" / "marcus" / "USER.md").exists()


def test_translate_writes_no_tombstones_for_any_persona(tmp_path):
    """Multi-persona customers get NO tombstones in any profile dir."""
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
        assert not (hermes_home / "profiles" / slug / "MEMORY.md").exists()
        assert not (hermes_home / "profiles" / slug / "USER.md").exists()
        config = yaml.safe_load((hermes_home / "profiles" / slug / "config.yaml").read_text())
        assert "honcho" not in config
        assert "local_memory_files" not in config


# ---------------------------------------------------------------------------
# ADR 0021 Stream D — skill bundles
# ---------------------------------------------------------------------------


# customer.yaml with one persona that ships two bundles. Each entry
# mirrors the operator/bundles/ catalog shape (slug, description,
# skills, instruction).
YAML_WITH_BUNDLES = VALID_YAML.replace(
    "        enabled: true\n",
    "        enabled: true\n"
    "    bundles:\n"
    "      - slug: pi-intake\n"
    "        description: 'Intake triage + conflict screen'\n"
    "        skills:\n"
    "          - law-pi-intake-triage\n"
    "          - law-conflict-check\n"
    "        instruction: 'Shared context across both skills'\n"
    "      - slug: pi-matter-prep\n"
    "        description: 'Demand draft + settlement prep'\n"
    "        skills:\n"
    "          - law-pi-demand-letter-draft\n"
    "          - law-pi-settlement-prep\n",
)


def test_translate_writes_per_profile_bundle_yaml(tmp_path):
    """Each persona bundle gets one YAML file in skill-bundles/."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=YAML_WITH_BUNDLES
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    bundles_dir = hermes_home / "profiles" / "marcus" / "skill-bundles"
    assert bundles_dir.exists()
    assert (bundles_dir / "pi-intake.yaml").exists()
    assert (bundles_dir / "pi-matter-prep.yaml").exists()


def test_translate_bundle_yaml_carries_canonical_shape(tmp_path):
    """The bundle file matches the Hermes-native bundle shape: slug,
    description, skills, instruction (optional)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=YAML_WITH_BUNDLES
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )

    intake_yaml = hermes_home / "profiles" / "marcus" / "skill-bundles" / "pi-intake.yaml"
    body = yaml.safe_load(intake_yaml.read_text())
    assert body["slug"] == "pi-intake"
    assert body["description"] == "Intake triage + conflict screen"
    assert body["skills"] == ["law-pi-intake-triage", "law-conflict-check"]
    assert body["instruction"] == "Shared context across both skills"

    prep_yaml = hermes_home / "profiles" / "marcus" / "skill-bundles" / "pi-matter-prep.yaml"
    prep_body = yaml.safe_load(prep_yaml.read_text())
    assert prep_body["slug"] == "pi-matter-prep"
    # `instruction` was omitted in customer.yaml — must not appear on
    # disk as `instruction: null`.
    assert "instruction" not in prep_body


def test_translate_bundles_are_idempotent(tmp_path):
    """Re-running translate does not rewrite unchanged bundle files."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=YAML_WITH_BUNDLES
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    intake_yaml = hermes_home / "profiles" / "marcus" / "skill-bundles" / "pi-intake.yaml"
    mtime_before = intake_yaml.stat().st_mtime_ns

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert intake_yaml.stat().st_mtime_ns == mtime_before


def test_translate_removes_stale_bundles_on_update(tmp_path):
    """A bundle removed from customer.yaml is deleted from disk."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=YAML_WITH_BUNDLES
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    bundles_dir = hermes_home / "profiles" / "marcus" / "skill-bundles"
    assert (bundles_dir / "pi-intake.yaml").exists()
    assert (bundles_dir / "pi-matter-prep.yaml").exists()

    # Rewrite customer.yaml dropping pi-matter-prep.
    yaml_with_only_intake = YAML_WITH_BUNDLES.replace(
        "      - slug: pi-matter-prep\n"
        "        description: 'Demand draft + settlement prep'\n"
        "        skills:\n"
        "          - law-pi-demand-letter-draft\n"
        "          - law-pi-settlement-prep\n",
        "",
    )
    customer_yaml.write_text(yaml_with_only_intake)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert (bundles_dir / "pi-intake.yaml").exists()
    assert not (bundles_dir / "pi-matter-prep.yaml").exists()


def test_translate_removes_all_bundles_when_block_dropped(tmp_path):
    """If customer.yaml drops the `bundles` block entirely, all
    per-profile bundle files for that persona are deleted."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=YAML_WITH_BUNDLES
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    bundles_dir = hermes_home / "profiles" / "marcus" / "skill-bundles"
    assert (bundles_dir / "pi-intake.yaml").exists()

    # Reset to the original VALID_YAML, which has no bundles block.
    customer_yaml.write_text(VALID_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    assert not (bundles_dir / "pi-intake.yaml").exists()
    assert not (bundles_dir / "pi-matter-prep.yaml").exists()


def test_translate_persona_without_bundles_creates_no_skill_bundles_dir(tmp_path):
    """A persona with no bundles[] declared does not get an empty
    skill-bundles/ directory."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML, no bundles
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    bundles_dir = hermes_home / "profiles" / "marcus" / "skill-bundles"
    assert not bundles_dir.exists()


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
# MCP connector materialization (mcp:agentmail -> mcp_servers block)
# ---------------------------------------------------------------------------


AGENTMAIL_YAML = VALID_YAML.replace("adapter: gmail", "adapter: agentmail").replace(
    "backend: mcp:gmail", "backend: mcp:agentmail"
)


def test_translate_materializes_agentmail_mcp_server(tmp_path, monkeypatch):
    """An enabled ``mcp:agentmail`` connector becomes a Hermes mcp_servers entry."""
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am_us_test_key")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=AGENTMAIL_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    servers = config["mcp_servers"]
    assert "agentmail" in servers
    am = servers["agentmail"]
    assert am["url"] == "https://mcp.agentmail.to/mcp"
    assert am["enabled"] is True
    assert am["headers"] == {"x-api-key": "am_us_test_key"}


def test_translate_does_not_exclude_agentmail_sends(tmp_path, monkeypatch):
    """ss#2258: agentmail TRANSMIT tools are excluded; reads and drafts are not.

    This reverses the ADR 0025 posture for the four send tools, and the reason
    matters. ADR 0025 said exposure is a configurable ceiling rather than an
    MCP-level exclusion, so sends stayed on the menu for the trust layer to
    govern. The 2026-08 incident showed the flaw: a path that never reached the
    trust layer sent four fabricated messages to a real client principal with no
    audit row. Governance that can be routed around is not governance.

    So the credential changed, and the menu follows it. The gateway's AgentMail
    key is now inbox-scoped WITHOUT ``message_send``/``draft_send``, so these
    four would 403 at the vendor — and a tool advertised but permanently failing
    is worse than an absent one: the agent retries it and the failure reads as a
    mystery instead of a routing decision.

    ADR 0025's actual principle survives intact. Sending is still governed by the
    authored per-action ceiling, not banned — it just executes through
    ``smd_send_message``, which carries the same EXTERNAL_SEND class and reaches
    the broker. Reads and drafts stay on the menu because the no-send key still
    performs them, and the reply channel depends on ``create_draft`` firing.
    """
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am_us_test_key")
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=AGENTMAIL_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    excluded = set(config["mcp_servers"]["agentmail"]["tools"]["exclude"])
    assert excluded == {
        # surface reduction (2026-07-15)
        "create_inbox",
        "delete_inbox",
        "update_inbox",
        "list_organizations",
        "select_organization",
        "delete_thread",
        "update_thread",
        "update_message",
        # governance: transmit moved behind the broker (ss#2258)
        "send_message",
        "send_draft",
        "reply_to_message",
        "forward_message",
    }
    # Reads and drafts MUST stay: the no-send key still performs them, the reply
    # channel triggers on create_draft, and the intake skill authors it. Dropping
    # one of these would silently break the Operator's mailbox.
    for tool in ("create_draft", "get_thread", "list_messages", "get_attachment"):
        assert tool not in excluded


def test_translate_skips_agentmail_when_key_unset(tmp_path, monkeypatch):
    """No key in the env => the agentmail MCP server is not wired (boot continues)."""
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=AGENTMAIL_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "agentmail" not in config.get("mcp_servers", {})


def test_translate_unregistered_mcp_backend_not_materialized(tmp_path):
    """A ``mcp:`` backend with no registry entry (e.g. gmail) yields no mcp_servers."""
    # VALID_YAML uses backend: mcp:gmail, which is not in the registry.
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "mcp_servers" not in config


# ---------------------------------------------------------------------------
# Clio — a LOCAL stdio MCP server (command + args + env), not a hosted URL.
# ---------------------------------------------------------------------------

CLIO_YAML = VALID_YAML.replace("backend: mcp:gmail", "backend: mcp:clio-oktopeak")


def test_translate_materializes_clio_stdio_mcp_server(tmp_path, monkeypatch):
    """An enabled ``mcp:clio-oktopeak`` connector becomes a stdio mcp_servers entry."""
    monkeypatch.setenv("CLIO_CLIENT_ID", "clio_id_test")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "clio_secret_test")
    monkeypatch.setenv("CLIO_ENCRYPTION_KEY", "f" * 64)
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=CLIO_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    servers = config["mcp_servers"]
    assert "clio-oktopeak" in servers
    clio = servers["clio-oktopeak"]
    # stdio shape: a command + env, NOT a url/headers.
    assert clio["command"] == "clio-mcp"
    assert clio["enabled"] is True
    assert "url" not in clio
    assert "headers" not in clio
    # env carries the client creds + the REMAPPED encryption key.
    assert clio["env"]["CLIO_CLIENT_ID"] == "clio_id_test"
    assert clio["env"]["CLIO_CLIENT_SECRET"] == "clio_secret_test"
    assert clio["env"]["ENCRYPTION_KEY"] == "f" * 64  # remapped from CLIO_ENCRYPTION_KEY
    # static env: TRANSPORT=stdio — without it clio-mcp defaults to HTTP mode and
    # fatals ("MCP_BASE_URL is required in HTTP mode"), closing the stdio
    # connection on launch (the pilot-law first-boot failure).
    assert clio["env"]["TRANSPORT"] == "stdio"


def test_translate_skips_clio_when_required_secret_unset(tmp_path, monkeypatch):
    """A missing required secret leaves the Clio server unwired (boot continues)."""
    monkeypatch.setenv("CLIO_CLIENT_ID", "clio_id_test")
    monkeypatch.setenv("CLIO_CLIENT_SECRET", "clio_secret_test")
    monkeypatch.delenv("CLIO_ENCRYPTION_KEY", raising=False)  # required, absent
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=CLIO_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "clio-oktopeak" not in config.get("mcp_servers", {})


# Native web search — the WebSearch capability wired via Hermes' bundled web
# providers (plugins/web/*, e.g. brave-free), NOT an MCP server (ADR 0070, native
# cut 2026-07-08). Selected by `backend: native:<provider>` -> config
# web.search_backend; the former mcp:brave stdio wrapper was the redundant layer.
# ---------------------------------------------------------------------------

NATIVE_WEB_YAML = VALID_YAML.replace("backend: mcp:gmail", "backend: native:brave-free")


def test_translate_materializes_native_web_search(tmp_path):
    """An enabled ``native:brave-free`` connector becomes a Hermes ``web`` block,
    NOT an mcp_servers entry (ADR 0070 native cut)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(
        tmp_path, customer_yaml_body=NATIVE_WEB_YAML
    )
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    # Named as the active native search backend; the provider reads its own key
    # (e.g. BRAVE_SEARCH_API_KEY) at runtime — not staged into config here.
    assert config["web"] == {"search_backend": "brave-free"}
    # NOT wrapped as an MCP server — that redundant layer was removed.
    assert "brave" not in config.get("mcp_servers", {})


def test_translate_omits_web_block_without_native_connector(tmp_path):
    """No native web connector => no ``web`` block (configs stay byte-identical)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=VALID_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    assert "web" not in config


# Smokeball — author-built stdio connector with required per-seat ENVIRONMENT and
# OPTIONAL per-seat auth_mode/refresh_token/account_id (the authorization_code path).
# ---------------------------------------------------------------------------

SMOKEBALL_YAML = VALID_YAML.replace("backend: mcp:gmail", "backend: mcp:smokeball")


def _smokeball_env(tmp_path, monkeypatch) -> dict:
    """Translate a smokeball seat and return its mcp_servers env block."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, customer_yaml_body=SMOKEBALL_YAML)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    config = yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())
    return config.get("mcp_servers", {})


def test_translate_materializes_smokeball_client_credentials_seat(tmp_path, monkeypatch):
    """A CC seat wires with the three creds + the required ENVIRONMENT; the optional
    auth_mode/refresh_token/account_id are absent (not in the env block)."""
    monkeypatch.setenv("SMOKEBALL_CLIENT_ID", "cid")
    monkeypatch.setenv("SMOKEBALL_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SMOKEBALL_API_KEY", "key")
    monkeypatch.setenv("SMOKEBALL_ENVIRONMENT", "staging")
    for v in ("SMOKEBALL_AUTH_MODE", "SMOKEBALL_REFRESH_TOKEN", "SMOKEBALL_ACCOUNT_ID"):
        monkeypatch.delenv(v, raising=False)
    servers = _smokeball_env(tmp_path, monkeypatch)
    assert "smokeball" in servers
    env = servers["smokeball"]["env"]
    assert env["SMOKEBALL_CLIENT_ID"] == "cid"
    assert env["SMOKEBALL_ENVIRONMENT"] == "staging"
    # The staging default is no longer hardcoded static — it comes from the seat.
    assert "SMOKEBALL_AUTH_MODE" not in env
    assert "SMOKEBALL_REFRESH_TOKEN" not in env
    assert "SMOKEBALL_ACCOUNT_ID" not in env


def test_translate_materializes_smokeball_authorization_code_seat(tmp_path, monkeypatch):
    """An authorization_code seat carries auth_mode + refresh_token (+ optional
    account_id) in the env block; ENVIRONMENT=production is honored per-seat."""
    monkeypatch.setenv("SMOKEBALL_CLIENT_ID", "cid")
    monkeypatch.setenv("SMOKEBALL_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SMOKEBALL_API_KEY", "key")
    monkeypatch.setenv("SMOKEBALL_ENVIRONMENT", "production")
    monkeypatch.setenv("SMOKEBALL_AUTH_MODE", "authorization_code")
    monkeypatch.setenv("SMOKEBALL_REFRESH_TOKEN", "rt-123")
    monkeypatch.delenv("SMOKEBALL_ACCOUNT_ID", raising=False)
    servers = _smokeball_env(tmp_path, monkeypatch)
    env = servers["smokeball"]["env"]
    assert env["SMOKEBALL_ENVIRONMENT"] == "production"
    assert env["SMOKEBALL_AUTH_MODE"] == "authorization_code"
    assert env["SMOKEBALL_REFRESH_TOKEN"] == "rt-123"
    assert "SMOKEBALL_ACCOUNT_ID" not in env  # optional, unset


def test_translate_skips_smokeball_when_environment_unset(tmp_path, monkeypatch):
    """ENVIRONMENT is REQUIRED — a seat without it is unwired (fail-closed), so a
    prod seat can never silently fall back to staging hosts."""
    monkeypatch.setenv("SMOKEBALL_CLIENT_ID", "cid")
    monkeypatch.setenv("SMOKEBALL_CLIENT_SECRET", "sec")
    monkeypatch.setenv("SMOKEBALL_API_KEY", "key")
    monkeypatch.delenv("SMOKEBALL_ENVIRONMENT", raising=False)  # required, absent
    servers = _smokeball_env(tmp_path, monkeypatch)
    assert "smokeball" not in servers


def test_sending_is_relocated_not_removed():
    """ss#2258: the capability survives the menu change; only the executor moved.

    This is the assertion that would have caught the mistake this change could
    easily have been. Both live seats author ``external_send_internal:
    autonomous`` — the Operator may answer a rostered colleague with no human in
    the loop. Dropping the MCP send tools WITHOUT a replacement would have
    revoked that silently, which is the same failure shape as the incident: a
    change whose real effect is invisible from the diff.

    So: the four MCP sends are off the menu, ``smd_send_message`` is on it with
    the SAME action class, and sends remain reclassified rather than banned.
    """
    from bootstrap.mcp_registry import MCP_CONNECTOR_REGISTRY
    from shared.action_classes import (
        BANNED_TOOLS,
        TOOL_ACTION_CLASS_MAP,
        ActionClass,
    )
    from shared.outbound_recipient import CLASSIFIED_SEND_TOOLS

    spec = MCP_CONNECTOR_REGISTRY["agentmail"]
    for tool in ("send_message", "send_draft", "reply_to_message", "forward_message"):
        assert tool in spec.blocked_tools, f"{tool} must not be directly callable"
        # Still classified, never banned — a ceiling decides, as ADR 0025 requires.
        assert f"agentmail:{tool}" not in BANNED_TOOLS

    # The replacement carries the same class, so the same ceiling governs it, and
    # is recipient-classified so INTERNAL/CLIENT/VENDOR reclassification applies.
    assert TOOL_ACTION_CLASS_MAP["smd_send_message"] == ActionClass.EXTERNAL_SEND
    assert "smd_send_message" in CLASSIFIED_SEND_TOOLS

    # The msgraph half, for a reason the AgentMail half did not have. There, the
    # gateway's key could not transmit, so an advertised send tool would have
    # 403'd anyway. Here the credential CAN transmit: on an `autonomous` seat the
    # gate returns allow and the MCP tool simply runs, reaching Graph with no
    # recipient fence and no audit row. Leaving these two on the menu would have
    # meant the fence covered only the seats that withhold.
    graph = MCP_CONNECTOR_REGISTRY["msgraph-mail"]
    for tool in ("send_message", "reply_message"):
        assert tool in graph.blocked_tools, f"msgraph-mail:{tool} must not be directly callable"
        assert f"msgraph-mail:{tool}" not in BANNED_TOOLS
    # Reads and drafts stay reachable — this fences transmit, not the mailbox.
    for tool in ("list_messages", "read_message", "create_draft", "poll_delta"):
        assert tool not in graph.blocked_tools


def test_the_broker_send_tool_is_actually_registered(fake_ctx):
    """A mapped-but-unregistered tool is inert — the failure that left the first
    escalation tools dead at runtime. Registration is the wiring; assert it."""
    from tests.conftest import load_plugin

    load_plugin("hermes-smd-trust").register(fake_ctx)
    assert "smd_send_message" in fake_ctx.tools


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_translate_customer_yaml_is_callable():
    """The ``translate_customer_yaml`` function must exist and be callable."""
    assert callable(translate_customer_yaml)


# --- inbound webhook platform materialization (ADR 0021 Stream E) ---------

from bootstrap import translate as _wh  # noqa: E402

_WH_CUSTOMER = {
    "connectors": {
        "Email": {
            "adapter": "agentmail",
            "backend": "mcp:agentmail",
            "enabled": True,
            "webhook_url": "https://hermes-smd.fly.dev/webhooks/agentmail",
        }
    },
    "webhook_triggers": [
        {
            "source": "agentmail",
            "event_type": "message.received",
            "skill": "inbox-triage",
            "persona": "crane",
        }
    ],
}


def test_webhook_platform_materialized_when_secret_present(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_AGENTMAIL", "shh")
    out = _wh._materialize_webhook_platform(_WH_CUSTOMER)
    assert out["webhook"]["enabled"] is True
    route = out["webhook"]["extra"]["routes"]["agentmail"]
    assert route["secret"] == "shh"
    assert route["events"] == ["message.received"]
    assert route["skills"] == ["inbox-triage"]
    assert "untrusted" in route["prompt"].lower()


def test_webhook_platform_fail_closed_without_secret(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET_AGENTMAIL", raising=False)
    assert _wh._materialize_webhook_platform(_WH_CUSTOMER) == {}


def test_webhook_platform_empty_when_no_webhook_url():
    cust = {"connectors": {"Email": {"adapter": "agentmail", "enabled": True}}}
    assert _wh._materialize_webhook_platform(cust) == {}


# --- msgraph poll-based inbound route (ADR 0078 slice 4) --------------------

_WH_MSGRAPH_CUSTOMER = {
    "connectors": {
        "Email": {
            "adapter": "msgraph",
            "backend": "mcp:msgraph-mail",
            "enabled": True,
            # No webhook_url — inbound is PULLED by the overlay delta poller.
        }
    },
    "webhook_triggers": [
        {
            "source": "msgraph",
            "event_type": "message.received",
            "skill": "inbox-triage",
            "persona": "marcus",
        }
    ],
}


def test_msgraph_reply_adapter_in_email_reply_set():
    assert "msgraph" in _wh._EMAIL_REPLY_ADAPTERS


def test_msgraph_loopback_route_materialized_without_webhook_url(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET_MSGRAPH", "graph-secret")
    out = _wh._materialize_webhook_platform(_WH_MSGRAPH_CUSTOMER)
    route = out["webhook"]["extra"]["routes"]["msgraph"]
    assert route["secret"] == "graph-secret"
    assert route["events"] == ["message.received"]
    assert route["skills"] == ["inbox-triage"]
    # The msgraph prompt names the tool an msgraph seat actually exposes and reads
    # DTO dot-paths (the poller's inbound_message shape), not {message.*}.
    assert "mcp_msgraph_mail_create_draft" in route["prompt"]
    assert "{inbound_message.from_addr}" in route["prompt"]
    assert "{message.from}" not in route["prompt"]


def test_msgraph_route_fail_closed_without_secret(monkeypatch):
    monkeypatch.delenv("WEBHOOK_SECRET_MSGRAPH", raising=False)
    assert _wh._materialize_webhook_platform(_WH_MSGRAPH_CUSTOMER) == {}


def test_msgraph_outbound_only_email_emits_no_route(monkeypatch):
    # adapter msgraph + enabled but NOT a webhook_triggers source → outbound-only,
    # never wakes the agent, so no inbound route (and no poller ingress).
    monkeypatch.setenv("WEBHOOK_SECRET_MSGRAPH", "graph-secret")
    cust = {
        "connectors": {"Email": {"adapter": "msgraph", "enabled": True}},
        "webhook_triggers": [],
    }
    assert _wh._materialize_webhook_platform(cust) == {}


def test_route_name_parsed_from_webhook_url():
    assert _wh._route_name_from_webhook_url("https://h.fly.dev/webhooks/agentmail") == "agentmail"
    assert _wh._route_name_from_webhook_url("https://h/webhooks/x/") == "x"
    assert _wh._route_name_from_webhook_url("") is None
    assert _wh._route_name_from_webhook_url("https://h/no-segment") is None


def test_telegram_platform_materialized_with_allowlist():
    cust = {
        "telegram": {
            "enabled": True,
            "allow_from": ["7367659986", " 123 "],
            "require_mention": False,
            "reactions": True,
        }
    }
    block = _wh._materialize_telegram_platform(cust)
    assert block["allow_from"] == ["7367659986", "123"]  # stringified + trimmed
    assert block["require_mention"] is False
    assert block["reactions"] is True


def test_telegram_platform_fail_closed_on_empty_allowlist():
    # enabled + empty allow_from must raise — pinned ref fails OPEN on empty allowlist
    for bad in ([], ["", "  "], None):
        with pytest.raises(ValueError, match="allow_from is empty"):
            _wh._materialize_telegram_platform({"telegram": {"enabled": True, "allow_from": bad}})


def test_telegram_platform_empty_when_absent_or_disabled():
    assert _wh._materialize_telegram_platform({}) == {}
    assert (
        _wh._materialize_telegram_platform({"telegram": {"enabled": False, "allow_from": ["1"]}})
        == {}
    )


def test_telegram_block_lands_in_persona_config():
    # End-to-end through _persona_config: the telegram block appears in the config dict.
    persona = {"slug": "crane", "name": "Crane", "status": "active", "skills": []}
    cust = {
        "customer_id": "smd",
        "telegram": {"enabled": True, "allow_from": ["7367659986"], "require_mention": False},
    }
    config = _wh._persona_config(persona, cust, {})
    assert config["telegram"]["allow_from"] == ["7367659986"]
    assert config["telegram"]["require_mention"] is False


def test_webhook_platform_toolsets_emitted_with_the_platform(monkeypatch):
    """ss-console#2145 CONFIG half. Without this block the webhook platform
    falls back to Hermes' ``hermes-webhook`` composite, which carries no
    ``file`` toolset — so ``read_file`` was absent on exactly the platform
    inbound email arrives on, and the trust plugin's spec read-mark could never
    be set there."""
    from shared.webhook_read_surface import WEBHOOK_READ_TOOLSET

    monkeypatch.setenv("WEBHOOK_SECRET_AGENTMAIL", "shh")
    persona = {"slug": "crane", "name": "Crane", "status": "active", "skills": []}
    cust = {"customer_id": "smd", **_WH_CUSTOMER}
    config = _wh._persona_config(persona, cust, {})

    emitted = config["platform_toolsets"]["webhook"]
    assert emitted == ["web", "vision", "clarify", WEBHOOK_READ_TOOLSET]
    # The safe toolsets must ride along: naming a platform REPLACES its default
    # composite, so omitting them would trade read_file for the four tools
    # webhook turns already had.
    assert emitted[:3] == ["web", "vision", "clarify"]
    # Never the whole ``file`` toolset — write_file/patch/search_files on an
    # untrusted inbound turn is what the webhook-safe default exists to deny.
    assert "file" not in emitted


def test_no_platform_toolsets_without_a_webhook_platform():
    """Seats with no inbound webhook stay byte-identical: no webhook platform,
    no toolset override for one."""
    persona = {"slug": "crane", "name": "Crane", "status": "active", "skills": []}
    config = _wh._persona_config(persona, {"customer_id": "smd"}, {})
    assert "platform_toolsets" not in config
    assert "platforms" not in config


def test_no_platform_toolsets_when_the_webhook_secret_is_absent(monkeypatch):
    """The platform fails closed without its signing secret; the toolset
    override must not outlive the platform it configures."""
    monkeypatch.delenv("WEBHOOK_SECRET_AGENTMAIL", raising=False)
    persona = {"slug": "crane", "name": "Crane", "status": "active", "skills": []}
    config = _wh._persona_config(persona, {"customer_id": "smd", **_WH_CUSTOMER}, {})
    assert "platform_toolsets" not in config


# ---------------------------------------------------------------------------
# Cron materialization: pre_run_decides staging (ADR 0047 phase 2)
# ---------------------------------------------------------------------------


class _RecordingCronStore:
    """Records create_job calls; no Hermes cron.jobs import (CI has none)."""

    def __init__(self) -> None:
        self.creates: list[dict] = []

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return []

    def create_job(self, **kwargs):
        self.creates.append(kwargs)
        return {"id": "job-1", **kwargs}

    def remove_job(self, job_id: str) -> bool:
        return False


_ESCALATOR_YAML = dedent(
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
        skills:
          - name: deadline-miss-escalator
            version: pending
            initiation:
              manual: true
              scheduled: true
              webhook: false
            enabled: true
        cron:
          - skill: deadline-miss-escalator
            schedule: '0 8 * * *'
            pre_run: pre_run.py
            wake_policy: pre_run_decides

    connectors:
      Email:
        adapter: gmail
        backend: mcp:gmail
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


def test_translate_stages_pre_run_script_and_registers_ref(tmp_path):
    """A pre_run_decides cron entry: the skill's pre_run.py is copied into the
    persona profile's scripts/ dir (Hermes' scheduler refuses scripts outside
    HERMES_HOME/scripts/) and the job is registered with the resolved ref."""
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(_ESCALATOR_YAML)
    skills_dir = tmp_path / "skills"
    (skills_dir / "deadline-miss-escalator").mkdir(parents=True)
    (skills_dir / "deadline-miss-escalator" / "SKILL.md").write_text("# escalator\n")
    (skills_dir / "deadline-miss-escalator" / "pre_run.py").write_text(
        "print('{\"wakeAgent\": false}')\n"
    )
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    stores: dict[str, _RecordingCronStore] = {}

    def store_for(slug: str) -> _RecordingCronStore:
        return stores.setdefault(slug, _RecordingCronStore())

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        cron_store_for=store_for,
    )

    staged = (
        hermes_home / "profiles" / "marcus" / "scripts" / "deadline-miss-escalator" / "pre_run.py"
    )
    assert staged.is_file(), "pre_run script must be staged into the profile scripts dir"
    assert "wakeAgent" in staged.read_text()
    create = stores["marcus"].creates[0]
    assert create["script"] == "deadline-miss-escalator/pre_run.py"
    assert create["no_agent"] is False
    assert create["skills"] == ["deadline-miss-escalator"]


# ---------------------------------------------------------------------------
# Cron reconciliation: drop-all orphan removal at the translate layer
# ---------------------------------------------------------------------------


class _RecordingReconcileStore:
    """A cron store that records removes and accepts preset jobs — enough to
    prove translate reconciles a persona's store even with NO authored cron."""

    def __init__(self, jobs: list[dict] | None = None) -> None:
        self.jobs: list[dict] = list(jobs or [])
        self.removed: list[str] = []

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return list(self.jobs)

    def create_job(self, **kwargs):
        job = {"id": "new", **kwargs}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        self.removed.append(job_id)
        return len(self.jobs) < before


def test_translate_reconciles_cron_for_persona_with_no_authored_cron(tmp_path):
    """The drop-all fix at the translate layer: even when a persona authors NO
    cron, translate still visits its store (reconcile_slugs = all persona slugs),
    so an orphaned managed job from a previous config is removed — not left to
    keep firing across reboots (the live customer-zero defect)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)  # VALID_YAML: marcus, no cron
    stores: dict[str, _RecordingReconcileStore] = {
        "marcus": _RecordingReconcileStore(
            [
                {
                    "id": "orphan-1",
                    "name": "op-managed:marcus:health-monitor",
                    "schedule": "*/30 * * * *",
                }
            ]
        )
    }
    asked: list[str] = []

    def store_for(slug: str) -> _RecordingReconcileStore:
        asked.append(slug)
        return stores.setdefault(slug, _RecordingReconcileStore())

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        cron_store_for=store_for,
    )
    assert "marcus" in asked, "translate must reconcile the persona even with no authored cron"
    assert "orphan-1" in stores["marcus"].removed, "orphaned managed job must be removed"


def test_translate_restores_hermes_home_after_cron_reconcile(tmp_path, monkeypatch):
    """translate snapshots/restores the process-global HERMES_HOME around cron
    reconcile, so a store factory that mutates it (the REAL one does, per
    profile) cannot leak the last-visited persona home into the rest of the
    process. The fakes elsewhere never mutate the env, so this is the only place
    that behavior is exercised."""
    import os

    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    monkeypatch.setenv("HERMES_HOME", "/sentinel/original")

    class _EnvMutatingStore:
        def list_jobs(self, include_disabled: bool = False) -> list[dict]:
            os.environ["HERMES_HOME"] = "/leaked/marcus"
            return []

        def create_job(self, **kwargs):
            return {"id": "x", **kwargs}

        def remove_job(self, job_id: str) -> bool:
            return False

    def store_for(slug: str) -> _EnvMutatingStore:
        return _EnvMutatingStore()

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        cron_store_for=store_for,
    )
    assert os.environ["HERMES_HOME"] == "/sentinel/original", (
        "HERMES_HOME must be restored after cron reconcile"
    )


_DIGEST_BLOCK = dedent(
    """
    digest:
      home_matter_id: 11111111-2222-3333-4444-555555555555
    """
)


def test_translate_renders_digest_home_into_soul(tmp_path):
    """ss-console #1742: the authored digest home reaches the agent via SOUL.md
    so the daily digest lands on the designated operations matter."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, VALID_YAML + _DIGEST_BLOCK)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "## Digest home" in soul
    assert "11111111-2222-3333-4444-555555555555" in soul
    assert "Do not write the digest to any client matter." in soul


def test_translate_omits_digest_home_when_unauthored(tmp_path):
    """No `digest:` block => no Digest-home section (fail-closed, byte-identical
    SOUL.md contract); a malformed block is skipped, never guessed."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )
    soul = (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()
    assert "## Digest home" not in soul


# ---------------------------------------------------------------------------
# Orphaned profile-home reconciliation (ss-console#2009)
# ---------------------------------------------------------------------------


def test_translate_removes_orphaned_profile_home(tmp_path):
    """A profile home whose persona is no longer authored is deleted —
    including its cron store, so a slug rename can never leave a frozen
    scheduler store behind on the volume (ss-console#2009)."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    orphan = hermes_home / "profiles" / "zeta"
    (orphan / "cron").mkdir(parents=True)
    (orphan / "config.yaml").write_text("stale: true\n")
    (orphan / "cron" / "jobs.json").write_text("[]\n")

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )

    assert not orphan.exists()
    assert (hermes_home / "profiles" / "marcus" / "config.yaml").exists()


def test_translate_reconciler_spares_dotdirs_and_files(tmp_path):
    """Only direct child DIRECTORIES are candidates: dot-prefixed entries
    and stray files under profiles/ survive reconciliation."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    profiles = hermes_home / "profiles"
    (profiles / ".snapshots").mkdir(parents=True)
    (profiles / "README").write_text("not a profile\n")

    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
    )

    assert (profiles / ".snapshots").is_dir()
    assert (profiles / "README").is_file()


def test_translate_reconciler_is_idempotent(tmp_path):
    """Second run with the same input removes nothing and leaves the
    authored home in place."""
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    for _ in range(2):
        slugs = translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
        )
    assert slugs == ["marcus"]
    assert (hermes_home / "profiles" / "marcus" / "SOUL.md").exists()


# ---------------------------------------------------------------------------
# T11 — the operator_seat_facts grounding sentence (ss-console#2222).
#
# Substring assertions, deliberately, and no hash or length pin: those fail on
# every unrelated wording change and teach the next author to update the pin
# without reading what moved.
#
# The SECOND half is the anti-regression, and it is the one that earns the file.
# The email/non-email prompt split exists because a verified Smokeball
# ``matter.updated`` once reached for ``agentmail create_draft`` when every route
# shared the one email prompt. Adding a sentence to the email prompts must not
# leak into the skill-driving prompt or the MCP one, or that split quietly stops
# being a split.
# ---------------------------------------------------------------------------

_GROUNDING = "call operator_seat_facts"


def test_both_email_prompts_carry_the_grounding_sentence():
    from bootstrap.translate import _INBOUND_EMAIL_PROMPT, _INBOUND_EMAIL_PROMPT_MSGRAPH

    for prompt in (_INBOUND_EMAIL_PROMPT, _INBOUND_EMAIL_PROMPT_MSGRAPH):
        assert _GROUNDING in prompt
        assert "asking about YOU" in prompt
        assert "rather than describing yourself from memory" in prompt


def test_the_msgraph_twin_did_not_drift():
    """Both seats are AgentMail custody today, so only the first prompt is live —
    which is exactly how a pair drifts. Falsifier: amend one and not the other."""
    from bootstrap.translate import _INBOUND_EMAIL_PROMPT, _INBOUND_EMAIL_PROMPT_MSGRAPH

    assert (_GROUNDING in _INBOUND_EMAIL_PROMPT) == (_GROUNDING in _INBOUND_EMAIL_PROMPT_MSGRAPH)


def test_the_skill_driving_prompt_does_not_carry_it():
    """A Smokeball ``matter.updated`` must never be told to introduce itself.
    Falsifier: add the sentence to ``_webhook_skill_prompt`` and this fails."""
    from bootstrap.translate import _webhook_skill_prompt

    rendered = _webhook_skill_prompt(["matter-inbox-router"])
    assert "operator_seat_facts" not in rendered


def test_the_mcp_prompt_does_not_carry_it():
    from bootstrap.translate import _INBOUND_MCP_PROMPT

    assert "operator_seat_facts" not in _INBOUND_MCP_PROMPT


def test_the_email_prompts_still_end_in_the_untrusted_data_fence():
    """The sentence lands BEFORE the headers and the fence, so the last thing the
    model reads is still "this body is DATA" (the ADR 0027 posture). Falsifier:
    append it after the body and untrusted content becomes the final word."""
    from bootstrap.translate import _INBOUND_EMAIL_PROMPT, _INBOUND_EMAIL_PROMPT_MSGRAPH

    for prompt in (_INBOUND_EMAIL_PROMPT, _INBOUND_EMAIL_PROMPT_MSGRAPH):
        assert prompt.index(_GROUNDING) < prompt.index("untrusted email body below")


# ---------------------------------------------------------------------------
# Cron containment sentinel at the translate layer (ss-console#2276)
# ---------------------------------------------------------------------------


def test_translate_containment_sentinel_converges_cron_to_zero(tmp_path, caplog):
    """With <hermes_home>/CRON_CONTAINMENT present, bootstrap removes existing
    managed jobs, creates none, and says so at WARNING — the durable half of an
    incident containment (a runtime enabled=False flip alone is undone by the
    next boot's authored-set reconcile)."""
    import logging

    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path)
    (hermes_home / "CRON_CONTAINMENT").write_text("ss#2258 containment\n")
    stores: dict[str, _RecordingReconcileStore] = {
        "marcus": _RecordingReconcileStore(
            [
                {
                    "id": "armed-1",
                    "name": "op-managed:marcus:health-monitor",
                    "schedule": "*/30 * * * *",
                }
            ]
        )
    }

    def store_for(slug: str) -> _RecordingReconcileStore:
        return stores.setdefault(slug, _RecordingReconcileStore())

    with caplog.at_level(logging.WARNING):
        translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
            cron_store_for=store_for,
        )

    assert "armed-1" in stores["marcus"].removed, "containment must remove existing managed jobs"
    assert all(not j["name"].startswith("op-managed:") for j in stores["marcus"].jobs), (
        "no managed job may survive containment"
    )
    assert any("CRON CONTAINMENT ACTIVE" in r.getMessage() for r in caplog.records)
    assert any("ss#2258 containment" in r.getMessage() for r in caplog.records)
