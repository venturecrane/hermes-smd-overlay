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

"May the Operator reply to you" is not "are you firm staff"
-----------------------------------------------------------
``scope.inbound_allow_from`` answers the first question. It was also used as the
``internal_roster`` here, which answered the second — so a firm that authored
"auto-reply to my client" silently also said "my client is staff", and staff are
exempt from the content floor (ADR 0072) and from the matter-identity gate
(ss#2167). ss#2263 split them: the typed outbound roster carries a ``firm_staff``
class, :func:`_classify_one_typed` reads the typed roster FIRST, and the internal
roster is consulted only where the typed roster is silent — which is what keeps
every already-authored seat classifying exactly as it did. Read that function's
docstring before changing the order back.

The typed outbound roster — CLIENT / VENDOR / staff are independently-authored
--------------------------------------------------------------------------------
Beyond the two-way internal/outside split, :func:`classify_recipients_typed`
resolves a send against a **typed outbound roster**: a human-authored list of
``(entry, class)`` pairs where ``class`` is a closed vocabulary of exactly
``client`` (the firm's own client), ``records_vendor`` (a records provider), and
``firm_staff`` (the firm's own people — the authored form of the fact that used
to be inferred from the reply list).
These map to the ``external_send_client`` / ``external_send_vendor`` action
classes, each with its OWN authored, fail-closed ceiling — so an engagement can
graduate "chase our own client" or "chase the records vendor" to autonomous with
a one-line config change, without touching the outside class. Same hard rules as
the internal roster apply: **human-authored only** (never grown from inbound),
strict canonicalization (exact address or exact ``@domain`` equality, no plus-tag
widening, no display-name parsing, homoglyph-safe), and tainted provenance forces
OUTSIDE before any match. The vocabulary is closed BY DESIGN: there is no
"opposing counsel" or "court" class — an un-rostered outside recipient stays
:data:`RecipientClass.OUTSIDE`, governed by the outside ``external_send`` ceiling,
exactly as before. The classifier NEVER guesses: an address that matches entries
in more than one class resolves to OUTSIDE (the validators make this unreachable,
but the classifier does not depend on them). The 3-class functions
(:func:`classify_recipient`, :func:`classify_recipients`) are unchanged — the
reactive reply gate consumes them; only the proactive send gate reaches for the
typed variant.
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
# A send to a firm's own rostered CLIENT / RECORDS VENDOR resolves to its own
# authored, fail-closed ceiling (graduatable to autonomous independently of the
# outside class). These MUST equal the ``ActionClass`` ``.value`` strings on both
# enforcement sides.
ACTION_CLASS_EXTERNAL_SEND_CLIENT = "external_send_client"
ACTION_CLASS_EXTERNAL_SEND_VENDOR = "external_send_vendor"


class UnclassifiedRecipientError(Exception):
    """Raised when a send's recipient cannot be classified (UNKNOWN).

    This is the fail-closed hard error that replaced the old silent
    "draft on anything unresolved" default — the default that *was* the
    "nothing ever sends" bug in reverse. A caller that catches this MUST refuse
    or surface loudly; it must never downgrade it to a draft.
    """


class RecipientClass(str, enum.Enum):
    """Outbound recipient trust classes.

    ``INTERNAL`` — a human-rostered internal-staff address; eligible for the
    ``external_send_internal`` ceiling. ``CLIENT`` / ``VENDOR`` — a human-rostered
    outbound-roster address typed as the firm's own client / records vendor;
    eligible for the ``external_send_client`` / ``external_send_vendor`` ceilings
    (each authored independently, fail-closed when unauthored). ``OUTSIDE`` — any
    other resolvable address (opposing counsel, court, anyone un-rostered);
    governed by the ``external_send`` ceiling. ``UNKNOWN`` — the recipient could
    not be resolved to a clean address; the caller MUST fail closed with a hard
    error (never a silent draft).
    """

    INTERNAL = "internal"
    CLIENT = "client"
    VENDOR = "records_vendor"
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

    A roster entry is either a whole-domain grant (``@firm.example``) or a
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


# Closed vocabulary of the typed outbound-roster class strings → the recipient
# class they resolve to. Anything not in this map is ignored (never guessed).
#
# ``firm_staff`` (ss#2263) is the authored form of "is firm staff". Before it,
# that fact had no field of its own: it was DERIVED from
# ``scope.inbound_allow_from``, which answers a different question ("may the
# Operator autonomously reply to you"). A firm that added its own client to the
# reply list therefore got that client treated as staff — exempt from the
# content floor (ADR 0072 / ss#1932) and from the matter-identity gate
# (ss#2167). Nothing warned. The two facts are now separately authorable.
_TYPED_ROSTER_CLASSES: dict[str, RecipientClass] = {
    "client": RecipientClass.CLIENT,
    "records_vendor": RecipientClass.VENDOR,
    "firm_staff": RecipientClass.INTERNAL,
}


def _roster_entry_matches(entry: str, canon: str, canon_domain: str) -> bool:
    """True iff a roster ENTRY matches a canonical recipient.

    Same strict semantics as :func:`classify_recipient`: an ``@domain`` entry
    matches on exact domain equality; a ``local@domain`` entry matches on exact
    address equality. A malformed entry canonicalizes to ``None`` and never
    matches, so a junk roster line can never widen into an accidental match.
    """
    centry = _canonicalize_roster_entry(entry)
    if centry is None:
        return False
    if centry.startswith("@"):
        return canon_domain == centry[1:]
    return canon == centry


def _classify_one_typed(
    recipient: str,
    internal_roster: Iterable[str],
    typed_roster: Sequence[tuple[str, str]],
    *,
    from_tainted: bool,
) -> RecipientClass:
    """Classify a single recipient across the typed roster + the internal roster.

    Order: unresolvable → UNKNOWN; tainted → OUTSIDE (before any match); **the
    typed roster decides** (CLIENT / VENDOR / INTERNAL via ``firm_staff``); only
    if the typed roster is SILENT about this address does an internal-roster
    match resolve INTERNAL; otherwise OUTSIDE. A defensive multi-class match (one
    address typed as more than one class) resolves to OUTSIDE without consulting
    the internal roster — the classifier never guesses, and never falls back into
    a WIDER class than the one the authored collision left ambiguous.

    THE PRECEDENCE IS THE FIX (ss#2263). It used to be the other way round: an
    internal-roster match returned INTERNAL *before* the typed roster was read,
    on the reasoning that "a rostered internal recipient outranks a typed class".
    That reasoning holds only while the internal roster is firm staff, and
    ``scope.inbound_allow_from`` is not that list — it is the list of people the
    Operator may autonomously REPLY to. The two questions had one field, so
    authoring "auto-reply to my client" silently also said "treat my client as
    staff", which exempted them from the content floor and the matter gate.

    Reading the typed roster first is what separates the two facts, and it moves
    the content floor, the send ceilings and the matter gate together, because all
    three read this one function. Nothing moves for a config that authors no
    typed class: the internal-roster fallback below is byte-for-byte the old
    behaviour, so a seat with no ``scope.outbound_roster`` (A&P today) classifies
    exactly as it did before.

    Specificity is deliberately NOT ranked between the two lists. If a domain
    grant in the typed roster and an exact address in the internal roster both
    match, the typed class wins by virtue of being read first. That resolves to
    the STRICTER outcome (a client/vendor ceiling and a live content floor rather
    than the internal exemption), and this module's standing rule is that an
    over-strict match costs a draft while a loose one is a hole. A firm that
    wants the other answer authors ``firm_staff`` for that address, which is
    exactly the expressiveness this class exists to provide.
    """
    canon = _canonicalize_address(recipient)
    if canon is None:
        return RecipientClass.UNKNOWN
    if from_tainted:
        return RecipientClass.OUTSIDE
    _, _, canon_domain = canon.partition("@")
    matched: set[RecipientClass] = set()
    for entry, class_str in typed_roster:
        cls = _TYPED_ROSTER_CLASSES.get(class_str)
        if cls is None:
            continue
        if _roster_entry_matches(entry, canon, canon_domain):
            matched.add(cls)
    if len(matched) == 1:
        return next(iter(matched))
    if matched:
        # More than one class on one address. The validators reject this, but the
        # classifier does not depend on them: refuse to guess, and do NOT fall
        # through to the internal roster — an ambiguous authored class must not
        # be resolved by widening it to the exemption.
        return RecipientClass.OUTSIDE
    for entry in internal_roster:
        if _roster_entry_matches(entry, canon, canon_domain):
            return RecipientClass.INTERNAL
    return RecipientClass.OUTSIDE


def classify_recipients_typed(
    recipients: Sequence[str],
    internal_roster: Iterable[str],
    typed_roster: Sequence[tuple[str, str]],
    *,
    from_tainted: bool = False,
) -> RecipientClass:
    """Aggregate class for a send, resolving CLIENT / VENDOR via a typed roster.

    Each recipient is classified by :func:`_classify_one_typed`; the send as a
    whole is aggregated to prevent ceiling-shopping on a mixed send:

      * empty recipient list → UNKNOWN (a send with no resolvable recipient is a
        hard error, not an autonomous send);
      * any UNKNOWN recipient → UNKNOWN;
      * any OUTSIDE recipient → OUTSIDE;
      * otherwise the per-recipient classes are ⊆ {INTERNAL, CLIENT, VENDOR}.
        Internal CCs ride along governed by the counterparty class:
        ⊆ {INTERNAL} → INTERNAL, ⊆ {INTERNAL, CLIENT} → CLIENT,
        ⊆ {INTERNAL, VENDOR} → VENDOR, and a CLIENT+VENDOR mix → OUTSIDE (a
        heterogeneous outside mix fails toward draft).

    ``internal_roster`` (``scope.inbound_allow_from``) and ``typed_roster``
    (``scope.outbound_roster`` as ``(entry, class)`` pairs) are both materialized
    once so a one-shot iterable is not exhausted across recipients.

    ``internal_roster`` is the BACKSTOP, not the authority (ss#2263): the typed
    roster is read first, and the reply list only classifies INTERNAL for an
    address the typed roster says nothing about. See :func:`_classify_one_typed`.
    """
    internal_materialized = list(internal_roster)
    typed_materialized = list(typed_roster)
    if not recipients:
        return RecipientClass.UNKNOWN
    seen: set[RecipientClass] = set()
    for r in recipients:
        seen.add(
            _classify_one_typed(
                r, internal_materialized, typed_materialized, from_tainted=from_tainted
            )
        )
    if RecipientClass.UNKNOWN in seen:
        return RecipientClass.UNKNOWN
    if RecipientClass.OUTSIDE in seen:
        return RecipientClass.OUTSIDE
    non_internal = seen - {RecipientClass.INTERNAL}
    if not non_internal:
        return RecipientClass.INTERNAL
    if non_internal == {RecipientClass.CLIENT}:
        return RecipientClass.CLIENT
    if non_internal == {RecipientClass.VENDOR}:
        return RecipientClass.VENDOR
    # CLIENT + VENDOR heterogeneous mix — fail toward the outside ceiling (draft).
    return RecipientClass.OUTSIDE


def send_action_class(recipient_class: RecipientClass) -> str:
    """Map a resolved recipient class to the send action-class string.

    INTERNAL → ``external_send_internal``; CLIENT → ``external_send_client``;
    VENDOR → ``external_send_vendor``; OUTSIDE → ``external_send``. UNKNOWN is a
    **hard error** (:class:`UnclassifiedRecipientError`), never a silent draft —
    a send whose recipient we cannot resolve does not fall through to a permissive
    OR a lenient default; it stops loudly. This is the structural guarantee that
    the "unclassified → draft" regression cannot silently return.
    """
    if recipient_class is RecipientClass.INTERNAL:
        return ACTION_CLASS_EXTERNAL_SEND_INTERNAL
    if recipient_class is RecipientClass.CLIENT:
        return ACTION_CLASS_EXTERNAL_SEND_CLIENT
    if recipient_class is RecipientClass.VENDOR:
        return ACTION_CLASS_EXTERNAL_SEND_VENDOR
    if recipient_class is RecipientClass.OUTSIDE:
        return ACTION_CLASS_EXTERNAL_SEND
    raise UnclassifiedRecipientError(
        "recipient could not be classified (UNKNOWN); refuse and surface — "
        "never route an unresolved-recipient send to a draft or a send"
    )
