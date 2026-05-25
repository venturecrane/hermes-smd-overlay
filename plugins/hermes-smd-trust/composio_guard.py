"""Per-connection isolation enforcement for Composio-managed connectors.

Ported from ``ss-console/ai-employee/adapter/connectors/composio_assertion.py``.

Composio uses a single shared API key per account. Per-customer isolation
requires every Composio tool response to carry the expected
``connection_id`` value derived from ``customer.yaml.connectors{}`` at
provisioning. A misrouted connection ID is a cross-customer leakage
vector that the platform-level boundary (the per-customer Machine,
ADR 0007) does not catch on its own.

Contract
--------

* ``customer.yaml`` connectors of ``backend: composio:*`` declare a
  ``composio_connection_id`` of shape ``conn_{slug}_<token>``. The TS
  validator enforces shape at authoring time; this module enforces it
  at runtime.

* ``ComposioConnectionGuard`` is constructed against the bound customer
  slug at Machine boot — one instance per Machine, never shared.

* Refusal is loud: a tool response missing or carrying a mismatched
  ``connection_id`` is replaced with a refusal-result string and the
  structured ``ComposioIsolationError`` is raised internally so the
  audit plugin's downstream observation captures the violation.

Hook surface
------------

The plugin's ``transform_tool_result`` hook calls
``verify_composio_response()``. The function returns either ``None``
(let the result pass through) or a replacement result string. Hook
callbacks are exception-safe; the plugin's ``__init__.py`` wraps the
call site in try/except so a transient guard fault cannot break the
agent loop.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug validation
#
# Matches the slug shape enforced by the provisioner: lowercase
# alphanumerics + dashes, 2-40 chars, no leading or trailing dash. Kept
# local rather than imported so this module is self-contained for the
# overlay boot path.
# ---------------------------------------------------------------------------


_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_PATTERN.match(slug):
        raise ValueError(
            f"composio guard slug {slug!r} does not match required pattern "
            "(lowercase alphanumerics + dashes, 2-40 chars, no leading/trailing dash); "
            "this is a bootstrap-time invariant failure"
        )
    return slug


# ---------------------------------------------------------------------------
# Connection-ID shape
#
# ``conn_{slug}_{suffix}`` where:
#   - ``conn_`` is the literal prefix.
#   - ``{slug}`` matches ``_SLUG_PATTERN``.
#   - ``{suffix}`` is ``[A-Za-z0-9_-]{4,80}``.
# ---------------------------------------------------------------------------


_CONNECTION_ID_SUFFIX = r"[A-Za-z0-9_-]{4,80}"
_CONNECTION_ID_PATTERN = re.compile(
    rf"^conn_([a-z0-9][a-z0-9-]{{0,38}}[a-z0-9])_({_CONNECTION_ID_SUFFIX})$"
)


def composio_connection_id_for_slug_prefix(slug: str) -> str:
    """Return the required ``conn_{slug}_`` prefix for a connection ID."""
    _validate_slug(slug)
    return f"conn_{slug}_"


@dataclass(frozen=True)
class _ConnectionIdDecision:
    ok: bool
    found_slug: str | None
    reason: str


def classify_composio_connection_id(
    connection_id: Any, expected_slug: str
) -> _ConnectionIdDecision:
    """Decide whether a Composio connection ID belongs to ``expected_slug``.

    Refusal cases (in order of inspection):
      * empty / non-string
      * shape does not match ``conn_{slug}_{suffix}``
      * captured slug does not equal ``expected_slug`` (foreign-customer ID)
    """
    if not isinstance(connection_id, str) or not connection_id:
        return _ConnectionIdDecision(
            ok=False, found_slug=None, reason="empty connection id"
        )
    match = _CONNECTION_ID_PATTERN.match(connection_id)
    if not match:
        return _ConnectionIdDecision(
            ok=False,
            found_slug=None,
            reason=(
                f"connection id {connection_id!r} does not match "
                "conn_{slug}_{suffix} shape required for Composio-managed connectors"
            ),
        )
    found_slug = match.group(1)
    if found_slug != expected_slug:
        return _ConnectionIdDecision(
            ok=False,
            found_slug=found_slug,
            reason=(
                f"connection id bound to foreign customer slug "
                f"{found_slug!r}; this Machine is bound to {expected_slug!r}"
            ),
        )
    return _ConnectionIdDecision(ok=True, found_slug=found_slug, reason="ok")


# ---------------------------------------------------------------------------
# Refusal exception
# ---------------------------------------------------------------------------


class ComposioIsolationError(RuntimeError):
    """Raised when a Composio connection ID is bound to a foreign customer.

    A misrouted Composio call is a safety-substrate alarm. The exception
    carries the structured attributes the audit plugin (separate plugin,
    loose-coupled via the standard post-hook observation path) records on
    the violation row.
    """

    def __init__(
        self,
        *,
        expected_slug: str,
        attempted_connection_id: str,
        detail: str,
    ) -> None:
        super().__init__(
            f"composio isolation violation: "
            f"expected slug={expected_slug!r}, "
            f"attempted connection_id={attempted_connection_id!r}; "
            f"{detail}"
        )
        self.violation_kind = "composio_connection_id"
        self.expected_slug = expected_slug
        self.attempted_connection_id = attempted_connection_id
        self.detail = detail


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


class ComposioConnectionGuard:
    """Per-customer enforcement for Composio connection IDs.

    Construction takes the customer slug the Machine is bound to. The
    guard is intended to live for the lifetime of the Machine — one
    instance, never shared across customers (a shared instance would be a
    category error).
    """

    def __init__(self, *, expected_slug: str) -> None:
        self._slug = _validate_slug(expected_slug)

    @property
    def expected_slug(self) -> str:
        return self._slug

    def assert_belongs(self, connection_id: Any) -> None:
        """Raise ``ComposioIsolationError`` if ``connection_id`` is foreign.

        On the happy path returns ``None`` silently. The async-emission
        path from ss-console is intentionally collapsed to sync here: the
        overlay's audit observation is plugin-driven via the standard
        ``post_tool_call`` hook (no direct cross-plugin coupling).
        """
        decision = classify_composio_connection_id(connection_id, self._slug)
        if decision.ok:
            return
        attempted = (
            connection_id
            if isinstance(connection_id, str)
            else repr(connection_id)
        )
        raise ComposioIsolationError(
            expected_slug=self._slug,
            attempted_connection_id=attempted,
            detail=decision.reason,
        )


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------


# Tool-name prefix that identifies a Composio-backed call. Connectors
# routed through Composio expose themselves as ``composio.<vendor>.<op>``
# (e.g. ``composio.gmail.messages.list``). Non-Composio tools are
# untouched by this hook.
_COMPOSIO_TOOL_PREFIX = "composio."


def _extract_connection_id(result: Any) -> Any | None:
    """Pull a ``connection_id`` out of a tool result.

    Tool results come back as JSON strings; some adapters may pass dicts
    directly. Return the value if found, or None when absent.
    """
    if isinstance(result, dict):
        return result.get("connection_id")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed.get("connection_id")
    return None


def _refusal_payload(reason: str) -> str:
    return json.dumps(
        {
            "error": "composio_isolation_violation",
            "message": f"Refused: {reason}",
        }
    )


def verify_composio_response(
    tool_name: str,
    result: Any,
    expected_connection_id: str,
) -> str | None:
    """Inspect a Composio tool response and refuse cross-tenant data.

    Args:
        tool_name: The tool that was invoked. Only ``composio.*`` tools
            are inspected; everything else returns None (pass through).
        result: The tool's raw result (JSON string or dict).
        expected_connection_id: The connection ID the Machine is bound
            to for this Composio call. Provided by the caller (the
            plugin's ``__init__.py`` resolves it from customer config).

    Returns:
        None to leave the result unchanged.
        A replacement result string (JSON) when the result is missing or
        carries a foreign connection_id.
    """
    if not isinstance(tool_name, str) or not tool_name.startswith(
        _COMPOSIO_TOOL_PREFIX
    ):
        return None

    if not expected_connection_id or not isinstance(expected_connection_id, str):
        # Misconfigured caller — refuse the result rather than letting an
        # un-bound Composio call surface data back into the conversation.
        logger.warning(
            "verify_composio_response: tool=%s called without expected_connection_id; "
            "refusing result",
            tool_name,
        )
        return _refusal_payload(
            "missing expected_connection_id for Composio tool — refusing"
        )

    actual = _extract_connection_id(result)
    if actual is None:
        logger.warning(
            "verify_composio_response: tool=%s returned a result with no "
            "connection_id; refusing",
            tool_name,
        )
        return _refusal_payload(
            "Composio tool result missing connection_id — refusing"
        )

    if actual != expected_connection_id:
        logger.warning(
            "verify_composio_response: tool=%s connection_id mismatch "
            "(expected does not match returned); refusing",
            tool_name,
        )
        return _refusal_payload(
            "Composio tool result connection_id mismatch — refusing"
        )

    return None


__all__ = [
    "ComposioConnectionGuard",
    "ComposioIsolationError",
    "classify_composio_connection_id",
    "composio_connection_id_for_slug_prefix",
    "verify_composio_response",
]
