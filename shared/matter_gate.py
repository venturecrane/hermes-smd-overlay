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
Membership is only a closed set when one of two things was READ. Otherwise a
recipient's absence proves nothing, and the verdict is ``unresolved`` — a
different sentence to a reviewer, deliberately. Collapsing the two would tell a
paralegal that a legitimate client is an outsider.

The two axes, both valid, both requiring an explicit completeness signal from the
connector (ss#2264):

* **matter axis** — this matter's own full party list (``parties_complete``, from
  ``get_matter``). "This recipient is not among its parties."
* **contact axis** — this address's full set of matters
  (``matters_for_contact_complete``, from a contact-filtered, unfiltered,
  untruncated ``list_matters``). "This matter is not among their matters."

The contact axis exists because the matter axis is nearly unreachable on the reply
lane: ``get_matter`` fires on 8 of 86 reply turns and ``list_matters`` on 34, so
the gate ran there and could almost never conclude anything.

POSTURE — read this before deciding it is safe to enable somewhere
------------------------------------------------------------------
This gate is **ON by default and is NOT gated on anything the seat authored.**
``SMD_MATTER_GATE_MODE`` is the only lever (``report`` observes; anything else,
including unset, is ``block``), and the ``enforce`` stanza runs for every
EXTERNAL_SEND / _CLIENT / _VENDOR call regardless of customer.yaml.

An earlier version of this docstring claimed the opposite — "additive and SILENT
when unauthored, matching the spec gate's precedent" — and that claim was
repeated in the shipping PRs and used to argue the pin could not disturb a client
seat during a rebuild. It was false: there is no CustomerConfig read anywhere in
this module. Corrected under ss#2252, which also carries the open decision about
whether to BUILD the authored posture rather than merely withdraw the claim.

What is actually true, and what the safety argument should rest on:

* a mismatch **downgrades to a human draft**; it never refuses outright;
* an ``unresolved`` membership does not withhold at all;
* a withhold additionally requires a CLOSED set on one of the two axes above —
  so the gate is *narrow*, but that narrowness is an emergent property of the
  data flow, NOT a designed opt-in, and it erodes as membership capture spreads.
  Do not lean on it as if it were a switch. ss#2264 widened it deliberately: the
  contact axis raises the withhold-capable share of reply turns from ~9% toward
  ~40% on today's read mix, so the narrowness argument is now weaker than it was
  when it was written, exactly as predicted.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from shared import matter_binding

logger = logging.getLogger(__name__)

# Matter-number shapes, LONGEST ALTERNATIVE FIRST — the ordering is the fix.
#
# An earlier version of this comment claimed the pattern was "kept byte-compatible
# with the identifier filter's case pattern". That was false: identifier_filter's
# ``_CASE_RE`` already carried an ``[A-Z]{2}-\d{4}-\d{4}`` branch and IGNORECASE,
# and this one carried neither. The two had diverged, and the comment asserting
# they had not is what made the divergence invisible.
#
# The cost of that divergence, measured on the pilot 2026-08-11
# (vfy_01KZRZH044CH4N5EEKHQ9A6KHW): alternation is first-match-wins, so against
# the real matter ``PI-2026-0001`` the short ``[A-Z]{2,4}-\d{4,6}`` branch matched
# ``PI-2026`` and left ``-0001`` behind. The truncated token resolved to nothing,
# the verdict was *unresolved*, and the send was not withheld — on the ONE matter
# of nine on that seat with a complete party list, i.e. the only one the gate
# could have acted on at all. A silently WRONG token is worse than no token: it
# reads as "this body cites a matter I have never seen" rather than as a defect.
#
# Deliberately NOT reusing identifier_filter._CASE_RE: its branches require
# exactly two letters, which drops the real ``2026-OPS-001`` (three). The two
# patterns stay separate and that is now stated rather than denied.
#
# IGNORECASE is safe HERE specifically, in a way it would not be in a reporting
# filter: ``_resolve_cited`` keeps only tokens that resolve to a matter this
# session actually read, so a false positive contributes nothing to any verdict.
# It cannot manufacture a mismatch; it can only fail to find a matter. That is
# what lets this close ss#2262 (a lower-case citation was not extracted at all)
# in the same pattern as ss#2269.
_MATTER_NUM_RE = re.compile(
    r"\b(?:"
    r"[A-Za-z]{2,4}-\d{4}-\d{2,5}"  # PI-2026-0001  (must precede the short form)
    r"|\d{2,4}-[A-Za-z]{2,4}-\d{2,5}"  # 2026-PI-101, 2026-OPS-001
    r"|[A-Za-z]{2,4}-\d{4,6}"  # PI-123456
    r")\b",
    re.IGNORECASE,
)

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
    of anything, and is left out rather than guessed at.

    The number half of that sentence was aspirational until ss#2167's second
    pass: this compared the token against ``known_matters()``, which holds
    connector ids only, so a body citing "2026-PI-101" — the form real
    correspondence uses — resolved to nothing and the verdict came back
    *unresolved* even against a CLOSED party set. Every test seeded a UUID body,
    so nothing caught it. ``membership.resolve`` performs the number->id join.
    """
    out: dict[str, str] = {}
    for token in cited:
        matter_id = membership.resolve(token)
        if matter_id:
            out[token] = matter_id
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
                # Membership is closed on EITHER axis (ss#2264). The matter axis
                # — this matter's own full party list — was the only one
                # implemented, and only ``get_matter`` produces it (8 of 86 reply
                # turns), so the reply lane could almost never conclude anything.
                # The contact axis proves it just as validly from the other
                # direction: if the FULL set of matters this address is party to
                # was read, and the cited matter is not in it, the address is not
                # a party. Absence from an OPEN set on either axis still proves
                # nothing and stays *unresolved*.
                if closed or membership.is_contact_closed(addr):
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


# ---------------------------------------------------------------------------
# The MIXING signal (ss#2167) — provenance, not membership
# ---------------------------------------------------------------------------
#
# ``evaluate`` above answers "is this recipient a party to the matter this letter
# CITES". It is silent by construction when the body cites nothing (see the
# ``if not cited`` branch), which leaves the likelier failure uncovered: content
# lifted from a second matter and never named.
#
# This answers a different, cheaper question — "did this session read more than
# one matter's substance before sending?" — and it deliberately needs NO party
# data. That matters: a party set closes on only ~40.7% of the firm's matters
# (census vfy_01M0BT2TF3GDFT3FZDBYZAVMNX), so every membership-based control is
# capped there. This one is not.
#
# It is NOT evidence that a send is wrong. Reading two matters and writing about
# one is ordinary work. It is evidence that a human should look, which is why
# Phase 1 only records and cannot withhold.


def multi_matter_mode() -> str:
    """``off`` | ``report`` | ``block``. Default ``block``, fail-closed.

    ``block`` here means what it means for :func:`mode`: an otherwise-permitted
    send is DOWNGRADED TO A HUMAN DRAFT. It never refuses outright and never
    discards work.

    This shipped observe-only for one revision, on the reasoning that the firing
    rate should be measured before anything enforced. That reasoning was
    calibrated against an earlier, much noisier signal keyed on ``get_matter``,
    and it was not re-derived after the signal narrowed to memo and document
    reads. It should have been, because the two errors are wildly asymmetric:

    * a false positive costs ONE HUMAN READ of a draft the firm already reads —
      every outbound send on the client seat sits at ``draft_for_review`` today;
    * a false negative is one client's facts in another client's letter.

    Measuring first is right when enforcement is expensive. Here it is nearly
    free, and waiting on data meant carrying the exposure for the length of the
    measurement. ``report`` still exists for a seat that wants the annotation
    without the downgrade."""
    raw = (os.environ.get("SMD_MULTI_MATTER_MODE") or "").strip().lower()
    if raw == "off":
        return "off"
    return "report" if raw == "report" else "block"


def multi_matter_session(session_id: str) -> tuple[str, ...]:
    """The matters whose content this session read, when there is more than one.

    Empty when the session read fewer than two — and empty unconditionally for a
    falsy session id. That guard is not defensive tidiness: ``resolve_session``
    returns ``""`` under MODE_AMBIGUOUS / MODE_NONE, and every unkeyed context
    shares that one bucket, so a flag raised from it could belong to two innocent
    sessions rather than one mixing session."""
    try:
        if not session_id or multi_matter_mode() == "off":
            return ()
        read = matter_binding.membership_for(session_id).content_read_matters()
        if len(read) < 2:
            return ()
        return tuple(sorted(read))
    except Exception:  # noqa: BLE001 — must never perturb the send path
        logger.debug("matter_gate: multi-matter evaluation failed", exc_info=True)
        return ()


def content_read_refusal(session_id: str, tool_name: str, args: Any) -> str | None:
    """Refuse a content read that would put a SECOND matter's substance into a
    session that already holds one. ``None`` means allow.

    THIS IS THE CONTROL. Everything else in this module reports on a send that
    has already been composed, and by then the damage this exists to prevent has
    happened: a draft containing two clients' facts is sitting in a paralegal's
    queue. Routing that draft to human review is not protection — it is the
    delivery mechanism. The firm discovering the Operator mixed two matters is
    the event the engagement does not survive, and it does not require the letter
    to have been sent.

    So the fence is at READ time. A session that has read matter A's substance
    cannot read matter B's, which means the mixed draft is never composed and
    there is nothing for anyone to find.

    Deliberately narrow, so ordinary work is untouched: only memo and document
    reads are fenced. A status digest or stalled-matter sweep reads matter
    METADATA across many matters and never trips this — which is why the signal
    was narrowed off ``get_matter`` earlier, and that narrowing is what makes a
    hard refusal affordable here.

    The refusal is recoverable by construction. Nothing is discarded and no work
    is lost: the agent finishes the matter it is on, and the second matter is
    read in a fresh session.

    Fail-open on an unresolvable session id, and that is a real hole rather than
    a design choice: with no session key there is no read-set to compare against,
    and refusing every content read on an unkeyed session would brick ordinary
    work on any seat where resolution degrades. The send-time annotation below
    stays as the second layer for exactly that case."""
    try:
        if multi_matter_mode() != "block":
            return None
        if not session_id or not matter_binding.is_content_read(tool_name):
            return None
        matter_id = matter_binding.content_matter_id(args)
        if not matter_id:
            return None
        held = matter_binding.membership_for(session_id).content_read_matters()
        if not held or matter_id in held:
            return None
        return (
            f"this session already read matter {', '.join(sorted(held))}; reading "
            f"matter {matter_id} as well would put two matters' content in one "
            "composition. Finish the matter you are on, then read the other in a "
            "new session (ss#2167 matter mixing)"
        )
    except Exception:  # noqa: BLE001 — must never perturb the read path
        logger.debug("matter_gate: content-read fence failed", exc_info=True)
        return None


__all__ = [
    "MatterVerdict",
    "evaluate",
    "cited_matters",
    "mode",
    "multi_matter_mode",
    "multi_matter_session",
    "content_read_refusal",
]
