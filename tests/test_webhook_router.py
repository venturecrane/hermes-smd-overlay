"""Tests for the hermes-smd-webhook-router plugin.

Covers:

  * Registration: ``register(ctx)`` wires ``pre_gateway_dispatch``.
  * Routing-table build from customer.yaml.
  * Missing / malformed customer.yaml -> empty table, register still
    completes, hook is registered, dispatches pass through.
  * Happy-path route: payload with matching markers returns the
    rewrite directive AND emits a WEBHOOK_ROUTED audit row.
  * No-match passthrough: payload with markers that don't match any
    trigger returns None.
  * Non-webhook payload: returns None.
  * Audit emission failure: routing still proceeds (mirror-don't-gate).
  * Two payload shapes accepted: top-level markers + nested under
    ``metadata``.
  * AGENTS.md hard rule #3: callbacks never raise.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

_SIGNING_SECRET = "topsecret-test-key"


def _sign(raw_body: bytes, secret: str = _SIGNING_SECRET, timestamp: str | None = None) -> str:
    signing_input = (f"{timestamp}.".encode() + raw_body) if timestamp else raw_body
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).hexdigest()


def signed_kwargs(
    payload: dict,
    *,
    secret: str = _SIGNING_SECRET,
    event_id: str = "evt-1",
    timestamp: str | None = None,
) -> dict:
    """Build a fully-verifiable on_pre_gateway_dispatch kwargs set.

    raw_body is the canonical JSON the router's fallback computes over,
    so the HMAC matches without the gateway passing verbatim bytes.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers = {
        "X-Webhook-Signature": _sign(raw, secret, timestamp),
        "X-Webhook-Id": event_id,
    }
    if timestamp is not None:
        headers["X-Webhook-Timestamp"] = timestamp
    return {"payload": payload, "raw_body": raw, "headers": headers}


def load_plugin(plugin_name: str):
    """Load a plugin package, replicating the conftest pattern."""
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake D1Client + customer.yaml fixtures
# ---------------------------------------------------------------------------


class FakeD1Client:
    def __init__(self, *, raise_on_execute: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._raise = raise_on_execute

    def execute(self, sql: str, *params) -> None:
        if self._raise is not None:
            raise self._raise
        self.calls.append((sql, tuple(params)))

    def rows(self) -> list[dict]:
        cols = [
            "id",
            "ts",
            "action_type",
            "actor",
            "actor_role",
            "skill_name",
            "matter_ref",
            "input_digest",
            "output_digest",
            "diff_digest",
            "trust_ceiling",
            "metadata",
        ]
        out: list[dict] = []
        for _sql, params in self.calls:
            out.append(dict(zip(cols, params, strict=False)))
        return out


YAML_WITH_TWO_TRIGGERS = dedent(
    """\
    schema_version: 1
    customer_id: acme
    webhook_triggers:
      - source: filevine
        event_type: matter.created
        skill: law-pi-intake-triage
        persona: marcus
      - source: filevine
        event_type: document.added
        skill: law-pi-discovery-response
        persona: marcus
    """
)


YAML_NO_TRIGGERS = dedent(
    """\
    schema_version: 1
    customer_id: acme
    """
)


@pytest.fixture
def with_customer_yaml(tmp_path, monkeypatch):
    """Return a writer that materializes a customer.yaml at a per-test
    path and points the router at it via env var."""

    def _write(body: str) -> Path:
        path = tmp_path / "customer.yaml"
        path.write_text(body)
        monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
        return path

    return _write


# ---------------------------------------------------------------------------
# Routing-table build
# ---------------------------------------------------------------------------


def test_router_build_from_customer_yaml_with_triggers(tmp_path):
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(YAML_WITH_TWO_TRIGGERS)
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.build_routing_table(customer_yaml)

    assert table.size() == 2
    intake = table.lookup("filevine", "matter.created")
    assert intake is not None
    assert intake.skill == "law-pi-intake-triage"
    assert intake.persona == "marcus"
    discovery = table.lookup("filevine", "document.added")
    assert discovery is not None
    assert discovery.skill == "law-pi-discovery-response"


def test_router_build_returns_empty_when_yaml_missing(tmp_path):
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.build_routing_table(tmp_path / "missing.yaml")
    assert table.size() == 0


def test_router_build_returns_empty_when_no_webhook_triggers_block(tmp_path):
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(YAML_NO_TRIGGERS)
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.build_routing_table(customer_yaml)
    assert table.size() == 0


def test_router_build_skips_malformed_entries(tmp_path):
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(
        dedent(
            """\
            webhook_triggers:
              - source: filevine
                event_type: matter.created
                skill: law-pi-intake-triage
                persona: marcus
              - source: filevine
                # missing event_type + skill + persona
              - "not a mapping"
            """
        )
    )
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.build_routing_table(customer_yaml)
    # Only the one well-formed entry survives.
    assert table.size() == 1
    assert table.lookup("filevine", "matter.created") is not None


def test_router_build_dedupes_on_source_event_key(tmp_path):
    """Duplicate (source, event_type) keys: first wins, later skipped."""
    customer_yaml = tmp_path / "customer.yaml"
    customer_yaml.write_text(
        dedent(
            """\
            webhook_triggers:
              - source: filevine
                event_type: matter.created
                skill: law-pi-intake-triage
                persona: marcus
              - source: filevine
                event_type: matter.created
                skill: law-pi-discovery-response  # would shadow
                persona: marcus
            """
        )
    )
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.build_routing_table(customer_yaml)
    assert table.size() == 1
    # First entry wins.
    assert table.lookup("filevine", "matter.created").skill == "law-pi-intake-triage"


# ---------------------------------------------------------------------------
# Webhook-marker detection + pure routing decision
# ---------------------------------------------------------------------------


def test_detect_markers_at_top_level():
    mod = load_plugin("hermes-smd-webhook-router")
    assert mod.router.detect_webhook_markers(
        {"source": "filevine", "event_type": "matter.created"}
    ) == ("filevine", "matter.created")


def test_detect_markers_under_metadata():
    mod = load_plugin("hermes-smd-webhook-router")
    assert mod.router.detect_webhook_markers(
        {"metadata": {"source": "filevine", "event_type": "document.added"}}
    ) == ("filevine", "document.added")


def test_detect_markers_returns_none_when_absent():
    mod = load_plugin("hermes-smd-webhook-router")
    assert mod.router.detect_webhook_markers({"user_message": "hi"}) is None
    assert mod.router.detect_webhook_markers("not-a-dict") is None
    assert mod.router.detect_webhook_markers(None) is None


def test_decide_route_match():
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.RoutingTable(
        entries={
            ("filevine", "matter.created"): mod.router.WebhookTrigger(
                source="filevine",
                event_type="matter.created",
                skill="law-pi-intake-triage",
                persona="marcus",
            )
        }
    )
    decision = mod.router.decide_route(
        table, {"source": "filevine", "event_type": "matter.created"}
    )
    assert decision.trigger is not None
    assert decision.trigger.skill == "law-pi-intake-triage"


def test_decide_route_marker_but_no_match():
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.RoutingTable.empty()
    decision = mod.router.decide_route(
        table, {"source": "filevine", "event_type": "matter.created"}
    )
    assert decision.trigger is None
    assert decision.matched_key == ("filevine", "matter.created")


def test_decide_route_no_markers():
    mod = load_plugin("hermes-smd-webhook-router")
    table = mod.router.RoutingTable.empty()
    decision = mod.router.decide_route(table, {"user_message": "hi"})
    assert decision.trigger is None
    assert decision.matched_key is None


# ---------------------------------------------------------------------------
# Hook callback - integration through register()
# ---------------------------------------------------------------------------


def test_register_wires_pre_gateway_dispatch(fake_ctx, monkeypatch, with_customer_yaml):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    assert "pre_gateway_dispatch" in fake_ctx.registered
    assert mod._TABLE.size() == 2


def test_register_no_ops_when_env_missing(fake_ctx, monkeypatch, with_customer_yaml):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.delenv("SMD_CUSTOMER_SLUG", raising=False)
    monkeypatch.delenv("SMD_D1_AUDIT_BINDING", raising=False)
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    # Hook still registered, table populated, but no audit client.
    assert "pre_gateway_dispatch" in fake_ctx.registered
    assert mod._D1_CLIENT is None


def test_on_pre_gateway_dispatch_returns_rewrite_on_match(
    fake_ctx, monkeypatch, with_customer_yaml
):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    monkeypatch.setenv("SMD_WEBHOOK_SIGNING_SECRET", _SIGNING_SECRET)
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)

    # Swap in the fake D1 client to capture the audit row.
    fake_client = FakeD1Client()
    mod._D1_CLIENT = fake_client

    payload = {
        "source": "filevine",
        "event_type": "matter.created",
        "matter_id": "matter-42",
    }
    result = mod.on_pre_gateway_dispatch(**signed_kwargs(payload))
    assert result is not None
    assert result["action"] == "route_to_skill"
    assert result["persona"] == "marcus"
    assert result["skill"] == "law-pi-intake-triage"
    assert result["payload"] is payload

    # Audit rows emitted: WEBHOOK_ROUTED (ADR 0021) + INBOUND_RECEIVED (ADR 0027).
    rows = fake_client.rows()
    assert len(rows) == 2
    action_types = {r["action_type"] for r in rows}
    assert action_types == {"WEBHOOK_ROUTED", "INBOUND_RECEIVED"}
    row = next(r for r in rows if r["action_type"] == "WEBHOOK_ROUTED")
    assert row["skill_name"] == "law-pi-intake-triage"
    md = json.loads(row["metadata"])
    assert md["source"] == "filevine"
    assert md["event_type"] == "matter.created"
    assert md["persona"] == "marcus"
    assert md["skill"] == "law-pi-intake-triage"
    # The ADR 0027 envelope is attached to the dispatch directive.
    assert result["inbound_envelope"]["trust_class"] == "unknown_external"
    assert result["inbound_envelope"]["verification"] == "verified"


def test_on_pre_gateway_dispatch_passthrough_on_no_marker(
    fake_ctx, monkeypatch, with_customer_yaml
):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    fake_client = FakeD1Client()
    mod._D1_CLIENT = fake_client

    result = mod.on_pre_gateway_dispatch(payload={"user_message": "hi"})
    assert result is None
    assert fake_client.calls == []  # no audit row for non-webhook payload


def test_on_pre_gateway_dispatch_passthrough_on_no_match(fake_ctx, monkeypatch, with_customer_yaml):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    fake_client = FakeD1Client()
    mod._D1_CLIENT = fake_client

    # Markers present but not in the routing table.
    result = mod.on_pre_gateway_dispatch(payload={"source": "clio", "event_type": "matter.created"})
    assert result is None
    assert fake_client.calls == []


def test_on_pre_gateway_dispatch_routes_on_metadata_wrapped_payload(
    fake_ctx, monkeypatch, with_customer_yaml
):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    monkeypatch.setenv("SMD_WEBHOOK_SIGNING_SECRET", _SIGNING_SECRET)
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    fake_client = FakeD1Client()
    mod._D1_CLIENT = fake_client

    result = mod.on_pre_gateway_dispatch(
        **signed_kwargs(
            {
                "metadata": {"source": "filevine", "event_type": "document.added"},
                "doc_id": "doc-7",
            }
        )
    )
    assert result is not None
    assert result["skill"] == "law-pi-discovery-response"


def test_on_pre_gateway_dispatch_route_succeeds_when_audit_writer_fails(
    fake_ctx, monkeypatch, with_customer_yaml
):
    """Per ADR 0016 mirror-don't-gate: audit failure must NOT block routing."""
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    monkeypatch.setenv("SMD_WEBHOOK_SIGNING_SECRET", _SIGNING_SECRET)
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    boom = FakeD1Client(raise_on_execute=RuntimeError("D1 unreachable"))
    mod._D1_CLIENT = boom

    payload = {"source": "filevine", "event_type": "matter.created"}
    result = mod.on_pre_gateway_dispatch(**signed_kwargs(payload))
    # Route still applied despite audit failure.
    assert result is not None
    assert result["skill"] == "law-pi-intake-triage"


def test_on_pre_gateway_dispatch_no_ops_on_empty_table(fake_ctx, monkeypatch, tmp_path):
    """No routing config = no routing, no work, no exception."""
    # Customer.yaml exists but has no webhook_triggers.
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(YAML_NO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    result = mod.on_pre_gateway_dispatch(
        payload={"source": "filevine", "event_type": "matter.created"}
    )
    assert result is None


def test_on_pre_gateway_dispatch_never_raises(fake_ctx, monkeypatch, with_customer_yaml):
    """AGENTS.md hard rule #3: callbacks must not raise."""
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)

    # Set up a router that raises on decide_route by monkeypatching.
    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("router internals exploded")

    monkeypatch.setattr(mod.router, "decide_route", boom)

    # Must NOT raise.
    result = mod.on_pre_gateway_dispatch(payload={"any": "shape"})
    assert result is None


# ---------------------------------------------------------------------------
# Inbound webhook verification (issue #13)
# ---------------------------------------------------------------------------


def _registered_router(fake_ctx, monkeypatch, with_customer_yaml, *, with_secret: bool):
    with_customer_yaml(YAML_WITH_TWO_TRIGGERS)
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_D1_AUDIT_BINDING", "CUSTOMER_DB")
    if with_secret:
        monkeypatch.setenv("SMD_WEBHOOK_SIGNING_SECRET", _SIGNING_SECRET)
    else:
        monkeypatch.delenv("SMD_WEBHOOK_SIGNING_SECRET", raising=False)
    mod = load_plugin("hermes-smd-webhook-router")
    mod.register(fake_ctx)
    mod._D1_CLIENT = FakeD1Client()
    return mod


_MATCH_PAYLOAD = {"source": "filevine", "event_type": "matter.created"}


def test_refuses_to_route_when_no_signing_secret(fake_ctx, monkeypatch, with_customer_yaml):
    """No signing secret = cannot verify = must not route (fail closed)."""
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=False)
    assert mod._SIGNING_SECRET is None
    # Even a well-formed, matching payload does not route without a secret.
    result = mod.on_pre_gateway_dispatch(payload=dict(_MATCH_PAYLOAD))
    assert result is None
    assert mod._D1_CLIENT.calls == []


def test_refuses_to_route_on_missing_signature(fake_ctx, monkeypatch, with_customer_yaml):
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    # Matching payload but no signature header at all.
    result = mod.on_pre_gateway_dispatch(payload=dict(_MATCH_PAYLOAD), headers={})
    assert result is None
    assert mod._D1_CLIENT.calls == []


def test_refuses_to_route_on_bad_signature(fake_ctx, monkeypatch, with_customer_yaml):
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    payload = dict(_MATCH_PAYLOAD)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = mod.on_pre_gateway_dispatch(
        payload=payload,
        raw_body=raw,
        headers={"X-Webhook-Signature": "deadbeef", "X-Webhook-Id": "evt-1"},
    )
    assert result is None
    assert mod._D1_CLIENT.calls == []


def test_refuses_to_route_on_forged_payload_same_signature(
    fake_ctx, monkeypatch, with_customer_yaml
):
    """A valid signature for one body must not validate a tampered body."""
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    # Sign the original, then tamper the routed payload's raw_body.
    signed = signed_kwargs(dict(_MATCH_PAYLOAD), event_id="evt-1")
    signed["raw_body"] = signed["raw_body"] + b'{"tamper":true}'
    result = mod.on_pre_gateway_dispatch(**signed)
    assert result is None


def test_refuses_to_route_on_stale_timestamp(fake_ctx, monkeypatch, with_customer_yaml):
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    # Timestamp far in the past — signature valid but outside the window.
    stale = signed_kwargs(dict(_MATCH_PAYLOAD), event_id="evt-1", timestamp="1000000000")
    result = mod.on_pre_gateway_dispatch(**stale)
    assert result is None


def test_refuses_to_route_on_replayed_event_id(fake_ctx, monkeypatch, with_customer_yaml):
    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    kwargs = signed_kwargs(dict(_MATCH_PAYLOAD), event_id="evt-replay")
    # First delivery routes.
    first = mod.on_pre_gateway_dispatch(**kwargs)
    assert first is not None
    assert first["skill"] == "law-pi-intake-triage"
    # Identical replay is rejected.
    second = mod.on_pre_gateway_dispatch(**kwargs)
    assert second is None


def test_routes_on_valid_signed_timestamped_webhook(fake_ctx, monkeypatch, with_customer_yaml):
    import time

    mod = _registered_router(fake_ctx, monkeypatch, with_customer_yaml, with_secret=True)
    fresh_ts = str(int(time.time()))
    kwargs = signed_kwargs(dict(_MATCH_PAYLOAD), event_id="evt-fresh", timestamp=fresh_ts)
    result = mod.on_pre_gateway_dispatch(**kwargs)
    assert result is not None
    assert result["skill"] == "law-pi-intake-triage"
    # WEBHOOK_ROUTED (ADR 0021) + INBOUND_RECEIVED (ADR 0027).
    assert len(mod._D1_CLIENT.calls) == 2


# ---------------------------------------------------------------------------
# verify.py unit tests
# ---------------------------------------------------------------------------


def test_verify_signature_roundtrip():
    mod = load_plugin("hermes-smd-webhook-router")
    verify = mod.verify
    body = b'{"a":1}'
    sig = verify.compute_signature(_SIGNING_SECRET, body)
    # Valid signature passes (no raise).
    verify.verify_signature(secret=_SIGNING_SECRET, raw_body=body, signature=sig)
    # Wrong secret fails.
    with pytest.raises(verify.WebhookVerificationError):
        verify.verify_signature(secret="other", raw_body=body, signature=sig)


def test_verify_signature_accepts_sha256_prefix():
    mod = load_plugin("hermes-smd-webhook-router")
    verify = mod.verify
    body = b"payload"
    sig = verify.compute_signature(_SIGNING_SECRET, body)
    verify.verify_signature(secret=_SIGNING_SECRET, raw_body=body, signature=f"sha256={sig}")


def test_verify_signature_rejects_missing_secret_and_signature():
    mod = load_plugin("hermes-smd-webhook-router")
    verify = mod.verify
    with pytest.raises(verify.WebhookVerificationError, match="signing secret"):
        verify.verify_signature(secret=None, raw_body=b"x", signature="abc")
    with pytest.raises(verify.WebhookVerificationError, match="signature"):
        verify.verify_signature(secret="s", raw_body=b"x", signature=None)


def test_replay_cache_detects_duplicates():
    mod = load_plugin("hermes-smd-webhook-router")
    cache = mod.verify.ReplayCache(ttl_seconds=100)
    assert cache.check_and_record("e1", now=0.0) is True
    assert cache.check_and_record("e1", now=1.0) is False  # replay
    # After TTL elapses the id is forgotten and accepted again.
    assert cache.check_and_record("e1", now=200.0) is True


# ---------------------------------------------------------------------------
# ACCEPTED_ACTION_TYPES schema additions
# ---------------------------------------------------------------------------


def test_webhook_routed_in_accepted_action_types():
    audit_mod = load_plugin("hermes-smd-audit")
    assert "WEBHOOK_ROUTED" in audit_mod.schemas.ACCEPTED_ACTION_TYPES
