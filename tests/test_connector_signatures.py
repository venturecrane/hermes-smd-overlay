"""Fixture tests for the conn-class error signatures (shared/connector_signatures.py).

These fixtures ARE the contract: real error strings captured from the pinned
Hermes transport layer and our typed connector clients. The Hermes pin-bump
checklist re-verifies the three transport strings against the new pin; a
format change in either typed client must update the fixture here in the
same PR.
"""

from __future__ import annotations

import pytest

from shared.connector_signatures import is_conn_class

# --- real shapes that MUST classify as connection-class --------------------

CONN_CLASS_FIXTURES = [
    # Hermes transport layer (tools/mcp_tool.py @ v2026.7.1)
    "MCP server 'smokeball' is not connected",
    "MCP server 'agentmail' transport is down; reconnect requested",
    "MCP call failed: BrokenPipeError(32, 'Broken pipe')",
    # Typed-client API errors ("<vendor> <method> <path> -> HTTP <status>: <body>")
    "Smokeball GET /matters -> HTTP 401: (empty body)",
    "Smokeball GET /matters -> HTTP 503: upstream unavailable",
    "MSGraph POST https://graph.microsoft.com/v1.0/me/sendMail -> HTTP 429: throttled",
    "MSGraph GET https://graph.microsoft.com/v1.0/me/messages -> HTTP 500: internal",
    # Token-mint / auth failures (both clients)
    "token request to https://auth.smokeball.com failed: timeout",
    "token mint (client_credentials) rejected with HTTP 400 at https://auth.smokeball.com/oauth2/token",
    "token response had no access_token",
    # httpx / OS transport strings
    "HTTPSConnectionPool(host='api.smokeball.com', port=443): Read timed out.",
    "[Errno 111] Connection refused",
    "All connection attempts failed",
    "[Errno -2] Name or service not known",
    "getaddrinfo failed",
]

# --- shapes that must NOT classify (business errors, derivative strings) ---

BUSINESS_FIXTURES = [
    # Hermes' own breaker string is DERIVATIVE (bumps on any error result,
    # mcp_tool.py:3446-3455) — three business errors manufacture it. Never
    # add it to the signature set.
    "MCP server 'smokeball' is unreachable after 3 consecutive failures; retrying in 60s",
    # Business-class HTTP statuses
    "Smokeball GET /matters/xyz -> HTTP 404: matter not found",
    "Smokeball POST /matters -> HTTP 400: missing required field 'name'",
    # Bare digits inside business text must not match (the anchored-format rule)
    "query returned 50012 rows which exceeds the limit",
    "invalid staffId 429 for this matter",
    "ValueError: matter_id is required",
    "",
]


@pytest.mark.parametrize("message", CONN_CLASS_FIXTURES)
def test_conn_class_fixtures_match(message):
    assert is_conn_class(message) is True


@pytest.mark.parametrize("message", BUSINESS_FIXTURES)
def test_business_fixtures_do_not_match(message):
    assert is_conn_class(message) is False


def test_non_string_inputs_are_never_evidence():
    assert is_conn_class(None) is False
    assert is_conn_class(42) is False
    assert is_conn_class({"error": "is not connected"}) is False
