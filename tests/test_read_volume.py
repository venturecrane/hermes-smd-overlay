"""Unit coverage for the read-volume accumulator (shared/read_volume.py).

The gate's placement in ``evaluate_tool_call`` is pinned separately by
``tests/test_read_volume_gate_placement.py``; this file covers the register
itself: envelope parsing, distinct-document counting, the char fallback, the
unmeasured posture, the unbound route handoff, and mode parsing.
"""

from __future__ import annotations

import json

import pytest

from shared import read_volume

SID = "s-rv-unit"


@pytest.fixture(autouse=True)
def _clean():
    read_volume.reset()
    yield
    read_volume.reset()


def _read_result(file_id: str, *, pages=None, total_chars=None, name="doc.pdf"):
    payload = {"fileId": file_id, "name": name, "text": "x"}
    if pages is not None:
        payload["pageCount"] = pages
    if total_chars is not None:
        payload["total_chars"] = total_chars
    return payload


def _mark(session_id: str = SID) -> None:
    read_volume.record_route(session_id, read_volume.GATED_SKILL)


def _total(session_id: str = SID) -> int:
    state = read_volume._sessions.get(session_id)
    return sum(state.pages_by_file.values()) if state else 0


def test_pdf_pages_accumulate_and_windows_count_once() -> None:
    _mark()
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, _read_result("f1", pages=40))
    # a second window of the same document re-reports the whole doc's pageCount
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, _read_result("f1", pages=40))
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, _read_result("f2", pages=7))
    assert _total() == 47


def test_string_result_parses() -> None:
    _mark()
    raw = json.dumps(_read_result("f1", pages=12))
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, raw)
    assert _total() == 12


def test_docx_falls_back_to_char_estimate() -> None:
    _mark()
    read_volume.note_read(
        SID, read_volume.COUNTED_TOOL, {}, _read_result("d1", total_chars=7500, name="resp.docx")
    )
    assert _total() == 3  # ceil(7500 / 3000)


def test_mixed_pdf_and_docx_session() -> None:
    _mark()
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, _read_result("p1", pages=30))
    read_volume.note_read(
        SID, read_volume.COUNTED_TOOL, {}, _read_result("d1", total_chars=3001, name="r.docx")
    )
    assert _total() == 32


def test_no_volume_signal_counts_zero_and_is_tracked() -> None:
    _mark()
    read_volume.note_read(SID, read_volume.COUNTED_TOOL, {}, _read_result("u1"))
    state = read_volume._sessions[SID]
    assert _total() == 0
    assert "u1" in state.unmeasured


def test_skill_md_read_marks_the_session() -> None:
    path = f"/app/skills/{read_volume.GATED_SKILL}/SKILL.md"
    read_volume.note_read(SID, "read_file", {"path": path}, "irrelevant")
    assert read_volume._sessions[SID].review is True


def test_skill_view_of_gated_skill_marks_the_session() -> None:
    # The path the live 2026-08-28 rehearsal actually took: the gateway-native
    # skill tool, not read_file. A marker watching only read_file was inert.
    read_volume.note_read(SID, "skill_view", {"name": read_volume.GATED_SKILL}, "")
    assert read_volume._sessions[SID].review is True


def test_skill_view_of_other_skill_does_not_mark() -> None:
    read_volume.note_read(SID, "skill_view", {"name": "matter-inbox-router"}, "")
    assert SID not in read_volume._sessions or not read_volume._sessions[SID].review


def test_other_skill_md_read_does_not_mark() -> None:
    read_volume.note_read(
        SID, "read_file", {"path": "/app/skills/matter-inbox-router/SKILL.md"}, ""
    )
    assert SID not in read_volume._sessions or not read_volume._sessions[SID].review


def test_non_gated_route_is_ignored() -> None:
    read_volume.record_route(SID, "matter-inbox-router")
    assert SID not in read_volume._sessions


def test_unbound_route_marks_every_fresh_claimant() -> None:
    # Dispatch-time session id empty; TWO routes pending; both claimant
    # sessions get marked (the deliberate inversion of claim_unbound).
    read_volume.record_route("", read_volume.GATED_SKILL)
    read_volume.record_route("", read_volume.GATED_SKILL)
    read_volume.claim_unbound_routes("s-a")
    read_volume.claim_unbound_routes("s-b")
    assert read_volume._sessions["s-a"].review is True
    assert read_volume._sessions["s-b"].review is True


def test_expired_route_is_dropped_and_never_marks(caplog) -> None:
    import time

    read_volume.record_route("", read_volume.GATED_SKILL)
    with caplog.at_level("WARNING"):
        read_volume.claim_unbound_routes(
            "s-late", now=time.monotonic() + read_volume._UNBOUND_TTL_SECONDS + 1
        )
    assert "expired unclaimed" in caplog.text
    assert "s-late" not in read_volume._sessions or not read_volume._sessions["s-late"].review


def test_lru_bound_holds() -> None:
    for i in range(read_volume._MAX_SESSIONS + 10):
        read_volume.record_route(f"s-{i}", read_volume.GATED_SKILL)
    assert len(read_volume._sessions) == read_volume._MAX_SESSIONS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "block"),
        ("", "block"),
        ("garbage", "block"),
        ("report", "report"),
        ("off", "off"),
        ("BLOCK", "block"),
    ],
)
def test_mode_parsing(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("SMD_READ_VOLUME_GATE_MODE", raising=False)
    else:
        monkeypatch.setenv("SMD_READ_VOLUME_GATE_MODE", raw)
    assert read_volume.mode() == expected
