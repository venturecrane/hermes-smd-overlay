"""Audit row construction and D1 emission.

Ported from ss-console/operator/adapter/audit_log.py (AuditLogWriter,
SHA-256 digesting, ULID generation, ISO-8601 timestamps, action_type
validation) and from ss-console/operator/adapter/audit_emit_points.py
(ToolCallTimer, build_per_tool_metadata, scope-aware metadata extraction).

The tool-classification helpers (``BannedToolError``, ``ToolClassification``,
``classify_tool``) live in ``shared.action_classes`` so the audit and trust
plugins share one source of truth (consolidation: task #33). They are
re-exported from this module's ``__all__`` so existing audit consumers
continue to import them by their original names.

In ss-console the writer talked to an injectable ``Executor`` Protocol with
two production implementations (Cloudflare D1 HTTP API and in-process
sqlite). On the Hermes Machine we go through the shared per-customer D1
binding instead — ``shared.d1_client.D1Client`` resolves the binding from
the Machine env and runtime-asserts the namespace matches the bound
customer slug. The plugin module never touches network code directly.

Substrate invariants preserved across the port:

  * action_type must be in ACCEPTED_ACTION_TYPES. Unknown action types
    raise ``ValueError`` before any SQL runs.
  * Payload bytes are never persisted — only the SHA-256 digest lands in
    the row. Caller writes the bytes elsewhere (R2) when required.
  * metadata serialization is deterministic (``sort_keys=True``, no
    whitespace) so the integrity check can compare across stores.
  * Every audit row carries a ULID id and an ISO-8601 UTC timestamp with
    millisecond precision and a trailing ``Z`` suffix.
  * Audit failures raise ``AuditWriteError`` from the writer. The hook
    wrappers in ``__init__.py`` catch the exception so the Hermes
    dispatcher is never destabilized by an unloggable action.
"""

import hashlib
import json
import logging
import re
import time
from typing import Any

from shared import object_identity
from shared.action_classes import (
    BannedToolError,
    ToolClassification,
    classify_tool,
)
from shared.audit_client import AuditWriteError
from shared.audit_contract import (
    CANONICAL_TOOL_CALL_KEY,
    DEPRECATED_TOOL_CALL_KEY,
    build_audit_params,
)
from shared.audit_contract import CHAIN_COLUMN_ALTERS as _CHAIN_COLUMN_ALTERS
from shared.audit_contract import CREATE_INDEX_SQL as _CREATE_INDEX_SQL
from shared.audit_contract import CREATE_TABLE_SQL as _CREATE_TABLE_SQL
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.cron_attribution import resolve_routine
from shared.ids import iso_utc as _iso_utc
from shared.ids import sha256 as _sha256
from shared.ids import ulid as _ulid
from shared.result_unwrap import parse_tool_result, peel_envelopes, unwrap_inbound
from shared.trust_decision import MATCH_NONE, TRUST_DECISIONS, TrustDecision

from .schemas import (
    ACCEPTED_ACTION_TYPES,
    SCOPE_KEYS,
    ActorRole,
    AuditEvent,
    HookActionClass,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


# AuditWriteError is canonically defined in ``shared.audit_client`` (imported at
# the top of this module) so the broker transport can raise it without importing
# this plugin layer. It is re-exported here (see __all__) for backward
# compatibility with existing importers.
#
# The hook wrappers catch it to keep the Hermes dispatcher healthy. The
# underlying invariant — an unloggable action must not execute — is preserved at
# the substrate layer above this plugin: the trust-ceiling enforcer
# (hermes-smd-trust) is the gate that blocks pre-call, not this observer plugin.


# ULID generation, ISO-8601 timestamps, and SHA-256 digesting are single-
# sourced in ``shared.ids`` (imported above as _ulid / _iso_utc / _sha256);
# the audit_log INSERT statement + column order in ``shared.audit_contract``
# (imported as _INSERT_SQL). All three audit writers share that one contract so
# a column reorder cannot desync them.


# ---------------------------------------------------------------------------
# Writer — talks to per-customer D1 through the shared D1Client
# ---------------------------------------------------------------------------


class AuditLogWriter:
    """Single-row-per-call audit log writer backed by the per-customer D1Client.

    Construction takes a ``D1Client`` (from ``shared.d1_client``). The writer
    holds no other state, so a single instance per Machine is sufficient and
    concurrency-safe — the D1Client owns its own connection semantics.
    """

    def __init__(
        self,
        client: Any,
        *,
        clock: Any = None,
        ulid_now_ms: Any = None,
    ) -> None:
        """Bind to a D1Client (or anything that exposes ``execute(sql, *params)``).

        Args:
            client: A ``shared.d1_client.D1Client`` (or a duck-typed object
                exposing the same ``execute(sql, *params)`` API). Tests pass
                a lightweight fake that records the SQL + params for inspection.
            clock: Optional callable returning a ``datetime`` for the row ``ts``.
                Used in tests to make timestamps deterministic.
            ulid_now_ms: Optional callable returning an integer epoch-ms for the
                ULID timestamp portion. Used in tests.
        """
        self._client = client
        self._clock = clock
        self._ulid_now_ms = ulid_now_ms

    def ensure_schema(self) -> None:
        """Idempotently create the audit_log table + indexes if absent.

        The Machine's bootstrap does not apply the ss-console per-customer
        migrations, so the table this writer targets may not exist — without
        this, the first write hits "no such table" and audit_log is never
        created (ss-console#1285). ``CREATE ... IF NOT EXISTS`` is safe whether
        or not a future bootstrap migration step lands. Runs against the raw
        D1Client (the immutability D1Executor blocks UPDATE/DELETE/DROP, not
        CREATE). Called once at plugin register.
        """
        self._client.execute(_CREATE_TABLE_SQL)
        for index_sql in _CREATE_INDEX_SQL:
            self._client.execute(index_sql)
        # Chain-column upgrade for pre-#1686 ledgers. "duplicate column name"
        # means already upgraded — expected on every boot after the first.
        for alter_sql in _CHAIN_COLUMN_ALTERS:
            try:
                self._client.execute(alter_sql)
            except Exception:  # noqa: BLE001 — duplicate-column is the normal case
                pass

    def write(self, event: AuditEvent) -> str:
        """Insert one audit_log row. Returns the inserted ULID.

        Raises:
            ValueError: ``action_type`` is not in ``ACCEPTED_ACTION_TYPES``.
            AuditWriteError: the underlying D1 client raised. The hook
                wrappers catch this; never re-raise out of a hook.
        """
        if event.action_type not in ACCEPTED_ACTION_TYPES:
            raise ValueError(f"action_type {event.action_type!r} not in ACCEPTED_ACTION_TYPES")

        now_dt = self._clock() if self._clock else None
        now_ms = self._ulid_now_ms() if self._ulid_now_ms else None
        ulid = _ulid(now_ms=now_ms)
        ts = _iso_utc(now_dt)

        actor_role_value: str | None
        if isinstance(event.actor_role, ActorRole):
            actor_role_value = event.actor_role.value
        else:
            actor_role_value = event.actor_role

        params = build_audit_params(
            row_id=ulid,
            ts=ts,
            action_type=event.action_type,
            actor=event.actor,
            actor_role=actor_role_value,
            skill_name=event.skill_name,
            matter_ref=event.matter_ref,
            input_digest=_sha256(event.input_payload),
            output_digest=_sha256(event.output_payload),
            diff_digest=_sha256(event.diff_payload),
            trust_ceiling=event.trust_ceiling,
            metadata=event.metadata,
        )

        try:
            self._client.execute(_INSERT_SQL, *params)
        except Exception as exc:  # noqa: BLE001 — re-raise as audit-specific
            # Never log the metadata or payload values — they may contain PII.
            logger.error(
                "audit_log INSERT failed: action_type=%s actor=%s skill=%s err=%s",
                event.action_type,
                event.actor,
                event.skill_name,
                exc,
            )
            raise AuditWriteError(
                f"audit_log INSERT failed for action_type={event.action_type}"
            ) from exc

        return ulid


# ---------------------------------------------------------------------------
# Per-tool timing + metadata builder
#
# Tool classification (``classify_tool`` / ``ToolClassification`` /
# ``BannedToolError``) is imported from ``shared.action_classes`` above and
# re-exported via ``__all__``.
# ---------------------------------------------------------------------------


class ToolCallTimer:
    """Monotonic per-tool-call latency timer. Millisecond precision.

    Single-shot: ``start()`` and ``stop()`` may each be called exactly once.
    Misuse raises ``RuntimeError`` so double-reports are caught early.
    """

    __slots__ = ("_started_perf", "_duration_ms")

    def __init__(self) -> None:
        self._started_perf: float | None = None
        self._duration_ms: float | None = None

    def start(self) -> "ToolCallTimer":
        """Begin timing. Returns self so callers can chain."""
        if self._started_perf is not None:
            raise RuntimeError("ToolCallTimer.start called twice on the same timer")
        self._started_perf = time.perf_counter()
        return self

    def stop(self) -> float:
        """Finish timing and return elapsed milliseconds."""
        if self._started_perf is None:
            raise RuntimeError("ToolCallTimer.stop called before start")
        if self._duration_ms is not None:
            raise RuntimeError("ToolCallTimer.stop called twice")
        elapsed = (time.perf_counter() - self._started_perf) * 1000.0
        self._duration_ms = elapsed
        return elapsed

    @property
    def duration_ms(self) -> float | None:
        """Read the last-measured duration. ``None`` if ``stop()`` has not run."""
        return self._duration_ms


def extract_scope_metadata(arguments: dict | None) -> dict[str, str]:
    """Lift scope-aware fields from a tool's arguments dict into metadata.

    Returns a dict with at most the keys in ``SCOPE_KEYS``. Missing or None
    values are omitted. Non-string values are coerced via ``str()`` so the
    audit row stays JSON-serializable; the dashboard treats these as opaque
    strings.
    """
    if not arguments:
        return {}
    out: dict[str, str] = {}
    for key in SCOPE_KEYS:
        value = arguments.get(key)
        if value is None:
            continue
        out[key] = str(value)
    return out


def build_per_tool_metadata(
    *,
    customer: str,
    tool_name: str,
    action_class: HookActionClass,
    outcome: str,
    skill_name: str | None = None,
    skill_version: str | None = None,
    ceiling_level: str | None = None,
    error_type: str | None = None,
    duration_ms: float | None = None,
    tool_call_id: str | None = None,
    arguments: dict | None = None,
    result: Any = None,
    unmapped: bool = False,
    banned_reason: str | None = None,
    trust: TrustDecision | None = None,
    trust_decision_match: str = MATCH_NONE,
) -> dict:
    """Build the canonical ``metadata`` dict for one per-tool audit row.

    Canonical keys (stable consumers depend on these):

    - per_tool_audit:       True
    - customer:             str (customer slug)
    - skill:                str | None
    - skill_version:        str | None
    - tool:                 str
    - action_class:         str (HookActionClass value) — the COARSE class, from
                            the tool name alone
    - ceiling_level:        str | None (the EFFECTIVE ceiling actually applied)
    - outcome:              str ("ok" | "error" | "blocked" | "shortfall")
    - shortfall_code:       str — set when outcome is "shortfall" (v3): the
                            closed-vocabulary reason token, never prose.
    - error_type:           str | None
    - duration_ms:          float | None
    - tool_call_id:         str | None — THE tool-call correlation key. audit_log
                            has no column for it (shared/audit_contract.py
                            COLUMNS), so correlating one dispatch across emitters
                            is a json_extract on this name, and every emitter
                            must spell it the same way (ss-console #2312).
    - trace_id:             str | None — DEPRECATED alias of tool_call_id, same
                            value. Kept only so a correlation query still reaches
                            rows written before #2312, which carry the old name
                            alone. Retire once the audit retention window has
                            cleared those rows.
    - trust_decision_match: str ("tool_call_id" | "sequential" | "none")
    - unmapped_tool:        True iff the tool was not in the registry
    - banned_tool:          True iff the tool was banned
    - banned_reason:        str (set when banned_tool is True)
    - matter_id:            str (set when arguments has one)
    - customer_segment:     str (set when arguments has one)

    The OBJECT the call touched (ss-console#2497 — ``shared.object_identity``).
    Present only on the tools that have one to name, and only when the tool
    actually produced it; a tool that mints no id contributes no key rather than
    an empty one. Until this, a row could say a document was read on a matter and
    not which document, and a memo was written and not which memo or what it
    said, which is the difference between a record and a list of verbs:

    - document_id:          str — ``mcp_smokeball_read_document``. From the
                            result's ``fileId``/``file_id``/``document_id``,
                            falling back to the args (the result is the source
                            system's echo; the args are the model's composition).
    - document_ids:         list[str] — ``mcp_smokeball_get_files_on_matter``:
                            the ids the listing exposed, capped, with
                            ``document_ids_truncated`` set when it stopped short.
    - memo_id:              str — ``mcp_smokeball_create_memo``, when the write
                            echoes one.
    - draft_id:             str — the mail ``create_draft``/``update_draft``
                            tools, via the same extractor the send gate uses.
    - written_body_sha256:  str — sha256 of the body a WRITE actually wrote
                            (``create_memo``'s ``text``, ``smd_deliver_draft``'s
                            ``body``, a draft's body). Never the body itself: the
                            digest proves the artifact in the firm's system is
                            the one this row describes, and holds no content.
    - written_body_field:   str — WHICH argument was digested, so the digest can
                            be reproduced and checked rather than trusted.
    - seam:                 str — ``smd_deliver_draft``'s declared destination.
                            That tool returns a sentence and mints no id, so the
                            digest plus the seam is the whole identity it has.

    The trust trail (present only when ``trust`` is supplied — i.e. when the
    gate's decision for this exact call was found):

    - resolved_action_class: str — the class after recipient reclassification.
      ``action_class`` above is what the tool NAME resolves to; this is what the
      entitlement was actually evaluated against, and for a send they differ
      (``external_send`` vs ``external_send_client`` / ``_vendor`` / ``_internal``).
      Both are recorded because they answer different questions.
    - authored_ceiling:      str | None — what the persona authored for the class
    - vertical_floor:        str | None — the pack floor, when one is declared
    - trust_decision:        str — allow | draft | refuse | await_approval
    - trust_allowed:         bool — whether the call was permitted to dispatch
    - trust_reason:          str — the gate's own words for why
    - trust_persona:         str — the persona the exposure resolved for
    - session_resolution:    str — how the gate resolved the session it keyed
      every per-session register off (``shared.provenance`` ``MODE_*``:
      ``keyed`` | ``thread`` | ``process_singleton`` | ``ambiguous`` | ``none``).
      Core drops ``session_id`` on the pre-hook path (#141), so a fallback there
      is routine; what was missing was any record that one occurred
      (ss-console #2288).

    ``trust.effective_ceiling`` wins over the ``ceiling_level`` argument when a
    decision is supplied: the gate's resolution is authoritative, and the
    argument exists for callers that have a ceiling but no decision. Ceiling
    fields stay ``None`` rather than becoming a placeholder string, because
    "unauthored" and "indeterminate" are different facts about authorization.
    """
    metadata: dict = {
        "per_tool_audit": True,
        "customer": customer,
        "skill": skill_name,
        "skill_version": skill_version,
        "tool": tool_name,
        "action_class": action_class.value,
        "ceiling_level": trust.effective_ceiling if trust is not None else ceiling_level,
        "outcome": outcome,
        "error_type": error_type,
        "duration_ms": duration_ms,
        # Canonical first, deprecated alias second — same value under both names
        # during the transition. See the docstring above and ss-console #2312.
        CANONICAL_TOOL_CALL_KEY: tool_call_id,
        DEPRECATED_TOOL_CALL_KEY: tool_call_id,
        # Always stamped, including "none": a row with no trust provenance must
        # SAY it has none rather than look like a row that predates the field.
        "trust_decision_match": trust_decision_match,
    }

    if trust is not None:
        metadata["resolved_action_class"] = trust.action_class
        metadata["authored_ceiling"] = trust.authored_ceiling
        metadata["vertical_floor"] = trust.vertical_floor
        metadata["trust_decision"] = trust.audit_action
        metadata["trust_allowed"] = trust.allowed
        metadata["trust_reason"] = trust.reason
        metadata["trust_persona"] = trust.persona
        if trust.session_match:
            metadata["session_resolution"] = trust.session_match

    if unmapped:
        metadata["unmapped_tool"] = True

    if banned_reason is not None:
        metadata["banned_tool"] = True
        metadata["banned_reason"] = banned_reason

    scope = extract_scope_metadata(arguments)
    if scope:
        metadata.update(scope)

    # WHAT the call touched (ss-console#2497). Applied after the scope keys and
    # before nothing: object identity never overwrites a scope key (the two key
    # sets are disjoint by construction — SCOPE_KEYS is matter_id /
    # customer_segment), so ordering here is documentation, not a precedence.
    identity = object_identity.extract(tool_name, arguments, result)
    if identity:
        metadata.update(identity)

    return metadata


# ---------------------------------------------------------------------------
# Per-hook emission helpers
#
# These are the entry points the hook wrappers in __init__.py call. Each
# accepts the kwargs Hermes fires at the documented hook surface
# (docs/hook-surface.md) and writes one D1 row through the supplied
# AuditLogWriter. The helpers are sync because shared.d1_client.D1Client
# is sync; Hermes invokes hook callbacks synchronously from the dispatch
# path.
# ---------------------------------------------------------------------------


# Structured-error keys we recognize in a tool result, in priority order.
# Tools that surface a failure do so through one of these conventional shapes;
# anything else is treated as success. We never FABRICATE an error — absence of
# a recognized error signal yields "ok".
#
# 1 = always-"ok" (bug); 2 = error-detecting; 3 = sees through the inbound
# fence and the dispatcher envelope, and reports a tool that ANSWERED but did
# not give the person what they asked for as "shortfall" (ss-console shortfall
# notifications). A v3 "shortfall" row would have been "ok" under v2.
_OUTCOME_SEMANTICS_VERSION = 3

#: ``status`` values that mean the tool answered and did not do the thing. Each
#: is a closed enum token a connector writes (ss-console
#: ``operator/connectors/smokeball``: ``letter_tools._refused``,
#: ``medicals_tools``), never prose.
_SHORTFALL_STATUSES = frozenset(
    {"refused", "needs_contact", "readback_mismatch", "link_not_visible"}
)

#: A token a shortfall ``code`` may carry: an enum word, never a sentence. A
#: reason that is not token-shaped is prose (a refusal message, an exception
#: string) and is replaced by the status word next to it, because prose can
#: quote a client's name, a document's words, or an address.
_CODE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")

#: medchron's ``accepted: false`` carries a prose ``reason`` (the broker's
#: sentence for the person). The refusals that are LIMITS rather than breakages
#: are mapped to tokens here by phrase; every other refusal is
#: ``not_accepted``. The phrases are the broker's own wording in ss-console
#: ``operator/workspace_broker/medchron_verbs.py``.
_MEDCHRON_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("allowance is spent", "allowance_spent"),
    ("no page allowance is authored", "allowance_unauthored"),
)


def _code_token(value: Any) -> str | None:
    """``value`` when it is an enum-shaped token, else ``None``."""
    if isinstance(value, str) and _CODE_TOKEN_RE.match(value.strip()):
        return value.strip()
    return None


def _error_shape(parsed: dict) -> tuple[str, str | None] | None:
    """``("error", error_type)`` when ``parsed`` carries a recognized error
    shape, else ``None``. The v2 rules, unchanged."""

    def _error_type(default: str | None) -> str | None:
        for key in ("error_type", "code", "type"):
            val = parsed.get(key)
            if isinstance(val, str) and val:
                return val
        return default

    if parsed.get("error"):
        err = parsed["error"]
        return ("error", _error_type(err if isinstance(err, str) and err else None))
    if parsed.get("is_error") is True or parsed.get("isError") is True:
        return ("error", _error_type(None))
    status = parsed.get("status")
    if isinstance(status, str) and status.lower() in ("error", "failure", "failed"):
        return ("error", _error_type(status))
    if parsed.get("ok") is False or parsed.get("success") is False:
        return ("error", _error_type(None))
    return None


def _shortfall_code(parsed: dict) -> str | None:
    """The closed-vocabulary code for a tool that answered and did not deliver,
    or ``None`` when the answer is not a shortfall.

    PROTOCOL IS NOT A SHORTFALL. ``read_attachment_pages`` answers a whole-bundle
    read of a long bundle with ``windowRequired`` and refuses a malformed window
    with a ``window_*`` reason; both are the tool telling the model how to ask,
    and the model asking again is the expected next step.
    """
    status = parsed.get("status")
    if isinstance(status, str) and status.strip().lower() in _SHORTFALL_STATUSES:
        word = status.strip().lower()
        if word == "refused":
            return _code_token(parsed.get("reason")) or word
        return word
    if parsed.get("readable") is False:
        reason = parsed.get("reason")
        if isinstance(reason, str) and reason.startswith("window_"):
            return None
        return _code_token(reason) or "unreadable"
    if parsed.get("accepted") is False:
        reason = parsed.get("reason")
        text = reason.lower() if isinstance(reason, str) else ""
        for phrase, code in _MEDCHRON_REASON_CODES:
            if phrase in text:
                return code
        return "not_accepted"
    if parsed.get("needsHumanRead") is True:
        return _code_token(parsed.get("extractionReason")) or "needs_human_read"
    return None


def _outcome_from_result(result: Any) -> tuple[str, str | None]:
    """Infer ``(outcome, code)`` from a Hermes tool result.

    Hermes' ``post_tool_call`` passes ``result`` as a str (usually JSON).
    Recording every call as ``"ok"`` — the v1 behavior — makes the audit
    ledger unable to distinguish a failed tool call from a successful one, which
    is unacceptable for a compliance ledger. Outcome semantics are versioned
    (``_OUTCOME_SEMANTICS_VERSION``) and stamped into metadata so an auditor can
    tell the eras apart without any historical row being rewritten.

    Outcomes:

    * ``"error"`` — a recognized structured-error shape, with the upstream error
      type as the second element:
      ``{"error": <truthy>}`` → ``error_type``/``code``/``type`` if present,
      else the stringified ``error``; ``{"is_error": true}`` /
      ``{"isError": true}``; ``{"status": "error"|"failure"|"failed"}``;
      ``{"ok": false}`` / ``{"success": false}``.
    * ``"shortfall"`` (v3) — the tool ANSWERED and did not give the person what
      they asked for: ``status`` in ``_SHORTFALL_STATUSES``, ``readable:
      false``, ``accepted: false``, ``needsHumanRead: true``. The second
      element is a closed-vocabulary CODE (``_shortfall_code``), never prose.
    * ``"ok"`` — anything else, including an unparseable or unrecognized
      result: we never fabricate a failure.

    v3 READS THROUGH THE WRAPPERS FIRST (``shared.result_unwrap``). On Hermes
    v0.20.4 ``transform_tool_result`` runs before ``post_tool_call``
    (``hermes-smd-establishment`` ``on_post_tool_call``), so a fenced read
    tool's result reached this function inside the quarantine fence, did not
    start with ``{``, and scored ``ok``; an enveloped ``{"result": "<json>"}``
    parsed to an object with no error keys and scored ``ok``. The OUTER object
    is still checked for an error shape before the peel, so a wrapper that
    itself reports failure (``{"ok": false, "result": ...}``) keeps the v2
    verdict.
    """
    if isinstance(result, str) and result:
        text = unwrap_inbound(result).lstrip()
        if not text.startswith("{"):
            return ("ok", None)  # not a JSON object; nothing structured to read.
        try:
            outer = json.loads(text)
        except (ValueError, TypeError):
            return ("ok", None)  # unparseable — fail toward "ok", never fabricate.
    elif isinstance(result, dict):
        outer = result
    else:
        # No inspectable payload — do not assert failure; the duration +
        # registry carry the load-bearing signal.
        return ("ok", None)

    if not isinstance(outer, dict):
        return ("ok", None)

    error = _error_shape(outer)
    if error is not None:
        return error
    inner = peel_envelopes(outer)
    if not isinstance(inner, dict):
        return ("ok", None)
    if inner is not outer:
        error = _error_shape(inner)
        if error is not None:
            return error
    code = _shortfall_code(inner)
    if code is not None:
        return ("shortfall", code)
    return ("ok", None)


# ---------------------------------------------------------------------------
# Bundle stamps (ss-console shortfall notifications, the "partial" class)
#
# A day's post arrives as ONE scanned PDF. The mail routine reads it
# (``read_attachment_pages``: ``sha256`` + ``pageCount``) and files it a page
# range at a time (``file_attachment_pages_to_matter``, or
# ``stage_vendor_invoice`` for a bill). On 2026-10-01 a 52-page bundle was read
# and nothing was filed, and every call scored "ok": the pages that were never
# filed had no row to be counted from. These stamps put the bundle's identity
# and page arithmetic on the rows, so the heartbeat can compare pages read
# against pages filed or held for approval. Hashes and integers only — never a
# file name, a matter, or a word of the document.
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: The connector tool names (after the ``mcp_<server>_`` prefix) that FILE a
#: page range out of a read bundle.
_FILING_TOOL_SUFFIXES = ("file_attachment_pages_to_matter", "stage_vendor_invoice")

#: The success ``status`` each filing tool returns. Anything else filed nothing.
_FILING_SUCCESS_STATUSES = frozenset({"filed", "staged"})

#: The bundle READ tool. Its result names the bundle's ``sha256`` and
#: ``pageCount`` on every answer, windowed or refused.
_BUNDLE_READ_SUFFIX = "read_attachment_pages"


def _tool_suffix_is(tool_name: str, suffix: str) -> bool:
    return tool_name == suffix or tool_name.endswith("_" + suffix)


def _sha_arg(args: dict | None) -> str | None:
    if not isinstance(args, dict):
        return None
    value = args.get("sha256")
    if isinstance(value, str) and _SHA256_RE.match(value.strip().lower()):
        return value.strip().lower()
    return None


def _page_range_arg(args: dict | None) -> list[int] | None:
    """``[first, last]`` from the filing args, or ``None`` when absent or not a
    range. Normalized to ints; never trusted beyond being a range."""
    if not isinstance(args, dict):
        return None
    try:
        first = int(args.get("first_page"))  # type: ignore[arg-type]
        last = int(args.get("last_page"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if first < 1 or last < first:
        return None
    return [first, last]


def bundle_stamps(
    tool_name: str,
    args: dict | None,
    result: Any,
    *,
    outcome: str,
    trust_decision: str | None,
) -> dict[str, Any]:
    """The bundle metadata for one tool call, possibly empty.

    * A bundle READ stamps ``bundle_sha256`` + ``bundle_page_count`` from the
      RESULT (the connector's count, not the model's).
    * A filing call that SUCCEEDED stamps ``bundle_sha256`` +
      ``bundle_pages_filed`` (``[[first, last]]``) from the args the connector
      verified the bytes against. A refused filing filed nothing and stamps
      nothing.
    * A filing call the trust gate HELD for approval stamps
      ``bundle_pages_pending``: those pages are not lost, they are waiting on a
      person, and a run that held them is not a run that dropped them.
    """
    out: dict[str, Any] = {}
    if _tool_suffix_is(tool_name, _BUNDLE_READ_SUFFIX):
        parsed = parse_tool_result(result)
        if isinstance(parsed, dict):
            sha = parsed.get("sha256")
            count = parsed.get("pageCount")
            if (
                isinstance(sha, str)
                and _SHA256_RE.match(sha.lower())
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            ):
                out["bundle_sha256"] = sha.lower()
                out["bundle_page_count"] = count
        return out
    if not any(_tool_suffix_is(tool_name, s) for s in _FILING_TOOL_SUFFIXES):
        return out
    sha = _sha_arg(args)
    pages = _page_range_arg(args)
    if sha is None or pages is None:
        return out
    if trust_decision == "await_approval":
        out["bundle_sha256"] = sha
        out["bundle_pages_pending"] = [pages]
        return out
    if outcome != "ok":
        return out
    parsed = parse_tool_result(result)
    status = parsed.get("status") if isinstance(parsed, dict) else None
    if isinstance(status, str) and status.strip().lower() in _FILING_SUCCESS_STATUSES:
        out["bundle_sha256"] = sha
        out["bundle_pages_filed"] = [pages]
    return out


# ---------------------------------------------------------------------------
# Object + procedure identity (the shortfall query's joins)
# ---------------------------------------------------------------------------

#: Args that name WHAT a call acted on, for "was this refused call retried on
#: the same thing, and did it work then". ``download_url`` only stands in when
#: there is no ``sha256``: a re-spool mints a fresh token for the same bytes.
_OBJECT_ARG_KEYS = (
    "sha256",
    "file_id",
    "fileId",
    "document_id",
    "attachment_id",
    "message_id",
    "job_id",
    "matter_id",
    "first_page",
    "last_page",
)


def object_digest(args: dict | None) -> str | None:
    """A 32-hex digest of the identifying args, or ``None`` when there are none.

    A digest, not the values: the shortfall query only needs EQUALITY between a
    refused call and a later one, and the values include URLs and ids that have
    no business riding a heartbeat.
    """
    if not isinstance(args, dict):
        return None
    parts: list[str] = []
    for key in _OBJECT_ARG_KEYS:
        value = args.get(key)
        if value is None or value == "":
            continue
        parts.append(f"{key}={str(value).strip().lower()}")
    if not args.get("sha256"):
        url = args.get("download_url")
        if isinstance(url, str) and url.strip():
            parts.append(f"download_url={url.strip()}")
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([a-z0-9][a-z0-9-]{0,63})/SKILL\.md$")


def skill_procedure(tool_name: str, args: dict | None) -> str | None:
    """The skill whose PROCEDURE this call read, or ``None``.

    The router runs another skill's procedure inside its own session by reading
    it first (``skill_view`` of the skill, or ``read_file`` of its SKILL.md —
    ``shared.read_volume`` watches both for the same reason). A webhook turn has
    no routine, so this read is the only mechanical record of which procedure
    the session was running. Only a slug-shaped name is kept.
    """
    if not isinstance(args, dict):
        return None
    if tool_name == "skill_view":
        for key in ("name", "skill", "slug"):
            value = args.get(key)
            if isinstance(value, str) and _SKILL_SLUG_RE.match(value.strip()):
                return value.strip()
        return None
    if tool_name == "read_file":
        for key in ("path", "file_path", "filename"):
            value = args.get(key)
            if isinstance(value, str):
                m = _SKILL_PATH_RE.search(value.strip())
                if m:
                    return m.group(1)
    return None


#: The hook's ``status`` / ``error_type`` vocabularies (docs/hook-surface.md
#: §2). Stamped only when one of these exact words, so the row never carries an
#: upstream string this plugin has not read.
_HOOK_STATUSES = frozenset({"ok", "error", "blocked"})
_HOOK_ERROR_TYPES = frozenset({"tool_error", "plugin_block"})

#: The host's words for a pre-call callback that did not answer in time; the
#: same phrase match ``hermes-smd-audit``'s ``_is_callback_timeout`` uses.
_CALLBACK_TIMEOUT_MARKERS = ("callback timed out", "is still running")


def hook_stamps(hook_status: Any, hook_error_type: Any, result: Any) -> dict[str, Any]:
    """The post-hook's own envelope, in its closed vocabulary.

    A plugin BLOCK is the policy layer saying no, which the shortfall query
    files under "not allowed"; a callback that never answered is not a no, and
    is marked so it is filed with failures instead (the same split
    ``_meter_loop_arms`` draws for the sticky-stop ladder).
    """
    out: dict[str, Any] = {}
    if hook_status in _HOOK_STATUSES:
        out["hook_status"] = hook_status
    if hook_error_type in _HOOK_ERROR_TYPES:
        out["hook_error_type"] = hook_error_type
    if hook_status == "blocked" and hook_error_type == "plugin_block":
        text = result.lower() if isinstance(result, str) else ""
        if text and all(marker in text for marker in _CALLBACK_TIMEOUT_MARKERS):
            out["callback_timeout"] = True
        else:
            out["plugin_block"] = True
    return out


def emit_tool_event(
    writer: AuditLogWriter,
    *,
    customer: str,
    tool_name: str,
    args: dict | None,
    result: Any,
    task_id: str,
    session_id: str,
    tool_call_id: str,
    duration_ms: int | None,
    actor: str = "agent",
    actor_role: ActorRole = ActorRole.AGENT,
    skill_name: str | None = None,
    hermes_home_for_attribution: str | None = None,
    hook_status: str | None = None,
    hook_error_type: str | None = None,
) -> str | None:
    """Write one ``TOOL_CALL_COMPLETED`` audit row for a post_tool_call event.

    Handles three cases:

      * Banned tool name → emit an ``INVARIANT_VIOLATION`` row with
        ``metadata.banned_tool=true`` and ``outcome=blocked``.
      * Known tool → look up action class, build metadata, emit
        ``TOOL_CALL_COMPLETED``.
      * Unknown tool → fail-closed action class is REFUSED (issue #1327);
        metadata is tagged ``unmapped_tool=true`` so the dashboard surfaces it.

    PROVENANCE (#2122). The row also carries WHAT AUTHORIZED the call, not only
    what it was: the trust gate's decision for this exact call is collected from
    ``shared.trust_decision`` (the pre→post seam — the two hooks live in
    different plugins, which cannot import each other) and lands as the
    effective ``ceiling_level``, the ``trust_ceiling`` COLUMN, the resolved typed
    action class, and the rest of the trail. ``matter_ref`` is populated from the
    matter id the arguments already carried; the column had been NULL on every
    row while the id sat in metadata.

    Returns the inserted ULID, or ``None`` if the write failed (the writer
    raised ``AuditWriteError`` and the hook wrapper swallowed it).
    """
    # Single-use: taken here so exactly one row can ever claim this decision.
    # Guarded because provenance is an ENRICHMENT of the row and the row itself
    # is the obligation — a register fault must degrade to a row with no trust
    # trail, never to no row at all.
    try:
        trust, trust_match = TRUST_DECISIONS.take(tool_call_id or "", tool_name)
    except Exception:  # noqa: BLE001 — never lose an audit row over its enrichment
        logger.warning("audit: trust-decision lookup failed; row emitted without the trail")
        trust, trust_match = None, MATCH_NONE

    # ATTRIBUTION (#2122). A cron-fired session's id embeds the live job id;
    # resolve it to the stable managed routine name NOW, while the id → name
    # mapping still exists (job ids rotate on re-materialization). Resolution
    # never raises; a non-cron session leaves skill_name as passed (honest
    # NULL for interactive/inbound turns, which no routine fired).
    routine = (
        resolve_routine(session_id, hermes_home=hermes_home_for_attribution)
        if skill_name is None
        else None
    )
    if routine is not None and routine.skill:
        skill_name = routine.skill

    try:
        classification = classify_tool(tool_name)
        action_class = classification.action_class
        unmapped = classification.unmapped
        banned_reason: str | None = None
        outcome, error_type = _outcome_from_result(result)
        action_type = "TOOL_CALL_COMPLETED"
        # A shortfall's code is NOT an error type: it lands in its own key so
        # every consumer that reads ``error_type`` keeps reading only errors.
        shortfall_code: str | None = None
        if outcome == "shortfall":
            shortfall_code, error_type = error_type, None
    except BannedToolError as exc:
        # The dispatch path SHOULD have caught this before the tool ran,
        # but the audit plugin still emits a refusal row if a banned tool
        # name reaches the post_tool_call seam (defense in depth).
        action_class = HookActionClass.EXTERNAL_SEND
        unmapped = False
        banned_reason = exc.reason
        outcome = "blocked"
        error_type = None
        shortfall_code = None
        action_type = "INVARIANT_VIOLATION"

    metadata = build_per_tool_metadata(
        customer=customer,
        tool_name=tool_name,
        action_class=action_class,
        outcome=outcome,
        skill_name=skill_name,
        error_type=error_type,
        duration_ms=float(duration_ms) if duration_ms is not None else None,
        tool_call_id=tool_call_id or None,
        arguments=args,
        # ss-console#2497: the result is what names the object the call touched
        # (a created memo id, a read document's id). It was already in scope here
        # for the outcome inference and simply never reached the metadata builder.
        result=result,
        unmapped=unmapped,
        banned_reason=banned_reason,
        trust=trust,
        trust_decision_match=trust_match,
    )

    # Carry session/task identifiers in metadata so the dashboard can
    # pivot between rows without needing dedicated columns.
    if session_id:
        metadata["session_id"] = session_id
    if task_id:
        metadata["task_id"] = task_id
    # The cross-attribution detector (ss-console #2288). The pre-hook and the
    # post-hook bracket ONE dispatch, so they are the same call in the same
    # session — core simply drops the id on the way in (#141) and supplies it on
    # the way out. If the gate resolved a DIFFERENT session than the one this row
    # is being written for, a peer's registers gated this call, and the row is
    # the only place that can ever say so. Presence is the signal; the value is
    # the session an investigator needs to pull next.
    if (
        trust is not None
        and trust.session_resolved
        and session_id
        and trust.session_resolved != session_id
    ):
        metadata["session_resolution_conflict"] = trust.session_resolved
        logger.warning(
            "audit: tool %s was gated under session %s but completed under %s "
            "(cross-session resolution — ss-console #2288)",
            tool_name,
            trust.session_resolved,
            session_id,
        )
    if routine is not None:
        # The durable routine identity + the ephemeral job id it resolved
        # from. The name survives id rotation; the id lets an auditor tie the
        # row to a specific materialization epoch while it lived.
        metadata["routine"] = routine.job_name
        metadata["cron_job_id"] = routine.job_id
    # ss-console shortfall notifications. Every key below is a closed-vocabulary
    # token, a hash, or an integer: the heartbeat ships these off the seat, so
    # nothing here may carry a name, an address, or a word of a document.
    if shortfall_code is not None:
        metadata["shortfall_code"] = shortfall_code
    metadata.update(hook_stamps(hook_status, hook_error_type, result))
    metadata.update(
        bundle_stamps(
            tool_name,
            args,
            result,
            outcome=outcome,
            trust_decision=trust.audit_action if trust is not None else None,
        )
    )
    digest = object_digest(args)
    if digest is not None:
        metadata["object_digest"] = digest
    procedure = skill_procedure(tool_name, args)
    if procedure is not None:
        metadata["skill_procedure"] = procedure
    # Stamp the outcome-semantics version so an auditor can distinguish
    # error-detecting rows (v2+) from the legacy always-"ok" rows (v1). No
    # historical row is ever rewritten — the version is the changepoint marker.
    metadata["outcome_semantics_version"] = _OUTCOME_SEMANTICS_VERSION

    event = AuditEvent(
        action_type=action_type,
        actor=actor,
        actor_role=actor_role,
        skill_name=skill_name,
        # The two COLUMNS the schema has always had for exactly these facts, and
        # which every live row left NULL (#2122). Neither is a schema change:
        # both already exist in ``shared.audit_contract.COLUMNS`` and in the
        # hash-chain input, so populating them changes the canonical body of NEW
        # rows only — every existing row's stored hash keeps verifying against
        # its own stored values, exactly as before.
        #
        # matter_ref: the matter id the scope extractor already lifted into
        # metadata. The value was captured all along; it just never reached the
        # column an auditor filters and indexes on. ``or None`` because that
        # extractor coerces with str(), so a blank id would land as "" — which
        # the chain canonicalizes distinctly from NULL and which reads as a
        # matter reference that is present but empty. Absent is absent.
        matter_ref=metadata.get("matter_id") or None,
        # trust_ceiling: the effective ceiling the gate actually applied.
        trust_ceiling=trust.effective_ceiling if trust is not None else None,
        metadata=metadata,
    )
    return writer.write(event)


def emit_llm_event(
    writer: AuditLogWriter,
    *,
    customer: str,
    session_id: str,
    user_message: str,
    assistant_response: str,
    model: str,
    platform: str,
    actor: str = "agent",
    actor_role: ActorRole = ActorRole.AGENT,
    hermes_home_for_attribution: str | None = None,
) -> str | None:
    """Write one ``LLM_TURN_COMPLETED`` audit row for a post_llm_call event.

    The user message and assistant response are NEVER stored verbatim — the
    writer takes a bytes object and persists only the SHA-256 digest. The
    caller (or a downstream content-archive worker) is responsible for
    persisting full text to R2 if compliance retention requires it.

    Returns the inserted ULID, or ``None`` if the write failed.
    """
    user_bytes = user_message.encode("utf-8") if isinstance(user_message, str) else None
    assistant_bytes = (
        assistant_response.encode("utf-8") if isinstance(assistant_response, str) else None
    )

    metadata: dict = {
        "per_llm_audit": True,
        "customer": customer,
        "session_id": session_id,
        "model": model,
        "platform": platform,
    }

    # ATTRIBUTION (#2122): same emission-time resolution as the tool path —
    # a cron session's turn rows carry the routine that fired them.
    routine = resolve_routine(session_id, hermes_home=hermes_home_for_attribution)
    if routine is not None:
        metadata["routine"] = routine.job_name
        metadata["cron_job_id"] = routine.job_id

    event = AuditEvent(
        action_type="LLM_TURN_COMPLETED",
        actor=actor,
        actor_role=actor_role,
        skill_name=routine.skill if routine is not None else None,
        input_payload=user_bytes,
        output_payload=assistant_bytes,
        metadata=metadata,
    )
    return writer.write(event)


def emit_subagent_stop_event(
    writer: AuditLogWriter,
    *,
    customer: str,
    session_id: str,
    parent_session_id: str | None,
    child_role: str,
    child_status: str,
    duration_ms: int | None,
    task_id: str = "",
    skill_name: str | None = None,
    actor: str = "agent",
    actor_role: ActorRole = ActorRole.AGENT,
    extra_metadata: dict | None = None,
) -> str | None:
    """Write one ``SUBAGENT_STOPPED`` audit row for a subagent_stop hook event.

    ADR 0021 Stream C requires one audit row per delegated child agent so
    that the parent skill's assembly-time schema contract has a visible
    trail (mirror-don't-gate per ADR 0016). The hook fires after each
    delegated subagent's run terminates, regardless of return status.

    Args:
        customer: The customer slug for namespacing.
        session_id: The subagent's own session id.
        parent_session_id: The dispatching parent's session id, when
            available. Carried in metadata so the dashboard can link a
            parent's draft assembly back to its child rows.
        child_role: The role label the parent passed when delegating
            (e.g. ``"medicals_summary"``, ``"interrogatory_map"``,
            ``"opposing_counsel_history"``).
        child_status: One of ``"ok"``, ``"failed"``, ``"timeout"``,
            or ``"interrupted"`` as reported by the Hermes dispatcher.
        duration_ms: Wall-clock duration of the subagent run.
        skill_name: The parent skill that delegated this child, if known.
        extra_metadata: Optional per-skill metadata (e.g. token counts
            the parent collected). Reserved keys ``child_role``,
            ``child_status``, ``duration_ms``, ``session_id``,
            ``parent_session_id``, ``task_id``, ``per_subagent_audit``,
            and ``customer`` are populated by this function and must not
            appear in ``extra_metadata``.

    Returns the inserted ULID, or ``None`` on writer failure (hook
    wrapper swallows ``AuditWriteError``).
    """
    metadata: dict = {
        "per_subagent_audit": True,
        "customer": customer,
        "child_role": child_role,
        "child_status": child_status,
        "session_id": session_id,
    }
    if parent_session_id:
        metadata["parent_session_id"] = parent_session_id
    if task_id:
        metadata["task_id"] = task_id
    if duration_ms is not None:
        metadata["duration_ms"] = float(duration_ms)
    if extra_metadata:
        reserved = set(metadata.keys())
        for key, value in extra_metadata.items():
            if key in reserved:
                raise ValueError(f"extra_metadata key {key!r} reserved by emit_subagent_stop_event")
            metadata[key] = value

    event = AuditEvent(
        action_type="SUBAGENT_STOPPED",
        actor=actor,
        actor_role=actor_role,
        skill_name=skill_name,
        metadata=metadata,
    )
    return writer.write(event)


# ``skill_manage`` is the Hermes-native tool name for the Skill Curator's
# create/edit/delete surface. Emitting AGENT_SKILL_CREATED on this tool
# (in addition to the usual TOOL_CALL_COMPLETED row) is the observation
# surface for ADR 0017 §40 — Hermes' agent-authored skill creation flow.
SKILL_MANAGE_TOOL_NAME = "skill_manage"


def emit_agent_skill_created_event(
    writer: AuditLogWriter,
    *,
    customer: str,
    session_id: str,
    skill_name_created: str,
    skill_manage_args: dict | None,
    tool_call_id: str = "",
    actor: str = "agent",
    actor_role: ActorRole = ActorRole.AGENT,
) -> str | None:
    """Write one ``AGENT_SKILL_CREATED`` audit row when ``skill_manage``
    is invoked to create a new skill.

    Hermes' Skill Curator exposes skill creation through the
    ``skill_manage`` tool. Per ADR 0017 §40 (mirror-don't-gate), we
    observe these creations into the per-customer D1 audit log so the
    dashboard can show what skills the agent authored without
    intercepting or gating the Curator's native flow.

    Args:
        customer: The customer slug for namespacing.
        session_id: Session id of the agent invocation that called
            ``skill_manage``.
        skill_name_created: The slug of the skill that was created
            (extracted by the caller from ``skill_manage`` arguments).
        skill_manage_args: The full args dict passed to ``skill_manage``,
            for the metadata trail.

    Returns the inserted ULID, or ``None`` on writer failure.
    """
    metadata: dict = {
        "per_agent_skill_creation": True,
        "customer": customer,
        "session_id": session_id,
        "skill_name_created": skill_name_created,
    }
    if tool_call_id:
        metadata[CANONICAL_TOOL_CALL_KEY] = tool_call_id
    if skill_manage_args is not None:
        # Carry the args verbatim (no payload digest — the args are public
        # skill-metadata, not user content); useful for "what did the agent
        # author" inspection on the dashboard.
        metadata["skill_manage_args"] = skill_manage_args

    event = AuditEvent(
        action_type="AGENT_SKILL_CREATED",
        actor=actor,
        actor_role=actor_role,
        skill_name=skill_name_created,
        metadata=metadata,
    )
    return writer.write(event)


def detect_skill_manage_creation(
    *,
    tool_name: str,
    args: dict | None,
) -> str | None:
    """Return the slug of the newly-created skill when ``skill_manage`` is
    invoked with a creation action, else ``None``.

    The detector accepts several plausible argument shapes the Curator may
    use (``action: "create"`` with a ``slug`` or ``name`` field, plain
    ``slug`` arg on a ``mode: "create"``-like contract). It is permissive
    on the input side because the Curator's exact argument schema lives in
    Hermes core; the overlay observes, it doesn't validate.
    """
    if tool_name != SKILL_MANAGE_TOOL_NAME:
        return None
    if not isinstance(args, dict):
        return None
    action = args.get("action") or args.get("mode") or args.get("op")
    if action and isinstance(action, str) and action.lower() not in {"create", "add", "new"}:
        return None
    # Allow create-like flows without an explicit action field if the
    # args carry a slug + a creation-shaped marker.
    candidate = args.get("slug") or args.get("name") or args.get("skill_slug")
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    return candidate.strip()


__all__ = [
    "AuditLogWriter",
    "AuditWriteError",
    "BannedToolError",
    "SKILL_MANAGE_TOOL_NAME",
    "ToolCallTimer",
    "ToolClassification",
    "build_per_tool_metadata",
    "classify_tool",
    "detect_skill_manage_creation",
    "emit_agent_skill_created_event",
    "emit_llm_event",
    "emit_subagent_stop_event",
    "emit_tool_event",
    "extract_scope_metadata",
]
