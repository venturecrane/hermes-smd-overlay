"""What each numbered line of a sent message said, so a reply can name it back.

THE GAP THIS CLOSES (pilot-smokeball, 2026-09-28). A staff member answered a
deadline digest with "Got it on 1." and got back "Got it: 1 is quiet for 7
days. Still open: 2." A task review answered "Yes to all." got "Got it. Closed
1." Bare numbers make the reader go find the email they answered to learn what
we did. A great case manager says what they quieted or closed.

THE RULE. A confirmation names an item only with text that was SENT on that
numbered line, in the message the reply answers. Never text the model writes
in the reply turn, never a caption the message did not carry. So the names are
captured at the one moment code holds the sent text: right after a gated send
succeeds, keyed by that send's ``dispatch_ref`` (the same id the broker joins
the raise rows on, :mod:`shared.digest_reply_ref`). The reply side reads the
raise rows for its thread, takes their ``dispatch_ref`` and looks the names up
here. A row with no captured name (every message sent before this module, or
a capture that failed) renders its bare number, as before.

WHY A FILE, NOT THE LEDGER. The ledgers' row shapes are closed allowlists the
broker enforces, mirrored in ss-console; a label is not a ledger fact. One small
JSON file per dispatch under the ``.smd`` fence (unwritable from inside a turn),
pruned after :data:`_TTL_DAYS`.

A LABEL is a prefix of the sent line, never a rewrite: the first sentence (a
task review line goes on to say why and what we suggest), without a trailing
aside in parentheses ("(in 4 days)" is true only the day it was sent) and
without its closing period, prefixed by the line's group heading when the line
itself does not already start with it. A label that is empty, long, or carries
a dash the firm's voice rules refuse is not a label, and its number renders bare.

Best-effort throughout: a failed write or read loses a name, never a reply.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR_ENV = "SMD_SENT_LINES_DIR"
_DEFAULT_HERMES_HOME = "/opt/data"
_RELDIR = Path(".smd") / "sent_lines"
_REF_RE = re.compile(r"^[0-9a-f]{32}$")
_TTL_DAYS = 90
_MAX_LINES = 60

#: The longest label a confirmation will carry. The sent line limits are 240
#: (a review line) and a digest line is shorter; a label is a prefix of one.
MAX_LABEL = 200

_NUMBERED_LINE = re.compile(r"^(\d{1,3})\.\s+(\S.*?)\s*$")
_TRAILING_ASIDE = re.compile(r"\s*\([^()]*\)\s*$")
# Dashes the outbound filter refuses: em and en. A spaced hyphen is NOT one of
# them: firms title tasks "Serve responses - Chen", the original message carried
# it through every send check, and refusing it left the pilot's first named
# confirmation bare ("Leaving 1 as it is.", 2026-09-28). The frame this module
# writes around a label still never uses one.
_DASHES = ("—", "–")


def _dir() -> Path:
    override = os.environ.get(_DIR_ENV)
    if override:
        return Path(override)
    home = os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    return Path(home) / _RELDIR


def usable(label: object) -> bool:
    """True iff ``label`` may be rendered inside a confirmation sentence."""
    return (
        isinstance(label, str)
        and bool(label.strip())
        and label == label.strip()
        and len(label) <= MAX_LABEL
        and "\n" not in label
        and "\r" not in label
        and not any(dash in label for dash in _DASHES)
    )


def _first_sentence(text: str) -> str:
    """``text`` up to its first sentence end outside double quotes."""
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif char in ".?!" and not quoted:
            after = text[index + 1 : index + 2]
            if after in ("", " "):
                return text[:index]
    return text


def line_label(line: object, group: object = None) -> str | None:
    """The label a sent numbered line carries, or None (see the module doc)."""
    if not isinstance(line, str):
        return None
    text = line.replace("\r\n", "\n").split("\n", 1)[0].strip()
    text = _first_sentence(text).strip()
    text = _TRAILING_ASIDE.sub("", text).strip().rstrip(" ,;:.")
    if not text:
        return None
    if isinstance(group, str) and group.strip():
        head = group.strip()
        if not text.startswith(head):
            text = f"{head}: {text}"
    return text if usable(text) else None


def numbered_lines(body: object, numbers: set[int] | None = None) -> dict[int, str]:
    """``{n: label}`` for each ``N. text`` line of a sent body, first one wins.

    ``numbers`` restricts the map to the numbers the body's raise rows carry,
    so a numbered line that is not an answerable item is never named."""
    found: dict[int, str] = {}
    if not isinstance(body, str):
        return found
    for raw in body.replace("\r\n", "\n").split("\n"):
        match = _NUMBERED_LINE.match(raw)
        if not match:
            continue
        number = int(match.group(1))
        if number in found or (numbers is not None and number not in numbers):
            continue
        label = line_label(match.group(2))
        if label is not None:
            found[number] = label
    return found


def _prune(directory: Path, now: float) -> None:
    cutoff = now - _TTL_DAYS * 86_400
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def record(dispatch_ref: object, labels: dict[int, str]) -> bool:
    """Keep the labels one successful send carried. Never raises."""
    try:
        if not (isinstance(dispatch_ref, str) and _REF_RE.match(dispatch_ref)):
            return False
        kept = {
            str(n): label
            for n, label in sorted(labels.items())[:_MAX_LINES]
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 999 and usable(label)
        }
        if not kept:
            return False
        directory = _dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f"{dispatch_ref}.json"
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"labels": kept}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        _prune(directory, time.time())
        return True
    except Exception as exc:  # noqa: BLE001 — losing a name must never lose a send
        logger.warning("sent_lines: labels not kept for a dispatch (%s)", exc)
        return False


def labels(dispatch_ref: object) -> dict[int, str]:
    """The labels a dispatch carried, ``{}`` when none were kept. Never raises."""
    try:
        if not (isinstance(dispatch_ref, str) and _REF_RE.match(dispatch_ref)):
            return {}
        raw = json.loads((_dir() / f"{dispatch_ref}.json").read_text(encoding="utf-8"))
        rows = raw.get("labels") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        found: dict[int, str] = {}
        for key, label in rows.items():
            if isinstance(key, str) and key.isdigit() and usable(label):
                found[int(key)] = label
        return found
    except (OSError, ValueError):
        return {}
    except Exception as exc:  # noqa: BLE001 — a bad file loses names, never the reply
        logger.warning("sent_lines: labels unreadable (%s)", exc)
        return {}


def _join(parts: list[str], conjunction: str) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def name_numbers(
    numbers: list[int], names: dict[int, str] | None = None, conjunction: str = "and"
) -> str:
    """``numbers`` as a reader matches them to the email: each number, followed
    by the label its line carried in parentheses. Adjacent numbers that carried
    the same label share it ("1 and 2 (2026-PI-105: Hearing, Oct 2)"). A number
    with no label renders bare, exactly as before labels existed."""
    names = names or {}
    groups: list[tuple[str | None, list[int]]] = []
    for number in numbers:
        label = names.get(number)
        if label is not None and not usable(label):
            label = None
        if label is not None and groups and groups[-1][0] == label:
            groups[-1][1].append(number)
            continue
        groups.append((label, [number]))
    parts = []
    for label, group in groups:
        head = _join([str(n) for n in group], "and")
        parts.append(f"{head} ({label})" if label else head)
    return _join(parts, conjunction)


def named_in(text: str, names: dict[int, str]) -> list[str]:
    """The labels that actually appear in a rendered confirmation."""
    return [label for label in dict.fromkeys(names.values()) if f"({label})" in text]


def seed_provenance(session_id: str, text: str, names: dict[int, str]) -> None:
    """Let the reply turn send back the names it was handed.

    A label carries a matter number and often a date, and the identifier gate
    verifies those against what THIS session read. The reply turn read nothing:
    the names came from a message the full gate already cleared and delivered
    to this very thread. So the labels the confirmation renders are seeded as
    read, exactly and only those (the same shape as the establishment plugin's
    revision seeding). Nothing the model writes is seeded, and the citation
    scan and every other check still run on the reply. Never raises."""
    shown = named_in(text, names)
    if not (session_id and shown):
        return
    try:
        from shared import provenance

        provenance.record_read(session_id, "\n".join(shown))
    except Exception:  # noqa: BLE001 — a failed seed only re-refuses, never breaks
        logger.debug("sent_lines: provenance not seeded", exc_info=True)


__all__ = [
    "MAX_LABEL",
    "labels",
    "line_label",
    "name_numbers",
    "named_in",
    "numbered_lines",
    "record",
    "seed_provenance",
    "usable",
]
