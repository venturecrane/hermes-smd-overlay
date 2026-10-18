"""Translate on a seat several people share (ADR 0090 R2, ADR 0091; ss-console #3264).

* ``people:`` authored: Hermes' native memory is forced off (both stores) and
  the ``memory`` toolset is disabled, because MEMORY.md is one file per profile
  that every partner's turn would read.
* ``seat.role: couples``: terminal, code_execution, delegation and the file
  WRITE tools (``smd_couples_no_file_writes``) are disabled whatever the
  persona's exposure says. ``file`` itself is not: that would strip read_file
  from the email surface (shared/couples_surface.py).
* A seat with neither renders exactly as before.
"""

from __future__ import annotations

import yaml

from bootstrap.translate import translate_customer_yaml
from config_applier.safety import live_writable, rebuild_class
from tests.test_bootstrap_translate import VALID_YAML, _seed_repo

PEOPLE = """
seat:
  kind: internal
  product: hosted-agent
people:
  - id: p1
    name: Ana
    telegram_id: '1001'
    email: ana@example.com
  - id: p2
    name: Ben
    telegram_id: '2002'
    email: ben@example.com
spaces:
  shared_telegram_chat: '-1001234567890'
"""


def _roles_dir(tmp_path) -> str:
    """A role seat fails closed without its package (bootstrap/role_persona.py),
    so every role these tests name gets a minimal one."""
    roles = tmp_path / "roles"
    for role in ("couples", "open-house"):
        (roles / role).mkdir(parents=True, exist_ok=True)
        (roles / role / "role.yaml").write_text(f"role: {role}\npersona: persona.md\n")
        (roles / role / "persona.md").write_text(f"# {role} persona\n")
    return str(roles)


def _with_people_roster(body: str) -> str:
    """A seat with people: must author exactly the people as its roster
    (bootstrap.validate._validate_people_roster, ss-console rule 8)."""
    if "\npeople:" not in body:
        return body
    return body.replace(
        "scope:\n  email_folders_visible",
        "scope:\n  inbound_allow_from: [ana@example.com, ben@example.com]\n  email_folders_visible",
        1,
    )


def _config(tmp_path, body: str) -> dict:
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, _with_people_roster(body))
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        roles_dir=_roles_dir(tmp_path),
    )
    return yaml.safe_load((hermes_home / "profiles" / "marcus" / "config.yaml").read_text())


def _rendered(tmp_path, body: str) -> bytes:
    customer_yaml, skills_dir, hermes_home = _seed_repo(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        roles_dir=_roles_dir(tmp_path),
    )
    return (hermes_home / "profiles" / "marcus" / "config.yaml").read_bytes()


def test_people_forces_native_memory_off_even_when_authored_on(tmp_path):
    """FALSIFIER: return ``authored`` unconditionally in ``_memory_block``."""
    config = _config(tmp_path, VALID_YAML + PEOPLE)
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False
    # The authored bindings ride along untouched.
    assert config["memory"]["d1_namespace"] == "acme"


def test_people_overrides_an_authored_true(tmp_path):
    body = VALID_YAML.replace(
        "memory:\n  d1_namespace: acme",
        "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n  d1_namespace: acme",
    )
    assert "memory_enabled: true" in body
    config = _config(tmp_path, body + PEOPLE)
    assert config["memory"]["memory_enabled"] is False
    assert config["memory"]["user_profile_enabled"] is False


def test_people_disables_the_memory_toolset(tmp_path):
    """MEMORY.md is injected unless the flags are off; the toolset comes off too
    so no memory tool writes into the shared file. FALSIFIER: drop the
    ``disabled_toolsets.append("memory")`` line."""
    config = _config(tmp_path, VALID_YAML + PEOPLE)
    assert "memory" in config["agent"]["disabled_toolsets"]


def test_couples_role_drops_shell_code_subagent_and_file(tmp_path):
    """Even with autonomous code_execution exposure. FALSIFIER: drop the
    ``_seat_role(customer) == _COUPLES_ROLE`` block."""
    body = VALID_YAML.replace(
        "internal_write: autonomous",
        "internal_write: autonomous\n        code_execution: autonomous",
    ).replace(
        "backend: mcp:gmail\n    enabled: true", "backend: mcp:gmail\n    enabled: false"
    ) + PEOPLE.replace("product: hosted-agent", "product: hosted-agent\n  role: couples")
    assert "role: couples" in body and "code_execution: autonomous" in body
    disabled = _config(tmp_path, body)["agent"]["disabled_toolsets"]
    for ts in ("terminal", "code_execution", "delegation", "smd_couples_no_file_writes", "memory"):
        assert ts in disabled, ts
    # FALSIFIER (captain-mark 2026-10-18): disabling `file` strips read_file from
    # the webhook surface and the activation gate refuses to boot.
    assert "file" not in disabled
    assert len(disabled) == len(set(disabled)), "no toolset listed twice"


def test_couples_role_with_refused_code_lists_each_toolset_once(tmp_path):
    body = VALID_YAML + PEOPLE.replace(
        "product: hosted-agent", "product: hosted-agent\n  role: couples"
    )
    disabled = _config(tmp_path, body)["agent"]["disabled_toolsets"]
    assert disabled.count("terminal") == 1
    assert disabled.count("smd_couples_no_file_writes") == 1


def test_seat_without_people_is_unchanged(tmp_path):
    """REGRESSION: no people, no role: the memory block is the authored mapping
    verbatim and no memory/file toolset is disabled."""
    config = _config(tmp_path, VALID_YAML)
    assert config["memory"] == {
        "d1_namespace": "acme",
        "r2_vault_path": "vaults/acme/",
        "vectorize_index": "hermes-acme-vault",
    }
    assert "memory" not in config["agent"]["disabled_toolsets"]
    assert "file" not in config["agent"]["disabled_toolsets"]


def test_a_role_other_than_couples_is_unchanged(tmp_path):
    seat = "seat:\n  kind: customer\n  product: hosted-agent\n"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_role = _rendered(tmp_path / "a", VALID_YAML + seat + "  role: open-house\n")
    without = _rendered(tmp_path / "b", VALID_YAML + seat)
    assert with_role == without


def test_people_role_and_spaces_never_apply_live():
    """translate reads them at boot, so a live apply would be a silent partial
    application. FALSIFIER: drop the three entries from the never-list."""
    for path in ("seat.role", "people", "people.0.telegram_id", "spaces.shared_telegram_chat"):
        assert not live_writable(path), path
        assert rebuild_class(path), path
    # The rest of `seat` stays live-writable (ADR 0083 grain).
    assert live_writable("seat.kind")
