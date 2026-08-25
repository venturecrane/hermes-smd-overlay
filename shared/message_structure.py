"""The message structure floor: a composed message must arrive as a document.

ADR 0083 §3 puts email, digest and memo in the row enforced by "template +
check", and that row was never built. This is its check half.

WHAT THIS IS NOT
----------------
It is **not** a format. ADR 0037 tenet 3 forbids imposed defaults and ADR 0083 §3
says a format slot is empty until the customer authors it, with empty meaning the
persona's own judgment produces the shape. So nothing here asserts which sections
exist, what they are called, or in what order. A firm that wants its digest
banded differently is authoring format, and that is the tier above this one.

What this asserts is MECHANICS: that prose was broken into blocks a renderer can
render. An unstructured wall is not "a different format the persona chose" — it
is not a document. That distinction is the whole licence for this module, and it
is why every rule below is structural or density-based and none of them reads a
word.

THE INCIDENT
------------
2026-08-25, pilot seat, the deadline digest. 4,280 characters whose ONLY markdown
block marker was a single ``---``. ``report_render.looks_like_report`` returns
True on any one block marker and ``_RE_HR`` matches ``---``, so one horizontal
rule bought a full render pass over a body with no headings and no list items.
Every band collapsed into one run-on ``<p>``; 38 escalation rows rendered flat,
20 of them for one matter. The identifiers were all correct. Nothing measured the
shape, so nothing noticed.

WHY DENSITY IS A RULE AND NOT A NICETY
--------------------------------------
The obvious minimum — "at least one heading and at least one list item" — would
have caught that body, and would accept the next one: a single heading followed
by 4,000 unbroken characters. That is the same failure minus one line. Rule
``paragraph_wall`` is the one that actually encodes "renders as one run-on
paragraph", and it is the strictest possible reading of mechanics: it says
nothing about sections, names, or order, only that the prose was broken up.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from shared import report_render

#: Families. A family is a SHAPE, not a skill: two skills that emit the same
#: shape share one entry, and one skill that emits two shapes needs two.
BANDED_DIGEST = "banded_digest"
DECISION_CARD = "decision_card"

#: Which family a routine's message belongs to.
#:
#: Repo-owned on purpose. The alternative — asking the model to declare its own
#: class on the send call — makes "absent" and "misspelled" indistinguishable in
#: effect, which is the failure recorded in ``shared/output_classes.py``: four
#: live rules installed against class names that did not exist, with the firm
#: told they were in effect.
#:
#: Absent from this map by design, each verified rather than assumed:
#:   inbox-triage        writes a daily note file; SKILL.md: "Never sends...
#:                       there is no send tool in this skill's surface."
#:   connector-auth-check  composes nothing; its FAILURES are the signal and the
#:                       fleet alerter writes whatever a human reads.
#:   workspace           the Google tool surface other skills travel over.
SKILL_FAMILY: dict[str, str] = {
    "deadline-miss-escalator": BANDED_DIGEST,
    "daily-needs-you-digest": BANDED_DIGEST,
    "client-verification-tracker": DECISION_CARD,
    "medical-records-chaser": DECISION_CARD,
    "new-matter-intake": DECISION_CARD,
}

#: A paragraph longer than this rendered as one wall. Chosen from the incident:
#: the 2026-08-25 escalation band was a single 2,900-character paragraph, and the
#: longest legitimate consequence line in the escalator's own output-format
#: template is comfortably under 300. The gap is wide enough that the threshold
#: is not load-bearing to a few dozen characters either way.
MAX_PARAGRAPH_CHARS = 600

_LIST_KINDS = frozenset({report_render.ORDERED_ITEM, report_render.UNORDERED_ITEM})


@dataclass(frozen=True)
class Violation:
    """One broken rule, named well enough to act on without reading this file."""

    rule: str
    detail: str


def _paragraphs(body: str) -> Iterable[str]:
    """Every run of consecutive prose lines, joined as the renderer joins them.

    Mirrors ``report_render``'s paragraph accumulation: a block line or a blank
    line ends the open paragraph, and everything else appends to it. A
    continuation line belongs to the item above it, so it is not prose here.
    """
    buf: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        if not raw.strip() or report_render.block_kind(raw) is not None:
            if buf:
                yield " ".join(buf)
                buf = []
            continue
        buf.append(raw.strip())
    if buf:
        yield " ".join(buf)


def _kinds(body: str) -> set[str]:
    found: set[str] = set()
    for raw in body.replace("\r\n", "\n").split("\n"):
        kind = report_render.block_kind(raw)
        if kind is not None:
            found.add(kind)
    return found


def check(body: str | None, family: str | None) -> list[Violation]:
    """Structural violations for ``body`` under ``family``.

    ``None`` or unknown ``family`` returns no violations — an unmapped routine is
    not this module's business, and inventing a rule for it would be the imposed
    default tenet 3 forbids. ``None`` body likewise: a send with nothing
    inspectable is the content floor's problem, and it already fails toward
    draft on exactly that condition.
    """
    if not family or family not in (BANDED_DIGEST, DECISION_CARD):
        return []
    if body is None or not body.strip():
        return []

    violations: list[Violation] = []
    kinds = _kinds(body)

    if report_render.HEADING not in kinds:
        violations.append(
            Violation("no_heading", "the message carries no heading, so it renders as flat prose")
        )

    # A decision card's fields are bold PARAGRAPH lines, not list items, so
    # requiring a list there would refuse the shape the skill is supposed to
    # emit. Only the banded families are lists by construction.
    if family == BANDED_DIGEST and not (kinds & _LIST_KINDS):
        violations.append(
            Violation(
                "no_list_items", "a banded digest carries no list items, so its bands are prose"
            )
        )

    longest = max((len(p) for p in _paragraphs(body)), default=0)
    if longest > MAX_PARAGRAPH_CHARS:
        violations.append(
            Violation(
                "paragraph_wall",
                f"a single paragraph runs {longest} characters "
                f"(limit {MAX_PARAGRAPH_CHARS}); it renders as one unbroken block",
            )
        )
    return violations


def family_for_skill(skill: str | None) -> str | None:
    """The family a routine's messages belong to, or ``None`` when unmapped."""
    if not skill:
        return None
    return SKILL_FAMILY.get(skill.strip())


def rule_names(violations: Iterable[Violation]) -> str:
    """The broken rules as one comma-joined string.

    Matches ``format_check.rule_names``'s shape rather than returning a list:
    the audit row's ``rules`` field is a string at the violation-shaped call site
    and a list at the broken-control one, and this is a violation.
    """
    return ", ".join(sorted({v.rule for v in violations}))


def describe(violations: Iterable[Violation]) -> str:
    """One human sentence naming every broken rule, for the operator message."""
    details = [v.detail for v in violations]
    if not details:
        return ""
    return "; ".join(details)


__all__ = [
    "BANDED_DIGEST",
    "DECISION_CARD",
    "MAX_PARAGRAPH_CHARS",
    "SKILL_FAMILY",
    "Violation",
    "check",
    "describe",
    "family_for_skill",
    "rule_names",
]
