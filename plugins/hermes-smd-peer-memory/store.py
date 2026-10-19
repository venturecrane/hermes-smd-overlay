"""Pure store + render helpers for per-peer working-preference memory.

Everything here takes an explicit :class:`shared.d1_client.D1Client` (or any
object with the same ``execute`` / ``query`` contract) so it is unit-testable
against a tmp sqlite file with no Machine env. The hook glue lives in
``__init__``; the policy (validation, supersession, rendering) lives here.
"""

from __future__ import annotations

import uuid
from typing import Any

from . import schemas

# Accepted provenance for a captured preference. ``stated`` = the person said
# it; ``demonstrated`` = observed concretely in how they worked. Anything else
# is rejected — there is no inference path (that would be Honcho by the back
# door, rejected per ADR 0048).
VALID_SOURCES: frozenset[str] = frozenset({"stated", "demonstrated"})

# Soft caps so a single capture can't bloat the injected context or the row.
_FIELD_CAP = 600

# Active preferences injected per turn are capped so a chatty history can't
# crowd the system prompt. Newest-first; the cap keeps the most recent.
_RENDER_CAP = 12

# Columns read for the active set / export shape.
_SELECT_COLS = "id, peer_id, persona_slug, preference, why, how_to_apply, source, recorded_at"


def ensure_schema(client: Any) -> None:
    """Create the peer_preferences table, add any column a table created before
    it lacks (``schemas.PEER_PREFERENCES_ADDED_COLUMNS``), then the indexes.
    Idempotent: a second run finds every column present and adds nothing."""
    for ddl in schemas.PEER_MEMORY_TABLE_DDLS:
        client.execute(ddl)
    present = {str(row.get("name")) for row in client.query("PRAGMA table_info(peer_preferences)")}
    for column, ddl in schemas.PEER_PREFERENCES_ADDED_COLUMNS:
        if column not in present:
            client.execute(ddl)
    for ddl in schemas.PEER_MEMORY_INDEX_DDLS:
        client.execute(ddl)


def _clip(value: Any) -> str | None:
    """Normalize an optional free-text field: trimmed str or None, capped."""
    if isinstance(value, str) and value.strip():
        return value.strip()[:_FIELD_CAP]
    return None


def parse_capture_args(args: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the agent-supplied capture args.

    Returns ``(clean, None)`` on success or ``(None, error)`` on rejection.
    ``preference`` is required and non-empty; ``source`` defaults to ``stated``
    and must be one of :data:`VALID_SOURCES`; ``why`` / ``how_to_apply`` are
    optional. No trait/label field is accepted by construction.
    """
    if not isinstance(args, dict):
        return None, "args must be an object"

    preference = args.get("preference")
    if not isinstance(preference, str) or not preference.strip():
        return None, "preference is required and must be a non-empty string"

    source = args.get("source")
    if source is None or source == "":
        source = "stated"
    if source not in VALID_SOURCES:
        return None, "source must be 'stated' or 'demonstrated'"

    return (
        {
            "preference": preference.strip()[:_FIELD_CAP],
            "why": _clip(args.get("why")),
            "how_to_apply": _clip(args.get("how_to_apply")),
            "source": source,
        },
        None,
    )


def record_preference(
    client: Any,
    *,
    customer_slug: str,
    peer_id: str,
    persona_slug: str,
    preference: str,
    why: str | None,
    how_to_apply: str | None,
    source: str,
    session_id: str,
    new_id: str | None = None,
    space: str = "",
    kind: str = "preference",
    from_web: bool = False,
) -> str:
    """Insert one active preference for a peer; return its id.

    Recency wins: any active row for the same (customer, peer, persona, space,
    kind) whose text matches case-insensitively/trimmed is marked
    ``superseded_by`` the new id first, so re-statements don't pile duplicates
    while genuinely different preferences coexist. Captain prunes via the admin
    Learned lane. ``recorded_at`` is left to the table default (``datetime('now')``).

    ``space`` and ``kind`` (ADR 0091) default to the values every row written
    before them carries, so a seat with no spaces writes and supersedes exactly
    as before. A restatement in one space never supersedes the same words kept
    in another: the two rows have different readers.
    """
    if kind not in schemas.KINDS:
        raise ValueError(f"kind must be one of {sorted(schemas.KINDS)}")
    pref_id = new_id or uuid.uuid4().hex

    client.execute(
        "UPDATE peer_preferences SET superseded_by = ? "
        "WHERE customer_slug = ? AND peer_id = ? AND persona_slug = ? "
        "AND space = ? AND kind = ? "
        "AND superseded_by IS NULL AND lower(trim(preference)) = lower(trim(?))",
        pref_id,
        customer_slug,
        peer_id,
        persona_slug,
        space,
        kind,
        preference,
    )
    client.execute(
        "INSERT INTO peer_preferences "
        "(id, customer_slug, peer_id, persona_slug, preference, why, how_to_apply, source, "
        "session_id, space, kind, from_web) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        pref_id,
        customer_slug,
        peer_id,
        persona_slug,
        preference,
        why,
        how_to_apply,
        source,
        session_id,
        space,
        kind,
        1 if from_web else 0,
    )
    return pref_id


def active_preferences(
    client: Any,
    *,
    peer_id: str,
    persona_slug: str | None = None,
    limit: int = 50,
    customer_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Return a peer's active (non-superseded) preferences, newest first.

    When ``persona_slug`` is falsy the read spans all personas for the peer
    (the safe superset when the active persona is unknown); when set it filters
    to that persona. ``rowid DESC`` breaks ties on the second-granularity
    ``recorded_at`` so ordering is stable.

    ``customer_slug``, when given, scopes the read to that customer's rows.
    The table lives in a per-seat sqlite file on the seat's own volume
    (``shared.d1_client``), so rows of another customer should never be
    there; the scope makes that a property of the query, not of where the
    file happens to live. Every write already stamps it.
    """
    clauses = ["peer_id = ?", "superseded_by IS NULL"]
    params: list[Any] = [peer_id]
    if customer_slug:
        clauses.insert(0, "customer_slug = ?")
        params.insert(0, customer_slug)
    if persona_slug:
        clauses.append("persona_slug = ?")
        params.append(persona_slug)
    return client.query(
        f"SELECT {_SELECT_COLS} FROM peer_preferences WHERE {' AND '.join(clauses)} "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
        *params,
        limit,
    )


# The capture-side instruction, injected on every sender-attributed turn.
# Without this the learned lane never fills: the 2026-07-15 live probe (ss
# #1941) found ZERO peer_preferences rows on every seat because the tool
# existed but nothing ever instructed the agent to use it. The read side
# (preferences above) and the write side (this nudge) ship together so the
# capability is wired into behavior, not just registered.
#
# The nudge also carries the SILENCE half (ss-console#2552). Telling the agent to
# record, and nothing else, produced a confirm email reading "That preference is
# recorded to your profile" — which to a colleague reads as being profiled. A
# person who tells you how they like something done expects "got it", not a
# receipt from a filing system. Keeping the record is fine; narrating it is not.
#
# This clause is an INSTRUCTION, not a control: it lowers the rate, it cannot
# guarantee. The enforcing floor is the record-narration-about-a-person marker in
# the outbound provenance gate.
_CAPTURE_NUDGE = (
    "If this person states a preference about how they want you to work with "
    "them, or concretely demonstrates one, record it with the "
    "record_peer_preference tool: one concrete working preference per call, "
    "marked stated or demonstrated. Never record a trait label or a guess. "
    "If they change a preference, record the new version. "
    "Do this silently. Acknowledge what they asked for in terms of what you "
    "will now do, and never tell them a preference was recorded, that a record "
    "or profile about them exists, or where it is kept. If they ask you "
    "directly what you know about how they like to work, answer completely."
)


def render_preference_block(rows: list[dict[str, Any]], *, peer_id: str) -> str:
    """Render the per-peer context block for pre_llm_call.

    Always ends with the capture instruction (the write side of the lane —
    ADR 0048 §3); when active preferences exist they render first (the read
    side). ``peer_id`` is intentionally NOT printed — it may be a raw email
    and carries no meaning for the model; the block is framed as "the person
    you are replying to".
    """
    lines: list[str] = []
    if rows:
        lines.append(
            "How the person you are replying to likes you to work with them "
            "(captured from what they have stated or demonstrated, never assumed). "
            "Apply these in how you respond:"
        )
        for row in rows[:_RENDER_CAP]:
            line = f"- {row['preference']}"
            why = row.get("why")
            how = row.get("how_to_apply")
            if why:
                line += f" (why: {why})"
            if how:
                line += f" [apply: {how}]"
            lines.append(line)
        lines.append("")
    lines.append(_CAPTURE_NUDGE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Space mode (ADR 0091, ss-console #3264): a seat several people share.
# ---------------------------------------------------------------------------

#: Memories injected per turn, newest first.
_MEMORY_RENDER_CAP = 40


def parse_remember_args(args: Any) -> tuple[str | None, str | None]:
    """Validate the agent-supplied ``remember`` args: ``(fact, None)`` or
    ``(None, error)``. The tool takes the fact only; WHERE it is kept is never
    an argument (it is the turn's space, resolved structurally)."""
    if not isinstance(args, dict):
        return None, "args must be an object"
    fact = args.get("fact")
    if not isinstance(fact, str) or not fact.strip():
        return None, "fact is required and must be a non-empty string"
    return fact.strip()[:_FIELD_CAP], None


def space_rows(
    client: Any,
    *,
    customer_slug: str,
    readable: frozenset[str] | set[str],
    speaker: str | None,
    persona_slug: str | None = None,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(memories, preferences)`` a turn may see, newest first.

    Only this customer's rows whose ``space`` is in ``readable`` are read; the
    SQL carries both, so a row outside them never leaves the database. Person
    ids are seat-local slugs (``person:p1`` on every couples seat), so the
    customer scope is what keeps one seat's p1 from ever being another's.
    Memories are the space's (anyone in it may see them, whichever persona kept
    them); preferences are additionally the speaker's own and, as on any seat,
    the active persona's. An empty ``readable`` or a missing ``customer_slug``
    reads nothing and does not query.
    """
    keys = sorted(k for k in readable if isinstance(k, str) and k)
    if not keys or not customer_slug:
        return [], []
    marks = ", ".join("?" for _ in keys)
    persona_clause = " AND persona_slug = ?" if persona_slug else ""
    persona_args = (persona_slug,) if persona_slug else ()
    cols = f"{_SELECT_COLS}, customer_slug, space, kind, from_web"
    memories = client.query(
        f"SELECT {cols} FROM peer_preferences "
        f"WHERE customer_slug = ? AND kind = 'memory' AND space IN ({marks}) "
        "AND superseded_by IS NULL "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
        customer_slug,
        *keys,
        limit,
    )
    preferences: list[dict[str, Any]] = []
    if speaker:
        preferences = client.query(
            f"SELECT {cols} FROM peer_preferences "
            f"WHERE customer_slug = ? AND kind = 'preference' AND peer_id = ? "
            f"AND space IN ({marks}) AND superseded_by IS NULL{persona_clause} "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
            customer_slug,
            speaker,
            *keys,
            *persona_args,
            limit,
        )
    # Defense in depth: the SQL already filtered, and a row that somehow did not
    # match is dropped rather than shown.
    allowed = set(keys)

    def ok(row: dict[str, Any]) -> bool:
        return row.get("space") in allowed and row.get("customer_slug") == customer_slug

    return [r for r in memories if ok(r)], [r for r in preferences if ok(r)]


_REMEMBER_NUDGE = (
    "To keep something for later, use the remember tool. It is kept in the "
    "space this conversation is in: said in one person's private conversation, it "
    "stays private to them; said where both people are, both can see it. Never "
    "carry anything from one person's private conversation to the other person "
    "unless that person asks you to."
)

_PRIVATE_SPACE_NOTE = (
    "This is a private conversation with one person. What they tell you here is "
    "theirs: never repeat it to the other person unless they ask you to."
)


def render_space_block(
    memories: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
    *,
    private: bool,
) -> str:
    """The per-turn context block on a space-mode seat. Each memory is labeled
    with whether it is shared or private to the person in this conversation."""
    lines: list[str] = []
    if private:
        lines.extend([_PRIVATE_SPACE_NOTE, ""])
    if memories:
        lines.append("Things you were asked to remember (newest first):")
        for row in memories[:_MEMORY_RENDER_CAP]:
            tag = "shared" if row.get("space") == "shared" else "private to this person"
            if row.get("from_web"):
                tag += ", from a web search"
            lines.append(f"- [{tag}] {row['preference']}")
        lines.append("")
    if preferences:
        lines.append(
            "How the person you are replying to likes you to work with them "
            "(captured from what they have stated or demonstrated, never assumed). "
            "Apply these in how you respond:"
        )
        for row in preferences[:_RENDER_CAP]:
            line = f"- {row['preference']}"
            if row.get("why"):
                line += f" (why: {row['why']})"
            if row.get("how_to_apply"):
                line += f" [apply: {row['how_to_apply']}]"
            lines.append(line)
        lines.append("")
    lines.append(_REMEMBER_NUDGE)
    lines.append(_CAPTURE_NUDGE)
    return "\n".join(lines)


__all__ = [
    "VALID_SOURCES",
    "active_preferences",
    "parse_remember_args",
    "render_space_block",
    "space_rows",
    "ensure_schema",
    "parse_capture_args",
    "record_preference",
    "render_preference_block",
]
