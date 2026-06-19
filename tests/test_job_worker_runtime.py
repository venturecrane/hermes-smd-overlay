"""Tests for the pure parts of the B1 worker runtime binding (ADR 0051).

The Hermes/infra functions (build_hermes_agent, cost readers, delivery, the
thread) are staging-exercised. The readiness barrier is pure and gates whether
the worker may claim a job, so it is unit-tested.
"""

from __future__ import annotations

from shared.job_worker_runtime import readiness_ok


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
