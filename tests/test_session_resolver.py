"""Session resolver for the provenance register (overlay #141).

Hermes core's three pre_tool_call fire sites pass task_id only — never
session_id — so the gate consulted the register under "" while reads were
recorded under the real id: 111/111 historical tier3 rows carried
register_was_empty=true and no session_id key. The resolver notes the real
id where core provides one (pre_llm_call, post_tool_call) and consulting
hooks fall back to it. A resolver miss degrades to the OLD behavior (empty
register — over-report, no exemption), never a widened one.
"""

import importlib
import sys

sys.path.insert(0, ".")

from shared import provenance
from shared.identifier_filter import ProvenanceRegister


def _fresh_provenance():
    importlib.reload(provenance)
    return provenance


def test_resolve_prefers_given_id() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-old")
    assert prov.resolve_session("sess-new") == "sess-new"


def test_resolve_falls_back_to_last_noted() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-real")
    assert prov.resolve_session("") == "sess-real"
    assert prov.resolve_session(None) == "sess-real"


def test_resolver_miss_degrades_to_empty_never_widens() -> None:
    prov = _fresh_provenance()
    # Nothing noted yet: resolve("") -> "" -> register_for("") is EMPTY.
    assert prov.resolve_session("") == ""
    reg = prov.register_for("")
    assert isinstance(reg, ProvenanceRegister)
    assert not reg.captions()


def test_note_ignores_empty() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-real")
    prov.note_session("")
    prov.note_session(None)
    assert prov.resolve_session("") == "sess-real"


def test_end_to_end_record_under_real_consult_under_missing() -> None:
    """The #141 failure mode, fixed: post_tool_call records under the real id;
    a pre_tool_call consult with NO id resolves to the same register."""
    prov = _fresh_provenance()
    sid = "20260707_000001_e2e141"
    # turn start: pre_llm_call notes the real id
    prov.note_session(sid)
    # post_tool_call: read recorded under the real id
    prov.record_read(sid, "Discovery capture on Alvarez v. Draper, matter 2026-PI-101.")
    # pre_tool_call: core drops the id; resolver recovers the same register
    reg = prov.register_for(prov.resolve_session(""))
    assert "alvarez v. draper" in reg.captions()
    assert bool(reg)
