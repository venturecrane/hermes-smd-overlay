"""Audit-log immutability enforcement at the Worker layer.

Ported from ss-console/operator/adapter/audit_log_immutability.py.

Cloudflare D1 does not ship per-role table permissions, so the substrate
cannot grant the agent-runtime binding INSERT-only on ``audit_log`` the way
a Postgres deployment would. This module is the Worker-layer answer.

Three pieces:

  1. ``D1Executor`` — wraps any object that exposes ``execute(sql, *params)``
     (such as ``shared.d1_client.D1Client``) and rejects UPDATE/DELETE/REPLACE/
     TRUNCATE/DROP/ALTER targeting the ``audit_log`` table. The single
     legitimate writer is ``emit.AuditLogWriter`` which constructs against
     the raw D1Client directly — every other caller wraps with ``D1Executor``.

  2. ``LogpushMirror`` — Protocol with a single ``mirror_audit_event(row)``
     callable. ``NoopLogpushMirror`` satisfies the protocol without I/O. A
     real implementation streams each row into a per-customer R2 archive
     bucket with Object Lock applied; that work lands on the Hermes-side
     deployment, not in this plugin.

  3. ``LegalHoldException`` + ``legal_hold_ticket`` kwarg — Captain-only
     redaction bypass. The bypass requires a non-empty ledger ticket and is
     logged at warning level so the operator audit picks it up.

The SQL inspection is conservative — comments are stripped before
inspection (so an attacker cannot hide ``DELETE FROM audit_log`` inside a
block comment) and multi-statement SQL that touches ``audit_log`` is
rejected wholesale. D1's HTTP API accepts only single-statement
parameterized queries, so this is not a real-world constraint loss.

Note on the ``async`` -> sync port
----------------------------------

The original ss-console module exposed ``async def execute``. The
hermes-smd-overlay's ``shared.d1_client.D1Client`` exposes a synchronous
``execute(sql, *params)`` API because Hermes invokes hook callbacks
synchronously. The wrapper here mirrors the D1Client signature: sync, with
positional ``*params``. The blocking SQL inspection is unchanged from the
original.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception types
# ---------------------------------------------------------------------------


class AuditLogImmutabilityError(RuntimeError):
    """Raised when a caller attempts UPDATE/DELETE/TRUNCATE on audit_log.

    The substrate treats audit_log rows as immutable. The only writer is the
    audit-log INSERT path (``emit.AuditLogWriter``). The only legitimate
    path that modifies an existing row is the Captain-supervised redaction
    script, which raises a ``LegalHoldException`` to bypass the guard — see
    audit-log-immutability.md in the ss-console docs tree.
    """


class LegalHoldException(Exception):  # noqa: N818 — sentinel name, not Error suffix
    """Sentinel attached to an executor call to bypass the immutability check.

    Raised only by the Captain-only audit-redact script after its
    multi-confirmation guard clears. The wrapper recognizes the exception
    via the ``legal_hold_ticket`` kwarg on ``execute()``. There is no other
    bypass path.

    The exception carries the ticket id of the corresponding row in the
    ``audit_exceptions_ledger`` (a separate immutable ledger maintained on
    the Captain-side control plane). Without a non-empty ticket the bypass
    is rejected even if the exception is raised.
    """

    def __init__(self, ticket: str) -> None:
        if not ticket:
            raise ValueError("LegalHoldException requires a non-empty ticket id")
        super().__init__(f"legal_hold_ticket={ticket}")
        self.ticket = ticket


# ---------------------------------------------------------------------------
# SQL inspection
# ---------------------------------------------------------------------------


# Mutating verbs we block when targeting audit_log.
_MUTATING_VERBS = ("UPDATE", "DELETE", "REPLACE", "TRUNCATE", "DROP", "ALTER")

# Regex finds ``audit_log`` as a SQL token (word boundaries, case-insensitive).
_AUDIT_LOG_TOKEN = re.compile(r"\baudit_log\b", re.IGNORECASE)

# Strip /* ... */ block comments and -- line comments before inspection
# so they cannot hide the table name.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments so they cannot hide table references."""
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    return sql


def _first_verb(sql: str) -> str:
    """Return the first SQL keyword (uppercased) after leading whitespace."""
    stripped = sql.lstrip()
    match = re.match(r"([A-Za-z]+)", stripped)
    return match.group(1).upper() if match else ""


def _touches_audit_log(sql: str) -> bool:
    return bool(_AUDIT_LOG_TOKEN.search(sql))


def _is_multi_statement(sql: str) -> bool:
    """True if the SQL contains a semicolon separating multiple statements.

    A trailing semicolon on a single statement does NOT count.
    """
    trimmed = sql.rstrip().rstrip(";").rstrip()
    return ";" in trimmed


def is_mutation_against_audit_log(sql: str) -> bool:
    """Public helper: does this statement attempt a forbidden write?

    Returns True if the SQL targets ``audit_log`` and the leading verb is
    in ``_MUTATING_VERBS``, OR if the SQL is multi-statement and any part
    of it mentions ``audit_log``.
    """
    clean = _strip_sql_comments(sql)
    if not _touches_audit_log(clean):
        return False
    if _is_multi_statement(clean):
        # Multi-statement queries against audit_log are rejected wholesale.
        return True
    verb = _first_verb(clean)
    return verb in _MUTATING_VERBS


# ---------------------------------------------------------------------------
# Underlying executor protocol
# ---------------------------------------------------------------------------


class _Executor(Protocol):
    """Duck-typed against ``shared.d1_client.D1Client.execute``."""

    def execute(self, sql: str, *params: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Logpush mirror protocol + no-op default
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirroredAuditRow:
    """The minimum shape mirrored to the immutable backing store.

    Keys mirror the audit_log columns 1:1 so the integrity check can
    deep-compare D1 rows against the mirror archive without translation.
    """

    id: str
    ts: str
    action_type: str
    actor: str
    actor_role: str | None
    skill_name: str | None
    matter_ref: str | None
    input_digest: str | None
    output_digest: str | None
    diff_digest: str | None
    trust_ceiling: str | None
    metadata: str | None


class LogpushMirror(Protocol):
    """Mirror one audit_log row to the immutable backing store.

    Real implementations POST the row into R2 keyed
    ``{YYYY}/{MM}/{DD}/{ulid}.json`` with R2 Object Lock applied at the
    bucket level. The mirror MUST NOT raise on transient failure — log
    and return is the contract. The integrity check (separate module)
    catches drift between D1 and the mirror and reports it.
    """

    def mirror_audit_event(self, row: MirroredAuditRow) -> None: ...


class NoopLogpushMirror:
    """v1 default mirror. Logs the row id and returns.

    Replace with the R2-backed implementation when Hermes-side deployment
    lands. Until then, the Logpush job declared in ``wrangler.toml`` is the
    operational backstop — every D1 query is shipped to Logpush at the
    Cloudflare-platform level regardless of this no-op.
    """

    def mirror_audit_event(self, row: MirroredAuditRow) -> None:
        # Never log the metadata or any row payload — only the id + action.
        logger.debug("noop logpush mirror: audit row id=%s action=%s", row.id, row.action_type)


# ---------------------------------------------------------------------------
# The wrapping executor
# ---------------------------------------------------------------------------


class D1Executor:
    """Wraps any executor and rejects forbidden mutations on ``audit_log``.

    Every non-writer caller in the substrate should hold a ``D1Executor``,
    not the raw D1Client. The audit-log writer (the only legitimate INSERT
    path) constructs against the raw D1Client directly.

    Bypass: pass ``legal_hold_ticket=<ticket>`` to ``execute()``. Bare
    ticket strings without a real ledger entry are rejected upstream
    (``LegalHoldException`` requires a non-empty ticket at construction).
    """

    def __init__(self, inner: _Executor) -> None:
        self._inner = inner

    def execute(
        self,
        sql: str,
        *params: Any,
        legal_hold_ticket: str | None = None,
    ) -> Any:
        if is_mutation_against_audit_log(sql):
            if legal_hold_ticket:
                # Bypass path — log loudly so the operator audit catches it.
                logger.warning(
                    "audit_log immutability bypass: ticket=%s sql=%s",
                    legal_hold_ticket,
                    sql.strip()[:200],
                )
                return self._inner.execute(sql, *params)
            logger.error(
                "audit_log immutability violation: rejected SQL=%s",
                sql.strip()[:200],
            )
            raise AuditLogImmutabilityError(
                "audit_log is append-only; UPDATE/DELETE/REPLACE/TRUNCATE/DROP/ALTER "
                "rejected at the Worker layer. The only legitimate writer is "
                "hermes-smd-audit/emit.AuditLogWriter. Captain-supervised "
                "redaction requires the documented exception process — see "
                "audit-log-immutability.md."
            )
        return self._inner.execute(sql, *params)


__all__ = [
    "AuditLogImmutabilityError",
    "D1Executor",
    "LegalHoldException",
    "LogpushMirror",
    "MirroredAuditRow",
    "NoopLogpushMirror",
    "is_mutation_against_audit_log",
]
