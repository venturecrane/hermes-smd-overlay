"""Tests for the Machine-hosted MCP Clerk validator (shared/mcp_auth.py).

Signs real RS256 tokens with a throwaway keypair and stubs the JWKS fetch, so
every validation path is exercised against genuine JWT verification — the same
checks the console's token-validation.ts enforces.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shared import mcp_auth
from shared.mcp_auth import McpAccessEntry, McpAuthBinding, McpAuthError, McpPrincipal

_ISSUER = "https://clerk.smd.services"
_RESOURCE = "https://hermes-smd.fly.dev/mcp"
_SUB = "user_3E1RPGrTMxkSqciXMTyybUNSJWu"


@pytest.fixture(scope="module")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _stub_jwks(keypair, monkeypatch):
    """Make the JWKS lookup return our public key (no network)."""

    class _Stub:
        def get_signing_key_from_jwt(self, _token):
            return type("K", (), {"key": keypair.public_key()})()

    monkeypatch.setattr(mcp_auth, "_jwks_client", lambda _issuer: _Stub())


def _token(keypair, **overrides) -> str:
    claims = {
        "sub": _SUB,
        "iss": _ISSUER,
        "aud": _RESOURCE,
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, keypair, algorithm="RS256")


def _binding(**overrides) -> McpAuthBinding:
    base = {
        "issuer": _ISSUER,
        "resource_uri": _RESOURCE,
        "clerk_org_id": None,
        "enabled": True,
        "access": (McpAccessEntry("scott@smd.services", "crane", _SUB),),
    }
    base.update(overrides)
    return McpAuthBinding(**base)


def test_valid_token_resolves_principal(keypair):
    result = mcp_auth.validate_mcp_token(_token(keypair), _binding())
    assert isinstance(result, McpPrincipal)
    assert result.profile == "crane"
    assert result.email == "scott@smd.services"
    assert result.subject == _SUB


def test_wrong_audience_rejected(keypair):
    # A token minted for the CONSOLE resource must not validate at the Machine.
    tok = _token(keypair, aud="https://smd.services/api/operator/smd/mcp")
    result = mcp_auth.validate_mcp_token(tok, _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.WRONG_AUDIENCE


def test_audience_list_membership(keypair):
    # aud may be an array; resource must be IN it.
    tok = _token(keypair, aud=[_RESOURCE, "https://other"])
    assert isinstance(mcp_auth.validate_mcp_token(tok, _binding()), McpPrincipal)


def test_wrong_issuer_rejected(keypair):
    tok = _token(keypair, iss="https://evil.example.com")
    result = mcp_auth.validate_mcp_token(tok, _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.WRONG_ISSUER


def test_subject_not_authored_rejected(keypair):
    tok = _token(keypair, sub="user_someoneelse")
    result = mcp_auth.validate_mcp_token(tok, _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.IDENTITY_NOT_AUTHORED


def test_connector_disabled_rejected(keypair):
    result = mcp_auth.validate_mcp_token(_token(keypair), _binding(enabled=False))
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.CONNECTOR_DISABLED


def test_org_mismatch_rejected(keypair):
    tok = _token(keypair, org_id="org_wrong")
    result = mcp_auth.validate_mcp_token(tok, _binding(clerk_org_id="org_right"))
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.ORGANIZATION_MISMATCH


def test_bad_signature_rejected(keypair):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = jwt.encode(
        {"sub": _SUB, "iss": _ISSUER, "aud": _RESOURCE, "exp": int(time.time()) + 3600},
        other,
        algorithm="RS256",
    )
    result = mcp_auth.validate_mcp_token(bad, _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.SIGNATURE_INVALID


def test_missing_token_rejected():
    result = mcp_auth.validate_mcp_token(None, _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.MISSING_TOKEN


def test_non_jwt_rejected():
    result = mcp_auth.validate_mcp_token("not-a-jwt", _binding())
    assert isinstance(result, McpAuthError) and result.reason == mcp_auth.TOKEN_NOT_JWT


def test_extract_bearer_token():
    assert mcp_auth.extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    assert mcp_auth.extract_bearer_token("bearer abc") == "abc"  # case-insensitive scheme
    assert mcp_auth.extract_bearer_token("  Bearer   tok  ") == "tok"
    assert mcp_auth.extract_bearer_token(None) is None
    assert mcp_auth.extract_bearer_token("Basic abc") is None
