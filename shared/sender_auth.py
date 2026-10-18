"""Is the ``From`` on an inbound email the sender, or only a claim? (AgentMail)

WHY THIS EXISTS. Every authority an inbound email can carry on a seat — roster
membership (``scope.inbound_allow_from``), admin status (``scope.admins``), the
record-store writer and reader lists, a confirmation of a held act, a rule
decline, a send-as instruction, a calendar reply into a person's space — is
keyed on the sender ADDRESS. On a Microsoft 365 seat that address comes from the
tenant, which authenticated it. On an AgentMail seat it came from the ``From``
header, which anyone can write: the Svix signature the gate verifies proves the
DELIVERY came from AgentMail, not that the person named in ``From`` sent it.
Three independent reviews (2026-10-17) found that a forged ``From:`` naming an
authored address was therefore treated as that person. The #2164 probe that
called this channel WEAK was right when it ran; AgentMail began publishing
per-message verdicts on 2026-09-09, and this module is where the seat starts
requiring them.

WHAT AGENTMAIL GIVES US. ``message.authentication_results`` on the
``message.received`` payload and on ``GET /inboxes/{inbox}/messages/{id}``
(docs.agentmail.to, ``MessageAuthenticationResults``): ``spf`` / ``dkim`` /
``dmarc`` verdicts in RFC 8601 vocabulary, plus ``spf_domain`` (withheld unless
aligned), ``dkim_domain`` (the ``d=`` tag, named only when every signature on
the message is from one domain) and ``dmarc_domain`` (the ``header.from`` the
DMARC verdict is about). Absent on mail received before 2026-09-09.

THE RULE. The ``From`` address is the sender's when, and only when, either

1. ``dmarc == "pass"`` and ``dmarc_domain`` IS the From domain — DMARC passing
   is itself the proof that an aligned SPF or DKIM identity vouches for that
   From domain; or
2. ``dkim == "pass"`` and ``dkim_domain`` is present and aligned with the From
   domain (equal, or one a dot-boundary parent of the other — DMARC relaxed
   alignment without a public-suffix list; a ``d=`` with no dot is refused).

Everything else is UNAUTHENTICATED: no verdicts, a ``dkim: pass`` with no
``dkim_domain`` (a message can carry signatures from several domains, and an
attacker controls which, so an unnamed passing signature may be the attacker's
own), ``spf`` alone (it vouches for the envelope, not the header the roster
reads), an unparseable From, and every read failure. Fail closed: "could not
tell" never reads as "authenticated".

WHAT A FAILURE DOES. It does not drop the mail. The webhook router delivers it
as an unknown external sender — fenced, tainted, no recorded origin, so no
roster, admin, record-store, act, rule or send-as authority attaches to the
address it claims — and logs a warning naming the reason. Calendar ingest
declines to file it into anyone's space and lets it flow on as ordinary mail.

Microsoft Graph is not evaluated here: its sender is tenant-authenticated
(ss ADR 0085 §5), and :func:`sender_authenticated` returns True for it so
A&P's behaviour is unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PASS = "pass"

#: Reasons, as stable tokens for logs and the envelope's audit detail.
OK_DMARC = "dmarc_pass_aligned"
OK_DKIM = "dkim_pass_aligned"
OK_TENANT = "tenant_authenticated"
NO_RESULTS = "no_authentication_results"
NO_FROM = "unparseable_from"
NOT_ALIGNED = "no_aligned_pass"
READ_FAILED = "authentication_results_unreadable"


def _domain_of(address: object) -> str:
    if not isinstance(address, str) or address.count("@") != 1:
        return ""
    local, _, domain = address.strip().lower().rpartition("@")
    domain = domain.strip().rstrip(".")
    if not local or "." not in domain or domain.startswith("."):
        return ""
    return domain


def _norm_domain(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower().rstrip(".")


def _aligned(signing: str, from_domain: str) -> bool:
    """DMARC relaxed alignment, approximated without a public-suffix list.

    Equal domains align; so does a ``d=`` that is a dot-boundary parent or
    child of the From domain. A dotless ``d=`` (a bare TLD) never aligns."""
    if not signing or "." not in signing or not from_domain:
        return False
    if signing == from_domain:
        return True
    return from_domain.endswith("." + signing) or signing.endswith("." + from_domain)


def evaluate(from_addr: object, results: object) -> tuple[bool, str]:
    """Decide whether ``results`` authenticate ``from_addr``. ``(ok, reason)``.

    Pure: no network, never raises. ``results`` is AgentMail's
    ``authentication_results`` object (a dict) or anything else (refused).
    """
    from_domain = _domain_of(from_addr)
    if not from_domain:
        return False, NO_FROM
    if not isinstance(results, dict) or not results:
        return False, NO_RESULTS
    dmarc = _norm_domain(results.get("dmarc"))
    if dmarc == PASS and _norm_domain(results.get("dmarc_domain")) == from_domain:
        return True, OK_DMARC
    dkim = _norm_domain(results.get("dkim"))
    if dkim == PASS and _aligned(_norm_domain(results.get("dkim_domain")), from_domain):
        return True, OK_DKIM
    return False, NOT_ALIGNED


def _fetch_results(message_id: str) -> Any:
    """The vendor's verdicts for one message in this seat's own inbox.

    Used only when the webhook payload did not carry them. Raises on any read
    failure; the caller turns that into "unauthenticated"."""
    from shared import agentmail_broker  # local: keeps this module import-light

    return agentmail_broker.message_authentication(message_id)


def sender_authenticated(dto: Any) -> tuple[bool, str]:
    """Whether a normalized inbound's ``from_addr`` is authenticated. ``(ok, reason)``.

    Graph: tenant-authenticated, always True. AgentMail: :func:`evaluate` over
    the verdicts the payload carried, or — when the payload carried none — over
    the verdicts read back from the vendor for the same message. Anything else,
    or any fault: False. Never raises.
    """
    try:
        provider = getattr(dto, "provider", "")
        if provider == "msgraph":
            return True, OK_TENANT
        if provider != "agentmail":
            return False, NO_RESULTS
        from_addr = getattr(dto, "from_addr", "")
        results = getattr(dto, "authentication_results", None)
        if results is None:
            message_id = getattr(dto, "message_id", "")
            if not isinstance(message_id, str) or not message_id.strip():
                return False, NO_RESULTS
            try:
                results = _fetch_results(message_id.strip())
            except Exception as exc:  # noqa: BLE001 - could-not-tell is not a pass
                logger.warning(
                    "sender_auth: authentication results unreadable for an inbound (%s); "
                    "treating the sender as unauthenticated",
                    exc.__class__.__name__,
                )
                return False, READ_FAILED
        return evaluate(from_addr, results)
    except Exception:  # noqa: BLE001 - never raise into the dispatch path
        logger.warning("sender_auth: evaluation failed; sender unauthenticated", exc_info=True)
        return False, READ_FAILED


__all__ = [
    "NOT_ALIGNED",
    "NO_FROM",
    "NO_RESULTS",
    "OK_DKIM",
    "OK_DMARC",
    "OK_TENANT",
    "READ_FAILED",
    "evaluate",
    "sender_authenticated",
]
