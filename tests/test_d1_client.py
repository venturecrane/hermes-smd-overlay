"""Tests for ``shared.d1_client``.

Ported from
``ss-console/operator/adapter/tests/test_namespace_assertion.py`` and
``test_namespace_adoption.py``. Adapted to the overlay's synchronous
``D1Client`` API and trimmed to the SQL-namespace surface that lives
in this client (the R2 + Vectorize wrappers in the source moved to the
audit/memory plugins and are tested there).

Covers:

* Slug validation at construction.
* SQL passthrough for steady-state queries (no foreign tokens, own-slug
  tokens).
* SQL refusal for foreign Vectorize-index names and foreign R2 vault
  paths interpolated into SQL.
* ``execute`` returns rowcount, ``query`` returns dicts, ``execute_many``
  handles bulk writes.
* Env-bound construction via ``shared.d1_env.d1_client_from_env``.
"""

import sqlite3

import pytest

from shared.d1_client import D1Client, NamespaceAssertionError
from shared.d1_env import d1_client_from_env

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Path to an empty per-test SQLite file with a tiny demo schema."""
    path = tmp_path / "customer.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE audit_log (
          id    TEXT PRIMARY KEY,
          actor TEXT NOT NULL,
          kind  TEXT NOT NULL
        );
        CREATE TABLE memory_index (
          key   TEXT PRIMARY KEY,
          slug  TEXT NOT NULL
        );
        CREATE TABLE memory_state (
          id     TEXT PRIMARY KEY,
          r2_key TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def client(db_path):
    """Return a D1Client bound to slug 'acme' and the demo DB."""
    return D1Client(binding_name="CUSTOMER_DB", customer_slug="acme", db_path=db_path)


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "",
        "A",
        "ABC",
        "-leading-dash",
        "trailing-dash-",
        "has space",
        "has_underscore",
        "way-too-long-" + ("x" * 50),
        "x",  # one-char too short
    ],
)
def test_construction_rejects_invalid_slug(bad_slug):
    with pytest.raises(ValueError, match="namespace slug"):
        D1Client(binding_name="CUSTOMER_DB", customer_slug=bad_slug, db_path=":memory:")


@pytest.mark.parametrize(
    "good_slug",
    ["ab", "smd", "acme", "client-1", "client-1-prod", "a0", "0a", "a-b-c"],
)
def test_construction_accepts_valid_slug(good_slug):
    client = D1Client(
        binding_name="CUSTOMER_DB",
        customer_slug=good_slug,
        db_path=":memory:",
    )
    assert client.customer_slug == good_slug


# ---------------------------------------------------------------------------
# SQL passthrough
# ---------------------------------------------------------------------------


def test_query_with_no_slug_token_passes_through(client):
    rows = client.query("SELECT * FROM audit_log WHERE id = ?", "01HZZZ")
    assert rows == []


def test_execute_returns_rowcount(client):
    rowcount = client.execute(
        "INSERT INTO audit_log (id, actor, kind) VALUES (?, ?, ?)",
        "row-1",
        "agent",
        "test",
    )
    assert rowcount == 1
    rows = client.query("SELECT id, actor FROM audit_log")
    assert rows == [{"id": "row-1", "actor": "agent"}]


def test_execute_many_handles_bulk_writes(client):
    rowcount = client.execute_many(
        "INSERT INTO audit_log (id, actor, kind) VALUES (?, ?, ?)",
        [
            ("row-1", "agent", "test"),
            ("row-2", "agent", "test"),
            ("row-3", "agent", "test"),
        ],
    )
    # sqlite reports the final batch's rowcount; the important check is
    # that all three rows landed.
    assert rowcount in (1, 3)
    rows = client.query("SELECT COUNT(*) AS n FROM audit_log")
    assert rows == [{"n": 3}]


def test_execute_many_empty_list_is_noop(client):
    assert client.execute_many("INSERT INTO audit_log VALUES (?, ?, ?)", []) == 0


def test_query_mentioning_bound_slug_passes_through(client):
    # The query embeds the bound slug's Vectorize index name. The wrapper
    # should NOT refuse it.
    client.execute(
        "INSERT INTO memory_index (key, slug) VALUES (?, ?)",
        "hermes-acme-vault",
        "acme",
    )
    rows = client.query("SELECT key FROM memory_index WHERE key = 'hermes-acme-vault'")
    assert rows == [{"key": "hermes-acme-vault"}]


def test_query_embedding_own_vault_path_passes_through(client):
    rowcount = client.execute(
        "UPDATE memory_state SET r2_key = 'vaults/acme/foo.json' WHERE id = ?",
        "k1",
    )
    # No row matched (table is empty); the test asserts no namespace refusal.
    assert rowcount == 0


# ---------------------------------------------------------------------------
# SQL refusal — foreign Vectorize-index name
# ---------------------------------------------------------------------------


def test_execute_refuses_foreign_vault_index(client):
    with pytest.raises(NamespaceAssertionError) as excinfo:
        client.execute(
            "INSERT INTO memory_index (key, slug) VALUES ('hermes-other-vault', 'other')"
        )
    assert excinfo.value.violation_kind == "d1_sql"
    assert excinfo.value.expected_slug == "acme"
    assert excinfo.value.attempted_target == "hermes-other-vault"


def test_execute_refuses_foreign_corrections_index(client):
    with pytest.raises(NamespaceAssertionError) as excinfo:
        client.execute("SELECT * FROM memory_index WHERE key = 'hermes-other-corrections'")
    assert excinfo.value.attempted_target == "hermes-other-corrections"


def test_query_refuses_foreign_corrections_index(client):
    with pytest.raises(NamespaceAssertionError):
        client.query("SELECT * FROM memory_index WHERE key = 'hermes-other-corrections'")


def test_execute_many_refuses_foreign_index_before_any_write(client):
    with pytest.raises(NamespaceAssertionError):
        client.execute_many(
            "INSERT INTO memory_index (key, slug) VALUES ('hermes-other-vault', ?)",
            [("a",), ("b",)],
        )
    rows = client.query("SELECT COUNT(*) AS n FROM memory_index")
    assert rows == [{"n": 0}]


# ---------------------------------------------------------------------------
# SQL refusal — foreign R2 vault path interpolated into SQL
# ---------------------------------------------------------------------------


def test_execute_refuses_foreign_vault_path(client):
    with pytest.raises(NamespaceAssertionError) as excinfo:
        client.execute(
            "UPDATE memory_state SET r2_key = 'vaults/other/foo.json' WHERE id = ?",
            "k1",
        )
    assert excinfo.value.violation_kind == "d1_sql"
    assert excinfo.value.attempted_target == "vaults/other/"


def test_query_refuses_foreign_vault_path(client):
    with pytest.raises(NamespaceAssertionError):
        client.query("SELECT id FROM memory_state WHERE r2_key LIKE 'vaults/other/%'")


# ---------------------------------------------------------------------------
# Exception carries structured attributes
# ---------------------------------------------------------------------------


def test_namespace_assertion_error_attributes(client):
    with pytest.raises(NamespaceAssertionError) as excinfo:
        client.execute("SELECT 'hermes-other-vault'")
    err = excinfo.value
    assert err.violation_kind == "d1_sql"
    assert err.expected_slug == "acme"
    assert err.attempted_target == "hermes-other-vault"
    assert "foreign" in err.detail or "different customer" in err.detail


# ---------------------------------------------------------------------------
# Env-bound construction
# ---------------------------------------------------------------------------


def test_d1_client_from_env_requires_customer_slug(monkeypatch):
    monkeypatch.delenv("CUSTOMER_SLUG", raising=False)
    with pytest.raises(RuntimeError, match="CUSTOMER_SLUG"):
        d1_client_from_env()


def test_d1_client_from_env_uses_env_slug(monkeypatch, db_path):
    monkeypatch.setenv("CUSTOMER_SLUG", "acme")
    monkeypatch.setenv("CUSTOMER_DB", db_path)
    client = d1_client_from_env()
    assert client.customer_slug == "acme"
    # Refusal probe — bound slug is correctly threaded through.
    with pytest.raises(NamespaceAssertionError) as excinfo:
        client.execute("SELECT 'hermes-other-vault'")
    assert excinfo.value.expected_slug == "acme"


def test_d1_client_from_env_accepts_explicit_slug(monkeypatch, db_path):
    monkeypatch.delenv("CUSTOMER_SLUG", raising=False)
    monkeypatch.setenv("CUSTOMER_DB", db_path)
    client = d1_client_from_env("operator-slug")
    assert client.customer_slug == "operator-slug"


def test_d1_client_from_env_raises_when_binding_unset(monkeypatch):
    monkeypatch.setenv("CUSTOMER_SLUG", "acme")
    monkeypatch.delenv("CUSTOMER_DB", raising=False)
    client = d1_client_from_env()
    # Lazy binding: error fires on first call, not at construction.
    with pytest.raises(RuntimeError, match="CUSTOMER_DB"):
        client.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Headline AC — cross-customer attempt is refused and instance is reusable
# ---------------------------------------------------------------------------


def test_cross_customer_attempt_refused_then_client_remains_usable(client):
    """A foreign-slug SQL statement raises; subsequent valid SQL works fine.

    The wrapper is stateless across calls — a refusal does not poison
    the client. The customer should still be able to write its own
    rows after a foreign attempt is caught.
    """
    with pytest.raises(NamespaceAssertionError):
        client.execute(
            "INSERT INTO memory_index (key, slug) VALUES ('hermes-other-vault', 'other')"
        )
    # Same client, valid SQL, works.
    client.execute(
        "INSERT INTO audit_log (id, actor, kind) VALUES (?, ?, ?)",
        "row-1",
        "agent",
        "after-refusal",
    )
    rows = client.query("SELECT kind FROM audit_log")
    assert rows == [{"kind": "after-refusal"}]
