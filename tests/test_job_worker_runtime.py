"""Tests for the pure parts of the B1 worker runtime binding (ADR 0051).

The Hermes/infra functions (build_hermes_agent, cost readers, delivery, the
thread) are staging-exercised. The readiness barrier is pure and gates whether
the worker may claim a job, so it is unit-tested.
"""

from __future__ import annotations

import logging

import pytest

from shared.job_ledger_client import JobLedgerError
from shared.job_worker_runtime import (
    _is_broker_unreachable,
    _unreachable_log_level,
    put_result,
    readiness_ok,
)

# Env vars that, when all present, switch put_result onto the R2 path. The
# entrypoint/config-applier contract: same names the ADR-0044 applier reads.
_R2_ENV = (
    "R2_ENDPOINT_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_CONFIG",
    "CUSTOMER_SLUG",
    "SMD_CUSTOMER_SLUG",
)


@pytest.fixture
def no_r2_env(monkeypatch):
    """Ensure no ambient R2 results env leaks into a fallback test."""
    for name in _R2_ENV:
        monkeypatch.delenv(name, raising=False)


def test_readiness_all_pass():
    assert readiness_ok([lambda: True, lambda: True]) is True


def test_readiness_one_fails():
    assert readiness_ok([lambda: True, lambda: False]) is False


def test_readiness_empty_is_ready():
    assert readiness_ok([]) is True


def test_readiness_raising_check_is_not_ready():
    def boom():
        raise OSError("broker socket not listening")

    assert readiness_ok([lambda: True, boom]) is False


# -- put_result: R2 result store (ADR 0051 Decision 8) ------------------------


def test_put_result_returns_r2_ref_with_injected_uploader(monkeypatch):
    """With R2 env present and a fake uploader injected, put_result PUTs to the
    per-customer bucket under jobs/<slug>/<id>.md and returns an r2:// ref."""
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_BUCKET_CONFIG", "smd-cust-bucket")
    monkeypatch.setenv("CUSTOMER_SLUG", "demo-law")

    calls: list[tuple[str, str, bytes]] = []

    def fake_uploader(bucket: str, key: str, body: bytes) -> None:
        calls.append((bucket, key, body))

    ref = put_result({"id": "job-123"}, "the result body", uploader=fake_uploader)

    assert ref == "r2://smd-cust-bucket/jobs/demo-law/job-123.md"
    assert calls == [("smd-cust-bucket", "jobs/demo-law/job-123.md", b"the result body")]


def test_put_result_falls_back_to_volume_when_r2_env_absent(no_r2_env, tmp_path, monkeypatch):
    """With R2 env unset (local/CI), put_result writes to the volume and returns
    a file:// ref — so unit envs still produce a retrievable artifact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    ref = put_result({"id": "job-abc"}, "fallback body")

    expected = tmp_path / "job_results" / "job-abc.md"
    assert ref == f"file://{expected}"
    assert expected.read_text(encoding="utf-8") == "fallback body"


def test_put_result_falls_back_to_volume_when_uploader_raises(tmp_path, monkeypatch):
    """An R2 PUT failure must not lose the result: fall back to the volume."""
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("R2_BUCKET_CONFIG", "smd-cust-bucket")
    monkeypatch.setenv("CUSTOMER_SLUG", "demo-law")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    def boom_uploader(bucket: str, key: str, body: bytes) -> None:
        raise RuntimeError("R2 unreachable")

    ref = put_result({"id": "job-xyz"}, "resilient body", uploader=boom_uploader)

    expected = tmp_path / "job_results" / "job-xyz.md"
    assert ref == f"file://{expected}"
    assert expected.read_text(encoding="utf-8") == "resilient body"


# -- broker-unreachable severity (2026-08-10) ---------------------------------
# The worker has no shutdown path: Hermes exposes no process-lifecycle hook to
# plugins, so on machine teardown the broker exits while this daemon thread is
# still sweeping. That produces a connection-refused sweep failure identical in
# shape to a genuinely dead broker. The loop separates them by persistence —
# a dying process never reaches a second consecutive miss.


def test_broker_unreachable_recognises_refused_and_missing_socket():
    """Both shapes of "the socket would not take a connection" count."""
    for cause in (ConnectionRefusedError(111, "Connection refused"), FileNotFoundError()):
        exc = JobLedgerError("job broker socket error")
        exc.__cause__ = cause
        assert _is_broker_unreachable(exc) is True


def test_broker_unreachable_excludes_a_hung_broker():
    """A timeout means the broker ACCEPTED and then failed to answer — a hung
    broker is a real fault, not teardown, so it must not be softened."""
    exc = JobLedgerError("job broker socket error")
    exc.__cause__ = TimeoutError("timed out")
    assert _is_broker_unreachable(exc) is False


def test_broker_unreachable_excludes_unrelated_failures():
    """Only a wrapped transport refusal qualifies: not other JobLedgerErrors
    (e.g. the unset-env constructor raise, which has no cause), and not
    arbitrary sweep crashes."""
    assert _is_broker_unreachable(JobLedgerError("SMD_WORKSPACE_BROKER_SOCKET is unset")) is False
    assert _is_broker_unreachable(ValueError("something else entirely")) is False
    # Pins the TYPE guard specifically: a refused connection raised by anything
    # other than the job-ledger transport is not this failure mode. Without
    # this case the guard can be deleted and the suite still passes.
    foreign = ValueError("some other client's socket")
    foreign.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert _is_broker_unreachable(foreign) is False


def test_first_miss_stays_below_the_sentry_event_threshold():
    """The whole point: miss #1 must NOT reach Sentry. sentry_init sets no
    LoggingIntegration, so the SDK default event_level=ERROR applies and
    anything below ERROR is a breadcrumb only."""
    assert _unreachable_log_level(1) == logging.WARNING
    assert _unreachable_log_level(1) < logging.ERROR


def test_second_consecutive_miss_escalates_to_a_real_outage():
    """One sweep interval later the process is still alive, which teardown does
    not survive. That is an outage and must reach Sentry."""
    assert _unreachable_log_level(2) == logging.ERROR
    assert _unreachable_log_level(9) == logging.ERROR
