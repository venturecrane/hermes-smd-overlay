"""The REAL couples seat template boots: read_file on email, no write tools (ss-console #3264).

This is the exact failure captain-mark hit on 2026-10-18 ("WEBHOOK TURNS CANNOT
READ FILES"): translate rendered a couples persona whose disabled toolsets
stripped ``read_file`` from the webhook surface, and the activation gate died.

``tests/fixtures/roles/couples/customer.yaml`` is a byte-identical twin of
ss-console ``operator/customers/_couples-template/customer.yaml`` (tracked in
ss-console ``operator/contracts/overlay-pairs.json``; regenerate by copying).
The test fills the bracketed placeholders, runs the real translate, and runs
the two activation checks on the rendered config: the webhook read-surface
assertion and step 5a (``shared.couples_surface``). Hermes' toolset resolution
is the faithful model from ``tests/test_couples_surface.py`` (enable, then
subtract every disabled toolset last), checked against the pinned Hermes source
by vfy_01M58PRD13ZZRGJF2B06KAP67A.

FALSIFIER: with ade63ad6's couples disable list (``file`` instead of
``smd_couples_no_file_writes``) the same template fails the read-surface check.

The template must also let the seat SEND to the partners on a scheduled turn
(captain-mark, 2026-10-18: the first scheduled email to a partner was refused
``external_send_internal refused: no authored exposure (fail-closed)``). The
send test resolves the persona's exposure through the real trusted-config
reader and the real ``enforce``: a rostered send with no current-turn approval
must be allowed, an off-roster send must stay withheld for confirm.
FALSIFIER: strip ``external_send_internal`` from the template and the rostered
send is refused.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from bootstrap import translate
from shared import couples_surface, webhook_read_surface
from tests.conftest import load_plugin
from tests.test_couples_surface import resolve

FIXTURE = Path(__file__).parent / "fixtures" / "roles" / "couples"

FILL = {
    "[SLUG]": "couple-test",
    "[COUPLE NAME]": "Test Couple",
    "[CURRENT BLESSED FLEET PIN, e.g., v2026.5.7@<40-hex-sha>]": (
        "v2026.9.14@345cd2b057a452236de401d3534b8502a7465e8d"
    ),
    "[PARTNER 1 NAME]": "Alex Example",
    "[PARTNER 2 NAME]": "Sam Example",
    "[PARTNER 1 EMAIL, lowercased]": "alex@example.com",
    "[PARTNER 2 EMAIL, lowercased]": "sam@example.com",
    "[AGENT NAME, lowercased]": "juniper",
    "[AGENT NAME]": "Juniper",
    "[SMD SENTINEL TELEGRAM ID until both partners are recorded]": "1000000001",
}

#: ade63ad6's couples disable list: the one the live seat booted with.
ADE63AD6_COUPLES_REFUSED = ("terminal", "code_execution", "delegation", "file")


def _filled_text() -> str:
    text = (FIXTURE / "customer.yaml").read_text()
    for placeholder, value in FILL.items():
        text = text.replace(placeholder, value)
    return text


def _render(tmp_path: Path) -> dict:
    text = _filled_text()
    body = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    assert not re.search(r"\[[A-Z][^\]]*\]", body), "an unfilled placeholder: update FILL"
    doc = yaml.safe_load(text)
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(text)
    skills = tmp_path / "skills"
    for skill in doc["personas"][0]["skills"]:
        (skills / skill["name"]).mkdir(parents=True, exist_ok=True)
        (skills / skill["name"] / "SKILL.md").write_text(f"# {skill['name']}\n")
    roles = tmp_path / "roles" / "couples"
    roles.mkdir(parents=True)
    (roles / "role.yaml").write_text("role: couples\npersona: persona.md\n")
    (roles / "persona.md").write_text((FIXTURE / "persona.md").read_text())
    home = tmp_path / "home"
    home.mkdir()
    translate.translate_customer_yaml(
        customer_yaml_path=str(customer_yaml),
        hermes_home=str(home),
        skills_dir=str(skills),
        roles_dir=str(tmp_path / "roles"),
    )
    slug = doc["personas"][0]["slug"]
    return yaml.safe_load((home / "profiles" / slug / "config.yaml").read_text())


@pytest.fixture
def hermes_model(monkeypatch):
    # The live seat has its AgentMail route secret (a Fly secret); without it
    # translate emits no webhook route and there is no email surface to check.
    monkeypatch.setenv("WEBHOOK_SECRET_AGENTMAIL", "test-secret")
    monkeypatch.setattr(webhook_read_surface, "resolve_platform_tool_names", resolve)
    monkeypatch.setattr(
        webhook_read_surface, "resolve_webhook_tool_names", lambda c: resolve(c, "webhook")
    )


def test_the_couples_template_passes_both_activation_checks(tmp_path, hermes_model):
    config = _render(tmp_path)
    assert webhook_read_surface.webhook_platform_enabled(config)
    webhook_read_surface.assert_read_file_on_webhook(config)
    assert couples_surface.is_couples_config(config)
    couples_surface.assert_no_forbidden_tools(config)


def test_FALSIFIER_ade63ad6s_disable_list_is_the_live_boot_failure(
    tmp_path, hermes_model, monkeypatch
):
    monkeypatch.setattr(translate, "_COUPLES_REFUSED_TOOLSETS", ADE63AD6_COUPLES_REFUSED)
    config = _render(tmp_path)
    with pytest.raises(webhook_read_surface.WebhookReadSurfaceError, match="read_file"):
        webhook_read_surface.assert_read_file_on_webhook(config)


def _scheduled_send(tmp_path, monkeypatch, text: str, action_name: str):
    """Resolve the template persona's exposure via the trusted-config reader and
    decide one send on a scheduled turn: internal trust class, no approval."""
    enforce = load_plugin("hermes-smd-trust").enforce
    customer_yaml = tmp_path / "trusted-customer.yaml"
    customer_yaml.write_text(text)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(customer_yaml))
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(tmp_path / "no-overrides.db"))
    doc = yaml.safe_load(text)
    slug = doc["personas"][0]["slug"]
    exposure = enforce._resolve_persona_exposure(slug)
    floors = dict(enforce._VERTICAL_FLOORS.get(doc.get("vertical") or "", {}))
    return enforce.enforce(
        action=enforce.ActionClass(action_name),
        exposure=exposure,
        tool_name="agentmail_send_message",
        persona_slug=slug,
        current_turn_approval=False,
        vertical_floors=floors,
    )


def test_the_couples_template_sends_to_a_partner_on_a_scheduled_turn(tmp_path, monkeypatch):
    decision = _scheduled_send(tmp_path, monkeypatch, _filled_text(), "external_send_internal")
    assert decision.allowed, decision.reason
    assert decision.audit_action == "allow"


def test_the_couples_template_still_confirms_an_off_roster_send(tmp_path, monkeypatch):
    decision = _scheduled_send(tmp_path, monkeypatch, _filled_text(), "external_send")
    assert not decision.allowed
    assert decision.audit_action != "allow"


def test_FALSIFIER_without_external_send_internal_the_partner_send_is_refused(
    tmp_path, monkeypatch
):
    text = _filled_text()
    line = "        external_send_internal: autonomous\n"
    assert text.count(line) == 1, "the template no longer authors the line this test strips"
    decision = _scheduled_send(
        tmp_path, monkeypatch, text.replace(line, ""), "external_send_internal"
    )
    assert not decision.allowed
    assert "no authored exposure" in decision.reason
