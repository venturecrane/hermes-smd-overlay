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

from shared import sentry_init, sentry_ratelimit
from shared.sentry_init import (
    SENSITIVE_HEADERS,
    init_sentry,
    redact_text,
    scrub_breadcrumb,
    scrub_event,
    scrub_then_throttle,
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
    crumb = {
        "message": "mail owner@example.com",
        "data": {"url": "pk_live_abcdefghijklmnopqrstuvwx"},
    }
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

    messages: list[tuple[str, str]] = []

    fake = types.ModuleType("sentry_sdk")

    def _init(**kwargs: object) -> None:
        captured.update(kwargs)

    def _set_tag(key: str, value: str) -> None:
        tags[key] = value

    def _capture_message(message: str, level: str = "info") -> None:
        messages.append((message, level))

    fake.init = _init  # type: ignore[attr-defined]
    fake.set_tag = _set_tag  # type: ignore[attr-defined]
    fake.capture_message = _capture_message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.sentry.io/2")
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("SMD_OVERLAY_REF", "deadbeef")
    # Seat-only gate: pytest sets PYTEST_CURRENT_TEST, so the contract test must
    # opt in explicitly to exercise a real init.
    monkeypatch.setenv("SMD_SENTRY_FORCE", "1")
    sentry_init._initialized.clear()

    assert init_sentry("gateway") is True
    assert captured["send_default_pii"] is False
    assert captured["before_send"] is scrub_then_throttle
    assert captured["before_breadcrumb"] is scrub_breadcrumb
    assert captured["traces_sample_rate"] == 0.0
    assert captured["release"] == "deadbeef"
    assert tags == {"tenant": "acme", "component": "gateway"}
    # Boot marker sent once, at info level, with a constant message (grouping key).
    assert messages == [("boot: monitoring active", "info")]

    # Idempotent per component: no re-init and no second boot marker.
    captured.clear()
    messages.clear()
    assert init_sentry("gateway") is True
    assert captured == {}
    assert messages == []


# ---------------------------------------------------------------------------
# scrub_then_throttle — the throttle must not become a hole in the scrub gate
# ---------------------------------------------------------------------------


def test_composed_hook_scrubs_what_it_passes() -> None:
    """An event the throttle lets through is still fully scrubbed."""
    sentry_ratelimit.reset_for_tests()
    out = scrub_then_throttle(
        {
            "message": "contact bob@example.com with sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA",
            "request": {"data": {"password": "hunter2"}, "headers": {"Authorization": "Bearer x"}},
        }
    )
    assert out is not None
    assert "bob@example.com" not in out["message"]
    assert "sk-ant-api03" not in out["message"]
    assert "data" not in out["request"]
    assert out["request"]["headers"]["Authorization"] == "[redacted]"


def test_composed_hook_suppresses_repeats_after_scrubbing() -> None:
    """The 3rd identical event is dropped; the scrub still ran on 1 and 2."""
    sentry_ratelimit.reset_for_tests()
    event = {"logger": "cron.jobs", "message": "reach admin@example.com"}
    first = scrub_then_throttle(dict(event))
    second = scrub_then_throttle(dict(event))
    third = scrub_then_throttle(dict(event))
    assert first is not None and "admin@example.com" not in first["message"]
    assert second is not None
    assert third is None


def test_composed_hook_passes_event_through_when_throttle_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttle bug must never silence monitoring — fail open, still scrubbed."""

    def _boom(_event: object) -> object:
        raise RuntimeError("throttle exploded")

    monkeypatch.setattr(sentry_ratelimit._throttle, "should_send", _boom)
    out = scrub_then_throttle({"message": "leak me: dev@example.com"})
    assert out is not None
    assert "dev@example.com" not in out["message"]


# ---------------------------------------------------------------------------
# Seat-only gate — a laptop must never write to the shared production project
# ---------------------------------------------------------------------------


def test_no_init_off_a_fly_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 13%-of-volume laptop-noise defect: DSN present, but not a seat."""
    monkeypatch.setenv("SENTRY_DSN", "https://pub@o1.ingest.sentry.io/2")
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.delenv("SMD_SENTRY_FORCE", raising=False)
    sentry_init._initialized.clear()
    assert init_sentry("gateway") is False


def test_init_allowed_on_a_fly_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """FLY_MACHINE_ID is the discriminator — the value Sentry logs as server_name."""
    monkeypatch.delenv("SMD_SENTRY_FORCE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FLY_MACHINE_ID", "2862965a4e7218")
    assert sentry_init._is_real_seat() is True


def test_pytest_never_reports_even_on_a_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test run executing ON a Machine still must not pollute the project."""
    monkeypatch.delenv("SMD_SENTRY_FORCE", raising=False)
    monkeypatch.setenv("FLY_MACHINE_ID", "2862965a4e7218")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    assert sentry_init._is_real_seat() is False


def test_force_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberate local reproduction stays possible, but only opt-in."""
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_x.py::test_y (call)")
    monkeypatch.setenv("SMD_SENTRY_FORCE", "1")
    assert sentry_init._is_real_seat() is True


def test_blank_fly_machine_id_is_not_a_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMD_SENTRY_FORCE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FLY_MACHINE_ID", "   ")
    assert sentry_init._is_real_seat() is False
