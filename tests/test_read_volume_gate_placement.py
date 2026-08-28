"""Where the read-volume gate sits in ``evaluate_tool_call`` (agreement §2.8).

Pins the same class of placement defect ``test_matter_gate_placement.py`` pins
for the matter-identity gate: the fence must run for READ-class calls, before
exposure resolution, and its falsifiers must fire BOTH WAYS — an over-threshold
review refuses, and every non-applicable shape (under threshold, unauthored,
non-review session, report mode, gate off) proceeds. A gate whose refusal test
passes while its allow tests were never written is a gate nobody proved can
stay open.
"""

from __future__ import annotations

import pytest

from shared import read_volume
from tests.conftest import load_plugin

trust = load_plugin("hermes-smd-trust")
enforce = trust.enforce

SID = "s-rv-placement"
READ = "mcp_smokeball_read_document"

CUSTOMER_YAML = f"""
schema_version: "1"
customer_id: testco
personas:
  - slug: marcus
    entitlements:
      exposure:
        internal_write: autonomous
    skills:
      - name: {read_volume.GATED_SKILL}
        version: pending
        initiation:
          manual: true
          scheduled: false
          webhook: true
        enabled: true
        settings:
          review_threshold_pages: 50
"""

UNAUTHORED_YAML = CUSTOMER_YAML.split("        settings:")[0]


@pytest.fixture()
def seat(tmp_path, monkeypatch):
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(CUSTOMER_YAML)
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
    monkeypatch.setenv("SMD_EXPOSURE_OVERRIDE_DB_PATH", str(tmp_path / "ovr.db"))
    monkeypatch.setenv("SMD_CUSTOMER_SLUG", "testco")
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.delenv("SMD_READ_VOLUME_GATE_MODE", raising=False)
    read_volume.reset()
    yield yaml_path
    read_volume.reset()


@pytest.fixture()
def recorded(monkeypatch):
    calls: list[dict] = []
    original = enforce._record_decision

    def spy(tool_call_id, tool_name, persona_slug, **kw):
        calls.append(kw)
        return original(tool_call_id, tool_name, persona_slug, **kw)

    monkeypatch.setattr(enforce, "_record_decision", spy)
    return calls


def _mark_and_fill(pages: int, session_id: str = SID) -> None:
    read_volume.record_route(session_id, read_volume.GATED_SKILL)
    read_volume.note_read(
        session_id, READ, {}, {"fileId": "seed", "name": "set.pdf", "pageCount": pages}
    )


def _read(session_id: str = SID):
    return enforce.evaluate_tool_call(
        READ,
        {"matterId": "m-1", "fileId": "next"},
        "testco",
        session_id=session_id,
        tool_call_id="tc-rv",
    )


def _gate_rows(recorded):
    return [c for c in recorded if "READ_VOLUME_GATE" in (c.get("reason") or "")]


def test_over_threshold_review_read_is_refused(seat, recorded) -> None:
    _mark_and_fill(50)
    result = _read()
    assert result is not None and result.get("action") == "block"
    assert "at least 50 pages" in result["message"]
    assert "review threshold of 50 pages" in result["message"]
    rows = _gate_rows(recorded)
    assert rows and rows[-1]["audit_action"] == "refuse" and rows[-1]["allowed"] is False


def test_under_threshold_review_read_proceeds(seat, recorded) -> None:
    _mark_and_fill(49)
    result = _read()
    assert result is None or result.get("action") != "block"
    assert not _gate_rows(recorded)


def test_non_review_session_is_never_fenced(seat, recorded) -> None:
    # Same volume, but the session was never marked as a review.
    read_volume.note_read(SID, READ, {}, {"fileId": "seed", "pageCount": 500})
    result = _read()
    assert result is None or result.get("action") != "block"
    assert not _gate_rows(recorded)


def test_unauthored_threshold_is_inert(seat, recorded) -> None:
    seat.write_text(UNAUTHORED_YAML)
    _mark_and_fill(500)
    result = _read()
    assert result is None or result.get("action") != "block"
    assert not _gate_rows(recorded)


def test_report_mode_records_once_and_never_blocks(seat, recorded, monkeypatch) -> None:
    monkeypatch.setenv("SMD_READ_VOLUME_GATE_MODE", "report")
    _mark_and_fill(50)
    first = _read()
    second = _read()
    assert first is None or first.get("action") != "block"
    assert second is None or second.get("action") != "block"
    rows = _gate_rows(recorded)
    assert len(rows) == 1, "report mode must record the crossing exactly once"
    assert rows[0]["audit_action"] == "report" and rows[0]["allowed"] is True


def test_off_mode_is_inert(seat, recorded, monkeypatch) -> None:
    monkeypatch.setenv("SMD_READ_VOLUME_GATE_MODE", "off")
    _mark_and_fill(500)
    result = _read()
    assert result is None or result.get("action") != "block"
    assert not _gate_rows(recorded)


def test_spine_path_marks_via_skill_md_read_and_gates(seat, recorded) -> None:
    # The production path: the spine session reads ODR's SKILL.md, then reads
    # documents. No webhook route for this session exists.
    read_volume.note_read(
        SID, "read_file", {"path": f"/app/skills/{read_volume.GATED_SKILL}/SKILL.md"}, ""
    )
    read_volume.note_read(SID, READ, {}, {"fileId": "seed", "pageCount": 80})
    result = _read()
    assert result is not None and result.get("action") == "block"


def test_a_refused_read_does_not_accumulate(seat, recorded) -> None:
    # post_tool_call never fires for a blocked call, so the accumulator holds.
    _mark_and_fill(50)
    before = sum(read_volume._sessions[SID].pages_by_file.values())
    _read()
    after = sum(read_volume._sessions[SID].pages_by_file.values())
    assert before == after == 50


def test_refusal_names_unmeasured_documents(seat, recorded) -> None:
    _mark_and_fill(50)
    read_volume.note_read(SID, READ, {}, {"fileId": "mystery", "name": "m.tif"})
    result = _read()
    assert result is not None and "no volume signal" in result["message"]
