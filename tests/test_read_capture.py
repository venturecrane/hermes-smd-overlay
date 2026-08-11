"""Tests for shared/read_capture.py — the held-reads store behind ss#2247.

Every test names the concrete regression it catches, because this module exists
to make one property mechanical: **the bytes staged are the bytes the connector
returned, whole**. The defects it guards against are the ones that produce a
plausible-looking document — a scrambled reassembly with the right length, a
first page staged as a whole letter, a Frankenstein of two reads that passes
every length check. None of those announce themselves downstream; the broker
would hash them happily and the compilers would derive a firm voice from them.

Run::

    pytest tests/test_read_capture.py -q
"""

from __future__ import annotations

import pytest

from shared import read_capture

_CONNECTOR = "smokeball"
_MATTER = "m-1"
_DOC = "f-1"
_SESSION = "sess-1"


@pytest.fixture(autouse=True)
def _clean_store():
    read_capture._reset_for_tests()
    yield
    read_capture._reset_for_tests()


def _record(text, offset, total=None, *, session=_SESSION, doc=_DOC, name="letter-01.pdf"):
    read_capture.record(
        _CONNECTOR,
        _MATTER,
        doc,
        session_id=session,
        name=name,
        offset=offset,
        text=text,
        total_chars=len(text) + offset if total is None else total,
    )


def _assemble(*, session=_SESSION, doc=_DOC):
    return read_capture.assemble(_CONNECTOR, _MATTER, doc, session_id=session)


# ---------------------------------------------------------------------------
# Coverage — the mechanical form of "paged to the end"
# ---------------------------------------------------------------------------


def test_single_window_covering_the_whole_document_assembles():
    """Falsifier: assembly demands more than one window, or is off by one at
    ``cursor == total_chars`` — every single-page document then refuses."""
    _record("Dear Ms. Reyes,", 0, 15)
    result = _assemble()
    assert result.ok and result.text == "Dear Ms. Reyes,"
    assert result.total_chars == 15 and result.covered_chars == 15


def test_exact_boundary_window_is_complete():
    """Falsifier: a ``<`` / ``<=`` slip on the tail check. Same shape as above,
    asserted at the boundary explicitly so the off-by-one cannot hide."""
    _record("A" * 40_000, 0, 80_000)
    assert not _assemble().ok
    _record("B" * 40_000, 40_000, 80_000)
    assert _assemble().ok


def test_multi_window_assembles_in_offset_order():
    """Falsifier: windows concatenated in INSERTION order. A model that pages
    0, 40000, 20000 then stages scrambled text with a correct length and a
    wrong sha — the exact defect no length check can see."""
    _record("aaa", 0, 9)
    _record("ccc", 6, 9)
    _record("bbb", 3, 9)
    assert _assemble().text == "aaabbbccc"


def test_out_of_order_windows_assemble_identically_to_in_order():
    """Falsifier: a sort that is stable-but-wrong. Asserted as byte equality
    against the in-order assembly rather than against a literal."""
    _record("aaa", 0, 9)
    _record("bbb", 3, 9)
    _record("ccc", 6, 9)
    in_order = _assemble().text
    read_capture._reset_for_tests()
    _record("ccc", 6, 9)
    _record("aaa", 0, 9)
    _record("bbb", 3, 9)
    assert _assemble().text == in_order


def test_short_read_refuses_naming_the_tail():
    """Falsifier: ``truncated`` ignored. This is the "specification about
    salutations" defect — the first page of every letter staged as the letter,
    and a firm voice derived from greetings."""
    _record("A" * 40_000, 0, 60_000)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_SHORT
    assert result.missing == ((40_000, 60_000),)
    assert result.covered_chars == 40_000


def test_gap_refuses_and_names_every_missing_range():
    """Falsifier: the walk returns on the FIRST gap. A two-gap document reports
    one, the model pages once, retries, is told about the next — forever."""
    _record("aaa", 0, 15)
    _record("ccc", 6, 15)
    _record("eee", 12, 15)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_GAP
    assert result.missing == ((3, 6), (9, 12))


# ---------------------------------------------------------------------------
# Duplicates, overlaps, and documents that move underneath the reads
# ---------------------------------------------------------------------------


def test_duplicate_identical_window_is_idempotent():
    """Falsifier: a re-read at the same offset APPENDED instead of replacing —
    doubled text, length mismatch, and a spurious refusal on an honest re-read."""
    _record("Dear Ms. Reyes,", 0, 15)
    _record("Dear Ms. Reyes,", 0, 15)
    result = _assemble()
    assert result.ok and result.text == "Dear Ms. Reyes,"


def test_overlapping_windows_are_sliced_not_doubled():
    """Falsifier: an overlap concatenated whole — assembled text longer than
    ``total_chars``, which the final length check must then catch."""
    _record("abcdef", 0, 9)
    _record("defghi", 3, 9)
    result = _assemble()
    assert result.ok and result.text == "abcdefghi"


def test_overlapping_windows_that_disagree_refuse_conflict():
    """Falsifier: a document edited between two EQUAL-LENGTH reads assembles a
    Frankenstein that passes every length check. ``total_chars`` cannot see this
    one; the overlap comparison is the only place it surfaces."""
    _record("abcdef", 0, 9)
    _record("XYZghi", 3, 9)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_CONFLICT


def test_overlap_check_is_correct_after_a_gap():
    """Falsifier: the overlap comparison slices the assembled text by DOCUMENT
    coordinate. After a gap the assembled length and the cursor diverge, so that
    slice reads the wrong span — and a disagreement is then either missed or
    invented. Here the overlap genuinely agrees and must not be flagged."""
    _record("aaa", 0, 12)
    _record("ccc", 6, 12)
    _record("ccddd", 7, 12)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_GAP
    assert result.missing == ((3, 6),)


def test_total_chars_change_between_windows_refuses_changed():
    """Falsifier: a document that GREW mid-read assembles a prefix and reports
    success — a truncated document staged as whole, with nothing to notice it."""
    _record("aaa", 0, 6)
    _record("bbb", 3, 9)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_CHANGED


# ---------------------------------------------------------------------------
# The two refusals that must NOT read as "read it again"
# ---------------------------------------------------------------------------


def test_empty_extraction_reports_empty_not_a_gap():
    """Falsifier (critique issue 5): an image-only scan or unsupported type
    yields no text, and a coverage-shaped refusal tells the model to read more.
    There is no read that fixes it, so the run never recovers. ``empty`` is the
    reason that lets staging say "drop it and name it in your report"."""
    _record("", 0, 0)
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_EMPTY


def test_oversize_is_decided_before_the_coverage_walk(monkeypatch):
    """Falsifier (critique issue 6): an over-ceiling document whose windows were
    dropped for size reports a GAP, so the model pages forever against a document
    that can never be staged. Ordering is the whole fix — oversize first."""
    monkeypatch.setattr(read_capture, "MAX_DOC_BYTES", 32)
    _record("A" * 16, 0, 64)
    _record("B" * 48, 16, 64)  # dropped: would exceed the per-document ceiling
    result = _assemble()
    assert not result.ok and result.reason == read_capture.REASON_OVERSIZE


def test_oversize_in_one_session_does_not_mask_another_sessions_miss(monkeypatch):
    """Falsifier: the oversize mark held per DOCUMENT rather than per session
    turns an unrelated session's honest "I have not read this" into a size
    refusal, telling it to drop a document it could have staged."""
    monkeypatch.setattr(read_capture, "MAX_DOC_BYTES", 32)
    _record("A" * 48, 0, 64, session="sess-big")
    assert _assemble(session="sess-big").reason == read_capture.REASON_OVERSIZE
    assert _assemble(session="sess-other").reason == read_capture.REASON_NO_CAPTURE


# ---------------------------------------------------------------------------
# Session scoping — the security boundary (critique issue 7)
# ---------------------------------------------------------------------------


def test_windows_from_another_session_do_not_satisfy_coverage():
    """THE critique-7 falsifier. Without per-session windows, one session's read
    satisfies another session's stage: an establishment turn could stage a
    document nobody in that conversation ever opened, while the admin gate, the
    possession ceremony, and the attribution chain all reason about a document
    the turn never touched."""
    _record("Dear Ms. Reyes,", 0, 15, session="sess-A")
    assert _assemble(session="sess-A").ok
    other = _assemble(session="sess-B")
    assert not other.ok and other.reason == read_capture.REASON_NO_CAPTURE


def test_same_offset_in_two_sessions_does_not_clobber():
    """Falsifier: windows keyed by offset alone, so session B's read of page one
    REPLACES session A's — and A, which had read the whole document, is suddenly
    told it has a gap it cannot explain."""
    _record("aaa", 0, 6, session="sess-A")
    _record("bbb", 3, 6, session="sess-A")
    _record("zzz", 0, 6, session="sess-B")
    assert _assemble(session="sess-A").text == "aaabbb"


def test_unresolvable_session_finds_nothing():
    """Falsifier: an empty session id matching every window (a falsy-comparison
    slip), which would restore exactly the cross-session leak above whenever the
    resolver misses."""
    _record("Dear Ms. Reyes,", 0, 15, session="sess-A")
    assert _assemble(session="").reason == read_capture.REASON_NO_CAPTURE


# ---------------------------------------------------------------------------
# Bounds and lifecycle
# ---------------------------------------------------------------------------


def test_ttl_expiry_drops_windows_and_reports_no_capture(monkeypatch):
    """Falsifier: no TTL — a four-hour-old read stages stale bytes while the
    model believes it staged what it just read."""
    _record("Dear Ms. Reyes,", 0, 15)
    assert _assemble().ok
    monkeypatch.setattr(read_capture, "CAPTURE_TTL_SECONDS", -1)
    assert _assemble().reason == read_capture.REASON_NO_CAPTURE


def test_lru_eviction_at_max_documents(monkeypatch):
    """Falsifier: an unbounded dict — a survey pass over hundreds of candidates
    grows the gateway process without limit."""
    monkeypatch.setattr(read_capture, "MAX_DOCUMENTS", 3)
    for i in range(5):
        _record("x", 0, 1, doc=f"f-{i}")
    assert len(read_capture._captures) == 3
    assert _assemble(doc="f-0").reason == read_capture.REASON_NO_CAPTURE
    assert _assemble(doc="f-4").ok


def test_byte_cap_evicts_until_under_total(monkeypatch):
    """Falsifier: a document-count bound alone lets 128 large documents blow
    memory on a seat sized for one agent."""
    monkeypatch.setattr(read_capture, "MAX_TOTAL_BYTES", 100)
    for i in range(6):
        _record("x" * 40, 0, 40, doc=f"f-{i}")
    assert sum(c.bytes_held for c in read_capture._captures.values()) <= 100


def test_record_is_total_on_malformed_input():
    """Falsifier: a ``None`` text or a string ``total_chars`` raises inside
    ``post_tool_call`` and kills capture for the rest of the turn."""
    for bad in ({"text": None}, {"total_chars": "40"}, {"offset": -1}, {"offset": None}):
        kwargs = {"text": "abc", "offset": 0, "total_chars": 3, **bad}
        read_capture.record(_CONNECTOR, _MATTER, _DOC, session_id=_SESSION, name="n", **kwargs)
    assert _assemble().reason == read_capture.REASON_NO_CAPTURE


def test_record_without_a_document_id_is_dropped():
    """Falsifier: a window stored under an empty key can never be looked up
    again, so it is pure retention with no benefit."""
    _record("abc", 0, 3, doc="")
    assert not read_capture._captures


def test_forget_removes_the_capture():
    """Falsifier: the capture survives staging, so a duplicate
    ``establish_stage_document`` silently stages the same document twice under
    two broker doc ids — and the corpus manifest counts it twice."""
    _record("Dear Ms. Reyes,", 0, 15)
    assert _assemble().ok
    read_capture.forget(_CONNECTOR, _MATTER, _DOC)
    assert _assemble().reason == read_capture.REASON_NO_CAPTURE


def test_connector_reported_name_is_held():
    """Falsifier: the name dropped, so staging falls back to the model's
    paraphrase and a demotion report points at a document the admin cannot find."""
    _record("Dear Ms. Reyes,", 0, 15, name="2026-04-02 Reyes demand.pdf")
    assert _assemble().name == "2026-04-02 Reyes demand.pdf"


def test_key_normalization_is_case_and_space_insensitive():
    """Falsifier: a capture recorded from a result echoing ``M-1`` missed by a
    stage naming ``m-1``, reported as ``no_capture`` on a document that WAS read."""
    read_capture.record(
        "Smokeball",
        " M-1 ",
        "F-1",
        session_id=_SESSION,
        name="n",
        offset=0,
        text="abc",
        total_chars=3,
    )
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id=_SESSION).ok
