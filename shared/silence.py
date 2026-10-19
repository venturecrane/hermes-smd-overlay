"""Whether a turn's final text is Hermes' "say nothing" answer.

One rule for every seat-side consumer of a turn's final response: the couples
routine email (``shared.space_delivery``) and the reply channel's end-of-turn
text delivery (``hermes-smd-reply`` ``text_delivery``). A response a skill
authored as silence must never become an email, whichever path would send it.
"""

from __future__ import annotations

_SILENT_TOKENS = frozenset({"[silent]", "silent", "no_reply", "no reply"})


def is_silent(text: str) -> bool:
    """Hermes' cron silence rule (``cron/scheduler.py`` ``_is_cron_silence_response``):
    the shared matcher when importable, else the same tokens on the whole text
    or its first or last line. Empty or whitespace-only text is silent."""
    if not isinstance(text, str) or not text.strip():
        return True
    try:
        from gateway.response_filters import (  # type: ignore[import-not-found]
            is_autonomous_silence_response,
        )

        return bool(is_autonomous_silence_response(text))
    except Exception:  # noqa: BLE001 - outside the gateway, the local rule
        lines = [ln.strip().lower() for ln in text.strip().splitlines() if ln.strip()]
        if not lines:
            return True
        return any(c in _SILENT_TOKENS for c in (text.strip().lower(), lines[0], lines[-1]))


__all__ = ["is_silent"]
