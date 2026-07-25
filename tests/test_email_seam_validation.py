"""On-box validator coverage for the email-channel seam (ADR 0078 / spec D3+D5).

Two things under test in ``bootstrap.validate``:

  * Cross-repo PARITY (D5): the on-box validator must accept exactly what the
    ss-console TS validator accepts on the new blocks — persona ``send_as``
    (provider-neutral ``send_identity`` + legacy ``agentmail_identity``
    back-compat) and the Email connector ``msgraph_auth`` / ``poll_seconds``.
    The cases here mirror the ss-console suite (tests/customer-yaml-validator.test.ts).
  * D3 structural enforcement: an Email connector bound for INBOUND must use a
    seam adapter ({agentmail, msgraph}) — a channel that cannot be fenced cannot
    be bound. Scoped to inbound-bound Email so the frozen cross-repo parity
    fixtures (outbound-only softeria ``microsoft-graph`` Email) stay accepted.
"""

from __future__ import annotations

import copy

import yaml

from bootstrap.validate import validate_customer_yaml

_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_CLIENT = "11111111-2222-3333-4444-555555555555"


def _base() -> dict:
    """A minimal customer.yaml that validates clean, for targeted mutation."""
    cid = "smith-pi-firm"
    return {
        "customer_id": cid,
        "customer_name": "Smith PI Firm",
        "vertical": "law-firm",
        "fly_region": "lax",
        "hermes_ref": "v2026.5.7@a91a57fa5a13d516c38b07a141a9ce8a3daabeb0",
        "model": "claude-opus-4-7",
        "memory": {
            "d1_namespace": cid,
            "r2_vault_path": f"vaults/{cid}/",
            "vectorize_index": f"hermes-{cid}-vault",
        },
        "personas": [
            {
                "slug": "marcus",
                "name": "Marcus",
                "status": "active",
                "skills": [
                    {
                        "name": "triage_inbox",
                        "enabled": True,
                        "initiation": {"manual": False, "scheduled": False, "webhook": True},
                    }
                ],
            }
        ],
    }


def _valid_msgraph_auth() -> dict:
    return {
        "tenant_id": _TENANT,
        "client_id": _CLIENT,
        "mailbox": "operator@clientdomain.com",
        "secret_ref": "fly-secret:MSGRAPH_CLIENT_SECRET",
    }


def _errors(cfg: dict, tmp_path) -> list[str]:
    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return validate_customer_yaml(path)


def _with_msgraph_email(tmp_path, mutate=None) -> list[str]:
    cfg = _base()
    conn = {"adapter": "msgraph", "backend": "mcp:msgraph-mail", "enabled": True}
    auth = _valid_msgraph_auth()
    if mutate is not None:
        mutate(auth, conn)
    conn["msgraph_auth"] = auth
    cfg["connectors"] = {"Email": conn}
    return _errors(cfg, tmp_path)


def _with_send_as(tmp_path, send_as) -> list[str]:
    cfg = _base()
    cfg["personas"][0]["send_as"] = send_as
    return _errors(cfg, tmp_path)


# ---------------------------------------------------------------------------
# Sanity: the base fixture validates clean
# ---------------------------------------------------------------------------


def test_base_fixture_is_valid(tmp_path) -> None:
    assert _errors(_base(), tmp_path) == []


# ---------------------------------------------------------------------------
# connectors.Email msgraph_auth + poll_seconds (parity with ss-console D5)
# ---------------------------------------------------------------------------


def test_accepts_valid_msgraph_email(tmp_path) -> None:
    assert _with_msgraph_email(tmp_path) == []


def test_accepts_authored_poll_seconds(tmp_path) -> None:
    assert _with_msgraph_email(tmp_path, lambda _a, c: c.__setitem__("poll_seconds", 30)) == []


def test_requires_msgraph_auth_when_adapter_is_msgraph(tmp_path) -> None:
    cfg = _base()
    cfg["connectors"] = {"Email": {"adapter": "msgraph", "backend": "mcp:msgraph-mail"}}
    errors = _errors(cfg, tmp_path)
    assert any("connectors.Email.msgraph_auth" in e and "required" in e for e in errors)


def test_rejects_msgraph_auth_on_non_msgraph_adapter(tmp_path) -> None:
    cfg = _base()
    cfg["connectors"] = {
        "Email": {
            "adapter": "microsoft-graph",
            "backend": "mcp:softeria/ms-365-mcp-server",
            "msgraph_auth": _valid_msgraph_auth(),
        }
    }
    errors = _errors(cfg, tmp_path)
    assert any("connectors.Email.msgraph_auth" in e and "only valid" in e for e in errors)


def test_rejects_poll_seconds_on_non_msgraph_adapter(tmp_path) -> None:
    cfg = _base()
    cfg["connectors"] = {
        "Email": {
            "adapter": "microsoft-graph",
            "backend": "mcp:softeria/ms-365-mcp-server",
            "poll_seconds": 45,
        }
    }
    errors = _errors(cfg, tmp_path)
    assert any("connectors.Email.poll_seconds" in e and "only valid" in e for e in errors)


def test_rejects_bad_poll_seconds_under_msgraph(tmp_path) -> None:
    for bad in (0, -1, 2.5, "30", True):
        errors = _with_msgraph_email(
            tmp_path, lambda _a, c, b=bad: c.__setitem__("poll_seconds", b)
        )
        assert errors, f"poll_seconds={bad!r} should be rejected"
        assert any("poll_seconds" in e for e in errors)


def test_rejects_malformed_guids(tmp_path) -> None:
    for key in ("tenant_id", "client_id"):
        errors = _with_msgraph_email(tmp_path, lambda a, _c, k=key: a.__setitem__(k, "not-a-guid"))
        assert any(f"connectors.Email.msgraph_auth.{key}" in e for e in errors)


def test_rejects_bad_mailbox(tmp_path) -> None:
    errors = _with_msgraph_email(tmp_path, lambda a, _c: a.__setitem__("mailbox", "operator-no-at"))
    assert any("connectors.Email.msgraph_auth.mailbox" in e for e in errors)


def test_rejects_bad_secret_ref(tmp_path) -> None:
    for bad in ("infisical:/operator/x/y", "MSGRAPH_CLIENT_SECRET", "fly-secret:"):
        errors = _with_msgraph_email(tmp_path, lambda a, _c, b=bad: a.__setitem__("secret_ref", b))
        assert any("connectors.Email.msgraph_auth.secret_ref" in e for e in errors), bad


def test_rejects_partial_msgraph_auth(tmp_path) -> None:
    errors = _with_msgraph_email(tmp_path, lambda a, _c: a.pop("secret_ref"))
    assert errors


def test_secret_ref_field_name_is_not_banned(tmp_path) -> None:
    """Regression: ``secret_ref`` contains the banned substring 'secret' but is a
    reference channel (like ``token_ref``) — a valid msgraph config must NOT be
    rejected by the secret scanner (parity with ss-console)."""
    errors = _with_msgraph_email(tmp_path)
    assert not any("reserved for Infisical" in e for e in errors)


# ---------------------------------------------------------------------------
# persona send_as normalization (parity with ss-console §4)
# ---------------------------------------------------------------------------


def test_accepts_send_identity_msgraph(tmp_path) -> None:
    errors = _with_send_as(
        tmp_path, {"send_identity": {"provider": "msgraph", "address": "operator@clientdomain.com"}}
    )
    assert errors == []


def test_accepts_send_identity_agentmail(tmp_path) -> None:
    errors = _with_send_as(
        tmp_path,
        {"send_identity": {"provider": "agentmail", "address": "ops@firm.agents.smd.services"}},
    )
    assert errors == []


def test_accepts_legacy_agentmail_identity(tmp_path) -> None:
    errors = _with_send_as(tmp_path, {"agentmail_identity": "marcus@firm.agents.smd.services"})
    assert errors == []


def test_rejects_both_send_identity_and_legacy(tmp_path) -> None:
    errors = _with_send_as(
        tmp_path,
        {
            "send_identity": {"provider": "agentmail", "address": "a@b.c"},
            "agentmail_identity": "a@b.c",
        },
    )
    assert any(e.startswith("personas[0].send_as:") and "both" in e for e in errors)


def test_rejects_unknown_send_identity_provider(tmp_path) -> None:
    errors = _with_send_as(tmp_path, {"send_identity": {"provider": "gmail", "address": "a@b.c"}})
    assert any("personas[0].send_as.send_identity.provider" in e for e in errors)


def test_rejects_send_identity_missing_address(tmp_path) -> None:
    errors = _with_send_as(tmp_path, {"send_identity": {"provider": "msgraph"}})
    assert any("personas[0].send_as.send_identity.address" in e for e in errors)


def test_rejects_send_as_with_neither(tmp_path) -> None:
    errors = _with_send_as(tmp_path, {})
    assert any("personas[0].send_as.send_identity" in e for e in errors)


# ---------------------------------------------------------------------------
# D3 structural enforcement — Email seam adapter (scoped to inbound-bound)
# ---------------------------------------------------------------------------


def _email_with_inbound_trigger(adapter: str, backend: str) -> dict:
    cfg = _base()
    cfg["connectors"] = {"Email": {"adapter": adapter, "backend": backend}}
    cfg["webhook_triggers"] = [
        {
            "source": adapter,
            "event_type": "message.received",
            "skill": "triage_inbox",
            "persona": "marcus",
        }
    ]
    return cfg


def test_seam_accepts_agentmail_inbound_email(tmp_path) -> None:
    cfg = _email_with_inbound_trigger("agentmail", "mcp:agentmail")
    assert _errors(cfg, tmp_path) == []


def test_seam_accepts_msgraph_inbound_email(tmp_path) -> None:
    cfg = _email_with_inbound_trigger("msgraph", "mcp:msgraph-mail")
    cfg["connectors"]["Email"]["msgraph_auth"] = _valid_msgraph_auth()
    assert _errors(cfg, tmp_path) == []


def test_seam_accepts_non_seam_email_without_inbound_trigger(tmp_path) -> None:
    """Parity preservation: an outbound-only Email connector (no webhook trigger
    naming its adapter) is out of D3 scope — the softeria microsoft-graph binding
    stays accepted, matching the frozen cross-repo parity fixtures."""
    cfg = _base()
    cfg["connectors"] = {
        "Email": {"adapter": "microsoft-graph", "backend": "mcp:softeria/ms-365-mcp-server"}
    }
    assert _errors(cfg, tmp_path) == []


def test_seam_rejects_non_seam_email_bound_for_inbound(tmp_path) -> None:
    """D3: a non-seam Email adapter wired to carry inbound (a webhook_triggers
    source names it) cannot be fenced, so it must be rejected."""
    cfg = _email_with_inbound_trigger("microsoft-graph", "mcp:softeria/ms-365-mcp-server")
    errors = _errors(cfg, tmp_path)
    assert any(
        "connectors.Email.adapter" in e and "seam" in e and "cannot be fenced" in e for e in errors
    )


def test_seam_ignores_disabled_email_connector(tmp_path) -> None:
    cfg = _email_with_inbound_trigger("microsoft-graph", "mcp:softeria/ms-365-mcp-server")
    cfg["connectors"]["Email"]["enabled"] = False
    # A disabled Email connector carries no inbound; D3 does not fire on it.
    assert not any("cannot be fenced" in e for e in _errors(cfg, tmp_path))


def test_base_is_untouched_by_mutation_helpers() -> None:
    # Guard against accidental shared-state mutation across cases.
    a = _base()
    b = copy.deepcopy(a)
    a["personas"][0]["skills"][0]["name"] = "changed"
    assert b["personas"][0]["skills"][0]["name"] == "triage_inbox"
