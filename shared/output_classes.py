"""The output classes that exist, and how to say each one to a person.

WHY THE SEAT NEEDS ITS OWN COPY (ss-console#2546 follow-up). Live on the pilot,
2026-08-22 between 20:29Z and 20:52Z, four firm rules were recorded against
output classes that do not exist. Rule ``b91c239c`` went to ``demand_letter``;
``0685fc1f``, ``234d57ea`` and ``c0a5ada6`` went to ``letter``. The last of
those was explicitly about "internal emails to our own staff" and landed in
``classes/letter/format.md``, a path nothing reads.

Nothing refused any of them. The broker checks a slug is well-formed
(``[a-z0-9_-]``) and the intake writes wherever the slug points, so an invented
class produces a real file, a real install, and a real "your rule is in effect"
letter about a rule that can never bind to a single output. That is the worst
shape a failure can take here: the firm is told its instruction was followed,
and the firm's own files say so, and no output will ever obey it.

The broker's looser check is not a bug and is not being tightened. It validates
SHAPE, which is what a component that cannot read the seat's contracts is
entitled to validate. Membership is a question about the registry, and the
registry ships to the seat, so the answer belongs on the seat.

WHERE THE LIST COMES FROM: ``operator/contracts/output-classes.yaml`` in
ss-console, the ``classes:`` block, which ADR 0083 makes the single declaration
of what the Operator produces. It is SIX. ``workspace``, which reads like a
seventh, is a key under ``skill_bindings:`` naming the workspace SKILL, not a
class -- and mistaking it for one is the same error as ``letter``, one level up.

PINNED RATHER THAN READ AT RUNTIME, deliberately. The registry is an ADR-level
contract that moves about once a year, and a gate that reads a file it may not
find has to decide what an unreadable registry means; every answer to that is
worse than a constant. Pinning costs one overlay bump if a class is ever added,
and the failure direction of being stale is REFUSAL with a named remedy, which
is the safe one. ``tests/test_output_classes.py`` compares this list against the
registry wherever the registry is reachable, and ss-console's own
``test_output_class_conformance.py`` pins the same six on the side the file
lives on, so a class added there fails CI there with this file named.
"""

from __future__ import annotations

from types import MappingProxyType

#: Every output class, and how to describe it to somebody who has never read a
#: contracts file. The plain words are not decoration: the model picked
#: ``letter`` for an internal staff email, which is a reasonable guess from the
#: slug list alone and an obviously wrong one from the sentences below.
OUTPUT_CLASS_MEANINGS: MappingProxyType[str, str] = MappingProxyType(
    {
        "staff": "internal email and notes to the firm's own people",
        "work_product": "a document drafted for the firm and filed to a matter",
        "record": "an internal record: a chronology row, a ledger line, a task field",
        "outbound_client": "letters and emails to the firm's own clients",
        "outbound_vendor": "letters and emails to records vendors, providers and lienholders",
        "outbound_external": (
            "letters and emails to opposing counsel, carriers, courts and other outside parties"
        ),
    }
)

#: The membership test itself.
OUTPUT_CLASSES: frozenset[str] = frozenset(OUTPUT_CLASS_MEANINGS)


def is_output_class(value: object) -> bool:
    """True iff ``value`` names a class in the registry. Exact match, lowered.

    No repair: a slug one character off is a rule that would attach to nothing,
    and guessing which class was meant is how ``letter`` became a directory.
    """
    return isinstance(value, str) and value.strip().lower() in OUTPUT_CLASSES


def describe(value: object) -> str:
    """The plain-words meaning of a class, or ``""`` for anything else."""
    if not isinstance(value, str):
        return ""
    return OUTPUT_CLASS_MEANINGS.get(value.strip().lower(), "")


def catalogue(separator: str = "; ") -> str:
    """Every class as ``slug (plain words)``, for a refusal that can be acted on.

    A refusal naming six slugs teaches the model six slugs. A refusal naming what
    each one IS lets it pick the right one on the next call, which is the whole
    difference between a gate and an obstacle.
    """
    return separator.join(f"{slug} ({words})" for slug, words in OUTPUT_CLASS_MEANINGS.items())


__all__ = [
    "OUTPUT_CLASSES",
    "OUTPUT_CLASS_MEANINGS",
    "catalogue",
    "describe",
    "is_output_class",
]
