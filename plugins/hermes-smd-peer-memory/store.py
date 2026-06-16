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
    """Create the peer_preferences table + indexes idempotently."""
    for ddl in schemas.PEER_MEMORY_DDLS:
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
) -> str:
    """Insert one active preference for a peer; return its id.

    Recency wins: any active row for the same (customer, peer, persona) whose
    preference text matches case-insensitively/trimmed is marked
    ``superseded_by`` the new id first, so re-statements don't pile duplicates
    while genuinely different preferences coexist. Captain prunes via the admin
    Learned lane. ``recorded_at`` is left to the table default (``datetime('now')``).
    """
    pref_id = new_id or uuid.uuid4().hex

    client.execute(
        "UPDATE peer_preferences SET superseded_by = ? "
        "WHERE customer_slug = ? AND peer_id = ? AND persona_slug = ? "
        "AND superseded_by IS NULL AND lower(trim(preference)) = lower(trim(?))",
        pref_id,
        customer_slug,
        peer_id,
        persona_slug,
        preference,
    )
    client.execute(
        "INSERT INTO peer_preferences "
        "(id, customer_slug, peer_id, persona_slug, preference, why, how_to_apply, source, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        pref_id,
        customer_slug,
        peer_id,
        persona_slug,
        preference,
        why,
        how_to_apply,
        source,
        session_id,
    )
    return pref_id


def active_preferences(
    client: Any,
    *,
    peer_id: str,
    persona_slug: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return a peer's active (non-superseded) preferences, newest first.

    When ``persona_slug`` is falsy the read spans all personas for the peer
    (the safe superset when the active persona is unknown); when set it filters
    to that persona. ``rowid DESC`` breaks ties on the second-granularity
    ``recorded_at`` so ordering is stable.
    """
    if persona_slug:
        return client.query(
            f"SELECT {_SELECT_COLS} FROM peer_preferences "
            "WHERE peer_id = ? AND persona_slug = ? AND superseded_by IS NULL "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
            peer_id,
            persona_slug,
            limit,
        )
    return client.query(
        f"SELECT {_SELECT_COLS} FROM peer_preferences "
        "WHERE peer_id = ? AND superseded_by IS NULL "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT ?",
        peer_id,
        limit,
    )


def render_preference_block(rows: list[dict[str, Any]], *, peer_id: str) -> str:
    """Render active preferences as the pre_llm_call context block.

    Returns ``""`` when there is nothing to inject (the hook then contributes
    no context). ``peer_id`` is intentionally NOT printed — it may be a raw
    email and carries no meaning for the model; the block is framed as "the
    person you are replying to".
    """
    if not rows:
        return ""
    lines = [
        "How the person you are replying to likes you to work with them "
        "(captured from what they have stated or demonstrated, never assumed). "
        "Apply these in how you respond:"
    ]
    for row in rows[:_RENDER_CAP]:
        line = f"- {row['preference']}"
        why = row.get("why")
        how = row.get("how_to_apply")
        if why:
            line += f" (why: {why})"
        if how:
            line += f" [apply: {how}]"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "VALID_SOURCES",
    "active_preferences",
    "ensure_schema",
    "parse_capture_args",
    "record_preference",
    "render_preference_block",
]
