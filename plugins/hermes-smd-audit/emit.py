"""Audit row construction and D1 emission.

Ported from ss-console/ai-employee/adapter/audit_log.py (AuditLogWriter,
SHA-256 digesting, ULID generation, ISO-8601 timestamps, action_type
validation) and from ss-console/ai-employee/adapter/audit_emit_points.py
(classify_tool, ToolCallTimer, build_per_tool_metadata, scope-aware
metadata extraction).

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
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .schemas import (
    ACCEPTED_ACTION_TYPES,
    BANNED_REASON,
    BANNED_TOOLS,
    SCOPE_KEYS,
    TOOL_ACTION_CLASS_MAP,
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


class BannedToolError(Exception):
    """Raised when a tool name appears in ``BANNED_TOOLS``.

    The dispatch path catches this and translates to a refusal audit row
    via the per-tool emit helper. ``tool_name`` carries the offending name
    for metadata; ``reason`` is the closed-set classification ("banned_tool_pattern_a"
    for autonomous send, "banned_tool_destructive" for irreversible ops).
    """

    def __init__(self, *, tool_name: str, reason: str = "banned_tool") -> None:
        super().__init__(f"tool {tool_name!r} is banned: {reason}")
        self.tool_name = tool_name
        self.reason = reason


# ---------------------------------------------------------------------------
# ULID generation
#
# A ULID is a 26-char Crockford-base32 string: 10 chars timestamp (ms since
# epoch) + 16 chars randomness. Sortable. No dashes. No external deps.
# ---------------------------------------------------------------------------


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def _ulid(now_ms: Optional[int] = None) -> str:
    """Return a 26-char ULID. ``now_ms`` is injectable for deterministic tests."""
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = secrets.randbits(80)
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


def _iso_utc(now: Optional[datetime] = None) -> str:
    """ISO 8601 UTC with millisecond precision and explicit ``Z`` suffix."""
    dt = now if now is not None else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sha256(payload: Optional[bytes]) -> Optional[str]:
    if payload is None:
        return None
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Writer — talks to per-customer D1 through the shared D1Client
# ---------------------------------------------------------------------------


_INSERT_SQL = (
    "INSERT INTO audit_log "
    "(id, ts, action_type, actor, actor_role, skill_name, matter_ref, "
    "input_digest, output_digest, diff_digest, trust_ceiling, metadata) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


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
            raise ValueError(
                f"action_type {event.action_type!r} not in ACCEPTED_ACTION_TYPES"
            )

        now_dt = self._clock() if self._clock else None
        now_ms = self._ulid_now_ms() if self._ulid_now_ms else None
        ulid = _ulid(now_ms=now_ms)
        ts = _iso_utc(now_dt)

        actor_role_value: Optional[str]
        if isinstance(event.actor_role, ActorRole):
            actor_role_value = event.actor_role.value
        else:
            actor_role_value = event.actor_role

        params = [
            ulid,
            ts,
            event.action_type,
            event.actor,
            actor_role_value,
            event.skill_name,
            event.matter_ref,
            _sha256(event.input_payload),
            _sha256(event.output_payload),
            _sha256(event.diff_payload),
            event.trust_ceiling,
            json.dumps(event.metadata, sort_keys=True, separators=(",", ":"))
            if event.metadata
            else None,
        ]

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
# Per-tool classification + timing + metadata builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolClassification:
    """Outcome of ``classify_tool()``.

    ``action_class`` is the action class the trust-ceiling enforcer should
    use for this tool call. ``unmapped`` is True if the tool name was not
    in ``TOOL_ACTION_CLASS_MAP`` (the helper returned the READ default).
    """

    action_class: HookActionClass
    unmapped: bool


def classify_tool(tool_name: str) -> ToolClassification:
    """Map a tool name to its action class.

    Raises:
        ValueError: ``tool_name`` is empty.
        BannedToolError: ``tool_name`` is in ``BANNED_TOOLS``.
    """
    if not tool_name:
        raise ValueError("tool_name is required")

    if tool_name in BANNED_TOOLS:
        reason = BANNED_REASON.get(tool_name, "banned_tool")
        raise BannedToolError(tool_name=tool_name, reason=reason)

    mapped = TOOL_ACTION_CLASS_MAP.get(tool_name)
    if mapped is not None:
        return ToolClassification(action_class=mapped, unmapped=False)

    logger.warning(
        "classify_tool: tool_name=%s not in TOOL_ACTION_CLASS_MAP; "
        "defaulting to READ and tagging metadata.unmapped_tool=true",
        tool_name,
    )
    return ToolClassification(action_class=HookActionClass.READ, unmapped=True)


class ToolCallTimer:
    """Monotonic per-tool-call latency timer. Millisecond precision.

    Single-shot: ``start()`` and ``stop()`` may each be called exactly once.
    Misuse raises ``RuntimeError`` so double-reports are caught early.
    """

    __slots__ = ("_started_perf", "_duration_ms")

    def __init__(self) -> None:
        self._started_perf: Optional[float] = None
        self._duration_ms: Optional[float] = None

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
    def duration_ms(self) -> Optional[float]:
        """Read the last-measured duration. ``None`` if ``stop()`` has not run."""
        return self._duration_ms


def extract_scope_metadata(arguments: Optional[dict]) -> dict[str, str]:
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
    skill_name: Optional[str] = None,
    skill_version: Optional[str] = None,
    ceiling_level: Optional[str] = None,
    error_type: Optional[str] = None,
    duration_ms: Optional[float] = None,
    trace_id: Optional[str] = None,
    arguments: Optional[dict] = None,
    unmapped: bool = False,
    banned_reason: Optional[str] = None,
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


def _outcome_from_result(result: Any) -> tuple[str, Optional[str]]:
    """Best-effort outcome + error_type inference from a Hermes tool result.

    Hermes' ``post_tool_call`` passes ``result`` as a str (usually a JSON
    blob). We do not try to parse it — that would couple the audit plugin
    to upstream tool-result conventions. Instead the helper returns
    ``("ok", None)`` for any non-empty string and lets the registry +
    duration carry the load-bearing signal. The hook wrapper can override
    by inspecting result itself before calling this helper.
    """
    if isinstance(result, str) and result:
        return ("ok", None)
    return ("ok", None)


def emit_tool_event(
    writer: AuditLogWriter,
    *,
    customer: str,
    tool_name: str,
    args: Optional[dict],
    result: Any,
    task_id: str,
    session_id: str,
    tool_call_id: str,
    duration_ms: Optional[int],
    actor: str = "agent",
    actor_role: ActorRole = ActorRole.AGENT,
    skill_name: Optional[str] = None,
) -> Optional[str]:
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
        banned_reason: Optional[str] = None
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
) -> Optional[str]:
    """Write one ``LLM_TURN_COMPLETED`` audit row for a post_llm_call event.

    The user message and assistant response are NEVER stored verbatim — the
    writer takes a bytes object and persists only the SHA-256 digest. The
    caller (or a downstream content-archive worker) is responsible for
    persisting full text to R2 if compliance retention requires it.

    Returns the inserted ULID, or ``None`` if the write failed.
    """
    user_bytes = user_message.encode("utf-8") if isinstance(user_message, str) else None
    assistant_bytes = (
        assistant_response.encode("utf-8")
        if isinstance(assistant_response, str)
        else None
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


__all__ = [
    "AuditLogWriter",
    "AuditWriteError",
    "BannedToolError",
    "ToolCallTimer",
    "ToolClassification",
    "build_per_tool_metadata",
    "classify_tool",
    "emit_llm_event",
    "emit_tool_event",
    "extract_scope_metadata",
]
