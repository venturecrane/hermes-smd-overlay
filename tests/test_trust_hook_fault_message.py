"""The fail-closed message names the actual fault (ss plan step 0.3).

WHY THIS EXISTS. The catch-all in ``on_pre_tool_call`` wraps the WHOLE hook —
roughly a hundred lines spanning session resolution, the ceiling resolver, the
content floor, the outbound scans, the workspace broker and the audit transport.
It reported ``trust-ceiling evaluation failed`` for every one of them, and the
real exception went only to the log.

The cost is measured, not theoretical. On 2026-07-31 that wording sent an agent
to a production-severity bug report against a WORKING security control: the
broker had correctly refused a caller that was not the gateway process, and the
message named a subsystem that was never involved. It cost a diagnosis, a wrong
report relayed to the Captain, and a rebuild cycle (ss#2103,
``vfy_01KYX1SHS2ZDCNNB6KR3PNSYQY``).

A message that describes the wrong subsystem is worse than a vague one, because
it is actionable in the wrong direction.

WHAT MUST NOT LEAK. The exception TYPE and the file:line it raised at name
machinery. The exception MESSAGE can carry recipient addresses or body
fragments from whatever raised, so it stays out of the agent-visible string and
lives only in the log.
"""

from __future__ import annotations

import pytest

from .conftest import load_plugin


class _Boom(RuntimeError):
    """Carries a payload-shaped message that must never reach the agent."""


@pytest.fixture
def mod():
    return load_plugin("hermes-smd-trust")


@pytest.fixture
def blocked(mod, monkeypatch):
    """Force the hook's body to raise from a known, non-ceiling location."""

    def _explode(*_a, **_k):
        raise _Boom("client@example.invalid asked about the Henderson matter")

    monkeypatch.setattr(mod.provenance, "resolve_session", _explode)
    result = mod.on_pre_tool_call(tool_name="email_send", args={}, session_id="s1")
    assert result is not None
    return result


def test_the_message_names_the_exception_type(blocked):
    assert "_Boom" in blocked["message"]


def test_the_message_names_where_it_raised(blocked):
    # file:line of the innermost frame — enough to go straight to the code.
    assert "test_trust_hook_fault_message.py:" in blocked["message"]


def test_it_no_longer_blames_the_trust_ceiling(blocked):
    """The regression. The fault raised above has nothing to do with the ceiling."""
    assert "trust-ceiling evaluation failed" not in blocked["message"]
    assert "NOT necessarily a ceiling" in blocked["message"]


def test_the_exception_message_never_reaches_the_agent(blocked):
    """Type and origin are machinery; the message can be payload."""
    assert "client@example.invalid" not in blocked["message"]
    assert "Henderson" not in blocked["message"]


def test_it_still_fails_closed(blocked):
    """The whole point survives the rewording: an indeterminate decision blocks."""
    assert blocked["action"] == "block"
    assert "Refused" in blocked["message"]
