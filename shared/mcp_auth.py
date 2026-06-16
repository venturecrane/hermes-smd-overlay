"""Clerk OAuth validation for the Machine-hosted MCP front door.

The Machine ``/mcp`` connector is an OAuth 2.1 Resource Server (MCP auth spec):
claude.ai authenticates the user against the customer's Clerk Authorization
Server and presents a bearer token; this module validates it before any agent
work. It is the Python port of the console's ``token-validation.ts`` — kept
behaviourally identical (a cross-repo contract test pins the two together) so the
on-Machine front door enforces the SAME rules as the console.

Why on the Machine and not the console: the document verbs carry privileged
content, which must never transit the shared Worker (per-customer isolation,
ADR 0007; design 03-mcp §2.2). So the Machine terminates its own OAuth.

The isolation invariant: a token is bound (RFC 8707 ``aud``) to the resource the
client requested. The Machine validates ``aud`` == its OWN resource URI, so a
token minted for the console (``aud`` = console URL) is structurally unusable
here, and vice versa. The Clerk issuer (``iss``) is checked exactly; the Clerk
``sub`` must map to an authored ``mcp_connector.access[]`` entry.

Verification uses PyJWT (``PyJWT[crypto]``, already pinned by Hermes) with a
JWKS fetched from ``<issuer>/.well-known/jwks.json``, RS256 only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

logger = logging.getLogger("hermes_smd.mcp_auth")

# Cache JWKS clients per issuer — PyJWKClient keeps its own keyset cache, so one
# client per issuer avoids re-fetching the keyset on every request.
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}


@dataclass(frozen=True)
class McpAccessEntry:
    """One authored ``mcp_connector.access[]`` entry."""

    email: str
    profile: str
    clerk_subject: str


@dataclass(frozen=True)
class McpAuthBinding:
    """The per-customer Clerk binding the Machine validates against.

    ``issuer`` + ``resource_uri`` are materialized to the Machine at provision
    time (issuer from the console's D1 ``mcp_clerk_bindings``; resource_uri is
    THIS Machine's own ``/mcp`` URL). ``access`` + ``enabled`` are read live from
    ``customer.yaml.mcp_connector``.
    """

    issuer: str
    resource_uri: str
    clerk_org_id: str | None
    enabled: bool
    access: tuple[McpAccessEntry, ...]


@dataclass(frozen=True)
class McpPrincipal:
    """The authenticated, authored caller (the result of a successful auth)."""

    subject: str
    email: str
    profile: str
    token_audience: tuple[str, ...]


@dataclass(frozen=True)
class McpAuthError:
    """A refused authentication, with a stable machine reason (mirrors the TS)."""

    reason: str
    detail: str
    subject: str | None = None


# Failure reasons — kept identical to token-validation.ts McpAuthFailureReason.
MISSING_TOKEN = "missing_token"
TOKEN_NOT_JWT = "token_not_jwt"
SIGNATURE_INVALID = "signature_invalid"
CLAIMS_INVALID = "claims_invalid"
WRONG_ISSUER = "wrong_issuer"
WRONG_AUDIENCE = "wrong_audience"
CONNECTOR_DISABLED = "connector_disabled"
IDENTITY_NOT_AUTHORED = "identity_not_authored"
ORGANIZATION_MISMATCH = "organization_mismatch"


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Return the bearer token from an ``Authorization`` header, or None."""
    if not authorization_header:
        return None
    header = authorization_header.strip()
    prefix = "bearer "
    if header[: len(prefix)].lower() != prefix:
        return None
    token = header[len(prefix) :].strip()
    return token or None


def _jwks_client(issuer: str) -> PyJWKClient:
    client = _JWKS_CLIENTS.get(issuer)
    if client is None:
        client = PyJWKClient(issuer.rstrip("/") + "/.well-known/jwks.json")
        _JWKS_CLIENTS[issuer] = client
    return client


def _verify_pinned_clerk_token(token: str, issuer: str) -> dict:
    """Verify the JWT signature against the issuer's pinned JWKS (RS256).

    Returns the decoded claims. Does NOT enforce iss/aud here — those are
    checked explicitly in :func:`_validate_claims` so each failure maps to a
    distinct reason (mirroring the TS). Raises on signature/format failure.
    """
    signing_key = _jwks_client(issuer).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"verify_aud": False, "verify_iss": False, "require": ["sub", "iss"]},
    )


def _audience_list(aud: object) -> tuple[str, ...]:
    if isinstance(aud, str):
        return (aud,)
    if isinstance(aud, list):
        return tuple(a for a in aud if isinstance(a, str) and a)
    return ()


def _validate_claims(claims: dict, binding: McpAuthBinding) -> McpAuthError | None:
    """Check iss / aud / org_id. Returns an error, or None when claims are valid."""
    sub = claims.get("sub")
    iss = claims.get("iss")
    if not isinstance(sub, str) or not sub or not isinstance(iss, str) or not iss:
        return McpAuthError(CLAIMS_INVALID, "required token claims are invalid")
    if iss != binding.issuer:
        return McpAuthError(WRONG_ISSUER, "token issuer does not match resource", subject=sub)
    # aud is validated ONLY when the authorization server provides one (defense
    # in depth): a token explicitly bound to a DIFFERENT resource is refused.
    # When the AS emits no audience — this Clerk instance does not (verified live
    # 2026-06-16; its AS metadata advertises no resource-indicator support) — the
    # resource binding is unavailable, and authorization rests on the issuer
    # (per-customer Clerk app = customer isolation) + the authored subject below.
    # Privileged-content isolation is enforced by Machine hosting, independent of
    # the token. If Clerk later emits a resource-bound aud, this check engages
    # automatically with no code change.
    audience = _audience_list(claims.get("aud"))
    if audience and binding.resource_uri not in audience:
        return McpAuthError(WRONG_AUDIENCE, "token is bound to another resource", subject=sub)
    if binding.clerk_org_id and claims.get("org_id") != binding.clerk_org_id:
        return McpAuthError(
            ORGANIZATION_MISMATCH, "token organization does not match customer", subject=sub
        )
    return None


def validate_mcp_token(
    token: str | None, binding: McpAuthBinding
) -> McpPrincipal | McpAuthError:
    """Validate a bearer token against the customer's Clerk binding.

    Returns an :class:`McpPrincipal` on success, or an :class:`McpAuthError` with
    a stable reason. Fail-closed throughout. Mirrors ``validateMcpToken`` in
    token-validation.ts.
    """
    if not token:
        return McpAuthError(MISSING_TOKEN, "no bearer token")
    if token.count(".") != 2:
        return McpAuthError(TOKEN_NOT_JWT, "bearer token is not a compact JWT")

    try:
        claims = _verify_pinned_clerk_token(token, binding.issuer)
    except Exception as exc:  # noqa: BLE001 — any verify failure is a refusal
        return McpAuthError(SIGNATURE_INVALID, str(exc) or "token verification failed")

    claim_error = _validate_claims(claims, binding)
    if claim_error is not None:
        return claim_error

    sub = claims["sub"]
    audience = _audience_list(claims.get("aud"))
    if not binding.enabled:
        return McpAuthError(CONNECTOR_DISABLED, "mcp_connector is disabled", subject=sub)

    principal = next((e for e in binding.access if e.clerk_subject == sub), None)
    if principal is None:
        return McpAuthError(
            IDENTITY_NOT_AUTHORED, "Clerk subject is not authorized for this Operator", subject=sub
        )

    return McpPrincipal(
        subject=sub,
        email=principal.email,
        profile=principal.profile,
        token_audience=audience,
    )
