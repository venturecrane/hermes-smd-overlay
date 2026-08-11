"""Outbound matter-identity gate (ss#2167).

Stops case A's content reaching case B's recipient — the one fabrication class
that had no control at all.

WHAT IS CHECKED, AND WHY IT IS NOT THE OBVIOUS THING
----------------------------------------------------
The obvious design is to have the send declare its matter and check that
declaration against the recipient. That design was built, reviewed, and
discarded, because the model composes the letter AND would supply the
declaration. Composing an outbound letter runs: resolve recipient → look up
their contact → get their matter → address them. So the model declares matter B,
the recipient genuinely IS a party to matter B, the gate returns "party", and
the case-A body ships. The check would have validated the one join the model
controls both ends of, and passed the exact scenario it exists to catch.

So neither side of this check is the model's word:

* **content side** — the matter identifiers physically present in the body,
  extracted by the same regex the identifier filter uses. The model cannot make
  a case number it wrote disappear.
* **recipient side** — who is a party to that matter, captured from connector
  reads (``shared.matter_binding``). The model cannot change who is a party.

A mismatch is: *the body cites matter X, and this recipient is not a party to
matter X.*

UNRESOLVED IS NOT NON-MEMBERSHIP
--------------------------------
Membership is only a closed set when the matter's own complete party list was
read. Otherwise a recipient's absence proves nothing, and the verdict is
``unresolved`` — a different sentence to a reviewer, deliberately. Collapsing
the two would tell a paralegal that a legitimate client is an outsider.

POSTURE
-------
Additive and SILENT when unauthored, matching the spec gate's precedent: a seat
that authored nothing is untouched, so shipping this cannot brick a seat during
a rebuild window. ``SMD_MATTER_GATE_MODE`` is the rollback lever (``report``
observes only; ``block`` downgrades to draft), fail-closed on a malformed value
in the same shape as ``SMD_IDENTIFIER_GATE_MODE``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from shared import matter_binding

logger = logging.getLogger(__name__)

# Matter-number shapes. Kept byte-compatible with the identifier filter's case
# pattern so the two agree on what "a matter number in the body" means.
_MATTER_NUM_RE = re.compile(r"\b(?:\d{2,4}-[A-Z]{2,4}-\d{2,5}|[A-Z]{2,4}-\d{4,6})\b")

# A matter id as the connector emits it (UUID), in case a body carries one.
_MATTER_ID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)

_MAX_CITED = 24


@dataclass(frozen=True)
class MatterVerdict:
    """Outcome of one send's matter-identity check."""

    status: str  # "ok" | "mismatch" | "unresolved" | "not_applicable"
    reason: str = ""
    matters: tuple[str, ...] = field(default_factory=tuple)
    recipients: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_mismatch(self) -> bool:
        return self.status == "mismatch"

    @property
    def should_withhold(self) -> bool:
        # Only a proven mismatch withholds. An unresolved membership is reported,
        # never enforced: enforcing it would withhold the majority of correct
        # sends on any seat whose reads do not happen to carry party data, and a
        # control that blocks correct work is removed rather than fixed.
        return self.status == "mismatch"


def mode() -> str:
    """``report`` | ``block``. Fail-closed: anything unrecognized is ``block``,
    mirroring SMD_IDENTIFIER_GATE_MODE — an operator typo must not silently
    disable a safety control."""
    raw = (os.environ.get("SMD_MATTER_GATE_MODE") or "").strip().lower()
    return "report" if raw == "report" else "block"


# Kept in step with ``outbound._SEND_SCAN_KEYS``. Duplicated rather than imported
# because ``outbound`` imports ``enforce``, and ``enforce`` calls this module — an
# import here would close that cycle. A key added there must be added here, or a
# matter cited only in the new field would go unseen.
_BODY_KEYS: tuple[str, ...] = (
    "subject",
    "text",
    "html",
    "html_body",
    "body",
    "body_text",
    "body_plain",
    "content",
    "message",
    "note",
)


def body_from_args(args: dict | None) -> str:
    """Every scannable field of a send, concatenated — a matter cited only in an
    html body must be as visible as one in the plaintext."""
    if not isinstance(args, dict):
        return ""
    parts: list[str] = []
    for key in _BODY_KEYS:
        value = args.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def cited_matters(body: str) -> set[str]:
    """Matter identifiers physically present in the send body."""
    if not isinstance(body, str) or not body:
        return set()
    found: set[str] = set()
    for pattern in (_MATTER_NUM_RE, _MATTER_ID_RE):
        for match in pattern.finditer(body):
            found.add(match.group(0).strip())
            if len(found) >= _MAX_CITED:
                return found
    return found


def _resolve_cited(membership: matter_binding.MatterMembership, cited: set[str]) -> dict[str, str]:
    """Map each cited token to a known matter id. A matter is addressable here by
    its id or by its number, and only tokens that resolve to a matter this
    session actually READ can be checked — a number nobody read is not evidence
    of anything, and is left out rather than guessed at."""
    known = membership.known_matters()
    out: dict[str, str] = {}
    for token in cited:
        if token in known:
            out[token] = token
    return out


def evaluate(
    *,
    session_id: str,
    body: str,
    recipients: set[str] | None,
    recipient_is_exempt: bool = False,
) -> MatterVerdict:
    """The verdict for one outbound send.

    ``recipient_is_exempt`` carries the roster classes that are not expected to
    be parties — firm staff (INTERNAL) and records vendors — resolved by the
    caller from the roster the CLIENT authored, so this gate imposes no defaults
    of its own.
    """
    try:
        if recipient_is_exempt:
            return MatterVerdict("not_applicable", "recipient class is not expected to be a party")
        if not recipients:
            # Unresolvable recipients are already a hard error upstream; this
            # gate adds nothing and must not double-withhold.
            return MatterVerdict("not_applicable", "no resolvable recipients")

        cited = cited_matters(body)
        if not cited:
            return MatterVerdict("not_applicable", "body cites no matter identifier")

        membership = matter_binding.membership_for(session_id)
        resolved = _resolve_cited(membership, cited)
        if not resolved:
            return MatterVerdict(
                "unresolved",
                "body cites a matter this session never read; membership unknown",
                tuple(sorted(cited)),
                tuple(sorted(recipients)),
            )

        addrs = {a for a in (r.strip().lower() for r in recipients) if a}
        offenders: list[str] = []
        unresolved_matters: list[str] = []

        for token, matter_id in sorted(resolved.items()):
            parties = membership.parties(matter_id)
            closed = membership.is_closed(matter_id)
            for addr in sorted(addrs):
                if addr in parties:
                    continue  # proven party
                if closed:
                    offenders.append(f"{addr} is not a party to {token}")
                else:
                    unresolved_matters.append(f"{addr} vs {token}")

        if offenders:
            return MatterVerdict(
                "mismatch",
                "; ".join(offenders[:6]),
                tuple(sorted(resolved)),
                tuple(sorted(addrs)),
            )
        if unresolved_matters:
            return MatterVerdict(
                "unresolved",
                "party list for the cited matter is not closed: "
                + "; ".join(unresolved_matters[:6]),
                tuple(sorted(resolved)),
                tuple(sorted(addrs)),
            )
        return MatterVerdict(
            "ok",
            "every recipient is a party to every cited matter",
            tuple(sorted(resolved)),
            tuple(sorted(addrs)),
        )
    except Exception:  # noqa: BLE001
        # A gate that raises must not take the send path with it. Report the
        # indeterminacy rather than inventing a verdict in either direction.
        logger.debug("matter_gate: evaluation failed", exc_info=True)
        return MatterVerdict("unresolved", "matter-identity evaluation raised")


__all__ = ["MatterVerdict", "evaluate", "cited_matters", "mode"]
