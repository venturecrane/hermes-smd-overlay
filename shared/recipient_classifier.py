"""Recipient classification for outbound sends — the internal/external gate.

Pure, deterministic, no I/O. This module is a **byte-identical twin**: this file
(`operator/adapter/recipient_classifier.py`) and the overlay
`shared/recipient_classifier.py` MUST stay identical (recorded in
`operator/contracts/overlay-pairs.json`). Both the proactive send gate
(`hermes-smd-trust`) and the reactive reply gate (`hermes-smd-reply`) classify
recipients through this one function, so the two paths agree by construction.

Why this exists — the "nothing ever sends" root
-----------------------------------------------
A single recipient-blind ``EXTERNAL_SEND`` action class collapsed *every* send —
an internal notification to the firm's own attorney and an outbound client email
alike — onto one flat ceiling that safe-defaulted to ``draft_for_review``. That
default *is* the bug: it silently held legitimate internal traffic. This module
splits the recipient axis so a send **to a rostered internal address** resolves
to the ``external_send_internal`` action class (autonomous-capable, per the
authored exposure) while a send to anyone else stays ``external_send`` (the
outside ceiling, unchanged). The distinction is **typed and required**: an
unclassifiable recipient returns :data:`RecipientClass.UNKNOWN`, which the caller
MUST treat as a hard error — never a silent draft. Re-introducing a
"draft on unknown" default re-introduces the bug; the golden tests forbid it.

Security posture — the roster is OUTBOUND AUTHORIZATION, not inbound trust
-------------------------------------------------------------------------
Classifying a recipient as INTERNAL authorises an autonomous *send to them*. That
is a different, stronger trust decision than "who may trigger the agent." Hard
rules, enforced here in code:

* **Human-authored roster only.** The roster passed in MUST be human-authored
  (``scope.inbound_allow_from`` is documented human-authored; if that ever
  changes, callers pass a dedicated outbound-authorization list). If a roster
  were ever auto-grown from observed inbound correspondents, "email in → become
  rostered → receive autonomous sends" would be an exfiltration path. This
  function does not read config; it trusts its caller to pass a human-authored
  roster, and the caller side asserts that.
* **Strict canonicalization.** lowercase; a single ``local@domain``; **exact**
  domain equality (no parent/subdomain widening); **no** plus-tag stripping (a
  ``+tag`` local part is not widened to the bare local part); **no** display-name
  parsing. A loose match is a spoofing hole; an over-strict match only costs a
  draft. Anything that is not a clean bare address is UNKNOWN, not a guess.
* **Tainted provenance never classifies INTERNAL.** If the recipient address
  originated in untrusted/tainted content (``from_tainted=True``), the result is
  OUTSIDE even if it matches the roster — an injected "send to X" can never
  promote X to internal.
"""

from __future__ import annotations

import enum
import unicodedata
from collections.abc import Iterable, Sequence

# Action-class strings the send router selects. These MUST equal the
# ``ActionClass`` ``.value`` strings in ``operator/adapter/trust_ceiling.py`` (ss)
# and ``shared/action_classes.py`` (overlay) — the shared vocabulary carried
# across the twins. A rostered/internal send resolves to ``external_send_internal``
# (its own authored, fail-closed exposure ceiling); anyone else to ``external_send``
# (the outside ceiling, unchanged).
ACTION_CLASS_EXTERNAL_SEND_INTERNAL = "external_send_internal"
ACTION_CLASS_EXTERNAL_SEND = "external_send"


class UnclassifiedRecipientError(Exception):
    """Raised when a send's recipient cannot be classified (UNKNOWN).

    This is the fail-closed hard error that replaced the old silent
    "draft on anything unresolved" default — the default that *was* the
    "nothing ever sends" bug in reverse. A caller that catches this MUST refuse
    or surface loudly; it must never downgrade it to a draft.
    """


class RecipientClass(str, enum.Enum):
    """Outbound recipient trust classes.

    ``INTERNAL`` — a human-rostered address; eligible for the
    ``external_send_internal`` ceiling. ``OUTSIDE`` — any other resolvable
    address; governed by the ``external_send`` ceiling. ``UNKNOWN`` — the
    recipient could not be resolved to a clean address; the caller MUST fail
    closed with a hard error (never a silent draft).
    """

    INTERNAL = "internal"
    OUTSIDE = "outside"
    UNKNOWN = "unknown"


# Restrictiveness ordering for aggregating a multi-recipient send: the send as a
# whole is only as trusted as its least-trusted recipient. UNKNOWN dominates
# (one unresolved recipient makes the whole send a hard error); OUTSIDE beats
# INTERNAL (one outside recipient makes the whole send outside-governed).
_RESTRICTIVENESS: dict[RecipientClass, int] = {
    RecipientClass.INTERNAL: 0,
    RecipientClass.OUTSIDE: 1,
    RecipientClass.UNKNOWN: 2,
}


def _canonicalize_address(raw: str) -> str | None:
    """Return the canonical ``local@domain`` for a bare address, or ``None``.

    Strict by design — returns ``None`` (→ UNKNOWN) for anything that is not a
    single clean bare address, because every lenient parse here is a spoofing
    surface:

    * NFC-normalised then lowercased (so an ASCII roster entry never equal-matches
      a homoglyph/pre-composed lookalike by accident — they stay distinct bytes).
    * Rejects display-name forms (``"Name <a@b>"``), angle brackets, quotes,
      whitespace, and comma/semicolon-separated lists — the caller must pass one
      address at a time.
    * Requires exactly one ``@`` with a non-empty local part and a domain that
      has a dot and no empty labels.
    * The local part is preserved verbatim (NO plus-tag stripping): ``a+x@d`` is
      not the same address as ``a@d``.
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFC", raw).strip().lower()
    if not s:
        return None
    # No display names, bracketed addresses, quoting, whitespace, or lists.
    if any(ch in s for ch in ("<", ">", '"', " ", "\t", ",", ";", "\n", "\r")):
        return None
    if s.count("@") != 1:
        return None
    local, _, domain = s.partition("@")
    if not local or not domain:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(label == "" for label in labels):
        return None
    return f"{local}@{domain}"


def _canonicalize_roster_entry(entry: str) -> str | None:
    """Canonicalize one roster entry to either ``@domain`` or ``local@domain``.

    A roster entry is either a whole-domain grant (``@ashtonandprice.com``) or a
    single address (``scott@smd.services``). Returns ``None`` for malformed
    entries so a junk roster line can never widen into an accidental match.
    """
    if entry is None:
        return None
    s = unicodedata.normalize("NFC", entry).strip().lower()
    if not s or any(ch in s for ch in ("<", ">", '"', " ", "\t", ",", ";", "\n", "\r")):
        return None
    if s.startswith("@"):
        domain = s[1:]
        labels = domain.split(".")
        if len(labels) < 2 or any(label == "" for label in labels):
            return None
        return f"@{domain}"
    return _canonicalize_address(s)


def classify_recipient(
    recipient: str,
    roster: Iterable[str],
    *,
    from_tainted: bool = False,
) -> RecipientClass:
    """Classify a single outbound ``recipient`` against a human-authored ``roster``.

    * ``from_tainted=True`` → OUTSIDE unconditionally (tainted provenance can
      never promote a recipient to internal), before any roster match.
    * Unresolvable recipient → UNKNOWN (caller fails closed with a hard error).
    * Exact address match, or exact domain match for an ``@domain`` roster entry
      → INTERNAL. Otherwise OUTSIDE.
    """
    canon = _canonicalize_address(recipient)
    if canon is None:
        return RecipientClass.UNKNOWN
    if from_tainted:
        return RecipientClass.OUTSIDE
    _, _, canon_domain = canon.partition("@")
    for entry in roster:
        centry = _canonicalize_roster_entry(entry)
        if centry is None:
            continue
        if centry.startswith("@"):
            if canon_domain == centry[1:]:
                return RecipientClass.INTERNAL
        elif canon == centry:
            return RecipientClass.INTERNAL
    return RecipientClass.OUTSIDE


def classify_recipients(
    recipients: Sequence[str],
    roster: Iterable[str],
    *,
    from_tainted: bool = False,
) -> RecipientClass:
    """Aggregate class for a send to one or more recipients (most-restrictive wins).

    An empty recipient list is UNKNOWN (a send with no resolvable recipient is a
    hard error, not an autonomous send). Otherwise the send is governed by its
    least-trusted recipient: any UNKNOWN → UNKNOWN (hard error); else any
    OUTSIDE → OUTSIDE; else INTERNAL. Materialise the roster once so a one-shot
    iterable is not exhausted across recipients.
    """
    materialized = list(roster)
    if not recipients:
        return RecipientClass.UNKNOWN
    worst = RecipientClass.INTERNAL
    for r in recipients:
        cls = classify_recipient(r, materialized, from_tainted=from_tainted)
        if _RESTRICTIVENESS[cls] > _RESTRICTIVENESS[worst]:
            worst = cls
    return worst


def send_action_class(recipient_class: RecipientClass) -> str:
    """Map a resolved recipient class to the send action-class string.

    INTERNAL → ``external_send_internal``; OUTSIDE → ``external_send``. UNKNOWN is
    a **hard error** (:class:`UnclassifiedRecipientError`), never a silent draft —
    a send whose recipient we cannot resolve does not fall through to a permissive
    OR a lenient default; it stops loudly. This is the structural guarantee that
    the "unclassified → draft" regression cannot silently return.
    """
    if recipient_class is RecipientClass.INTERNAL:
        return ACTION_CLASS_EXTERNAL_SEND_INTERNAL
    if recipient_class is RecipientClass.OUTSIDE:
        return ACTION_CLASS_EXTERNAL_SEND
    raise UnclassifiedRecipientError(
        "recipient could not be classified (UNKNOWN); refuse and surface — "
        "never route an unresolved-recipient send to a draft or a send"
    )
