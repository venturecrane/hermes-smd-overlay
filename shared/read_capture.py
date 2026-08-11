"""Connector document reads, held for the turn so staging never retypes them (ss#2247).

WHAT THIS CLOSES. ``establish_stage_document`` took the document's text as a
tool argument, which made "staged byte-for-byte" a thing the model was asked to
achieve by careful copying. It cannot. Live on the pilot 2026-08-11: a 19,114
character letter staged as 19,066, and a second document came through with an
equal-length character substitution — a corruption no length check can see. The
same class had already been recorded at overlay#236, where the identifier gate
refused staged documents carrying dollar figures and the model responded by
*deleting the wage rates* so the letter would stage. A mechanical step routed
through model discretion produces model-shaped errors; the fix is to take the
step out of the model's hands, not to instruct harder.

So the seat holds the bytes itself. ``hermes-smd-establishment`` observes every
``mcp_smokeball_read_document`` at ``post_tool_call`` — which fires BEFORE
``transform_tool_result`` (docs/hook-surface.md §2, ordering invariant), so this
module records the RAW connector text while the model only ever sees the
nonce-fenced wrap. At stage time the windows are reassembled here and the
result is what goes on the wire. The model no longer supplies document text at
all, so it can no longer drift, trim, or repair it.

SESSION SCOPING IS THE SECURITY BOUNDARY, NOT AN OPTIMIZATION. Each window
records the ``session_id`` its read happened in, and :func:`assemble` counts
only windows from the session doing the staging. Without that, one session's
read of a document would satisfy another session's stage — an establishment
turn could stage a document nobody in that conversation ever opened, and the
admin gate, the possession ceremony, and the whole attribution chain would be
reasoning about a document the turn never touched. Coverage is per session by
construction: a document read in session A is not stageable from session B.

THE COVERAGE WALK IS THE MECHANICAL FORM OF "PAGED TO THE END". The connector
returns a slice (``text[offset:offset+max_chars]`` plus ``total_chars`` and
``truncated``), so a model that reads one default window of a long letter holds
a first page. Assembly refuses anything that does not cover ``[0, total_chars)``
exactly, and names the ranges that are missing. A specification derived from the
first page of every letter is a specification about salutations.

RETENTION POSTURE. Process-local only. Never written to disk, never in an audit
row, never in a broker payload except as the ``text`` the model would otherwise
have typed by hand. Bounded four ways — a 30-minute TTL (exactly the broker's
staging TTL, so a capture cannot outlive the staging set it would feed), a
document count cap, a per-document byte cap, and a total byte cap — and dropped
outright by :func:`forget` the moment a stage succeeds. Same posture the broker
module states for the staging spool itself.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Exactly the broker's ``STAGING_TTL_SECONDS``. A capture cannot outlive the
#: staging set it would feed, so this also caps how stale staged bytes can be
#: relative to the source document.
CAPTURE_TTL_SECONDS = 1800

#: 2x the broker's 64-document set ceiling. Survey passes read two windows per
#: *candidate* and can exceed this — harmless, because partial windows never
#: satisfy the coverage walk and LRU keeps the most recent reads (the post-
#: blessing full reads that actually get staged).
MAX_DOCUMENTS = 128

#: Deliberately ABOVE the broker's 1 MiB per-document ceiling: an over-ceiling
#: document must still be captured far enough to earn a *named* size refusal at
#: stage time. Dropping its windows at capture time would surface as a coverage
#: gap, and the model would page forever against a document that can never be
#: staged.
MAX_DOC_BYTES = 2_097_152

#: 2x the broker's 16 MiB per-set ceiling — a maximal corpus fits with headroom
#: for survey noise.
MAX_TOTAL_BYTES = 33_554_432

#: Closed refusal vocabulary. Every value names a cause the caller can render
#: with a remedy; there is deliberately no "unknown" member, because a refusal
#: the model cannot act on is a refusal it will try to edit its way around.
REASON_NO_CAPTURE = "no_capture"
REASON_GAP = "gap"
REASON_SHORT = "short"
REASON_CHANGED = "changed"
REASON_CONFLICT = "conflict"
REASON_OVERSIZE = "oversize"
REASON_EMPTY = "empty"


@dataclass
class _Window:
    """One connector read: the slice, where it started, and who read it."""

    offset: int
    text: str
    total_chars: int
    session_id: str
    recorded_at: float


@dataclass
class _Capture:
    """Every window held for one document, across every session that read it."""

    name: str = ""
    #: Keyed ``(session_id, offset)``. Same session re-reading the same offset
    #: REPLACES (idempotent re-reads); a different session reading the same
    #: offset is a separate window, because coverage is counted per session.
    windows: dict[tuple[str, int], _Window] = field(default_factory=dict)
    #: Sessions for which a window was dropped because the document exceeded
    #: :data:`MAX_DOC_BYTES`. Per session, so an oversize read in one session
    #: does not turn another session's honest miss into a size refusal.
    oversize_sessions: set[str] = field(default_factory=set)
    bytes_held: int = 0
    touched_at: float = 0.0


@dataclass(frozen=True)
class AssemblyResult:
    """The verdict on one document's captured windows for one session."""

    ok: bool
    text: str = ""
    reason: str = ""
    #: Half-open ``[start, end)`` codepoint ranges the session has not read.
    missing: tuple[tuple[int, int], ...] = ()
    total_chars: int = 0
    covered_chars: int = 0
    #: The connector-reported document name — a fact about the document, held
    #: here so the caller can prefer it over the model's paraphrase.
    name: str = ""
    size_bytes: int = 0


_Key = tuple[str, str, str]

#: ``(connector, matter_id, document_id) -> _Capture``, module-global and
#: process-local. One customer Machine is one agent process, the same scope
#: ``shared.provenance`` and ``shared.matter_binding`` use for their registers.
_captures: OrderedDict[_Key, _Capture] = OrderedDict()


def _norm(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def make_key(connector: object, matter_id: object, document_id: object) -> _Key:
    """The capture key. Absent matter normalizes to ``""`` so a connector that
    does not scope documents by matter still keys consistently."""
    return (_norm(connector), _norm(matter_id), _norm(document_id))


def record(
    connector: object,
    matter_id: object,
    document_id: object,
    *,
    session_id: object,
    name: object,
    offset: object,
    text: object,
    total_chars: object,
) -> None:
    """Hold one connector read window.

    Total by contract and never raises: this runs inside ``post_tool_call`` on
    every document read, and a malformed result must cost one uncaptured window,
    never the turn. Same posture as ``provenance.record_read``.
    """
    try:
        if not isinstance(text, str) or not isinstance(total_chars, int):
            return
        if isinstance(total_chars, bool) or total_chars < 0:
            return
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return
        key = make_key(connector, matter_id, document_id)
        if not key[0] or not key[2]:
            return  # a window we could never look up again
        session = _norm(session_id)
        added = len(text.encode("utf-8"))

        capture = _captures.get(key)
        if capture is None:
            capture = _Capture()
            _captures[key] = capture
        else:
            _captures.move_to_end(key)
        if isinstance(name, str) and name.strip():
            capture.name = name.strip()
        capture.touched_at = time.monotonic()

        window_key = (session, offset)
        superseded = capture.windows.get(window_key)
        held = capture.bytes_held - (len(superseded.text.encode("utf-8")) if superseded else 0)
        if held + added > MAX_DOC_BYTES:
            # Drop the window but REMEMBER why. A capture whose windows were
            # silently dropped reads as an unfinished read, and the model's
            # remedy for an unfinished read is to read more — against a document
            # that can never fit. Marking it lets staging say "drop this one".
            capture.oversize_sessions.add(session)
            _evict()
            return
        capture.windows[window_key] = _Window(
            offset=offset,
            text=text,
            total_chars=total_chars,
            session_id=session,
            recorded_at=time.monotonic(),
        )
        capture.bytes_held = held + added
        _evict()
    except Exception:  # noqa: BLE001 — capture must never perturb the tool path
        logger.debug("read_capture: record failed", exc_info=True)


def assemble(
    connector: object,
    matter_id: object,
    document_id: object,
    *,
    session_id: object,
) -> AssemblyResult:
    """Rebuild one document from the windows ``session_id`` read of it.

    The refusal ORDER is load-bearing. ``oversize`` is decided before the
    coverage walk, because a document whose windows were dropped for size would
    otherwise report a gap — and a gap tells the model to read more, which it
    would do forever. A size refusal tells it to drop the document, which is the
    only true remedy.
    """
    key = make_key(connector, matter_id, document_id)
    session = _norm(session_id)
    capture = _captures.get(key)
    if capture is None:
        return AssemblyResult(ok=False, reason=REASON_NO_CAPTURE)
    _captures.move_to_end(key)
    _expire(capture)

    if session in capture.oversize_sessions:
        return AssemblyResult(
            ok=False,
            reason=REASON_OVERSIZE,
            name=capture.name,
            size_bytes=capture.bytes_held,
        )

    windows = sorted(
        (w for k, w in capture.windows.items() if k[0] == session),
        key=lambda w: w.offset,
    )
    if not windows:
        return AssemblyResult(ok=False, reason=REASON_NO_CAPTURE, name=capture.name)

    total = max(w.total_chars for w in windows)
    if total == 0:
        # The connector reached the document and got nothing out of it — an
        # image-only scan or an unsupported type. There is no read that fixes
        # this, so it must not be phrased as one.
        return AssemblyResult(ok=False, reason=REASON_EMPTY, name=capture.name, total_chars=0)
    if any(w.total_chars != total for w in windows):
        return AssemblyResult(ok=False, reason=REASON_CHANGED, name=capture.name, total_chars=total)

    parts: list[str] = []
    missing: list[tuple[int, int]] = []
    cursor = 0
    for window in windows:
        end = window.offset + len(window.text)
        if end <= cursor:
            continue  # fully covered by what we already hold
        if window.offset > cursor:
            # Keep walking rather than returning: the refusal must name EVERY
            # missing range, or the model pages once, retries, and is told about
            # the next gap one round trip at a time.
            missing.append((cursor, window.offset))
            parts.append(window.text)
        else:
            overlap = cursor - window.offset
            if overlap and window.text[:overlap] != _tail(parts, overlap):
                # Two reads of the same span disagree. Equal-length edits between
                # reads are invisible to total_chars; this is the only place they
                # surface.
                return AssemblyResult(
                    ok=False, reason=REASON_CONFLICT, name=capture.name, total_chars=total
                )
            parts.append(window.text[overlap:])
        cursor = end

    if cursor < total:
        missing.append((cursor, total))
    if missing:
        # One trailing gap is an unfinished read ("short"); anything else means
        # the middle is holed. Different remedies, so different words.
        reason = REASON_SHORT if missing == [(cursor, total)] else REASON_GAP
        return AssemblyResult(
            ok=False,
            reason=reason,
            missing=tuple(missing),
            name=capture.name,
            total_chars=total,
            covered_chars=cursor,
        )

    text = "".join(parts)
    if len(text) != total:
        # Belt and braces: a walk that covered every range must produce exactly
        # total_chars. A mismatch means the slicing is wrong, and staging bytes
        # we cannot account for is the failure this module exists to prevent.
        return AssemblyResult(
            ok=False, reason=REASON_CONFLICT, name=capture.name, total_chars=total
        )
    return AssemblyResult(
        ok=True,
        text=text,
        total_chars=total,
        covered_chars=total,
        name=capture.name,
        size_bytes=len(text.encode("utf-8")),
    )


def _tail(parts: list[str], n: int) -> str:
    """The last ``n`` characters of the assembled text so far.

    Not ``"".join(parts)[-n:]``: the walk can append a whole window after a gap,
    so assembled length and document cursor diverge, and slicing by document
    coordinate would compare the wrong span. The assembled TAIL, however, always
    ends exactly at the cursor — gap or no gap — so the overlap check reads
    backwards from the end.
    """
    out: list[str] = []
    need = n
    for chunk in reversed(parts):
        if need <= 0:
            break
        out.append(chunk[-need:] if len(chunk) >= need else chunk)
        need -= len(out[-1])
    return "".join(reversed(out))


def forget(connector: object, matter_id: object, document_id: object) -> None:
    """Drop a document's capture. Called after a successful stage, so retention
    ends at the moment the bytes are no longer needed — and so a duplicate stage
    of the same document refuses rather than silently staging it twice under two
    broker doc ids."""
    _captures.pop(make_key(connector, matter_id, document_id), None)


def _expire(capture: _Capture) -> None:
    now = time.monotonic()
    stale = [k for k, w in capture.windows.items() if now - w.recorded_at > CAPTURE_TTL_SECONDS]
    for key in stale:
        window = capture.windows.pop(key)
        capture.bytes_held -= len(window.text.encode("utf-8"))
    if capture.bytes_held < 0:
        capture.bytes_held = 0


def _evict() -> None:
    """Drop expired captures, then LRU until both caps hold."""
    now = time.monotonic()
    for key in [k for k, c in _captures.items() if now - c.touched_at > CAPTURE_TTL_SECONDS]:
        _captures.pop(key, None)
    while len(_captures) > MAX_DOCUMENTS:
        evicted, _ = _captures.popitem(last=False)
        logger.debug("read_capture: evicted %s (document cap %d)", evicted, MAX_DOCUMENTS)
    while sum(c.bytes_held for c in _captures.values()) > MAX_TOTAL_BYTES and _captures:
        evicted, _ = _captures.popitem(last=False)
        logger.debug("read_capture: evicted %s (total byte cap)", evicted)


def _reset_for_tests() -> None:
    _captures.clear()


__all__ = [
    "AssemblyResult",
    "CAPTURE_TTL_SECONDS",
    "MAX_DOCUMENTS",
    "MAX_DOC_BYTES",
    "MAX_TOTAL_BYTES",
    "REASON_CHANGED",
    "REASON_CONFLICT",
    "REASON_EMPTY",
    "REASON_GAP",
    "REASON_NO_CAPTURE",
    "REASON_OVERSIZE",
    "REASON_SHORT",
    "assemble",
    "forget",
    "make_key",
    "record",
]
