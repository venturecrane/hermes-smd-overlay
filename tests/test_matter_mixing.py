"""The MIXING signal (ss#2167) — session read-set provenance, not membership.

``matter_gate.evaluate`` answers "is this recipient a party to the matter this
letter CITES", and is silent by construction when the body cites nothing. That
leaves the likelier failure uncovered: content lifted from a second matter and
never named. This module covers the signal that fills it.

Two properties matter more than any individual assertion here:

* **Every positive is paired with a negative.** A detector that fired on every
  send would satisfy the positives alone and would have measured nothing.
* **Phase 1 cannot withhold.** ``test_multi_matter_never_blocks_an_allowed_send``
  is the falsifier for the whole design: it fails the moment someone wires
  enforcement in without moving to Phase 2, which is exactly the drift this
  observe-only phase is meant to survive.
"""

from __future__ import annotations

import pytest

from shared import matter_binding, matter_gate

SID = "s-mix"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
M_B = "bbbbbbbb-1111-2222-3333-444444444444"

MEMOS = "mcp_smokeball_get_memos_on_matter"
DOC = "mcp_smokeball_read_document"
GET_MATTER = "mcp_smokeball_get_matter"
LIST_MATTERS = "mcp_smokeball_list_matters"


@pytest.fixture(autouse=True)
def _clean():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


def _read(tool: str, matter_id: str, result="{}", session: str = SID) -> None:
    matter_binding.record_from_read(session, result, tool_name=tool, args={"matter_id": matter_id})


# ---- the signal -------------------------------------------------------------


def test_two_memo_reads_flag() -> None:
    _read(MEMOS, M_A)
    _read(MEMOS, M_B)
    assert matter_gate.multi_matter_session(SID) == (M_A, M_B)


def test_one_memo_read_does_not_flag() -> None:
    """The control. Without it the test above proves only that the function
    returns a non-empty tuple, not that it discriminates."""
    _read(MEMOS, M_A)
    assert matter_gate.multi_matter_session(SID) == ()


def test_memo_on_one_matter_plus_document_on_another_flags() -> None:
    """The blend the first draft of this work would have missed entirely.

    ``read_document`` taints the session, and that was originally taken as
    reason enough to leave it out. Taint only forces a send to OUTSIDE, and on a
    seat already posturing ``draft_for_review`` every send is human-fronted
    anyway — so taint does not subsume this signal, and the highest-value blend
    (one matter's memo, another's document text) would have raised nothing."""
    _read(MEMOS, M_A)
    _read(DOC, M_B, result="SECOND AMENDED NOTICE OF SERVICE ...")
    assert matter_gate.multi_matter_session(SID) == (M_A, M_B)


def test_document_read_is_captured_though_its_result_is_not_json() -> None:
    """``read_document`` returns extracted TEXT, so ``_as_payload`` yields None.

    Capture therefore has to run BEFORE the payload early-return. If it does not,
    every document read is invisible to this signal and the test above passes for
    the wrong reason (on the memo read alone)."""
    _read(DOC, M_A, result="plain text, no JSON envelope at all")
    _read(DOC, M_B, result="also plain text")
    assert matter_gate.multi_matter_session(SID) == (M_A, M_B)


# ---- what must NOT flag -----------------------------------------------------


def test_get_matter_is_not_a_content_read() -> None:
    """``get_matter`` appears in seven of the eight law-firm wedge skills, and
    ``new-matter-intake`` reads several matters BY DESIGN for the conflict
    cross-check. Treating it as content would fire this detector on the firm's
    own conflict detection every time, and a flag that fires on routine work
    trains a reviewer to ignore it."""
    _read(GET_MATTER, M_A)
    _read(GET_MATTER, M_B)
    assert matter_gate.multi_matter_session(SID) == ()


def test_contact_keyed_listing_does_not_flag() -> None:
    """A contact-filtered ``list_matters`` adds every matter one person is on, so
    ``_by_matter`` can hold thirty entries after a single reply turn. That is why
    the content set is separate from it rather than derived from it."""
    # The listing names the contact by id, so the binding needs the address a
    # prior contact read supplied — without it Direction 2 captures nothing and
    # this test would pass for the wrong reason (an empty _by_matter).
    matter_binding.record_contact(SID, "c-1", "okafor@example.com")
    payload = {
        "matters_for_contact": "c-1",
        "matters_for_contact_complete": True,
        "value": [{"id": M_A, "number": "2026-PI-101"}, {"id": M_B, "number": "2026-PI-102"}],
    }
    matter_binding.record_from_read(SID, payload, tool_name=LIST_MATTERS, args={})
    assert len(matter_binding.membership_for(SID).known_matters()) >= 2
    assert matter_gate.multi_matter_session(SID) == ()


def test_falsy_session_never_flags() -> None:
    """``resolve_session`` returns "" under MODE_AMBIGUOUS / MODE_NONE and every
    unkeyed context shares that one bucket. For the party sets a pooled bucket was
    harmless — it only ever ADDS parties, pushing verdicts toward *allow*. This
    consumer inverts the direction, so pooling would manufacture a mixing signal
    out of two innocent sessions."""
    _read(MEMOS, M_A, session="")
    _read(MEMOS, M_B, session="")
    assert matter_gate.multi_matter_session("") == ()
    assert matter_binding.membership_for("").content_read_matters() == set()


def test_sessions_do_not_bleed_into_each_other() -> None:
    _read(MEMOS, M_A, session="s-one")
    _read(MEMOS, M_B, session="s-two")
    assert matter_gate.multi_matter_session("s-one") == ()
    assert matter_gate.multi_matter_session("s-two") == ()


def test_evicted_session_yields_no_verdict() -> None:
    _read(MEMOS, M_A)
    _read(MEMOS, M_B)
    matter_binding.drop(SID)
    assert matter_gate.multi_matter_session(SID) == ()


# ---- the lever --------------------------------------------------------------


def test_mode_defaults_to_block(monkeypatch) -> None:
    monkeypatch.delenv("SMD_MULTI_MATTER_MODE", raising=False)
    assert matter_gate.multi_matter_mode() == "block"


def test_mode_off_silences_the_signal(monkeypatch) -> None:
    monkeypatch.setenv("SMD_MULTI_MATTER_MODE", "off")
    _read(MEMOS, M_A)
    _read(MEMOS, M_B)
    assert matter_gate.multi_matter_session(SID) == ()


def test_unrecognized_mode_keeps_the_signal_on(monkeypatch) -> None:
    """Same fail-closed SHAPE as SMD_MATTER_GATE_MODE: an operator typo must not
    silently disable it."""
    monkeypatch.setenv("SMD_MULTI_MATTER_MODE", "repot")
    _read(MEMOS, M_A)
    _read(MEMOS, M_B)
    assert matter_gate.multi_matter_session(SID) != ()


def test_report_mode_annotates_without_fencing(monkeypatch) -> None:
    """``report`` keeps the send-time annotation but opens the read fence — the
    escape hatch for a seat that wants visibility before enforcement."""
    monkeypatch.setenv("SMD_MULTI_MATTER_MODE", "report")
    _read(MEMOS, M_A)
    assert matter_gate.content_read_refusal(SID, MEMOS, {"matter_id": M_B}) is None
    _read(MEMOS, M_B)
    assert matter_gate.multi_matter_session(SID) != ()


# ---- the fence: what stops a mixed draft being COMPOSED ---------------------


def test_second_matters_memo_read_is_refused() -> None:
    """The kill-test. A session holding matter A's substance may not read B's."""
    _read(MEMOS, M_A)
    refusal = matter_gate.content_read_refusal(SID, MEMOS, {"matter_id": M_B})
    assert refusal is not None and M_B in refusal


def test_same_matter_read_again_is_allowed() -> None:
    """The control. A fence that refused every content read would satisfy the
    kill-test above and have measured nothing — and would also make the Operator
    useless, since reading one matter twice is ordinary work."""
    _read(MEMOS, M_A)
    assert matter_gate.content_read_refusal(SID, MEMOS, {"matter_id": M_A}) is None


def test_first_content_read_is_always_allowed() -> None:
    assert matter_gate.content_read_refusal(SID, MEMOS, {"matter_id": M_A}) is None


def test_document_read_on_a_second_matter_is_refused() -> None:
    _read(MEMOS, M_A)
    assert matter_gate.content_read_refusal(SID, DOC, {"matter_id": M_B}) is not None


def test_metadata_reads_are_never_fenced() -> None:
    """A status digest or stalled-matter sweep reads metadata across many matters
    and must be untouched. This is what makes a hard refusal affordable."""
    _read(MEMOS, M_A)
    for tool in (GET_MATTER, LIST_MATTERS, "mcp_smokeball_list_tasks"):
        assert matter_gate.content_read_refusal(SID, tool, {"matter_id": M_B}) is None


def test_fence_opens_when_mode_is_off(monkeypatch) -> None:
    monkeypatch.setenv("SMD_MULTI_MATTER_MODE", "off")
    _read(MEMOS, M_A)
    assert matter_gate.content_read_refusal(SID, MEMOS, {"matter_id": M_B}) is None


def test_unresolvable_session_does_not_fence() -> None:
    """Fail-open, and a known hole rather than a design choice: with no session
    key there is no read-set to compare against, and refusing every content read
    on an unkeyed session would brick ordinary work."""
    assert matter_gate.content_read_refusal("", MEMOS, {"matter_id": M_B}) is None


def test_missing_matter_id_does_not_fence() -> None:
    _read(MEMOS, M_A)
    assert matter_gate.content_read_refusal(SID, MEMOS, {}) is None
