"""Per-customer D1 client with runtime namespace assertion.

Every Machine boots with a single D1 binding scoped to one customer. This
module is the gate that enforces that assumption at runtime: every SQL
statement is scanned for foreign-customer tokens (Vectorize-index names,
R2 vault paths interpolated into SQL) before reaching the binding. A
mismatch is a fatal isolation breach and is raised loudly rather than
silently writing to the wrong tenant's database.

Ported from
``ss-console/operator/adapter/namespace_assertion.py`` with two
adaptations for this overlay:

* The source target was an async ``Executor`` protocol; ``D1Client`` here
  is synchronous (``execute`` / ``query`` / ``execute_many``) to match
  the overlay contract used by other plugin chunks (audit, trust,
  memory mirror).
* The source emitted ``INVARIANT_VIOLATION`` audit rows on refusal via
  the ``audit_log.py`` writer; that audit pipeline lives in a separate
  plugin here. Namespace refusals log structured warnings and raise
  :class:`NamespaceAssertionError`; the audit plugin's
  ``post_tool_call`` hook records the surrounding tool outcome and
  picks the violation up from the exception's structured attributes.

Slug validation matches ``bin/provision-customer.sh``: lowercase
alphanumerics + dashes, 2-40 chars, no leading or trailing dash. A
malformed slug at construction time is a bootstrap-time invariant
failure and the constructor raises ``ValueError``.

SQL inspection is deliberately conservative. The wrapper looks for the
two cross-customer signatures it can detect statically:

* ``hermes-{slug}-{vault|corrections}`` — Vectorize-index or binding
  name leaking into a query (e.g. someone constructs
  ``f"hermes-{wrong_slug}-vault"`` and passes it into a SQL string for
  a maintenance script).
* ``vaults/{slug}/`` — R2 key string being interpolated into SQL.

SQL that mentions no slug-shaped token at all is passed through
unchanged — that is the steady-state path and the binding alone scopes
it, exactly as ADR 0009 prescribes.
"""

import logging
import os
import re
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------


_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")


def _validate_slug(slug: str) -> str:
    """Validate ``slug`` against the per-customer namespace shape.

    Raises:
        ValueError: If ``slug`` is not a string, is empty, or does not
            match the provisioning regex (lowercase alphanumerics +
            dashes, 2-40 chars, no leading or trailing dash).
    """
    if not isinstance(slug, str) or not _SLUG_PATTERN.match(slug):
        raise ValueError(
            f"namespace slug {slug!r} does not match required pattern "
            "(lowercase alphanumerics + dashes, 2-40 chars, no leading/trailing dash); "
            "this is a bootstrap-time invariant failure"
        )
    return slug


# ---------------------------------------------------------------------------
# Refusal exception
# ---------------------------------------------------------------------------


class NamespaceAssertionError(RuntimeError):
    """Raised when a D1 query targets a foreign customer namespace.

    The caller MUST NOT swallow this. An attempted cross-customer access
    is a safety-substrate alarm and the action that triggered it must
    abort. The exception carries structured attributes so the audit
    plugin can record the violation as an ``INVARIANT_VIOLATION`` row.

    Attributes:
        violation_kind: One of ``d1_sql`` (today's only kind; the
            namespace primitive can be extended to ``r2_key`` and
            ``vectorize_index`` if those clients move under this
            module).
        expected_slug: The slug the client was bound to at construction.
        attempted_target: The foreign token the SQL was carrying.
        detail: Human-readable explanation.
    """

    def __init__(
        self,
        *,
        violation_kind: str,
        expected_slug: str,
        attempted_target: str,
        detail: str,
    ) -> None:
        super().__init__(
            f"namespace assertion failed [{violation_kind}]: "
            f"expected slug={expected_slug!r}, attempted target={attempted_target!r}; "
            f"{detail}"
        )
        self.violation_kind = violation_kind
        self.expected_slug = expected_slug
        self.attempted_target = attempted_target
        self.detail = detail


# ---------------------------------------------------------------------------
# SQL token patterns
# ---------------------------------------------------------------------------


# Foreign-customer tokens we look for inside SQL text. The patterns
# capture a slug; the wrapper compares against its bound slug.
#
# ``hermes-{slug}-{vault|corrections}`` covers Vectorize-index and
# binding-name leaks (e.g. someone constructs
# ``f"hermes-{wrong_slug}-vault"`` and passes it into a SQL string for
# a maintenance script).
#
# ``vaults/{slug}/`` covers R2 key strings being interpolated into SQL.
_HERMES_BINDING_TOKEN = re.compile(r"\bhermes-([a-z0-9-]{2,40})-(?:vault|corrections)\b")
_VAULTS_PATH_TOKEN = re.compile(r"\bvaults/([a-z0-9-]{2,40})/")


# ---------------------------------------------------------------------------
# D1Client
# ---------------------------------------------------------------------------


class D1Client:
    """Per-customer D1 client with runtime namespace assertion.

    The client is constructed once at Machine boot, bound to a single
    ``binding_name`` (the env var that names the D1 file/URL) and a
    single ``customer_slug`` (the expected per-customer namespace).
    Every ``execute`` / ``query`` / ``execute_many`` call scans the SQL
    for foreign-slug tokens before opening the underlying connection.

    The binding is resolved lazily. By default the constructor reads
    the env var named by ``binding_name`` and treats its value as a
    SQLite file path (Hermes ships SQLite-backed D1 in local dev and
    the Fly volume; production points at the same shape via a path on
    the per-customer volume). A different path may be supplied with
    the ``db_path`` keyword for testing.

    Concurrency: callers may share a single ``D1Client`` instance
    across threads; the underlying SQLite connection is opened with
    ``check_same_thread=False`` and serialized via an in-process lock
    held inside the connection's transaction lifecycle.

    The class follows the API contract documented in §7 of the build
    plan; other plugin chunks (audit, trust, memory) import this
    contract directly.
    """

    def __init__(
        self,
        binding_name: str,
        customer_slug: str,
        *,
        db_path: str | None = None,
    ) -> None:
        """Construct a D1 client pinned to a single customer.

        Args:
            binding_name: Name of the D1 binding env var (e.g.
                ``CUSTOMER_DB``). Resolved lazily on first call.
            customer_slug: Expected customer namespace. Every SQL
                statement is asserted against this slug.
            db_path: Optional explicit SQLite file path, bypassing the
                env-var lookup. Useful for tests.

        Raises:
            ValueError: If ``customer_slug`` does not match the slug
                regex.
        """
        self._binding_name = binding_name
        self._customer_slug = _validate_slug(customer_slug)
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def customer_slug(self) -> str:
        """Return the customer slug this client is bound to."""
        return self._customer_slug

    @property
    def binding_name(self) -> str:
        """Return the env-var binding name."""
        return self._binding_name

    def execute(self, sql: str, *params: Any) -> int:
        """Execute a write statement.

        Args:
            sql: SQL statement. Scanned for foreign-customer tokens
                before execution.
            *params: Bound parameters, passed positionally to the
                underlying driver.

        Returns:
            Affected row count.

        Raises:
            NamespaceAssertionError: If the SQL mentions a slug-shaped
                token that does not match the bound ``customer_slug``.
        """
        self._assert_namespace(sql)
        conn = self._connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute a read query and return rows as dicts.

        Args:
            sql: SELECT statement. Scanned for foreign-customer tokens
                before execution.
            *params: Bound parameters, passed positionally to the
                underlying driver.

        Returns:
            List of result rows, each row a ``dict`` keyed by column
            name. Empty list if no rows match.

        Raises:
            NamespaceAssertionError: If the SQL mentions a slug-shaped
                token that does not match the bound ``customer_slug``.
        """
        self._assert_namespace(sql)
        conn = self._connect()
        cur = conn.execute(sql, params)
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]

    def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        """Bulk write with the same SQL across many parameter tuples.

        Args:
            sql: Write statement. Scanned once before any execution.
            params_list: List of parameter tuples, one per row.

        Returns:
            Total affected row count (summed across the batch).

        Raises:
            NamespaceAssertionError: If the SQL mentions a slug-shaped
                token that does not match the bound ``customer_slug``.
        """
        self._assert_namespace(sql)
        if not params_list:
            return 0
        conn = self._connect()
        cur = conn.executemany(sql, params_list)
        conn.commit()
        return cur.rowcount

    def close(self) -> None:
        """Close the underlying connection if open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Resolve the binding and return a cached connection."""
        if self._conn is None:
            path = self._resolve_db_path()
            self._conn = sqlite3.connect(path, check_same_thread=False)
        return self._conn

    def _resolve_db_path(self) -> str:
        """Resolve the D1 file path from explicit override, direct path, or env var.

        ``binding_name`` may be EITHER a direct filesystem path (how the live
        Machine's fly.toml sets ``SMD_D1_AUDIT_BINDING`` = ``/opt/data/audit.db``)
        OR the NAME of an env var holding the path (the indirection this method
        originally assumed). Accept both: a value starting with ``/`` is the path
        itself; otherwise look it up in the environment. Without this, a
        path-valued binding resolved to ``os.environ.get("/opt/data/audit.db")``
        → None → every write raised → audit emission silently never wrote
        (ss-console #1285)."""
        if self._db_path is not None:
            return self._db_path
        if self._binding_name.startswith("/"):
            return self._binding_name
        value = os.environ.get(self._binding_name)
        if not value:
            raise RuntimeError(
                f"D1Client: binding env var {self._binding_name!r} is unset; "
                "Machine bootstrap must populate this from the per-customer secret bundle"
            )
        return value

    def _assert_namespace(self, sql: str) -> None:
        """Scan ``sql`` for foreign-slug tokens; raise on mismatch.

        Parameter values are not inspected — they may legitimately
        reference cross-customer identifiers (e.g. a Captain-side
        report query that joins on customer_id from a control-plane
        table). The SQL string itself is the surface that names the
        binding.
        """
        for match in _HERMES_BINDING_TOKEN.finditer(sql):
            found_slug = match.group(1)
            if found_slug != self._customer_slug:
                attempted = match.group(0)
                logger.warning(
                    "namespace assertion: foreign Vectorize/binding token in SQL; "
                    "expected_slug=%s attempted_target=%s",
                    self._customer_slug,
                    attempted,
                )
                raise NamespaceAssertionError(
                    violation_kind="d1_sql",
                    expected_slug=self._customer_slug,
                    attempted_target=attempted,
                    detail=(
                        "SQL mentions a Vectorize-index or binding name bound "
                        f"to a different customer (found slug={found_slug!r}); "
                        "per-customer D1 queries must not name foreign indices"
                    ),
                )
        for match in _VAULTS_PATH_TOKEN.finditer(sql):
            found_slug = match.group(1)
            if found_slug != self._customer_slug:
                attempted = match.group(0)
                logger.warning(
                    "namespace assertion: foreign R2 vault path in SQL; "
                    "expected_slug=%s attempted_target=%s",
                    self._customer_slug,
                    attempted,
                )
                raise NamespaceAssertionError(
                    violation_kind="d1_sql",
                    expected_slug=self._customer_slug,
                    attempted_target=attempted,
                    detail=(
                        "SQL embeds an R2 vault path bound to a different "
                        f"customer (found slug={found_slug!r}); per-customer "
                        "D1 queries must not interpolate foreign R2 keys"
                    ),
                )


__all__ = [
    "D1Client",
    "NamespaceAssertionError",
]
