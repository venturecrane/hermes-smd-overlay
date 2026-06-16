"""customer.yaml secret detector (on-box port of the console's TS detector).

A faithful port of ``ss-console/src/lib/operator/customer-yaml/secret-detector.ts``.
customer.yaml is the file that wires one customer's configuration; a literal
secret committed to it lands in git history permanently and, for a regulated
tenant like a law firm, is a privilege breach. The console runs this scan at
authoring time; the broker runs THIS port on-box before writing a pulled
customer.yaml to the volume (ADR 0044) — defense in depth, so a secret never
reaches the Machine even if the console path is bypassed.

SOURCE OF TRUTH is the TS module. The two MUST classify identically; the
cross-repo contract fixtures pin the agreement. Port changes here without the TS
side (or vice versa) are a drift bug.

Two operating modes mirror the TS detector:

* :func:`scan_raw_yaml` — line-by-line scan of the raw text, run BEFORE the
  structural parse so a malformed YAML carrying a secret still fails closed.
* :func:`scan_parsed_value` — recursive scan of the parsed object, run after the
  structural parse so JSONPath-level context is available for each finding.

Critical invariant: a finding NEVER stores or echoes the matched substring. The
detector exists precisely so secret values do not enter persistent stores (git,
CI logs, transcripts, agent context). Echoing the match would defeat it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Patterns (ported verbatim from secret-detector.ts)
# ---------------------------------------------------------------------------

# Provider-shaped patterns: a match is almost certainly a real secret of that
# provider. They run inside allowlisted fields too (a key smuggled into
# signature_html is still a leak).
_PROVIDER_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "stripe_or_resend_shaped",
        re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{20,}\b"),
        "value resembles a Stripe / Resend / Anthropic-shaped key",
    ),
    (
        "jwt",
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "value resembles a JSON Web Token",
    ),
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "value resembles an AWS access key ID",
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
        "value resembles a GitHub personal/OAuth token",
    ),
    (
        "openai_api_key",
        re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{32,}\b"),
        "value resembles an OpenAI API key",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
        "value resembles a Slack token",
    ),
    (
        "google_oauth_client_secret",
        re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
        "value resembles a Google OAuth client secret",
    ),
]

# Field names that must never carry a value in customer.yaml — they belong in
# Infisical, not git. Matched case-insensitively as a substring of the key.
_BANNED_FIELD_NAME_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",  # matches client_secret, api_secret, ...
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "bearer",
    "auth_token",
)

# Paths permitted to carry long high-entropy / base64-shaped values. They are
# STILL scanned for provider-shaped keys; only the shape heuristics are skipped.
_SHAPE_HEURISTIC_ALLOWLIST_PATHS: tuple[str, ...] = (
    "customer_name",
    "personas[*].signature_html",
    "personas[*].avatar_url",
    "personas[*].send_as.agentmail_identity",
    "users[*].email",
    "users[*].full_name",
    "escalation.red_flag_recipients",
    "escalation.failure_recipients",
    # token_ref is the ONE permitted secret-reference channel: an Infisical path
    # string, carrying no secret. Bypass shape heuristics so the path is not
    # flagged as base64-shaped or high-entropy.
    "connectors.*.token_ref",
    # clerk_subject is a stable PUBLIC identifier (``user_…`` / ``org_…``) — high
    # entropy but not a secret. Provider-key + banned-name checks still run on it;
    # only the generic shape heuristic is skipped (same posture as above).
    "mcp_connector.access[*].clerk_subject",
)

# Field NAMES exempt from the shape heuristic in the RAW line scan, where the
# structural path is unavailable (path is None, so the path allowlist cannot
# match). Mirrors the path allowlist for the fail-closed raw pass. The
# authoritative parsed pass still provider-key-checks these fields, so skipping
# the raw shape heuristic here cannot let a provider-shaped key through.
_SHAPE_HEURISTIC_ALLOWLIST_FIELDS: frozenset[str] = frozenset({"clerk_subject"})


@dataclass(frozen=True)
class SecretFinding:
    """One match. The matched substring is intentionally NOT stored."""

    category: str
    line: int | None  # 1-indexed for raw scans; None for parsed-value scans
    path: str | None
    reason: str


# ---------------------------------------------------------------------------
# Shape heuristics
# ---------------------------------------------------------------------------

_HEX_LONG = re.compile(r"^[a-f0-9]{40,}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_ARRAY_INDEX = re.compile(r"\[\d+\]")

# Clerk PUBLIC identifiers (``user_…`` / ``org_…``) — they appear in URLs and API
# responses; they are NOT secrets. They are high-entropy and so trip the generic
# shape heuristic. Exempt them from the SHAPE check only — the provider-key and
# banned-field-name checks still run (a real key cannot hide behind a user_/org_
# prefix). This is value-based so it covers BOTH the parsed pass (has a path) and
# the raw line pass (bare list items, path=None). Fixes the
# mcp_connector.access[*].clerk_subjects[*] false-positive that crash-looped
# customer-zero on 2026-06-15.
_CLERK_PUBLIC_ID = re.compile(r"^(?:user|org)_[A-Za-z0-9]{16,}$")


def _shannon_entropy(s: str) -> float:
    """Bits per character. Random base64 ~5.5, English prose ~3.5."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    h = 0.0
    n = len(s)
    for count in freq.values():
        p = count / n
        h -= p * math.log2(p)
    return h


def _check_provider_patterns(value: str) -> str | None:
    for category, pattern, _reason in _PROVIDER_PATTERNS:
        if pattern.search(value):
            return category
    return None


def _check_shape_heuristics(value: str) -> str | None:
    trimmed = value.strip()
    if _HEX_LONG.match(trimmed):
        return "hex_long"
    if len(trimmed) > 80 and _BASE64.match(trimmed):
        return "base64_long"
    if len(trimmed) >= 32 and _shannon_entropy(trimmed) >= 4.5:
        return "high_entropy_long"
    return None


def _path_template(path: str) -> str:
    return _ARRAY_INDEX.sub("[*]", path)


def _is_path_shape_allowlisted(path: str | None, extra: tuple[str, ...]) -> bool:
    if path is None:
        return False
    template = _path_template(path)
    for entry in (*_SHAPE_HEURISTIC_ALLOWLIST_PATHS, *extra):
        if (
            template == entry
            or template.startswith(entry + ".")
            or template.startswith(entry + "[")
        ):
            return True
    return False


def _reason_for_category(category: str) -> str:
    reasons = {
        "stripe_or_resend_shaped": "value resembles a Stripe / Resend / Anthropic-shaped key",
        "jwt": "value resembles a JSON Web Token",
        "aws_access_key_id": "value resembles an AWS access key ID",
        "github_token": "value resembles a GitHub token",
        "openai_api_key": "value resembles an OpenAI API key",
        "slack_token": "value resembles a Slack token",
        "google_oauth_client_secret": "value resembles a Google OAuth client secret",
        "hex_long": "value is a long hex string (likely a secret hash or key)",
        "base64_long": "value is a long base64-shaped string",
        "high_entropy_long": "value is long and high-entropy (likely a generated secret)",
        "banned_field_name": (
            "field name is reserved for Infisical-managed secrets and must not "
            "appear in customer.yaml"
        ),
    }
    return reasons[category]


def _scan_value_shape(value: str, path: str | None, extra: tuple[str, ...]) -> str | None:
    provider = _check_provider_patterns(value)
    if provider is not None:
        return provider
    if _CLERK_PUBLIC_ID.match(value.strip()):
        return None
    if _is_path_shape_allowlisted(path, extra):
        return None
    return _check_shape_heuristics(value)


# ---------------------------------------------------------------------------
# Parsed-value scan
# ---------------------------------------------------------------------------


def scan_parsed_value(
    value: object, path: str = "", extra_allowlist: tuple[str, ...] = ()
) -> list[SecretFinding]:
    """Scan a parsed YAML value recursively, with JSONPath context per finding."""
    findings: list[SecretFinding] = []
    _visit(value, path, findings, extra_allowlist)
    return findings


def _visit(value: object, path: str, findings: list[SecretFinding], extra: tuple[str, ...]) -> None:
    if isinstance(value, str):
        category = _scan_value_shape(value, path, extra)
        if category is not None:
            findings.append(SecretFinding(category, None, path, _reason_for_category(category)))
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _visit(item, f"{path}[{i}]", findings, extra)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            key_str = str(key)
            key_lower = key_str.lower()
            banned = any(s in key_lower for s in _BANNED_FIELD_NAME_SUBSTRINGS)
            # token_ref is the explicitly permitted Infisical-reference field,
            # even though the name contains "token". Skip the field-name ban.
            if banned and key_str != "token_ref":
                findings.append(
                    SecretFinding(
                        "banned_field_name",
                        None,
                        key_str if path == "" else f"{path}.{key_str}",
                        _reason_for_category("banned_field_name"),
                    )
                )
                # Continue; the value still goes through the value-shape check.
            _visit(child, key_str if path == "" else f"{path}.{key_str}", findings, extra)
        return
    # Non-string scalars (numbers, booleans, None) are not scanned.


# ---------------------------------------------------------------------------
# Raw-text scan
# ---------------------------------------------------------------------------


def scan_raw_yaml(text: str, extra_allowlist: tuple[str, ...] = ()) -> list[SecretFinding]:
    """Scan raw YAML line-by-line, BEFORE the structural parse, so a malformed
    file carrying a secret still fails closed. Heuristic per-line: it does not
    understand block scalars / anchors / multi-line strings — the parsed-value
    pass is authoritative; this is the fail-closed-on-malformed-input defense."""
    findings: list[SecretFinding] = []
    lines = re.split(r"\r?\n", text)
    for i, raw_line in enumerate(lines):
        line = raw_line.lstrip()
        if line == "" or line.startswith("#"):
            continue
        colon = _find_unquoted_colon(line)
        field_name: str | None = None
        if colon == -1:
            value_text = _strip_leading_dash(line)
        else:
            field_name = line[:colon].strip()
            value_text = line[colon + 1 :].strip()
        if field_name is not None:
            key_name = field_name.strip("\"'")
            key_lower = key_name.lower()
            banned = any(s in key_lower for s in _BANNED_FIELD_NAME_SUBSTRINGS)
            if banned and key_name != "token_ref":
                findings.append(
                    SecretFinding(
                        "banned_field_name",
                        i + 1,
                        key_name,
                        _reason_for_category("banned_field_name"),
                    )
                )
        cleaned = _strip_comment_and_quotes(value_text)
        if cleaned == "":
            continue
        if (
            field_name is not None
            and field_name.strip("\"'").lower() in _SHAPE_HEURISTIC_ALLOWLIST_FIELDS
        ):
            # High-entropy but non-secret (e.g. clerk_subject). The authoritative
            # parsed pass still provider-key-checks this value.
            continue
        category = _scan_value_shape(cleaned, None, extra_allowlist)
        if category is not None:
            findings.append(
                SecretFinding(category, i + 1, field_name, _reason_for_category(category))
            )
    return findings


def _find_unquoted_colon(line: str) -> int:
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == ":":
            return i
    return -1


def _strip_leading_dash(line: str) -> str:
    if line.startswith("- "):
        return line[2:].strip()
    if line == "-":
        return ""
    return line.strip()


def _strip_comment_and_quotes(value: str) -> str:
    quote: str | None = None
    end = len(value)
    for i, ch in enumerate(value):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            continue
        if ch == "#":
            end = i
            break
    out = value[:end].strip()
    if len(out) >= 2 and (
        (out.startswith('"') and out.endswith('"')) or (out.startswith("'") and out.endswith("'"))
    ):
        out = out[1:-1]
    return out


def finding_to_error(f: SecretFinding) -> str:
    """Format a finding as a non-revealing error string (never the value)."""
    where = f.path if f.path else (f"line {f.line}" if f.line is not None else "<root>")
    return f"secret-shaped value at {where}: {f.reason}"


__all__ = [
    "SecretFinding",
    "scan_parsed_value",
    "scan_raw_yaml",
    "finding_to_error",
]
