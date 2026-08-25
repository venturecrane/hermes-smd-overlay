"""The activation handler's completion line must name every gate it ran.

WHY THIS IS WORTH A TEST. That single log line is the surface an operator reads
to learn what was actually PROVEN at boot. It is not decoration: every check
above it ``_die``s on failure, so the line is unreachable unless all of them
passed. That makes it the seat's own report of its governance state.

It had drifted twice, and writing this test is what found the second one. The
line said "ACTIVE + AUDITING + SPEND-CAPPED" while the webhook read-surface
check was already fail-closed and unnamed, and it stayed that way when the
runaway-loop arms became a boot gate (overlay#319/#320). A reader would have
concluded the seat's only circuit breaker was the spend one — a belief that was
true for months and is exactly what the loop-brake work ended.

A status line asserting LESS than the system knows is the same failure class as
one asserting more.

STRUCTURAL, NOT A SNAPSHOT. A snapshot test would be updated by ``-u`` alongside
the very drift it exists to catch. This derives the set of fail-closed gates
from the handler source and requires each to contribute a claim, so a NEW gate
added without a claim fails — which is the case that actually recurs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

HANDLER_PATH = Path(__file__).parent.parent / "hooks" / "smd-overlay-activation" / "handler.py"
HANDLER = HANDLER_PATH.read_text()

#: Fail-closed gates that are called as functions from ``handle()``, mapped to a
#: term their claim must contribute to the completion line.
FUNCTION_GATES = {
    "_cost_breaker_self_check": "cost breaker",
    "_webhook_read_self_check": "webhook read surface",
}

#: Gates written INLINE in ``handle()`` rather than extracted into a function.
#: They have no call site to discover, so they are listed rather than derived —
#: and `test_inline_gate_claims_are_not_fiction` keeps the list honest.
INLINE_GATES = {
    "trust gate": "_BANNED_PROBE_TOOL",
    "audit row": "post_llm_call",
}

#: Checks that deliberately WARN and continue rather than dying. They are out of
#: scope for the completion line, which reports what was proven, not what was
#: attempted. Named explicitly so the derivation below cannot silently absorb a
#: fail-closed gate by accident.
ADVISORY_CHECKS = {"_webhook_expected_tools_check"}


def completion_line() -> str:
    """The final ``logger.info`` call — from it to its closing paren."""
    idx = HANDLER.rindex("logger.info(")
    return HANDLER[idx : HANDLER.index("\n    )", idx)]


def called_check_functions() -> set[str]:
    """Every ``_*check*`` invoked at statement level inside ``handle()``.

    Tolerant of both arguments and an assignment target, on purpose. Earlier
    versions broke when a gate gained a parameter, and again when the call site
    started capturing the gate's OUTCOME. A test that goes red because a gate
    grew a return value is measuring the call signature, not whether the gate
    runs.
    """
    return set(re.findall(r"^\s+(?:\w+ = )?(?:await\s+)?(_\w*check\w*)\(", HANDLER, re.M))


def test_completion_line_names_every_function_gate() -> None:
    line = completion_line()
    missing = [name for name, claim in FUNCTION_GATES.items() if claim not in line]
    assert not missing, (
        "the boot completion line does not mention: "
        + ", ".join(f"{n} (expected {FUNCTION_GATES[n]!r})" for n in missing)
        + ". That line is what an operator reads to learn what was proven; a gate "
        "that runs and is not named reads as a gate that did not run."
    )


def test_completion_line_names_every_inline_gate() -> None:
    line = completion_line()
    missing = [claim for claim in INLINE_GATES if claim not in line]
    assert not missing, f"the completion line does not mention: {missing}"


@pytest.mark.parametrize("gate", sorted(FUNCTION_GATES))
def test_each_named_gate_actually_runs(gate: str) -> None:
    """The inverse, and what keeps the line honest rather than merely long: a
    claim must correspond to a gate that is actually called. Otherwise the line
    could advertise a check that was deleted."""
    assert re.search(rf"^(async )?def {gate}\(", HANDLER, re.M), (
        f"{gate} is claimed in the completion line but no longer defined"
    )
    assert gate in called_check_functions(), (
        f"{gate} is defined but never called — the completion line would claim a "
        "gate that does not run"
    )


def test_inline_gate_claims_are_not_fiction() -> None:
    """Each inline gate's claim must correspond to machinery still in the file."""
    for claim, marker in INLINE_GATES.items():
        assert marker in HANDLER, (
            f"the completion line claims {claim!r} but {marker!r} is gone from the handler"
        )


def test_every_fail_closed_gate_is_represented() -> None:
    """A new fail-closed gate must be added to FUNCTION_GATES.

    Without this, someone adds a fourth ``_die``-ing check, the completion line
    silently keeps advertising three, and every test above still passes. This is
    the assertion that found the webhook read-surface gate was already unnamed.
    """
    unknown = called_check_functions() - set(FUNCTION_GATES) - ADVISORY_CHECKS
    assert not unknown, (
        f"self-check(s) not represented in the completion line: {sorted(unknown)}. "
        "Add each to FUNCTION_GATES with the term it contributes, and say it in the "
        "line — or to ADVISORY_CHECKS if it warns rather than dying."
    )


def test_advisory_checks_really_are_advisory() -> None:
    """An exemption that quietly covers a fail-closed gate would be worse than no
    test at all, so the exemption list is itself checked against the source."""
    for name in ADVISORY_CHECKS:
        start = HANDLER.index(f"def {name}(")
        nxt = HANDLER.find("\ndef ", start + 1)
        anxt = HANDLER.find("\nasync def ", start + 1)
        ends = [e for e in (nxt, anxt) if e != -1]
        body = HANDLER[start : min(ends)] if ends else HANDLER[start:]
        assert "_die(" not in body, (
            f"{name} is on the advisory list but calls _die — it is fail-closed and "
            "must be named in the completion line instead"
        )
