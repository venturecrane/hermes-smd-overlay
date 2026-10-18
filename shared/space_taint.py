"""What a tainted turn may still keep, on a seat several people share (ss-console #3283).

WHY. Every read of outside content (``web_search``, a fenced inbound) marks the
session tainted (``shared.inbound.SESSION_TAINT``), and a tainted session may
write nothing to a record store or to memory for the rest of the conversation.
That is the injection defense: a page cannot plant something a later turn
trusts. On a couples seat it also breaks the job. A price watch cannot keep
the price it found; a date-night idea from a search cannot be saved.

The narrowing, on a seat that authors ``people:`` only:

* A tainted turn in a PERSON's own private space may write into that same
  space (``record_store_write``, ``remember``). What it writes is stamped
  ``from_web: true`` so a later turn knows where it came from. The person who
  asked is the only reader, and the words never leave their space.
* Anything that crosses into ``shared`` (or leaves the seat) stays blocked,
  unless it went through the "share it" gate (``plugins/hermes-smd-space-gate``):
  a person confirming the exact words is the injection defense there. The gate
  notes a release (``shared.space_writes.note_release``) and the write spends it.
* A scheduled run has no chat to confirm in: it may write only into its own
  bound space.
* A tainted turn in the shared group, or on an unresolved space, writes nothing,
  as before.

Seats without ``people:`` never reach this module: the old rule (no write on a
tainted turn) is untouched there.
"""

from __future__ import annotations

from typing import Any

from shared import space as space_mod

#: The provenance key a record written on a tainted turn carries.
FROM_WEB_KEY = "from_web"


def tainted_write_allowed(tool_name: str, session_id: Any, target_key: str | None) -> bool:
    """Whether a tainted turn may make this write. ``target_key`` is the space the
    write lands in. Call only on a space-mode seat; raises nothing."""
    try:
        current = space_mod.resolve_space(str(session_id or "") or None)
    except space_mod.SpaceConfigError:
        return False
    if current is None or not target_key:
        return False
    if target_key == current.key and (current.private or current.source == space_mod.SOURCE_CRON):
        return True
    if current.private and current.source in (space_mod.SOURCE_TELEGRAM_DM, space_mod.SOURCE_EMAIL):
        from shared import space_writes

        return space_writes.take_release(tool_name, current)
    return False


def stamp_from_web(content: str) -> str:
    """``content`` with ``from_web: true`` in its frontmatter. Idempotent; a
    record without frontmatter gains a frontmatter block holding only that key."""
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                head = lines[1:i]
                if any(h.split(":", 1)[0].strip() == FROM_WEB_KEY for h in head):
                    return content
                return "\n".join([lines[0], *head, f"{FROM_WEB_KEY}: true", *lines[i:]])
    return f"---\n{FROM_WEB_KEY}: true\n---\n{content}"


__all__ = ["FROM_WEB_KEY", "stamp_from_web", "tainted_write_allowed"]
