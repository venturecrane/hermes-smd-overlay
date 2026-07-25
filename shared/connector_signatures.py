"""Connection-class error signatures for connector-outage detection (ss#1990).

The connector-health plugin counts EVERY tool error toward a per-server
consecutive-failure run, but the fast alert path additionally requires
connection-class evidence inside the current run — otherwise an agent
retrying a malformed write would page "connector down". This module is the
ONE place those signatures live, with fixture-backed tests, so the
Hermes-pin-bump checklist has a single grep target.

Signature sources, in order:

1. Hermes transport strings (pinned ``tools/mcp_tool.py`` at v2026.7.1
   @ 7c1a0295: "is not connected" / "transport is down" at :3358/:3375,
   the ``"MCP call failed:"`` exception-wrap prefix). Re-verify these on
   every Hermes pin bump.
2. Our typed build-adapter clients (smokeball / msgraph-mail), which both
   format API failures as ``"<vendor> <method> <path> -> HTTP <status>: <body>"``
   and token-mint failures with the auth markers below. Anchored to those
   exact shapes — never bare digit substrings, which would match record
   ids and byte counts inside business error text.
3. httpx / OS transport strings (best-effort extra coverage; the
   signature-free backstop condition catches anything these miss).

Deliberately EXCLUDED:

* Hermes' circuit-breaker string ("unreachable after N consecutive
  failures") — source-verified that the breaker bumps on ANY ``{"error":..}``
  result (mcp_tool.py:3446-3455), so three consecutive *business* errors
  manufacture it. It is derivative, not evidence. Do not add it.
* ``-> HTTP 400`` / ``-> HTTP 404`` and friends — business-class (bad
  request / missing record), not connection-class.
"""

from __future__ import annotations

import re

# Typed-client HTTP failures: "... -> HTTP 503: <body>". Connection-class
# statuses only: auth (401/403/407), timeout-ish (408), throttle (429), 5xx.
_HTTP_CONN_RE = re.compile(r"->\s*HTTP\s*(?:401|403|407|408|429|5\d\d)\b")

# Hermes transport layer (pin v2026.7.1 — re-grep on pin bump).
HERMES_TRANSPORT_MARKERS: tuple[str, ...] = (
    "is not connected",
    "transport is down",
    "MCP call failed:",
)

# Token-mint / auth failures from the typed clients (both connectors share
# these phrasings; smokeball client.py:211/242/249, msgraph client.py:191-203).
AUTH_MARKERS: tuple[str, ...] = (
    "rejected with HTTP",
    "token request to",
    "token response had no access_token",
)

# httpx / httpcore / OS-level transport failures. Best-effort: stable across
# releases in practice, and non-load-bearing (the backstop pages without them).
TRANSPORT_OS_MARKERS: tuple[str, ...] = (
    "timed out",
    "Connection refused",
    "All connection attempts failed",
    "Name or service not known",
    "getaddrinfo failed",
)

_SUBSTRING_MARKERS: tuple[str, ...] = HERMES_TRANSPORT_MARKERS + AUTH_MARKERS + TRANSPORT_OS_MARKERS


def is_conn_class(error_message: object) -> bool:
    """True when ``error_message`` carries connection-class evidence.

    Anything non-string or empty is not evidence (never guess). Substring
    membership is intentional — messages arrive wrapped in arbitrary
    prefixes (tool text, JSON error envelopes) — but every marker is
    anchored to a distinctive phrase, not a bare number.
    """
    if not isinstance(error_message, str) or not error_message:
        return False
    if _HTTP_CONN_RE.search(error_message):
        return True
    return any(marker in error_message for marker in _SUBSTRING_MARKERS)


__all__ = [
    "AUTH_MARKERS",
    "HERMES_TRANSPORT_MARKERS",
    "TRANSPORT_OS_MARKERS",
    "is_conn_class",
]
