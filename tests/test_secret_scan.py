"""Tests for ``bootstrap.secret_scan`` — the on-box port of the console's
customer.yaml secret detector.

These mirror ``ss-console/tests/customer-yaml-secret-detector.test.ts`` case for
case, using the SAME synthetic secret shapes, so the two detectors are pinned to
identical behavior (ADR 0044 validator parity). The fixtures are built by
runtime concatenation of prefix + body so neither GitHub's push scanner nor
gitleaks flags this test file as a real leak.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from bootstrap.secret_scan import finding_to_error, scan_parsed_value, scan_raw_yaml
from bootstrap.validate import validate_customer_yaml

# Synthetic secret-shaped strings — none are real credentials; each follows a
# provider's published shape so the detector flags it. Built at runtime so
# static scanners cannot match them (identical trick to the TS fixtures).
_BODY_36 = "abcdefghijklmnopqrstuvwxyz0123456789ab"
_BODY_40 = "abcdefghijklmnopqrstuvwxyz0123456789abcd"
_BODY_HEX_64 = "deadbeefcafebabe0123456789abcdef0123456789abcdef0123456789abcdef"

SYNTH = {
    "stripe_live": "_".join(["sk", "live", _BODY_36]),
    "stripe_test": "_".join(["pk", "test", _BODY_36]),
    "jwt": ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "AbCdEfGhIjKlMnOpQrStUv"]),
    "aws_key": "AKIA" + "IOSFODNN7EXAMPLE",
    "gh_pat": "ghp" + "_" + "aaaabbbbccccddddeeeeffffgggghhhhiiiiJJJJ",
    "openai_key": "sk" + "-" + _BODY_40,
    "slack_bot": "-".join(["xoxb", "1111111111", "2222222222", "aaaaaaaaaaaa"]),
    "google_cs": "GOCSPX" + "-" + "abcdefghijklmnopqrstuvwxyz1234",
    "hex_long": _BODY_HEX_64,
    "base64_long": "A" * 60 + "B" * 40 + "=",
}


def _categories(findings) -> list[str]:
    return [f.category for f in findings]


# ---------------------------------------------------------------------------
# Provider-shaped patterns (parsed)
# ---------------------------------------------------------------------------


def test_flags_stripe_live_key_in_any_field():
    findings = scan_parsed_value(
        {"customer_id": "smith", "personas": [{"name": SYNTH["stripe_live"]}]}
    )
    assert "stripe_or_resend_shaped" in _categories(findings)


def test_flags_stripe_test_key():
    assert "stripe_or_resend_shaped" in _categories(scan_parsed_value({"x": SYNTH["stripe_test"]}))


def test_flags_jwt():
    assert "jwt" in _categories(scan_parsed_value({"x": SYNTH["jwt"]}))


def test_flags_aws_access_key_id():
    assert "aws_access_key_id" in _categories(scan_parsed_value({"creds": SYNTH["aws_key"]}))


def test_flags_github_pat():
    assert "github_token" in _categories(scan_parsed_value({"x": SYNTH["gh_pat"]}))


def test_flags_openai_key():
    assert "openai_api_key" in _categories(scan_parsed_value({"x": SYNTH["openai_key"]}))


def test_flags_slack_bot_token():
    assert "slack_token" in _categories(scan_parsed_value({"x": SYNTH["slack_bot"]}))


def test_flags_google_oauth_client_secret():
    assert "google_oauth_client_secret" in _categories(scan_parsed_value({"x": SYNTH["google_cs"]}))


def test_provider_checks_run_even_on_allowlisted_paths():
    # signature_html is shape-heuristic allowlisted, but a smuggled OpenAI key
    # is still flagged (provider checks always run).
    findings = scan_parsed_value(
        {"personas": [{"signature_html": f"<img>{SYNTH['openai_key']}</img>"}]}
    )
    assert "openai_api_key" in _categories(findings)


# ---------------------------------------------------------------------------
# Shape heuristics
# ---------------------------------------------------------------------------


def test_flags_long_hex_outside_allowlist():
    assert "hex_long" in _categories(scan_parsed_value({"x": SYNTH["hex_long"]}))


def test_flags_long_base64_outside_allowlist():
    cats = _categories(scan_parsed_value({"x": SYNTH["base64_long"]}))
    assert "base64_long" in cats or "high_entropy_long" in cats


def test_skips_shape_heuristics_in_signature_html():
    # A bare long benign string in signature_html must not flag as base64_long.
    findings = scan_parsed_value({"personas": [{"signature_html": "A" * 120}]})
    assert "base64_long" not in _categories(findings)


def test_skips_shape_heuristics_on_token_ref():
    findings = scan_parsed_value(
        {
            "connectors": {
                "PracticeManagement": {
                    "token_ref": "infisical:/operator/smith/practice-management/oauth-refresh"
                }
            }
        }
    )
    assert findings == []


def test_does_not_flag_short_low_entropy_strings():
    findings = scan_parsed_value({"name": "Marcus", "tone": ["warm", "concise"]})
    assert findings == []


# ---------------------------------------------------------------------------
# Banned field names
# ---------------------------------------------------------------------------


def test_flags_client_secret_even_when_value_empty():
    findings = scan_parsed_value({"connectors": {"Email": {"client_secret": ""}}})
    assert "banned_field_name" in _categories(findings)


def test_flags_api_key():
    assert "banned_field_name" in _categories(scan_parsed_value({"api_key": "whatever"}))


def test_flags_refresh_token():
    assert "banned_field_name" in _categories(scan_parsed_value({"refresh_token": "whatever"}))


def test_flags_bearer():
    assert "banned_field_name" in _categories(scan_parsed_value({"x": {"bearer": "whatever"}}))


def test_exempts_token_ref():
    findings = scan_parsed_value(
        {"connectors": {"Email": {"token_ref": "infisical:/scope/customer/email/refresh"}}}
    )
    assert "banned_field_name" not in _categories(findings)


def test_reports_jsonpath_of_banned_field():
    findings = scan_parsed_value({"connectors": {"Email": {"client_secret": "foo"}}})
    banned = next(f for f in findings if f.category == "banned_field_name")
    assert banned.path == "connectors.Email.client_secret"


# ---------------------------------------------------------------------------
# No-echo invariant — the single most important property
# ---------------------------------------------------------------------------


def test_never_echoes_secret_substring():
    cases = [
        ({"x": SYNTH["stripe_live"]}, SYNTH["stripe_live"]),
        ({"x": SYNTH["jwt"]}, SYNTH["jwt"]),
        ({"creds": SYNTH["aws_key"]}, SYNTH["aws_key"]),
        ({"x": SYNTH["openai_key"]}, SYNTH["openai_key"]),
        ({"x": SYNTH["gh_pat"]}, SYNTH["gh_pat"]),
        ({"x": SYNTH["slack_bot"]}, SYNTH["slack_bot"]),
        ({"x": SYNTH["google_cs"]}, SYNTH["google_cs"]),
        ({"x": SYNTH["hex_long"]}, SYNTH["hex_long"]),
    ]
    for doc, secret in cases:
        findings = scan_parsed_value(doc)
        assert findings, f"expected a finding for {secret[:6]}…"
        for f in findings:
            blob = f"{f.category}|{f.path}|{f.line}|{f.reason}|{finding_to_error(f)}"
            assert secret not in blob


# ---------------------------------------------------------------------------
# Raw-text scan
# ---------------------------------------------------------------------------


def test_raw_reports_1_indexed_line_number():
    text = dedent(
        f"""\
        customer_id: smith
        vertical: law-firm
        leaked: {SYNTH["stripe_live"]}
        """
    )
    lines = [f.line for f in scan_raw_yaml(text)]
    assert 3 in lines


def test_raw_flags_banned_field_names():
    assert "banned_field_name" in _categories(scan_raw_yaml("api_key: whatever\n"))


def test_raw_flags_provider_shaped_values():
    assert "stripe_or_resend_shaped" in _categories(scan_raw_yaml(f"x: {SYNTH['stripe_live']}\n"))


def test_raw_ignores_comment_only_lines():
    assert scan_raw_yaml(f"# leaked: {SYNTH['openai_key']}\n") == []


def test_raw_handles_trailing_inline_comments():
    assert scan_raw_yaml("fly_region: iad # set by ops\n") == []


# ---------------------------------------------------------------------------
# Validator integration (ADR 0044 gate)
# ---------------------------------------------------------------------------


_VALID_MIN = dedent(
    """\
    schema_version: 1
    customer_id: acme
    customer_name: Acme Corp
    vertical: home-services
    fly_region: iad
    model: claude-opus-4-7
    hermes_ref: v2026.5.16-smd.0
    personas:
      - slug: marcus
        status: active
        name: Marcus
    memory:
      d1_namespace: acme
      r2_vault_path: 'vaults/acme/'
      vectorize_index: 'hermes-acme-vault'
    """
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "customer.yaml"
    p.write_text(body)
    return p


def test_validator_now_accepts_home_services_vertical(tmp_path):
    # The synced vertical enum (Python ← TS) must accept home-services.
    assert validate_customer_yaml(_write(tmp_path, _VALID_MIN)) == []


def test_validator_rejects_a_leaked_key(tmp_path):
    bad = _VALID_MIN + f"\nleaked_field: {SYNTH['stripe_live']}\n"
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("secret-shaped value" in e for e in errors)
    # And the error never echoes the secret.
    assert all(SYNTH["stripe_live"] not in e for e in errors)


def test_validator_rejects_banned_field_name(tmp_path):
    bad = _VALID_MIN + "\napi_key: anything\n"
    errors = validate_customer_yaml(_write(tmp_path, bad))
    assert any("secret-shaped value" in e for e in errors)
