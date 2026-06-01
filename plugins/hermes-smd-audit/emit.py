"""Audit row construction and D1 emission.

Ported from ss-console/ai-employee/adapter/audit_log.py (AuditLogWriter,
SHA-256 digesting, ULID generation, ISO-8601 timestamps, action_type
validation) and from ss-console/ai-employee/adapter/audit_emit_points.py
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

import json
import logging
import time
from typing import Any

from shared.action_classes import (
    BannedToolError,
    ToolClassification,
    classify_tool,
)
from shared.audit_contract import INSERT_SQL as _INSERT_SQL
from shared.audit_contract import build_audit_params
from shared.ids import iso_utc as _iso_utc
from shared.ids import sha256 as _sha256
from shared.ids import ulid as _ulid

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


class AuditWriteError(RuntimeError):
    """Raised when the audit log cannot be written.

    The hook wrappers catch this to keep the Hermes dispatcher healthy. The
    underlying invariant — an unloggable action must not execute — is
    preserved at the substrate layer above this plugin: the trust-ceiling
    enforcer (hermes-smd-trust) is the gate that blocks pre-call, not this
    observer plugin.
    """


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
    trace_id: str | None = None,
    arguments: dict | None = None,
    unmapped: bool = False,
    banned_reason: str | None = None,
) -> dict:
    """Build the canonical ``metadata`` dict for one per-tool audit row.

    Canonical keys (stable consumers depend on these):

    - per_tool_audit:       True
    - customer:             str (customer slug)
    - skill:                str | None
    - skill_version:        str | None
    - tool:                 str
    - action_class:         str (HookActionClass value)
    - ceiling_level:        str | None
    - outcome:              str ("ok" | "error" | "blocked")
    - error_type:           str | None
    - duration_ms:          float | None
    - trace_id:             str | None
    - unmapped_tool:        True iff the tool was not in the registry
    - banned_tool:          True iff the tool was banned
    - banned_reason:        str (set when banned_tool is True)
    - matter_id:            str (set when arguments has one)
    - customer_segment:     str (set when arguments has one)
    """
    metadata: dict = {
        "per_tool_audit": True,
        "customer": customer,
        "skill": skill_name,
        "skill_version": skill_version,
        "tool": tool_name,
        "action_class": action_class.value,
        "ceiling_level": ceiling_level,
        "outcome": outcome,
        "error_type": error_type,
        "duration_ms": duration_ms,
        "trace_id": trace_id,
    }

    if unmapped:
        metadata["unmapped_tool"] = True

    if banned_reason is not None:
        metadata["banned_tool"] = True
        metadata["banned_reason"] = banned_reason

    scope = extract_scope_metadata(arguments)
    if scope:
        metadata.update(scope)

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
_OUTCOME_SEMANTICS_VERSION = 2  # 1 = always-"ok" (bug); 2 = error-detecting.


def _outcome_from_result(result: Any) -> tuple[str, str | None]:
    """Infer ``(outcome, error_type)`` from a Hermes tool result.

    Hermes' ``post_tool_call`` passes ``result`` as a str (usually JSON).
    Recording every call as ``"ok"`` — the prior behavior — makes the audit
    ledger unable to distinguish a failed tool call from a successful one, which
    is unacceptable for a compliance ledger. This helper now recognizes the
    conventional structured-error shapes and reports ``"error"`` with the
    upstream error type when present, while staying conservative: an
    unparseable or unrecognized result is reported as ``"ok"`` (we never
    fabricate an error). Outcome semantics are versioned
    (``_OUTCOME_SEMANTICS_VERSION``) and stamped into metadata so an auditor can
    tell error-detecting rows (v2+) from the legacy always-"ok" rows (v1)
    without any historical row being rewritten.

    Recognized error shapes (JSON object at the top level):
      * ``{"error": <truthy>}``           → error_type from ``error_type``/
        ``code``/``type`` if present, else the stringified ``error``.
      * ``{"is_error": true}`` / ``{"isError": true}``
      * ``{"status": "error"|"failure"|"failed"}`` /
        ``{"ok": false}`` / ``{"success": false}``
    """
    if not isinstance(result, str) or not result:
        # No inspectable payload — do not assert failure; the duration +
        # registry carry the load-bearing signal. (Matches prior conservatism.)
        return ("ok", None)

    stripped = result.lstrip()
    if not stripped.startswith("{"):
        return ("ok", None)  # not a JSON object; nothing structured to read.

    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return ("ok", None)  # unparseable — fail toward "ok", never fabricate.

    if not isinstance(parsed, dict):
        return ("ok", None)

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

    return ("ok", None)


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
) -> str | None:
    """Write one ``TOOL_CALL_COMPLETED`` audit row for a post_tool_call event.

    Handles three cases:

      * Banned tool name → emit an ``INVARIANT_VIOLATION`` row with
        ``metadata.banned_tool=true`` and ``outcome=blocked``.
      * Known tool → look up action class, build metadata, emit
        ``TOOL_CALL_COMPLETED``.
      * Unknown tool → default action class is READ; metadata is tagged
        ``unmapped_tool=true`` so the dashboard surfaces it.

    Returns the inserted ULID, or ``None`` if the write failed (the writer
    raised ``AuditWriteError`` and the hook wrapper swallowed it).
    """
    try:
        classification = classify_tool(tool_name)
        action_class = classification.action_class
        unmapped = classification.unmapped
        banned_reason: str | None = None
        outcome, error_type = _outcome_from_result(result)
        action_type = "TOOL_CALL_COMPLETED"
    except BannedToolError as exc:
        # The dispatch path SHOULD have caught this before the tool ran,
        # but the audit plugin still emits a refusal row if a banned tool
        # name reaches the post_tool_call seam (defense in depth).
        action_class = HookActionClass.EXTERNAL_SEND
        unmapped = False
        banned_reason = exc.reason
        outcome = "blocked"
        error_type = None
        action_type = "INVARIANT_VIOLATION"

    metadata = build_per_tool_metadata(
        customer=customer,
        tool_name=tool_name,
        action_class=action_class,
        outcome=outcome,
        skill_name=skill_name,
        ceiling_level=None,
        error_type=error_type,
        duration_ms=float(duration_ms) if duration_ms is not None else None,
        trace_id=tool_call_id or None,
        arguments=args,
        unmapped=unmapped,
        banned_reason=banned_reason,
    )

    # Carry session/task identifiers in metadata so the dashboard can
    # pivot between rows without needing dedicated columns.
    if session_id:
        metadata["session_id"] = session_id
    if task_id:
        metadata["task_id"] = task_id
    # Stamp the outcome-semantics version so an auditor can distinguish
    # error-detecting rows (v2+) from the legacy always-"ok" rows (v1). No
    # historical row is ever rewritten — the version is the changepoint marker.
    metadata["outcome_semantics_version"] = _OUTCOME_SEMANTICS_VERSION

    event = AuditEvent(
        action_type=action_type,
        actor=actor,
        actor_role=actor_role,
        skill_name=skill_name,
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

    event = AuditEvent(
        action_type="LLM_TURN_COMPLETED",
        actor=actor,
        actor_role=actor_role,
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
        metadata["tool_call_id"] = tool_call_id
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
