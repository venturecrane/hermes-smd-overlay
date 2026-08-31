"""In-turn rendered-body check for slot-templated routines (WS-RENDER).

A cron session whose consumed dispatch envelope DECLARED in-turn templates
(``prerendered_dispatch.in_turn_templates``) may send, in that session, only a
body that IS one of those templates with each ``{slot}`` region filled from
the slot's closed phrase list. Everything the model would add beyond a slot is
a deviation — the 2026-08-25 class of defect, blocked before it sends rather
than reviewed after.

The check binds ONLY when the envelope declared templates with
``enforce: true``. No declaration, an interactive session, a skill that opted
out (the verification tracker's Shape A approve-and-send is a legitimate
free-form internal send whose template pre_run cannot pre-key) — all pass
untouched. Fail-open on any internal error: this is a second wall behind the
ceiling and the content gates, not the first.

Slot semantics: a template is split on ``{name}`` markers; the submitted body
must reproduce the literal text between markers exactly, and each slot region
must be at most :data:`_SLOT_MAX_CHARS` characters and — when the slot
declares a closed phrase list — one of those phrases. An empty phrase list
accepts any value under the cap (a plan-carried value set).

Two deviations on one session flip the message from "use the prepared text"
to "move on" — the items re-fire next run by the ledger's own re-fire
property, which is the same recovery the out-of-turn ladder uses. The
messages are corrective action only and never name a gate or a rule.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict

logger = logging.getLogger(__name__)

_SLOT_MAX_CHARS = 120
_SLOT_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

#: Per-session deviation counts, bounded.
_DEVIATIONS: OrderedDict[str, int] = OrderedDict()
_MAX_SESSIONS = 128

_FIRST_MESSAGE = (
    "Use the prepared text for this item exactly as provided in your Script "
    "Output; fill only the marked fields, and change nothing else. Do not "
    "compose your own version."
)
_SECOND_MESSAGE = (
    "Use the prepared text for this item exactly as provided in your Script "
    "Output; fill only the marked fields. This item will be picked up on the "
    "next run; move on."
)


def _canon(text: str) -> str:
    """Whitespace-tolerant canonical form: CRLF->LF, per-line trailing
    whitespace stripped, trailing newlines stripped — the same normalization
    the body hash uses, so 'exactly the template' and 'hashes like the
    template' are one judgment."""
    lines = [line.rstrip(" \t") for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).rstrip("\n")


def body_matches_template(body: str, template: str, slots: dict | None) -> bool:
    """True iff ``body`` is ``template`` with every slot region satisfied."""
    body = _canon(body)
    template = _canon(template)
    slots = slots if isinstance(slots, dict) else {}
    parts = _SLOT_RE.split(template)
    # parts = [literal, slot_name, literal, slot_name, ..., literal]
    pattern_parts: list[str] = []
    slot_names: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            pattern_parts.append(re.escape(part))
        else:
            slot_names.append(part)
            pattern_parts.append(r"(.{0," + str(_SLOT_MAX_CHARS) + r"}?)")
    match = re.fullmatch("".join(pattern_parts), body, re.DOTALL)
    if match is None:
        return False
    for name, value in zip(slot_names, match.groups(), strict=False):
        allowed = slots.get(name)
        if isinstance(allowed, list) and allowed:
            if value.strip() not in {str(a).strip() for a in allowed}:
                return False
        elif len(value) > _SLOT_MAX_CHARS:
            return False
    return True


def check_body(session_id: str, body: str, declaration: dict | None) -> dict | None:
    """Block directive for a non-conformant body, or None to allow.

    ``declaration`` is ``prerendered_dispatch.in_turn_templates(session_id)``
    (None -> allow). Only ``enforce: true`` declarations block."""
    try:
        if not isinstance(declaration, dict) or not declaration.get("enforce"):
            return None
        templates = declaration.get("templates") or []
        if not templates:
            return None
        if not isinstance(body, str) or not body.strip():
            return None  # nothing to check; the send gates own empty bodies
        for entry in templates:
            template = entry.get("template")
            if isinstance(template, str) and body_matches_template(
                body, template, entry.get("slots")
            ):
                return None
        count = _DEVIATIONS.get(session_id, 0) + 1
        _DEVIATIONS[session_id] = count
        while len(_DEVIATIONS) > _MAX_SESSIONS:
            _DEVIATIONS.popitem(last=False)
        logger.info(
            "rendered_body_gate: non-conformant body on %s (deviation %d)", session_id, count
        )
        return {
            "action": "block",
            "message": _FIRST_MESSAGE if count < 2 else _SECOND_MESSAGE,
        }
    except Exception:  # noqa: BLE001 — a second wall fails open
        logger.debug("rendered_body_gate: check failed", exc_info=True)
        return None


__all__ = ["body_matches_template", "check_body"]
