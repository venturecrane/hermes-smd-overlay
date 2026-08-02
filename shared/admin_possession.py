"""Mailbox-possession confirmation for admin-classed establishment instructions
on AgentMail-custody channels (ss ADR 0085 §5, ss-console #2164).

WHY THIS EXISTS. Establishment authority is an email identity (``scope.admins``),
so admin classification is only as strong as sender attribution on the channel
the instruction arrived on. The #2164 probe returned WEAK: AgentMail exposes no
per-message SPF/DKIM/DMARC verdict a seat can require, so on an
AgentMail-custody seat the ``From`` header of an inbound message is a claim,
not proof. ADR 0085 §5 names the fallback: mailbox possession — ADR 0057's own
identity primitive — applied only to the spoofable channel and only to
firm-level acts. The Operator emails a challenge to the ROSTERED admin address
(the authored ``scope.admins`` entry — never a claimed From display or a
Reply-To); a reply containing the challenge proves the person can read that
mailbox, which is the identity the allow list actually names.

THE CEREMONY'S PROPERTIES, all load-bearing:

* **Nonce** — server-generated, unguessable (``secrets``), never derived from
  anything the requester supplied.
* **TTL** — :data:`NONCE_TTL_SECONDS` (72 hours). Hours, not minutes, and
  deliberately unlike the 15-minute ``pending_send`` approval window: that
  window bounds an approval inside a live conversation, while this one bounds a
  cross-mailbox round trip to a person who may not open mail until the next
  business day. Single-use plus the recipient lock (the nonce may only ever be
  emailed to the rostered address, enforced at the tool gate) is what bounds
  the exposure of the longer window.
* **Single-use** — a nonce confirms exactly once; confirmation consumes it.
* **Restart-durable** — Machine-local SQLite on the Fly volume, the
  ``shared/exposure_override.py`` precedent (ADR 0062 posture). Chosen over the
  ``shared/pending_send.py`` in-process register deliberately: pending-send
  state is a minutes-scale conversational handshake where a restart correctly
  voids the approval, while possession is a durable one-time-per-admin fact —
  losing it on restart would re-run the ceremony on every deploy, and losing an
  outstanding nonce mid-round-trip would strand the admin's in-flight reply.

ONE-TIME PER ADMIN. Once confirmed, the state persists until ``scope.admins``
changes for that entry: :func:`reconcile` deletes every row whose address is no
longer on the authored list, so removing an admin revokes their possession
state and re-adding them re-runs the ceremony. An unrelated list change (adding
a second admin) does not disturb a confirmed row.

This module is pure state + rules. Channel scoping (AgentMail custody only,
M365 tenant-auth exempt) and the challenge-send instruction live in the
establishment plugin's gate; the recipient lock on outbound nonce-carrying
sends lives there too. Plugins import shared, never the reverse.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from shared.ids import iso_utc

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "/opt/data/smd/admin_possession.db"

#: 72 hours. Possession of a mailbox is being proven, not a login performed:
#: the admin may not open mail until after a weekend, and the challenge is
#: single-use and recipient-locked, so a longer window adds negligible risk
#: while a short one strands legitimate replies. See the module docstring.
NONCE_TTL_SECONDS: float = 72 * 3600

#: Human-legible prefix so the code reads as deliberate inside an email body.
#: Unguessability comes entirely from the token_hex suffix.
_NONCE_PREFIX = "smd-confirm-"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS admin_possession (
  customer            TEXT NOT NULL,
  admin_address       TEXT NOT NULL,
  nonce               TEXT,
  nonce_issued_at     TEXT,
  nonce_expires_epoch REAL,
  confirmed           INTEGER NOT NULL DEFAULT 0,
  confirmed_at        TEXT,
  confirmed_via       TEXT,
  PRIMARY KEY (customer, admin_address)
)
"""

#: The per-person twin of the admin table (ss ADR 0085 §6 / O5). A SEPARATE
#: table, not extra rows in ``admin_possession``, because the two re-arm rules
#: differ and :func:`reconcile` deletes every admin-table row whose address is
#: not on ``scope.admins`` — a person's row stored there would be revoked by
#: the very next admin ceremony. Person rows re-arm against ROSTER membership
#: (a predicate, because roster entries may be ``@domain`` grants that cannot
#: be enumerated into an address list).
_PERSON_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS person_possession (
  customer            TEXT NOT NULL,
  admin_address       TEXT NOT NULL,
  nonce               TEXT,
  nonce_issued_at     TEXT,
  nonce_expires_epoch REAL,
  confirmed           INTEGER NOT NULL DEFAULT 0,
  confirmed_at        TEXT,
  confirmed_via       TEXT,
  PRIMARY KEY (customer, admin_address)
)
"""

STATE_CONFIRMED = "confirmed"
STATE_CHALLENGE_ISSUED = "challenge_issued"
STATE_CHALLENGE_PENDING = "challenge_pending"


def db_path() -> str:
    """Resolve the possession state file path (env override for tests)."""
    return os.environ.get("SMD_ADMIN_POSSESSION_DB_PATH") or DEFAULT_DB_PATH


def _customer_slug() -> str:
    return os.environ.get("SMD_CUSTOMER_SLUG") or os.environ.get("CUSTOMER_SLUG") or "_machine"


def _connect(path: str | None) -> sqlite3.Connection:
    resolved = Path(path or db_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_PERSON_CREATE_TABLE_SQL)
    return conn


def _normalize(address: object) -> str:
    return address.strip().lower() if isinstance(address, str) else ""


def new_nonce() -> str:
    """A fresh unguessable challenge code."""
    return _NONCE_PREFIX + secrets.token_hex(16)


def reconcile(current_admins: list[str], path: str | None = None) -> None:
    """Delete every possession row whose address left ``scope.admins``.

    This is the re-arm rule: removing an admin entry revokes both their
    confirmed possession and any outstanding challenge, so re-adding the entry
    re-runs the ceremony from scratch (an old nonce can never confirm the
    re-added entry — its row is gone). Rows for addresses still on the list are
    untouched, so an unrelated list change never disturbs a confirmed admin.
    """
    admins = {_normalize(a) for a in current_admins if _normalize(a)}
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT admin_address FROM admin_possession WHERE customer = ?",
            (_customer_slug(),),
        ).fetchall()
        stale = [r[0] for r in rows if r[0] not in admins]
        for address in stale:
            conn.execute(
                "DELETE FROM admin_possession WHERE customer = ? AND admin_address = ?",
                (_customer_slug(), address),
            )
            logger.info(
                "admin_possession: %s left scope.admins; possession state revoked (re-armed)",
                address,
            )
        conn.commit()
    finally:
        conn.close()


def verdict(
    admin_address: object,
    current_admins: list[str],
    *,
    path: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve one admin's possession state, minting a challenge when none stands.

    Returns ``{"state": ...}`` where state is :data:`STATE_CONFIRMED` (gate may
    pass), :data:`STATE_CHALLENGE_ISSUED` (a nonce was created by THIS call —
    the caller instructs the one challenge send; ``nonce`` included), or
    :data:`STATE_CHALLENGE_PENDING` (an unexpired nonce is already outstanding —
    the caller must NOT instruct a duplicate send; the same ``nonce`` included).
    An expired outstanding nonce is superseded by a fresh one (issued again).

    Reconciles against ``current_admins`` first, so a removed-then-re-added
    entry re-arms here without any separate lifecycle call.
    """
    reconcile(current_admins, path=path)
    address = _normalize(admin_address)
    if not address:
        # No address to challenge — treat as an outstanding-forever challenge the
        # caller can only refuse on (fail closed; should be unreachable behind
        # the admin gate).
        return {"state": STATE_CHALLENGE_PENDING, "nonce": ""}
    ts = time.time() if now is None else now
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT nonce, nonce_expires_epoch, confirmed FROM admin_possession "
            "WHERE customer = ? AND admin_address = ?",
            (_customer_slug(), address),
        ).fetchone()
        if row is not None and row[2]:
            return {"state": STATE_CONFIRMED}
        if (
            row is not None
            and isinstance(row[0], str)
            and row[0]
            and isinstance(row[1], float)
            and ts <= row[1]
        ):
            return {"state": STATE_CHALLENGE_PENDING, "nonce": row[0]}
        nonce = new_nonce()
        conn.execute(
            "INSERT INTO admin_possession "
            "(customer, admin_address, nonce, nonce_issued_at, nonce_expires_epoch, confirmed) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(customer, admin_address) DO UPDATE SET "
            "nonce = excluded.nonce, nonce_issued_at = excluded.nonce_issued_at, "
            "nonce_expires_epoch = excluded.nonce_expires_epoch",
            (_customer_slug(), address, nonce, iso_utc(), ts + NONCE_TTL_SECONDS),
        )
        conn.commit()
        return {"state": STATE_CHALLENGE_ISSUED, "nonce": nonce}
    finally:
        conn.close()


def try_confirm(
    sender_address: object,
    message_text: object,
    current_admins: list[str],
    *,
    source: str = "",
    path: str | None = None,
    now: float | None = None,
) -> bool:
    """Confirm possession iff a rostered admin's message carries their live nonce.

    True only when ``sender_address`` exactly matches a ``scope.admins`` entry
    AND that admin has an outstanding, unexpired nonce AND ``message_text``
    contains it. Confirmation consumes the nonce (single-use): the row flips to
    confirmed and the nonce is cleared, so the same code can never confirm a
    second time — including after the state is later cleared or re-armed. A
    wrong nonce, an expired nonce, a non-admin sender, or another admin's nonce
    all return False and change nothing.
    """
    reconcile(current_admins, path=path)
    address = _normalize(sender_address)
    admins = {_normalize(a) for a in current_admins if _normalize(a)}
    if not address or address not in admins:
        return False
    if not isinstance(message_text, str) or not message_text:
        return False
    ts = time.time() if now is None else now
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT nonce, nonce_expires_epoch, confirmed FROM admin_possession "
            "WHERE customer = ? AND admin_address = ?",
            (_customer_slug(), address),
        ).fetchone()
        if row is None or row[2]:
            return False
        nonce, expires = row[0], row[1]
        if not isinstance(nonce, str) or not nonce or nonce not in message_text:
            return False
        if not isinstance(expires, float) or ts > expires:
            return False
        conn.execute(
            "UPDATE admin_possession SET confirmed = 1, confirmed_at = ?, confirmed_via = ?, "
            "nonce = NULL, nonce_issued_at = NULL, nonce_expires_epoch = NULL "
            "WHERE customer = ? AND admin_address = ?",
            (iso_utc(), str(source or ""), _customer_slug(), address),
        )
        conn.commit()
        logger.info("admin_possession: mailbox possession confirmed for %s", address)
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Person possession (ss ADR 0085 §6 / O5) — same ceremony, roster re-arm rule
# ---------------------------------------------------------------------------


def person_reconcile(is_member, path: str | None = None) -> None:
    """Delete every person-possession row that no longer satisfies ``is_member``.

    The person twin of :func:`reconcile`, with a PREDICATE instead of a list
    because roster entries may be ``@domain`` grants: pass
    ``CustomerConfig.sender_on_roster``. A person who leaves the roster is
    revoked and re-runs the ceremony if re-added. A raising predicate deletes
    nothing (the verdict path fails toward challenging, never toward passing,
    so keeping a possibly-stale row is the harmless direction).
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT admin_address FROM person_possession WHERE customer = ?",
            (_customer_slug(),),
        ).fetchall()
        for (address,) in rows:
            try:
                if is_member(address):
                    continue
            except Exception:  # noqa: BLE001 — an unreadable roster deletes nothing
                logger.warning(
                    "person_possession: roster predicate raised during reconcile; "
                    "leaving rows in place",
                    exc_info=True,
                )
                return
            conn.execute(
                "DELETE FROM person_possession WHERE customer = ? AND admin_address = ?",
                (_customer_slug(), address),
            )
            logger.info(
                "person_possession: %s left the roster; possession state revoked (re-armed)",
                address,
            )
        conn.commit()
    finally:
        conn.close()


def person_verdict(
    person_address: object,
    is_member,
    *,
    path: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolve one person's possession state, minting a challenge when none stands.

    Same contract as :func:`verdict` (CONFIRMED / CHALLENGE_ISSUED /
    CHALLENGE_PENDING), over the person table with the roster predicate as the
    re-arm rule. CROSS-ACCEPTS a confirmed ADMIN-table row for the same
    address: possession is a fact about a mailbox, not about a role, and an
    admin whose mailbox was confirmed for firm-level establishment has already
    proven exactly what the person ceremony would ask them to prove — running
    it again for their own preferences would be ceremony for its own sake.
    (The reverse is deliberately NOT true: the admin gate consults only the
    admin table, whose lifecycle is bound to ``scope.admins``.)
    """
    person_reconcile(is_member, path=path)
    address = _normalize(person_address)
    if not address:
        return {"state": STATE_CHALLENGE_PENDING, "nonce": ""}
    ts = time.time() if now is None else now
    conn = _connect(path)
    try:
        admin_row = conn.execute(
            "SELECT confirmed FROM admin_possession WHERE customer = ? AND admin_address = ?",
            (_customer_slug(), address),
        ).fetchone()
        if admin_row is not None and admin_row[0]:
            return {"state": STATE_CONFIRMED}
        row = conn.execute(
            "SELECT nonce, nonce_expires_epoch, confirmed FROM person_possession "
            "WHERE customer = ? AND admin_address = ?",
            (_customer_slug(), address),
        ).fetchone()
        if row is not None and row[2]:
            return {"state": STATE_CONFIRMED}
        if (
            row is not None
            and isinstance(row[0], str)
            and row[0]
            and isinstance(row[1], float)
            and ts <= row[1]
        ):
            return {"state": STATE_CHALLENGE_PENDING, "nonce": row[0]}
        nonce = new_nonce()
        conn.execute(
            "INSERT INTO person_possession "
            "(customer, admin_address, nonce, nonce_issued_at, nonce_expires_epoch, confirmed) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(customer, admin_address) DO UPDATE SET "
            "nonce = excluded.nonce, nonce_issued_at = excluded.nonce_issued_at, "
            "nonce_expires_epoch = excluded.nonce_expires_epoch",
            (_customer_slug(), address, nonce, iso_utc(), ts + NONCE_TTL_SECONDS),
        )
        conn.commit()
        return {"state": STATE_CHALLENGE_ISSUED, "nonce": nonce}
    finally:
        conn.close()


def person_try_confirm(
    sender_address: object,
    message_text: object,
    is_member,
    *,
    source: str = "",
    path: str | None = None,
    now: float | None = None,
) -> bool:
    """Confirm person possession iff a rostered sender's message carries their
    live person-table nonce. Same single-use / exact-address rules as
    :func:`try_confirm`; membership is the roster predicate."""
    person_reconcile(is_member, path=path)
    address = _normalize(sender_address)
    if not address:
        return False
    try:
        if not is_member(address):
            return False
    except Exception:  # noqa: BLE001 — an unreadable roster confirms nothing
        return False
    if not isinstance(message_text, str) or not message_text:
        return False
    ts = time.time() if now is None else now
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT nonce, nonce_expires_epoch, confirmed FROM person_possession "
            "WHERE customer = ? AND admin_address = ?",
            (_customer_slug(), address),
        ).fetchone()
        if row is None or row[2]:
            return False
        nonce, expires = row[0], row[1]
        if not isinstance(nonce, str) or not nonce or nonce not in message_text:
            return False
        if not isinstance(expires, float) or ts > expires:
            return False
        conn.execute(
            "UPDATE person_possession SET confirmed = 1, confirmed_at = ?, confirmed_via = ?, "
            "nonce = NULL, nonce_issued_at = NULL, nonce_expires_epoch = NULL "
            "WHERE customer = ? AND admin_address = ?",
            (iso_utc(), str(source or ""), _customer_slug(), address),
        )
        conn.commit()
        logger.info("person_possession: mailbox possession confirmed for %s", address)
        return True
    finally:
        conn.close()


def outstanding_nonces(*, path: str | None = None, now: float | None = None) -> dict[str, str]:
    """Every live (unexpired, unconsumed) nonce → its address, BOTH tables.

    The recipient lock's read surface: an outbound tool call carrying one of
    these strings may only ship to exactly the mapped address. Person-table
    nonces are included so a person's challenge code is contained by the same
    mechanical lock as an admin's. Missing state file ⇒ ``{}`` (nothing
    outstanding, nothing to contain).
    """
    resolved = Path(path or db_path())
    if not resolved.exists():
        return {}
    ts = time.time() if now is None else now
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_PERSON_CREATE_TABLE_SQL)
        out: dict[str, str] = {}
        for table in ("admin_possession", "person_possession"):
            rows = conn.execute(
                f"SELECT nonce, admin_address, nonce_expires_epoch FROM {table} "  # noqa: S608 — table names are module constants
                "WHERE customer = ? AND confirmed = 0 AND nonce IS NOT NULL",
                (_customer_slug(),),
            ).fetchall()
            for nonce, address, expires in rows:
                if nonce and isinstance(expires, float) and ts <= expires:
                    out[str(nonce)] = str(address)
        return out
    finally:
        conn.close()


__all__ = [
    "DEFAULT_DB_PATH",
    "NONCE_TTL_SECONDS",
    "STATE_CHALLENGE_ISSUED",
    "STATE_CHALLENGE_PENDING",
    "STATE_CONFIRMED",
    "db_path",
    "new_nonce",
    "outstanding_nonces",
    "person_reconcile",
    "person_try_confirm",
    "person_verdict",
    "reconcile",
    "try_confirm",
    "verdict",
]
