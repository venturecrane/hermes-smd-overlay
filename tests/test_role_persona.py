"""A seat's role persona reaches SOUL.md verbatim, or the boot fails (ss ADR 0090).

``seat.role: <role>`` makes translate render ``<roles_dir>/<role>/<persona>``
(the persona file named by the package's role.yaml) into every profile's
SOUL.md. Three properties are pinned here:

* verbatim: the persona's safety rules arrive word for word. The fixture is a
  copy of ss-console ``operator/roles/couples/persona.md``; the assertions read
  whatever the fixture holds, so a later edit to the copy cannot make them pass
  vacuously, and the rule-by-rule check names each rule that went missing.
* fail closed: a role whose package, manifest or persona is missing, malformed
  or empty raises TranslateError before any profile is written.
* no-role regression: a seat without ``seat.role`` writes the same SOUL.md bytes
  whether or not a roles dir exists, and whether or not it carries a ``seat:``
  block at all.
"""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path

import pytest
import yaml

from bootstrap.role_persona import RolePersonaError, load_role_persona
from bootstrap.translate import TranslateError, translate_customer_yaml
from tests.test_bootstrap_translate import VALID_YAML, _seed_repo

FIXTURE_PERSONA = Path(__file__).parent / "fixtures" / "roles" / "couples" / "persona.md"

_ROLE_YAML = "role: couples\nschema_version: 1\npersona: persona.md\n"


def _roles_dir(tmp_path: Path, *, role_yaml: str = _ROLE_YAML, persona: str | None = None) -> Path:
    package = tmp_path / "roles" / "couples"
    package.mkdir(parents=True)
    (package / "role.yaml").write_text(role_yaml)
    if persona is None:
        shutil.copy(FIXTURE_PERSONA, package / "persona.md")
    else:
        (package / "persona.md").write_text(persona)
    return tmp_path / "roles"


def _seed(tmp_path: Path, body: str) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _seed_repo(tmp_path, body)


def _with_seat(role: str | None) -> str:
    seat = "seat:\n  kind: customer\n  product: hosted-agent\n"
    if role is not None:
        seat += f"  role: {role}\n"
    return VALID_YAML + "\n" + seat


def _soul(tmp_path: Path, body: str, roles_dir: Path | None) -> str:
    customer_yaml, skills_dir, hermes_home = _seed(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        roles_dir=str(roles_dir) if roles_dir else str(tmp_path / "no-roles-here"),
    )
    return (hermes_home / "profiles" / "marcus" / "SOUL.md").read_text()


def _translate(tmp_path: Path, body: str, roles_dir: Path) -> Path:
    customer_yaml, skills_dir, hermes_home = _seed(tmp_path, body)
    translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(hermes_home),
        skills_dir=str(skills_dir),
        roles_dir=str(roles_dir),
    )
    return hermes_home


# ---------------------------------------------------------------------------
# Verbatim
# ---------------------------------------------------------------------------


def test_role_persona_is_in_soul_verbatim(tmp_path):
    roles = _roles_dir(tmp_path)
    soul = _soul(tmp_path / "seat", _with_seat("couples"), roles)
    assert FIXTURE_PERSONA.read_text() in soul


def _safety_rules(text: str) -> list[str]:
    section = text.split("## Safety rules\n", 1)[1].split("\n## ", 1)[0]
    rules = re.split(r"\n(?=\d+\. )", section.strip())
    return [r for r in rules if re.match(r"\d+\. ", r)]


def test_every_safety_rule_is_in_soul_word_for_word(tmp_path):
    persona = FIXTURE_PERSONA.read_text()
    rules = _safety_rules(persona)
    # The couples persona authors six rules (#3264 Captain decision 5); a
    # fixture that parsed to fewer would make this test pass on nothing.
    assert len(rules) == 6
    soul = _soul(tmp_path / "seat", _with_seat("couples"), _roles_dir(tmp_path))
    missing = [r.splitlines()[0] for r in rules if r not in soul]
    assert missing == []
    for resource in ("**988**", "**911**", "**1-800-799-7233**", "**88788**"):
        assert resource in soul


def test_role_persona_reaches_every_profile(tmp_path):
    customer = yaml.safe_load(_with_seat("couples"))
    nova = copy.deepcopy(customer["personas"][0])
    nova.update(slug="nova", name="Nova")
    customer["personas"].append(nova)
    body = yaml.safe_dump(customer, sort_keys=False)
    roles = _roles_dir(tmp_path)
    home = _translate(tmp_path / "seat", body, roles)
    persona = FIXTURE_PERSONA.read_text()
    for slug in ("marcus", "nova"):
        assert persona in (home / "profiles" / slug / "SOUL.md").read_text()


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def _assert_refused(tmp_path: Path, body: str, roles_dir: Path, match: str) -> None:
    customer_yaml, skills_dir, hermes_home = _seed(tmp_path, body)
    with pytest.raises(TranslateError, match=match):
        translate_customer_yaml(
            customer_yaml_path=str(customer_yaml),
            hermes_home=str(hermes_home),
            skills_dir=str(skills_dir),
            roles_dir=str(roles_dir),
        )
    assert not (hermes_home / "profiles").exists(), "no profile may be written"


def test_missing_role_package_fails_the_boot(tmp_path):
    _assert_refused(tmp_path / "seat", _with_seat("couples"), tmp_path / "empty", "role.yaml")


def test_missing_persona_file_fails_the_boot(tmp_path):
    roles = _roles_dir(tmp_path)
    (roles / "couples" / "persona.md").unlink()
    _assert_refused(tmp_path / "seat", _with_seat("couples"), roles, "persona")


def test_empty_persona_fails_the_boot(tmp_path):
    roles = _roles_dir(tmp_path, persona="  \n")
    _assert_refused(tmp_path / "seat", _with_seat("couples"), roles, "empty")


def test_manifest_for_another_role_fails_the_boot(tmp_path):
    roles = _roles_dir(tmp_path, role_yaml="role: brokers\npersona: persona.md\n")
    _assert_refused(tmp_path / "seat", _with_seat("couples"), roles, "does not declare")


@pytest.mark.parametrize("persona", ["../persona.md", "/etc/passwd", "sub/persona.md", 7])
def test_persona_must_be_a_bare_filename(tmp_path, persona):
    roles = _roles_dir(tmp_path, role_yaml=f"role: couples\npersona: {persona}\n")
    _assert_refused(tmp_path / "seat", _with_seat("couples"), roles, "bare filename")


def test_role_slug_cannot_leave_the_roles_dir(tmp_path):
    with pytest.raises(RolePersonaError, match="not a role slug"):
        load_role_persona({"seat": {"role": "../couples"}}, str(tmp_path))


# ---------------------------------------------------------------------------
# No role: byte-identical
# ---------------------------------------------------------------------------


def test_seat_without_role_is_byte_identical(tmp_path):
    roles = _roles_dir(tmp_path)
    bare = _soul(tmp_path / "a", VALID_YAML, None)
    with_roles_dir = _soul(tmp_path / "b", VALID_YAML, roles)
    with_seat_block = _soul(tmp_path / "c", _with_seat(None), roles)
    assert bare == with_roles_dir == with_seat_block
    assert "## Safety rules" not in bare


def test_seat_without_role_never_reads_the_roles_dir(tmp_path):
    assert load_role_persona({}, str(tmp_path / "absent")) == ""
    assert load_role_persona({"seat": {"kind": "customer"}}, str(tmp_path / "absent")) == ""
