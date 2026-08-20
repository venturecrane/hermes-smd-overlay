"""The matter-mixing fence survives the Hermes v0.19 tool rename (ss#2167 x ss#2444).

Why this file exists SEPARATELY from ``tests/test_mcp_tool_names.py``: that file
proves the canonicalizer, and drives the wire form through ``matter_binding``.
Neither proves the thing that actually matters for this control — that the FENCE
refuses a second matter when the read arrives under the v0.19+ wire spelling.

The failure it guards is the nastiest shape there is. ``_CONTENT_READ_TOOLS`` is
keyed on the legacy single-underscore names. On a v0.20 seat without the boundary
translation, ``is_content_read("mcp__smokeball__read_document")`` is False, so the
fence never fires — SILENTLY, on exactly the reads it exists to catch. It does not
refuse, it does not log a refusal, it simply is not there. A green unit suite over
the canonical spelling would have reported the control healthy the entire time.
"""

from __future__ import annotations

import pytest

from shared import matter_binding, matter_gate
from shared.mcp_tool_names import canonical_tool_name

SID = "s-wire"
M_A = "aaaaaaaa-1111-2222-3333-444444444444"
M_B = "bbbbbbbb-1111-2222-3333-444444444444"

WIRE_MEMOS = "mcp__smokeball__get_memos_on_matter"
WIRE_DOC = "mcp__smokeball__read_document"
WIRE_GET_MATTER = "mcp__smokeball__get_matter"


@pytest.fixture(autouse=True)
def _clean():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


def _read_at_the_boundary(tool_name: str, matter_id: str) -> None:
    """Capture as the ``post_tool_call`` hook sees it — i.e. AFTER the fan-out has
    canonicalized the wire name, which is the only reason this works."""
    matter_binding.record_from_read(
        SID,
        "{}",
        tool_name=canonical_tool_name(tool_name),
        args={"matter_id": matter_id},
    )


def _fence_at_the_boundary(tool_name: str, matter_id: str):
    return matter_gate.content_read_refusal(
        SID, canonical_tool_name(tool_name), {"matter_id": matter_id}
    )


def test_wire_named_memo_read_is_captured() -> None:
    """If this fails, the fence has nothing to compare against and would never
    refuse anything — the silent-inert case."""
    _read_at_the_boundary(WIRE_MEMOS, M_A)
    assert M_A in matter_binding.membership_for(SID).content_read_matters()


def test_wire_named_second_matter_is_refused() -> None:
    _read_at_the_boundary(WIRE_MEMOS, M_A)
    assert _fence_at_the_boundary(WIRE_MEMOS, M_B) is not None


def test_wire_named_document_read_on_a_second_matter_is_refused() -> None:
    _read_at_the_boundary(WIRE_MEMOS, M_A)
    assert _fence_at_the_boundary(WIRE_DOC, M_B) is not None


def test_wire_named_same_matter_is_allowed() -> None:
    """The control. Without it a fence that refused everything would pass above."""
    _read_at_the_boundary(WIRE_MEMOS, M_A)
    assert _fence_at_the_boundary(WIRE_MEMOS, M_A) is None


def test_wire_named_metadata_read_is_never_fenced() -> None:
    _read_at_the_boundary(WIRE_MEMOS, M_A)
    assert _fence_at_the_boundary(WIRE_GET_MATTER, M_B) is None


def test_the_raw_wire_name_is_unmapped_without_translation() -> None:
    """THE FALSIFIER for this whole file.

    Every test above routes through ``canonical_tool_name`` because the fan-out
    does. This one asserts the UNtranslated wire name does not match the
    content-read set — proving the tests above pass because the translation
    works, not because the set happens to accept both spellings on its own.

    If someone later adds the wire names directly to ``_CONTENT_READ_TOOLS``,
    this fails and says so: the fix belongs at the boundary, in ONE place, not
    duplicated into every policy table."""
    assert not matter_binding.is_content_read(WIRE_MEMOS)
    assert matter_binding.is_content_read(canonical_tool_name(WIRE_MEMOS))
