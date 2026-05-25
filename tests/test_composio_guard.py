"""Tests for ``plugins/hermes-smd-trust/composio_guard.py``.

Ports the substantive coverage from
``ss-console/ai-employee/adapter/tests/test_composio_assertion.py``:

- Slug validation at guard construction (accepts valid, rejects invalid).
- ``classify_composio_connection_id`` decisions across every refusal case.
- ``ComposioConnectionGuard.assert_belongs`` raises the structured
  ``ComposioIsolationError`` with the expected attributes.
- ``verify_composio_response`` (the hook entry point) returns None for
  matching IDs, replacement payloads for missing / mismatched IDs, and
  passes through non-Composio tool names.
- Headline integration scenario: a cross-customer Composio response is
  refused with the structured replacement payload the audit plugin can
  observe via the standard ``post_tool_call`` hook path.

Audit emission is **not** tested here. Per AGENTS.md, trust does not call
into audit directly; the audit plugin observes the refusal downstream via
its own ``post_tool_call`` hook on the resulting error result. That cross-
plugin path is tested in the audit plugin's suite.
"""

import json

import pytest

from tests.conftest import load_plugin


def _load_composio_module():
    plugin = load_plugin("hermes-smd-trust")
    return plugin.composio_guard


# ---------------------------------------------------------------------------
# Slug validation at construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",
        "A",
        "ABC",
        "-leading-dash",
        "trailing-dash-",
        "has space",
        "has_underscore",
        "way-too-long-" + ("x" * 50),
        "x",
    ],
)
def test_slug_validation_rejects_invalid_slugs(bad_slug) -> None:
    mod = _load_composio_module()
    with pytest.raises(ValueError, match="composio guard slug"):
        mod.ComposioConnectionGuard(expected_slug=bad_slug)


@pytest.mark.parametrize(
    "good_slug",
    ["ab", "smd", "acme", "client-1", "client-1-prod", "a0", "0a", "a-b-c"],
)
def test_slug_validation_accepts_valid_slugs(good_slug) -> None:
    mod = _load_composio_module()
    mod.ComposioConnectionGuard(expected_slug=good_slug)


def test_prefix_helper_returns_expected_shape() -> None:
    mod = _load_composio_module()
    assert mod.composio_connection_id_for_slug_prefix("acme") == "conn_acme_"
    assert mod.composio_connection_id_for_slug_prefix("smith-pi-firm") == "conn_smith-pi-firm_"


def test_prefix_helper_rejects_invalid_slug() -> None:
    mod = _load_composio_module()
    with pytest.raises(ValueError, match="composio guard slug"):
        mod.composio_connection_id_for_slug_prefix("BAD-SLUG")


# ---------------------------------------------------------------------------
# classify_composio_connection_id — pure function
# ---------------------------------------------------------------------------


def test_classify_accepts_well_formed_own_slug_connection_id() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_acme_xyz-1234", "acme")
    assert decision.ok is True
    assert decision.found_slug == "acme"


def test_classify_accepts_long_dashed_slug() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_smith-pi-firm_abcd", "smith-pi-firm")
    assert decision.ok is True
    assert decision.found_slug == "smith-pi-firm"


def test_classify_rejects_empty_connection_id() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("", "acme")
    assert decision.ok is False
    assert decision.found_slug is None
    assert "empty connection id" in decision.reason


def test_classify_rejects_non_string_connection_id() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id(None, "acme")
    assert decision.ok is False
    assert "empty connection id" in decision.reason


def test_classify_rejects_unprefixed_connection_id() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("xyz-1234", "acme")
    assert decision.ok is False
    assert "does not match" in decision.reason


def test_classify_rejects_conn_prefix_without_slug_segment() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_xyz", "acme")
    assert decision.ok is False
    assert "does not match" in decision.reason


def test_classify_rejects_too_short_suffix() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_acme_abc", "acme")
    assert decision.ok is False
    assert "does not match" in decision.reason


def test_classify_rejects_foreign_slug() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_other_xyz-1234", "acme")
    assert decision.ok is False
    assert decision.found_slug == "other"
    assert "foreign customer slug" in decision.reason


def test_classify_rejects_uppercase_slug_segment() -> None:
    mod = _load_composio_module()
    decision = mod.classify_composio_connection_id("conn_ACME_xyz-1234", "acme")
    assert decision.ok is False
    assert "does not match" in decision.reason


# ---------------------------------------------------------------------------
# Guard — happy path
# ---------------------------------------------------------------------------


def test_guard_passes_through_own_slug_connection_id() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    guard.assert_belongs("conn_acme_xyz-1234")


def test_guard_exposes_expected_slug_property() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    assert guard.expected_slug == "acme"


# ---------------------------------------------------------------------------
# Guard — refusal paths
# ---------------------------------------------------------------------------


def test_guard_refuses_foreign_slug_connection_id() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    with pytest.raises(mod.ComposioIsolationError) as excinfo:
        guard.assert_belongs("conn_other_xyz-1234")
    err = excinfo.value
    assert err.violation_kind == "composio_connection_id"
    assert err.expected_slug == "acme"
    assert err.attempted_connection_id == "conn_other_xyz-1234"
    assert "foreign customer slug" in err.detail


def test_guard_refuses_malformed_connection_id() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    with pytest.raises(mod.ComposioIsolationError) as excinfo:
        guard.assert_belongs("not-a-real-composio-id")
    assert excinfo.value.violation_kind == "composio_connection_id"
    assert excinfo.value.attempted_connection_id == "not-a-real-composio-id"


def test_guard_refuses_empty_connection_id() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    with pytest.raises(mod.ComposioIsolationError, match="empty connection id"):
        guard.assert_belongs("")


def test_guard_refuses_non_string_connection_id() -> None:
    mod = _load_composio_module()
    guard = mod.ComposioConnectionGuard(expected_slug="acme")
    with pytest.raises(mod.ComposioIsolationError, match="empty connection id"):
        guard.assert_belongs(None)


# ---------------------------------------------------------------------------
# verify_composio_response — hook entry point
# ---------------------------------------------------------------------------


def test_verify_passes_non_composio_tool_through() -> None:
    mod = _load_composio_module()
    result = mod.verify_composio_response(
        "email_list_messages", json.dumps({"data": "anything"}), "conn_acme_xyz-1234"
    )
    assert result is None


def test_verify_accepts_matching_connection_id_in_dict_result() -> None:
    mod = _load_composio_module()
    result = mod.verify_composio_response(
        "composio.gmail.messages.list",
        {"connection_id": "conn_acme_xyz-1234", "data": []},
        "conn_acme_xyz-1234",
    )
    assert result is None


def test_verify_accepts_matching_connection_id_in_json_string_result() -> None:
    mod = _load_composio_module()
    payload = json.dumps({"connection_id": "conn_acme_xyz-1234", "data": []})
    result = mod.verify_composio_response(
        "composio.gmail.messages.list", payload, "conn_acme_xyz-1234"
    )
    assert result is None


def test_verify_refuses_missing_connection_id() -> None:
    mod = _load_composio_module()
    payload = json.dumps({"data": []})
    result = mod.verify_composio_response(
        "composio.gmail.messages.list", payload, "conn_acme_xyz-1234"
    )
    assert isinstance(result, str)
    body = json.loads(result)
    assert body["error"] == "composio_isolation_violation"
    assert "missing connection_id" in body["message"]


def test_verify_refuses_mismatched_connection_id() -> None:
    mod = _load_composio_module()
    payload = json.dumps({"connection_id": "conn_other_zzzz-1111", "data": []})
    result = mod.verify_composio_response(
        "composio.gmail.messages.list", payload, "conn_acme_xyz-1234"
    )
    assert isinstance(result, str)
    body = json.loads(result)
    assert body["error"] == "composio_isolation_violation"
    assert "mismatch" in body["message"]


def test_verify_refuses_composio_call_with_missing_expected() -> None:
    """A Composio tool result with no bound expected_connection_id is
    refused — defense in depth against unprovisioned callers."""
    mod = _load_composio_module()
    payload = json.dumps({"connection_id": "conn_acme_xyz-1234", "data": []})
    result = mod.verify_composio_response("composio.gmail.messages.list", payload, "")
    assert isinstance(result, str)
    body = json.loads(result)
    assert body["error"] == "composio_isolation_violation"


def test_verify_returns_none_for_non_json_non_composio_result() -> None:
    mod = _load_composio_module()
    # Non-Composio tool, non-JSON result — should pass through untouched.
    result = mod.verify_composio_response(
        "some_other_tool", "raw text not json", "conn_acme_xyz-1234"
    )
    assert result is None


# ---------------------------------------------------------------------------
# Headline cross-customer scenario
# ---------------------------------------------------------------------------


def test_cross_customer_composio_response_refused_with_replacement_payload() -> None:
    """The threat model: customer A's Machine bound to ``acme``; a
    misconfigured Composio response carries customer B's connection ID.
    The guard must replace the result with a refusal payload so the
    cross-tenant data never reaches the conversation.
    """
    mod = _load_composio_module()
    foreign_payload = json.dumps(
        {
            "connection_id": "conn_other-customer_zzzz-1111",
            "data": [{"subject": "leaked email"}],
        }
    )

    result = mod.verify_composio_response(
        "composio.gmail.messages.list",
        foreign_payload,
        "conn_acme_xyz-1234",
    )

    assert isinstance(result, str)
    body = json.loads(result)
    assert body["error"] == "composio_isolation_violation"
    # The leaked subject must NOT appear in the replacement payload.
    assert "leaked email" not in result


# ---------------------------------------------------------------------------
# Hook surface exception safety
# ---------------------------------------------------------------------------


def test_on_transform_tool_result_swallows_internal_exceptions(monkeypatch) -> None:
    """A raise inside composio_guard.verify_composio_response must not
    propagate out of the hook callback.
    """
    plugin = load_plugin("hermes-smd-trust")

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic guard failure")

    monkeypatch.setattr(plugin.composio_guard, "verify_composio_response", boom)
    result = plugin.on_transform_tool_result(
        tool_name="composio.gmail.messages.list",
        args={},
        result=json.dumps({"connection_id": "conn_acme_xyz-1234"}),
        task_id="t",
        session_id="s",
        tool_call_id="c",
        duration_ms=1,
    )
    # Exception-safe — None returned, hook does nothing.
    assert result is None
