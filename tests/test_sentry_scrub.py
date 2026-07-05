"""Regression suite for the LOCKED Sentry PII scrub (ADR 0023 decision #11).

This suite is the merge gate that keeps a refactor from re-opening a leak. It
runs WITHOUT ``sentry-sdk`` installed: the scrub functions are pure, and the one
init-contract test injects a fake ``sentry_sdk`` module so we can assert the
locked init kwargs (``send_default_pii=False``, the two scrub hooks, no tracing)
without the real dependency.

If you change ``shared/sentry_init.py``'s scrub policy, change this suite in the
same PR — that is the point of it.
"""

from __future__ import annotations

import sys
import types

import pytest

from shared import sentry_init
from shared.sentry_init import (
    SENSITIVE_HEADERS,
    init_sentry,
    redact_text,
    scrub_breadcrumb,
    scrub_event,
)

# ---------------------------------------------------------------------------
# redact_text — email + provider key shapes
# ---------------------------------------------------------------------------


def test_redact_text_email() -> None:
    assert redact_text("ping owner@example.com now") == "ping [redacted-email] now"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-abcdefghijklmnopqrstuvwxyz0123",
        "sk-abcdefghijklmnopqrstuvwx",
        "pk_live_abcdefghijklmnopqrstuvwx",
        "pk_test_abcdefghijklmnopqrstuvwx",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_redact_text_key_shapes(secret: str) -> None:
    out = redact_text(f"leaked {secret} here")
    assert secret not in out
    assert "[redacted-key]" in out


def test_redact_text_empty_is_noop() -> None:
    assert redact_text("") == ""


def test_redact_text_plain_untouched() -> None:
    assert redact_text("a normal log line") == "a normal log line"


# ---------------------------------------------------------------------------
# scrub_event — request body, headers, message, exception, breadcrumbs
# ---------------------------------------------------------------------------


def test_request_body_dropped_entirely() -> None:
    event = {"request": {"data": {"password": "hunter2", "ssn": "123-45-6789"}}}
    out = scrub_event(event)
    assert "data" not in out["request"]


@pytest.mark.parametrize("header", sorted(SENSITIVE_HEADERS))
def test_sensitive_headers_redacted_case_insensitive(header: str) -> None:
    event = {"request": {"headers": {header.upper(): "secret-value"}}}
    out = scrub_event(event)
    assert out["request"]["headers"][header.upper()] == "[redacted]"


def test_non_sensitive_headers_preserved() -> None:
    event = {"request": {"headers": {"content-type": "application/json"}}}
    out = scrub_event(event)
    assert out["request"]["headers"]["content-type"] == "application/json"


def test_message_email_redacted() -> None:
    out = scrub_event({"message": "failed for user@corp.com"})
    assert out["message"] == "failed for [redacted-email]"


def test_logentry_message_redacted() -> None:
    out = scrub_event({"logentry": {"message": "token sk-abcdefghijklmnopqrstuvwx leaked"}})
    assert "sk-abcdefghijklmnopqrstuvwx" not in out["logentry"]["message"]


def test_exception_value_redacted() -> None:
    event = {"exception": {"values": [{"value": "boom at admin@x.io with AKIAIOSFODNN7EXAMPLE"}]}}
    out = scrub_event(event)
    val = out["exception"]["values"][0]["value"]
    assert "admin@x.io" not in val
    assert "AKIAIOSFODNN7EXAMPLE" not in val


def test_embedded_breadcrumbs_redacted() -> None:
    event = {"breadcrumbs": {"values": [{"message": "sent to a@b.com"}]}}
    out = scrub_event(event)
    assert out["breadcrumbs"]["values"][0]["message"] == "sent to [redacted-email]"


def test_scrub_event_never_raises_on_malformed() -> None:
    # Non-dict request/exception/breadcrumbs must not blow up the safety net.
    for bad in ({}, {"request": "not-a-dict"}, {"exception": 5}, {"breadcrumbs": "x"}):
        assert isinstance(scrub_event(bad), dict)


# ---------------------------------------------------------------------------
# scrub_breadcrumb
# ---------------------------------------------------------------------------


def test_breadcrumb_message_and_data_redacted() -> None:
    crumb = {"message": "mail owner@example.com", "data": {"url": "pk_live_abcdefghijklmnopqrstuvwx"}}
    out = scrub_breadcrumb(crumb)
    assert out is not None
    assert out["message"] == "mail [redacted-email]"
    assert "pk_live_" not in out["data"]["url"]


def test_breadcrumb_non_dict_passthrough() -> None:
    assert scrub_breadcrumb(None) is None


# ---------------------------------------------------------------------------
# init_sentry — disabled-safe + locked init contract
# ---------------------------------------------------------------------------


def test_init_disabled_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    sentry_init._initialized.clear()
    assert init_sentry("gate") is False


def test_init_disabled_with_blank_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "   ")
    sentry_init._initialized.clear()
    assert init_sentry("gateway") is False


def test_init_contract_with_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a fake sentry_sdk and assert the LOCKED init kwargs + tags."""
    captured: dict[str, object] = {}
    tags: dict[str, str] = {}

    fake = types.ModuleType("sentry_sdk")

    def _init(**kwargs: object) -> None:
        captured.update(kwargs)

    def _set_tag(key: str, value: str) -> None:
        tags[key] = value

    fake.init = _init  # type: ignore[attr-defined]
    fake.set_tag = _set_tag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.sentry.io/2")
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_OVERLAY_REF", "deadbeef")
    sentry_init._initialized.clear()

    assert init_sentry("gateway") is True
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is scrub_event
    assert captured["before_breadcrumb"] is scrub_breadcrumb
    assert captured["traces_sample_rate"] == 0.0
    assert captured["release"] == "deadbeef"
    assert tags == {"tenant": "acme", "component": "gateway"}

    # Idempotent per component.
    captured.clear()
    assert init_sentry("gateway") is True
    assert captured == {}
