"""Sentry error monitoring for Operator Machines (ADR 0023 Wave 1, decision #11).

One shared ``smd-operator`` Sentry project. Every Machine process initializes
with a ``tenant=<slug>`` tag and a ``component=<gate|gateway>`` tag so a single
project surfaces per-customer, per-process error streams by tag filter.

Three properties this module guarantees:

* **Disabled-safe.** No ``SENTRY_DSN`` in the environment -> ``init_sentry`` is a
  no-op that returns ``False``. The ``sentry_sdk`` import is lazy, so this module
  imports cleanly even where the SDK is not installed (e.g. the scrub-suite CI
  runner). Observability must never crash boot: every failure path fails soft.

* **Seat-only.** Reporting requires a real Fly Machine (see
  :func:`_is_real_seat`). A laptop or a test run never writes to the shared
  production project, no matter what is in its environment.

* **PII scrub is LOCKED here (ADR 0023 decision #11).** ``send_default_pii=False``
  plus ``before_send`` / ``before_breadcrumb`` hooks that (a) drop request bodies
  entirely, (b) redact named sensitive headers, (c) redact email-shaped tokens
  from messages and breadcrumbs, (d) redact provider key shapes
  (``sk-…`` / ``pk_(live|test)_…`` / AWS ``AKIA…``), and (e) redact client-record
  identifier shapes — dashed GUIDs and matter-number formats — from every walked
  surface, including ``extra``, ``contexts``, and ``logentry.params``
  (ss-console #2150; a live matter GUID reached the shared project via a
  ``set_extra``-adjacent path the hooks never walked, SMD-OPERATOR-5). The scrub
  functions are pure (dict-in/dict-out, str-in/str-out) so
  ``tests/test_sentry_scrub.py`` gates them without the SDK. Do NOT widen what
  reaches Sentry without updating that suite — the regression suite is the merge
  gate that keeps a refactor from re-opening a leak.

The registered ``before_send`` is :func:`scrub_then_throttle`: it scrubs first,
then applies the logarithmic per-issue throttle in
:mod:`shared.sentry_ratelimit` (see that module for the 2026-07-16 incident
that motivated it). Scrub-before-throttle is deliberate — the throttle's key
table then holds only redacted text, and redaction improves grouping by
collapsing varying emails/keys into one key.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from shared.sentry_ratelimit import throttle_event

logger = logging.getLogger("hermes_smd.sentry")

# ---------------------------------------------------------------------------
# LOCKED scrub policy (ADR 0023 decision #11). Changing any constant here is a
# policy change and MUST be reflected in tests/test_sentry_scrub.py.
# ---------------------------------------------------------------------------

#: Header names redacted from every event, matched case-insensitively.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "authorization",
        "x-api-key",
        "x-machine-token",
        "x-sentry-auth",
        "x-tenant-slug",
    }
)

_REDACTED = "[redacted]"
_REDACTED_EMAIL = "[redacted-email]"
_REDACTED_KEY = "[redacted-key]"

#: Email-shaped substrings in free text.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Provider key shapes: Anthropic/OpenAI ``sk-``, Stripe ``pk_(live|test)_``,
#: AWS access-key id ``AKIA…``. The ``sk-`` class deliberately extends ADR 0023
#: #11's literal ``sk-[a-zA-Z0-9]{20,}`` to include ``-`` and ``_`` — real
#: Anthropic keys are ``sk-ant-api03-…`` (hyphenated), and ``ANTHROPIC_API_KEY``
#: is on every Machine, so the literal shape would miss the single most likely
#: key to leak. Over-redaction of a secret-shaped token is safe; under-redaction
#: leaks.
_KEY_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"pk_(?:live|test)_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)

_REDACTED_GUID = "[redacted-guid]"
_REDACTED_MATTER = "[redacted-matter]"

#: Dashed-GUID shape. Smokeball record ids (matters, tasks, events, contacts,
#: files) are all GUIDs, and a matter GUID in the shared project is client data
#: under DPA Exhibit B-1 (SMD-OPERATOR-5 shipped one; audit 2026-07-31 §3.12).
#: ALL dashed GUIDs are redacted, not only ones known to be matters, because a
#: GUID's referent is indistinguishable by shape — over-redacting an internal id
#: costs a little triage convenience; under-redacting breaches the DPA.
_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: Matter-number shapes observed across the fleet: year-first (``2026-PI-101``)
#: and prefix-first (``PI-2026-0001``). Uppercase-only and year-anchored so ISO
#: dates (all digits) and version strings never match. A firm with a scheme
#: outside these shapes needs a shape added HERE and a case added to the suite.
_MATTER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:19|20)\d{2}-[A-Z]{1,6}-\d{1,6}\b"),
    re.compile(r"\b[A-Z]{1,6}-(?:19|20)\d{2}-\d{1,6}\b"),
)


def redact_text(text: str) -> str:
    """Redact email addresses, provider key shapes, dashed GUIDs, and
    matter-number shapes from a string.

    Order matters: key shapes are redacted after emails so an ``sk-``-prefixed
    token embedded in a longer string is still caught; identifier shapes run
    last so a GUID inside an already-redacted span is not resurrected.
    """
    if not text:
        return text
    out = _EMAIL_RE.sub(_REDACTED_EMAIL, text)
    for rx in _KEY_RES:
        out = rx.sub(_REDACTED_KEY, out)
    out = _GUID_RE.sub(_REDACTED_GUID, out)
    for rx in _MATTER_RES:
        out = rx.sub(_REDACTED_MATTER, out)
    return out


#: Recursion bound for the tree walk. Sentry event sections are shallow in
#: practice; anything deeper is pathological and gets replaced wholesale.
_MAX_SCRUB_DEPTH = 8


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Redact every string leaf of an arbitrarily nested dict/list value.

    Non-string scalars pass through; a tree deeper than the bound is replaced
    with the redaction marker rather than walked forever.
    """
    if depth > _MAX_SCRUB_DEPTH:
        return _REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v, depth + 1) for v in value]
    return value


def _scrub_headers(headers: Any) -> None:
    """Redact sensitive header VALUES in place (case-insensitive key match)."""
    if not isinstance(headers, dict):
        return
    for key in list(headers.keys()):
        if isinstance(key, str) and key.lower() in SENSITIVE_HEADERS:
            headers[key] = _REDACTED


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """``before_send`` hook: drop request bodies, redact sensitive headers, and
    redact email/key shapes from message + exception values + embedded
    breadcrumbs. Mutates and returns ``event``. Never raises — a scrub failure
    withholds the event rather than risk leaking an unscrubbed one.
    """
    try:
        req = event.get("request")
        if isinstance(req, dict):
            # Request bodies are dropped ENTIRELY (ADR 0023 #11): we never need
            # the payload to triage a Machine error, and it is the single richest
            # PII surface.
            req.pop("data", None)
            _scrub_headers(req.get("headers"))

        msg = event.get("message")
        if isinstance(msg, str):
            event["message"] = redact_text(msg)

        logentry = event.get("logentry")
        if isinstance(logentry, dict):
            if isinstance(logentry.get("message"), str):
                logentry["message"] = redact_text(logentry["message"])
            # The %s params carry the actual values the template interpolates —
            # scrubbing only the template left them verbatim, and a client
            # address in SMD-OPERATOR-10 was caught by Sentry's server-side
            # scrubbing, not ours (ss-console #2150).
            params = logentry.get("params")
            if isinstance(params, (list, tuple, dict)):
                logentry["params"] = _scrub_value(params)

        # ``set_extra`` / ``set_context`` payloads ship verbatim and were never
        # walked before (SMD-OPERATOR-10 shipped an unredacted filesystem path
        # this way). Walk every string leaf of both sections.
        for section in ("extra", "contexts"):
            sec = event.get(section)
            if isinstance(sec, dict):
                event[section] = _scrub_value(sec)

        exc = event.get("exception")
        if isinstance(exc, dict):
            for val in exc.get("values", []) or []:
                if isinstance(val, dict) and isinstance(val.get("value"), str):
                    val["value"] = redact_text(val["value"])

        breadcrumbs = event.get("breadcrumbs")
        crumbs = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
        for crumb in crumbs or []:
            scrub_breadcrumb(crumb)
    except Exception:  # noqa: BLE001 — scrubbing must never leak an unscrubbed event
        logger.exception("sentry: scrub_event raised; withholding event")
        return {
            "message": "[scrub-error: event withheld]",
            "level": event.get("level", "error") if isinstance(event, dict) else "error",
        }
    return event


def scrub_then_throttle(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The registered ``before_send`` hook: scrub, then throttle.

    Scrub runs unconditionally and first, so the throttle's key table only ever
    holds redacted text. Returns ``None`` when the throttle suppresses the event
    (a repeat of something already reported at this volume); see
    :mod:`shared.sentry_ratelimit`.
    """
    return throttle_event(scrub_event(event, hint))


def scrub_breadcrumb(
    crumb: dict[str, Any] | None, hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """``before_breadcrumb`` hook: redact email/key shapes from the breadcrumb
    message and any string-valued ``data`` entries. Never raises — a scrub
    failure drops the breadcrumb.
    """
    if not isinstance(crumb, dict):
        return crumb
    try:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = redact_text(crumb["message"])
        data = crumb.get("data")
        if isinstance(data, dict):
            for key, value in list(data.items()):
                data[key] = _scrub_value(value)
    except Exception:  # noqa: BLE001
        logger.exception("sentry: scrub_breadcrumb raised; dropping breadcrumb")
        return None
    return crumb


_initialized: set[str] = set()

#: Set to ``1`` to force init off a Fly Machine. For deliberately reproducing a
#: reporting bug locally — never in a test run or a normal dev loop.
_FORCE_ENV = "SMD_SENTRY_FORCE"


def _is_real_seat() -> bool:
    """True only inside a running Fly Machine.

    Why this gate exists. Until 2026-07-27 any process with ``SENTRY_DSN`` in its
    environment reported to the shared production project — including local runs
    and the test suite on a developer laptop, where ``infisical run`` injects the
    DSN. 919 of the project's 6,994 lifetime events (13%) came from one laptop,
    tagged ``environment: prod``, and they were the *loudest*-looking issues in
    the project: a 276-event "SMD OVERLAY ACTIVATION FAILED — refusing to serve
    an ungoverned operator", a 65-event "audit_log immutability violation:
    rejected SQL=UPDATE audit_log SET actor = 'forged'" (a unit test asserting
    the guard works), and pytest fixtures reporting as ``RuntimeError: kaboom``.

    That noise is not free: it trained the reader to treat Sentry mail as
    meaningless, and it competes for a 5,000-event monthly budget with the real
    seat signal it is drowning out.

    ``FLY_MACHINE_ID`` is the discriminator — set by the platform inside every
    Machine (and the value Sentry already records as ``server_name``), never set
    on a laptop. ``PYTEST_CURRENT_TEST`` additionally keeps a test run that
    happens to execute *on* a seat from reporting.
    """
    if (os.environ.get(_FORCE_ENV) or "").strip() == "1":
        return True
    if (os.environ.get("PYTEST_CURRENT_TEST") or "").strip():
        return False
    return bool((os.environ.get("FLY_MACHINE_ID") or "").strip())


def init_sentry(component: str) -> bool:
    """Initialize Sentry for one Machine process.

    Disabled-safe: returns ``False`` (no-op) when ``SENTRY_DSN`` is unset/empty,
    when this is not a real seat (see :func:`_is_real_seat`), or when the SDK is
    unavailable. Idempotent per ``component`` within a process. Tags the
    isolation scope with ``tenant=<slug>`` and ``component=<component>``.
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("sentry: disabled (no SENTRY_DSN) for component=%s", component)
        return False
    if not _is_real_seat():
        logger.info(
            "sentry: disabled (not a Fly Machine; set %s=1 to override) for component=%s",
            _FORCE_ENV,
            component,
        )
        return False
    if component in _initialized:
        return True
    try:
        import sentry_sdk
    except Exception as exc:  # noqa: BLE001
        logger.warning("sentry: sentry-sdk unavailable (%s); monitoring off", exc)
        return False

    slug = os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "unknown"
    environment = os.environ.get("SMD_ENV") or os.environ.get("SENTRY_ENVIRONMENT") or "prod"
    release = os.environ.get("SMD_OVERLAY_REF") or None
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            send_default_pii=False,
            before_send=scrub_then_throttle,
            before_breadcrumb=scrub_breadcrumb,
            # Errors only. No performance tracing: it adds cost and widens the
            # PII surface (span data) for no fleet-ops value.
            traces_sample_rate=0.0,
            # Never capture stack-frame local VARIABLE VALUES. The SDK default
            # (True) ships frame locals inside stacktraces, which the scrub
            # hooks do not reach — in gate/gateway frames those locals can hold
            # inbound mail or webhook payload content. Structural fix per the
            # request-body posture above: what is never captured needs no
            # scrubbing. This backs the customer-facing data-map representation
            # that Sentry receives technical error data only (engagements
            # DPA Exhibit B-1, 2026-07-27).
            include_local_variables=False,
        )
        sentry_sdk.set_tag("tenant", slug)
        sentry_sdk.set_tag("component", component)
    except Exception as exc:  # noqa: BLE001 — observability must never crash boot
        logger.warning("sentry: init failed for component=%s: %s", component, exc)
        return False

    _initialized.add(component)
    logger.info("sentry: initialized component=%s tenant=%s env=%s", component, slug, environment)
    # One-time boot marker sent to Sentry itself. Two purposes: (1) a DIRECT,
    # visible confirmation that this process's monitoring is live — the gateway
    # process filters our INFO log below root=WARNING, so this event is the only
    # first-class proof the gateway init fired; (2) a per-boot restart signal
    # (a Machine crash-looping shows a burst of these). Constant message so all
    # markers group into one Sentry issue, filterable by the component + tenant
    # tags already on the scope. Best-effort: a marker failure never unwinds a
    # good init.
    try:
        sentry_sdk.capture_message("boot: monitoring active", level="info")
    except Exception:  # noqa: BLE001 — the marker is diagnostic, never load-bearing
        logger.warning(
            "sentry: boot marker capture failed for component=%s", component, exc_info=True
        )
    return True


__all__ = [
    "SENSITIVE_HEADERS",
    "init_sentry",
    "redact_text",
    "scrub_breadcrumb",
    "scrub_event",
]
