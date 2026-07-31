"""Does this output actually have the shape the customer authored? (ss ADR 0083 §3)

WHY THIS IS SEPARATE FROM VOICE, AND WHY ONLY THIS HALF BLOCKS. The ADR draws
the line and this module lives on one side of it: *"format is a separate axis
from voice, and it is binary where voice is probabilistic. A model writes IN a
register and one grades whether it sounds right; typography either complies or
does not."*

So voice keeps the read-gate — did the model consult its spec — and format gets
a real check, because format is the half a machine can decide. Promising to
enforce how something SOUNDS is a promise this substrate cannot keep. Promising
that a required closing line is present is one it can keep every single time.

WHY ASSERTIONS AND NOT PROSE. The customer's authored prose goes in front of the
model; the assertions go in front of this checker. Both come from the same
submission, and neither is derived from the other — nothing here parses English.
A rule this module enforces is a rule the customer chose from a closed
vocabulary and can read back, never one a model inferred from their sentence and
then applied as a hard block they never asked for.

The vocabulary is deliberately small. Every entry answers a question about the
text alone, with no judgement: is this line here, is this word absent, is this
list a list. Anything needing taste belongs in the prose half, where a human
grades it.

WHERE THE ASSERTIONS COME FROM, AND WHY THAT IS SAFE. They arrive in the
customer's vault object, are installed by the root-owned applier, and are read
back out of the ROOT-OWNED MANIFEST — the same trust path as the body digest.
The agent cannot write them, so it cannot widen its own shape rules. That is the
same reasoning that made the spec tree root-owned in the first place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Every assertion this checker understands. A key outside this set is IGNORED
#: rather than refused: an older seat running against a newer authoring surface
#: must degrade to checking less, never to blocking everything it cannot parse.
#: The authoring surface is where an unknown rule is refused, at write time,
#: while a person is present to be told.
KNOWN_ASSERTIONS = frozenset(
    {
        "opening_line_prefix",
        "closing_line_prefix",
        "single_closing_line",
        "forbid_bullets",
        "forbid_substrings",
        "max_chars",
    }
)

_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


@dataclass(frozen=True)
class Violation:
    """One broken rule, named well enough to act on without reading the spec."""

    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"{self.rule}: {self.detail}"


def _lines(body: str) -> list[str]:
    """Non-empty lines, stripped. Blank lines are layout, not content."""
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def check(body: str, assertions: dict) -> list[Violation]:
    """Every way ``body`` fails ``assertions``. Empty list means it complies.

    Returns ALL violations rather than the first. A writer told about one broken
    rule fixes it, resubmits, and is told about the next — which reads as the
    checker moving the goalposts. Reporting the whole set once is the difference
    between a rule and an obstacle course.
    """
    if not isinstance(assertions, dict) or not assertions:
        return []

    found: list[Violation] = []
    lines = _lines(body)

    prefix = assertions.get("opening_line_prefix")
    if isinstance(prefix, str) and prefix:
        if not lines:
            found.append(Violation("opening_line_prefix", "the output is empty"))
        elif not lines[0].startswith(prefix):
            found.append(
                Violation(
                    "opening_line_prefix",
                    f"the first line must begin {prefix!r}; it begins {lines[0][:40]!r}",
                )
            )

    closing = assertions.get("closing_line_prefix")
    if isinstance(closing, str) and closing:
        if not lines:
            found.append(Violation("closing_line_prefix", "the output is empty"))
        elif not lines[-1].startswith(closing):
            found.append(
                Violation(
                    "closing_line_prefix",
                    f"the last line must begin {closing!r}; it begins {lines[-1][:40]!r}",
                )
            )
        # "Exactly one" is its own rule because "ends with it" and "contains it
        # once" fail differently, and the fix differs too: one is a missing line,
        # the other is a duplicate to delete. A live output failed precisely
        # this way — it closed correctly and also carried a second closer
        # earlier, which an ends-with check would have passed.
        if assertions.get("single_closing_line") is True:
            hits = [ln for ln in lines if ln.startswith(closing)]
            if len(hits) > 1:
                found.append(
                    Violation(
                        "single_closing_line",
                        f"{len(hits)} lines begin {closing!r}; exactly one is allowed",
                    )
                )

    if assertions.get("forbid_bullets") is True:
        offenders = [ln for ln in lines if _BULLET.match(ln)]
        if offenders:
            found.append(
                Violation(
                    "forbid_bullets",
                    f"{len(offenders)} line(s) are bulleted or numbered; "
                    f"first is {offenders[0][:40]!r}",
                )
            )

    forbidden = assertions.get("forbid_substrings")
    if isinstance(forbidden, list):
        lowered = body.lower()
        hit = [w for w in forbidden if isinstance(w, str) and w and w.lower() in lowered]
        if hit:
            found.append(
                Violation("forbid_substrings", "contains " + ", ".join(repr(w) for w in hit))
            )

    ceiling = assertions.get("max_chars")
    if isinstance(ceiling, int) and not isinstance(ceiling, bool) and ceiling > 0:
        if len(body) > ceiling:
            found.append(
                Violation("max_chars", f"{len(body)} characters exceeds the authored {ceiling}")
            )

    return found


def describe(violations: list[Violation]) -> str:
    """Full detail, for the refusal handed back to the MODEL.

    Includes short quoted fragments of the offending lines, because a writer
    told "the first line is wrong" without being shown which line it read
    cannot reliably fix it. This is safe HERE and nowhere else: the model
    already holds the text it just composed, so the fragment discloses nothing
    it does not have.
    """
    return "; ".join(str(v) for v in violations)


def rule_names(violations: list[Violation]) -> str:
    """Rule names ONLY, for the audit row.

    The audit row is durable and read by people who were not in the session, so
    it records WHICH rule broke and never a fragment of what the customer's
    Operator wrote. Provenance, not content — the same line every other audit
    writer in this plugin holds.
    """
    return ",".join(sorted({v.rule for v in violations}))


__all__ = ["KNOWN_ASSERTIONS", "Violation", "check", "describe", "rule_names"]
