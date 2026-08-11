"""Tests for plugins/hermes-smd-establishment (ss ADR 0085).

The load-bearing properties, each with an input the OLD/broken behavior would
have mishandled (Law 12):

 1. THE TOOLS ARE MAPPED. Unmapped = terminal REFUSED (the ss #1915
    invisibility): stage + submit INTERNAL_WRITE, status READ.
 2. NON-ADMIN REFUSED, ADMIN PASSES. The admin predicate is the ONLY gate, it
    fails closed (no stash, unreadable config), and the refusal names who can.
 3. TAINT IS DELIBERATELY NOT CHECKED. Asserted positively, with the decision
    cited, so a future "hardening" that adds a taint gate fails this suite and
    has to confront the decision rather than silently reverting it.
 4. LAST-WRITER-WINS DOWNGRADE. A mid-session non-admin message strips the
    session's establishment authority (fail-safe direction).
 5. THE WIRE SHAPE IS §3's. The broker's C0 half is built from the same design
    section; a renamed field here would strand the two halves.

The broker is faked; its validation is tested where it lives (console side).
"""

from __future__ import annotations

import json

import pytest

from shared import read_capture
from shared.action_classes import ActionClass, classify_tool
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT, InboundOrigin
from tests.conftest import load_plugin


@pytest.fixture(autouse=True)
def _clean_origin_register():
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_INBOUND_ORIGIN._unbound.clear()
    SESSION_INBOUND_ORIGIN._by_address.clear()
    SESSION_INBOUND_ORIGIN._by_message.clear()
    yield
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_INBOUND_ORIGIN._unbound.clear()
    SESSION_INBOUND_ORIGIN._by_address.clear()
    SESSION_INBOUND_ORIGIN._by_message.clear()


class _FakeConfig:
    def __init__(self, admins, connectors=None):
        self._admins = admins
        self.connectors = dict(connectors or {})

    @property
    def admins(self):
        return list(self._admins)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins


class _FakeCustomerConfig:
    admins: list[str] = []
    #: No Email connector by default — no mail channel, so the possession
    #: ceremony (ss#2164) never binds and the pre-ceremony gate behavior below
    #: is asserted unchanged. Custody-specific behavior is covered in
    #: tests/test_admin_possession.py.
    connectors: dict = {}

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.connectors)


@pytest.fixture
def establishment(monkeypatch, tmp_path):
    plugin = load_plugin("hermes-smd-establishment")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "staging_id": "set-1", "doc_id": "doc-1", "run_id": "run-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = ["chris@firm.com"]
    _FakeCustomerConfig.connectors = {}
    monkeypatch.setattr(plugin, "CustomerConfig", _FakeCustomerConfig)
    plugin._ADMIN_STASH.clear()
    return plugin, requests


def _turn(plugin, sender, session="sess-1"):
    return plugin.on_pre_llm_call(session_id=session, sender_id=sender, user_message="x")


def _gate(plugin, tool, session="sess-1"):
    return plugin.on_pre_tool_call(tool_name=tool, session_id=session)


# ---------------------------------------------------------------------------
# 1. Mapping + registration
# ---------------------------------------------------------------------------


def test_tools_are_mapped_with_the_declared_classes(establishment):
    plugin, _ = establishment
    assert classify_tool(plugin.TOOL_STAGE).action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool(plugin.TOOL_SUBMIT).action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool(plugin.TOOL_STATUS).action_class is ActionClass.READ


def test_registers_three_tools_and_three_hooks(establishment):
    """``post_tool_call`` joined the set for ss#2247 read capture. If it is never
    wired, capture silently never happens and EVERY reference stage refuses
    ``no_capture`` — a total feature outage that no other test would catch,
    because each half works in isolation."""
    plugin, _ = establishment
    tools: list[dict] = []
    hooks: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            tools.append(kwargs)

        def register_hook(self, name, _cb):
            hooks.append(name)

    plugin.register(Ctx())
    assert [t["name"] for t in tools] == list(plugin.ESTABLISH_TOOLS)
    # Wrapped function shape — a bare JSON-schema advertises empty parameters.
    assert all("parameters" in t["schema"] for t in tools)
    assert sorted(hooks) == ["post_tool_call", "pre_llm_call", "pre_tool_call"]


# ---------------------------------------------------------------------------
# 2. The admin gate
# ---------------------------------------------------------------------------


def test_non_admin_is_refused_and_told_who_can(establishment):
    """Firm-level establishment stays admin-only. ``establish_status`` is the
    O5 exception (ADR 0085 §6): a classified non-admin session may poll — a
    person who established their own preferences must be able to read their
    run's result, and run ids are broker-minted secrets."""
    plugin, _ = establishment
    _turn(plugin, "sarah@firm.com")
    for tool in (plugin.TOOL_STAGE, plugin.TOOL_SUBMIT):
        verdict = _gate(plugin, tool)
        assert verdict is not None and verdict["action"] == "block"
        assert "Operator admins" in verdict["message"]
        assert "correction_capture" in verdict["message"]
    assert _gate(plugin, plugin.TOOL_STATUS) is None


def test_unclassified_session_is_refused_fail_closed(establishment):
    """No stash entry — a session where pre_llm_call never classified a sender
    (restart, hook fault) admits nobody."""
    plugin, _ = establishment
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="never-seen")["action"] == "block"


def test_admin_passes_all_three_tools(establishment):
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool) is None


def test_gate_ignores_other_tools(establishment):
    plugin, _ = establishment
    assert _gate(plugin, "email_send", session="never-seen") is None


def test_unreadable_config_classifies_nobody_as_admin(establishment, monkeypatch):
    plugin, _ = establishment

    class Broken:
        @classmethod
        def from_volume(cls, path=None):
            raise RuntimeError("volume gone")

    monkeypatch.setattr(plugin, "CustomerConfig", Broken)
    _turn(plugin, "chris@firm.com", session="sess-broken")
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="sess-broken")["action"] == "block"


# ---------------------------------------------------------------------------
# 3. Taint is DELIBERATELY not checked
# ---------------------------------------------------------------------------


def test_establishment_proceeds_on_a_turn_that_read_fenced_content(establishment):
    """Captain decision 2026-08-02 (same-breath establishment; recorded in the
    intake design's amendment block, point 1): the establishment turn READS the
    firm's documents through a connector, so the session is necessarily marked
    non-internal by the very act the admin instructed — a taint gate here would
    refuse every legitimate establishment. The admin allow-list is the gate;
    the broker's server-side provenance verification and the compiler gates
    (leak check above all) bound what a hostile document could smuggle into a
    spec. This test asserts the decision POSITIVELY so a future taint check
    fails here and must confront the decision rather than silently revert it.
    """
    plugin, _ = establishment
    session = "sess-tainted-establishment"
    SESSION_TAINT.mark(session, "unknown_external")
    _turn(plugin, "chris@firm.com", session=session)
    for tool in plugin.ESTABLISH_TOOLS:
        assert _gate(plugin, tool, session=session) is None, (
            f"{tool} was refused on a tainted turn — the establishment gate must "
            "check the admin predicate ONLY (Captain decision 2026-08-02)"
        )


# ---------------------------------------------------------------------------
# 4. Last-writer-wins stash
# ---------------------------------------------------------------------------


def test_mid_session_non_admin_message_downgrades(establishment):
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    assert _gate(plugin, plugin.TOOL_SUBMIT) is None
    _turn(plugin, "stranger@elsewhere.com")
    assert _gate(plugin, plugin.TOOL_SUBMIT)["action"] == "block"


def test_unattributed_turn_leaves_the_classification(establishment):
    """A cron/self-wake turn carries no sender; it is not a new person, so it
    neither grants nor strips the session's classification."""
    plugin, _ = establishment
    _turn(plugin, "chris@firm.com")
    _turn(plugin, "")
    assert _gate(plugin, plugin.TOOL_SUBMIT) is None


def test_webhook_route_turn_classifies_the_verified_admin(establishment):
    """ss#2222, live-caught: on the email path the gateway threads the ROUTE
    as sender_id, so classifying it asks "is this channel an admin?" and an
    authored admin was refused with "only the firm's Operator admins can
    establish" — the possession ceremony never reached, no challenge ever
    sent. The verified sender recorded by the webhook router is the person.
    """
    plugin, _ = establishment
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="chris@firm.com", message_id="m-1")
    )
    _turn(plugin, "webhook:agentmail", session="sess-9")
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="sess-9") is None
    # The claimed origin is re-keyed so later resolvers in the same pass find it.
    rekeyed = SESSION_INBOUND_ORIGIN.get("sess-9")
    assert rekeyed is not None and rekeyed.sender_address == "chris@firm.com"


def test_webhook_route_turn_does_not_promote_a_non_admin(establishment):
    """The falsifier for the fix: resolution must grant the PERSON's authority,
    never blanket-pass a webhook turn. A rostered non-admin stays refused."""
    plugin, _ = establishment
    SESSION_INBOUND_ORIGIN.record(
        "", InboundOrigin(sender_address="paralegal@firm.com", message_id="m-2")
    )
    _turn(plugin, "webhook:agentmail", session="sess-8")
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="sess-8")["action"] == "block"


def test_unresolvable_webhook_route_stays_refused(establishment):
    """No recorded origin: the route falls through unchanged, fails the admin
    match, and the gate refuses. Fail-safe, not fail-open."""
    plugin, _ = establishment
    _turn(plugin, "webhook:agentmail", session="sess-7")
    assert _gate(plugin, plugin.TOOL_SUBMIT, session="sess-7")["action"] == "block"


# ---------------------------------------------------------------------------
# 5. Nudge — admin turns only
# ---------------------------------------------------------------------------


def test_nudge_rides_admin_turns_only(establishment):
    """The ADMIN nudge stays admin-only (it advertises what the gate would
    refuse anyone else). Since O5, every attributed turn also carries the
    person-scope nudge — that gate any attributed sender can satisfy for
    themselves, so advertising it to everyone is correct (overlay #170)."""
    plugin, _ = establishment
    admin_context = _turn(plugin, "chris@firm.com")["context"]
    assert plugin._NUDGE in admin_context
    assert plugin._PERSON_NUDGE in admin_context
    non_admin_context = _turn(plugin, "sarah@firm.com")["context"]
    assert plugin._NUDGE not in non_admin_context
    assert plugin._PERSON_NUDGE in non_admin_context
    assert _turn(plugin, "") is None


# ---------------------------------------------------------------------------
# 6. Wire shape — the C0 contract
# ---------------------------------------------------------------------------


def test_stage_marshals_exactly_the_design_fields(establishment):
    """The wire shape is unchanged by ss#2247 — same five fields, same names;
    only where ``text`` comes from moved. Asserted here on the text path (a
    connector the seat cannot read for itself); the reference path asserts the
    identical shape in ``test_reference_mode_stages_the_captured_bytes``."""
    plugin, requests = establishment
    plugin._stage(
        {
            "staging_id": None,
            "name": "letter-01.md",
            "text": "Dear Ms. Reyes,",
            "source": {"connector": "sharepoint", "document_id": "d1", "matter_id": "m1"},
            "status": "approved",  # the model tries; nothing forwards it
        }
    )
    assert requests[0]["action"] == "establish_stage_document"
    assert set(requests[0]) == {"action", "staging_id", "name", "text", "source"}
    assert requests[0]["text"] == "Dear Ms. Reyes,"


def test_submit_marshals_exactly_the_design_fields(establishment):
    plugin, requests = establishment
    plugin._submit(
        {
            "staging_id": "set-1",
            "phase": "install",
            "output_class": "work_product",
            "property": "voice",
            "spec_body": "Plain sentences.",
            "assertions": {"rules": []},
            "corpus_manifest": [{"doc_id": "doc-1", "sha256": "ab" * 32}],
            "instructed_by": "chris@firm.com",
            "source_ref": "msg-123",
            "extra_field": "dropped",
        }
    )
    assert requests[0]["action"] == "establish_submit"
    assert set(requests[0]) == {
        "action",
        "scope",
        "person",
        "staging_id",
        "phase",
        "output_class",
        "property",
        "spec_body",
        "assertions",
        "corpus_manifest",
        "instructed_by",
        "source_ref",
    }


def test_status_returns_the_broker_verdict_verbatim(establishment):
    plugin, requests = establishment
    out = json.loads(plugin._status({"run_id": "run-1"}))
    assert requests[0] == {"action": "establish_status", "run_id": "run-1"}
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# 7. Staging by reference (ss#2247)
#
# The defect: `establish_stage_document` took the document's text as a tool
# ARGUMENT, so "the corpus is what the firm actually wrote" was a property the
# model had to achieve by retyping. Live on the pilot 2026-08-11 it did not — a
# 19,114 character letter staged as 19,066, and a second document came through
# with an equal-length character substitution. The seat now holds the connector's
# own bytes and stages those; the model cannot supply text for a captured
# connector at all.
# ---------------------------------------------------------------------------

_SMOKEBALL_SOURCE = {"connector": "smokeball", "document_id": "f-1", "matter_id": "m-1"}


def _read(plugin, text, offset=0, total=None, *, session="sess-1", name="Reyes demand.pdf", **over):
    """Fire ``post_tool_call`` with the connector's literal result shape."""
    payload = {
        "matterId": "m-1",
        "fileId": "f-1",
        "name": name,
        "text": text,
        "offset": offset,
        "total_chars": len(text) + offset if total is None else total,
        "truncated": False,
    }
    payload.update(over.pop("result_extra", {}))
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={"matter_id": "m-1", "file_id": "f-1"},
        result=json.dumps(payload),
        session_id=session,
        tool_call_id="c-1",
        duration_ms=5,
        status=over.pop("status", "success"),
        error_type=over.pop("error_type", None),
        **over,
    )


def _stage_call(plugin, args, session="sess-1"):
    """The real two-seam flow: the gate prepares, then the handler stages.
    Returns the gate's block directive, or the handler's output."""
    verdict = plugin.on_pre_tool_call(tool_name=plugin.TOOL_STAGE, session_id=session, args=args)
    if verdict is not None:
        return verdict
    return plugin._stage(args, session_id=session)


@pytest.fixture
def admin_session(establishment):
    """An admin-classified session with the capture store cleared."""
    plugin, requests = establishment
    read_capture._reset_for_tests()
    plugin._STAGE_PLANS.clear()
    _turn(plugin, "chris@firm.com")
    yield plugin, requests
    read_capture._reset_for_tests()
    plugin._STAGE_PLANS.clear()


# --- capture ---------------------------------------------------------------


def test_post_tool_call_captures_a_smokeball_read(admin_session):
    """Falsifier: the wrong result-field names (``totalChars`` vs
    ``total_chars``, ``fileId`` vs ``file_id``). The fixture above is the
    connector's literal response shape for exactly this reason — a capture keyed
    off invented field names records nothing and every stage refuses."""
    plugin, _ = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    result = read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1")
    assert result.ok and result.text == "Dear Ms. Reyes,"


def test_post_tool_call_ignores_other_tools(admin_session):
    """Falsifier: capture keyed on any read — unrelated tool results pollute the
    store and can then be staged as a document."""
    plugin, _ = admin_session
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_files_on_matter",
        args={"matter_id": "m-1"},
        result=json.dumps({"matterId": "m-1", "fileId": "f-1", "text": "x", "total_chars": 1}),
        session_id="sess-1",
        status="success",
    )
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1").reason == (
        read_capture.REASON_NO_CAPTURE
    )


def test_post_tool_call_ignores_a_failed_read(admin_session):
    """Falsifier: this hook fires for FAILED calls too (docs/hook-surface.md §2 —
    outcome kwargs are load-bearing, not telemetry). An error payload recorded as
    a window assembles garbage into a staged document."""
    plugin, _ = admin_session
    _read(plugin, "connection reset", status="error", error_type="ConnectionError")
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1").reason == (
        read_capture.REASON_NO_CAPTURE
    )


def test_post_tool_call_ignores_a_result_with_no_text_key(admin_session):
    """Falsifier: the unsupported-type branch returns no ``text`` key at all;
    captured as an empty window at offset 0 it would block a later real read."""
    plugin, _ = admin_session
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={"matter_id": "m-1", "file_id": "f-1"},
        result=json.dumps({"matterId": "m-1", "fileId": "f-1", "error": "unsupported type"}),
        session_id="sess-1",
        status="success",
    )
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1").reason == (
        read_capture.REASON_NO_CAPTURE
    )


def test_capture_unwraps_the_live_dispatcher_envelope(admin_session):
    """Falsifier: the exact silent failure caught live on pilot-smokeball at
    2026-08-11T17:25 — the hook's ``result`` is ``{"result": "<connector JSON
    as a string>"}``, not the connector's JSON. The outer object parses, has no
    top-level ``text``, and the old guard returned silently: no warning, no
    capture, and every stage in the turn refused ``no_capture`` after four real
    reads. This fixture is the LIVE string shape, transcribed from the seat's
    session store, not the connector's documented shape."""
    plugin, _ = admin_session
    inner = json.dumps(
        {
            "fileId": "f-1",
            "matterId": "m-1",
            "name": "Reyes demand.pdf",
            "fileExtension": ".txt",
            "text": "Dear Ms. Reyes,",
            "offset": 0,
            "total_chars": 15,
            "truncated": False,
        },
        indent=2,
    )
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={"matter_id": "m-1", "file_id": "f-1"},
        result=json.dumps({"result": inner}),
        session_id="sess-1",
        status="success",
    )
    result = read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1")
    assert result.ok and result.text == "Dear Ms. Reyes,"


def test_capture_unwraps_an_mcp_content_block_envelope(admin_session):
    """Falsifier: a future Hermes hands the MCP protocol shape
    (``{"content": [{"type": "text", "text": ...}]}``) through the hook and
    capture goes silently dark again, exactly like the live incident."""
    plugin, _ = admin_session
    inner = json.dumps(
        {
            "fileId": "f-1",
            "matterId": "m-1",
            "name": "n",
            "text": "Body.",
            "offset": 0,
            "total_chars": 5,
            "truncated": False,
        }
    )
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={"matter_id": "m-1", "file_id": "f-1"},
        result=json.dumps({"content": [{"type": "text", "text": inner}]}),
        session_id="sess-1",
        status="success",
    )
    result = read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1")
    assert result.ok and result.text == "Body."


def test_unwrap_never_reparses_a_result_that_already_carries_text(admin_session):
    """Falsifier: a connector result carrying a field literally named
    ``result`` alongside ``text`` gets re-unwrapped into garbage. The unwrap
    must stop the moment the payload looks like the read result itself."""
    plugin, _ = admin_session
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={"matter_id": "m-1", "file_id": "f-1"},
        result=json.dumps(
            {
                "matterId": "m-1",
                "fileId": "f-1",
                "name": "n",
                "text": "Keep me.",
                "offset": 0,
                "total_chars": 8,
                "truncated": False,
                "result": "IGNORED",
            }
        ),
        session_id="sess-1",
        status="success",
    )
    result = read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1")
    assert result.ok and result.text == "Keep me."


def test_an_unfamiliar_status_word_still_captures(admin_session):
    """Falsifier: failure detected by an ALLOW-list of success words. The day
    Hermes reports ``status="completed"`` instead of ``"success"``, capture stops
    fleet-wide and every stage refuses ``no_capture`` — a refusal whose remedy is
    "read it again", which cannot possibly fix it. Positive detection degrades
    the other way: we capture a little more, and an error result carries no
    ``text`` key anyway."""
    plugin, _ = admin_session
    _read(plugin, "Dear Ms. Reyes,", status="completed")
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1").ok


def test_post_tool_call_survives_a_non_json_result(admin_session):
    """Falsifier: a raising hook. Hermes wraps callbacks, but a raise here would
    still abort capture for the rest of the turn and every stage would refuse."""
    plugin, _ = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_read_document",
        args={},
        result="<html>gateway timeout</html>",
        session_id="sess-1",
        status="success",
    )
    assert read_capture.assemble("smokeball", "m-1", "f-1", session_id="sess-1").ok


# --- staging ---------------------------------------------------------------


def test_reference_mode_stages_the_captured_bytes(admin_session):
    """The whole feature. Asserts the broker payload's ``text`` is byte-identical
    to the concatenated connector windows — not merely the right length, which is
    what the 2026-08-11 equal-length substitution passed."""
    plugin, requests = admin_session
    body = "Dear Ms. Reyes,\n\nWe represent " + ("x" * 25_000) + "\n\nVery truly yours,"
    _read(plugin, body[:20_000], 0, len(body))
    _read(plugin, body[20_000:], 20_000, len(body))
    out = _stage_call(
        plugin, {"staging_id": None, "name": "paraphrase", "source": _SMOKEBALL_SOURCE}
    )
    assert json.loads(out)["ok"] is True
    assert set(requests[0]) == {"action", "staging_id", "name", "text", "source"}
    assert requests[0]["text"] == body


def test_model_supplied_text_is_refused_for_smokeball(admin_session):
    """Falsifier: the transcription path survives and ss#2247 recurs. The refusal
    must land BEFORE the broker — a staged retyping is unrecoverable once written,
    because the broker hashes whatever it is given."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    verdict = _stage_call(
        plugin,
        {"name": "letter", "text": "Dear Ms Reyes", "source": _SMOKEBALL_SOURCE},
    )
    assert verdict["action"] == "block"
    assert "you do not supply" in verdict["message"]
    assert requests == []


def test_model_supplied_text_is_refused_even_with_no_capture(admin_session):
    """Falsifier: refusing text only WHEN a capture exists. Then a gateway
    restart, an LRU eviction, or a TTL expiry silently reopens the transcription
    path — and supplying text is exactly the model's natural recovery from a
    "no capture" refusal. A control must not be conditional on the state it
    protects."""
    plugin, requests = admin_session
    verdict = _stage_call(
        plugin,
        {"name": "letter", "text": "Dear Ms Reyes", "source": _SMOKEBALL_SOURCE},
    )
    assert verdict["action"] == "block"
    assert "you do not supply" in verdict["message"]
    assert requests == []


def test_text_path_still_works_for_another_connector(admin_session):
    """Falsifier: the connector field stops being vendor-agnostic, and a corpus
    from any source the seat cannot read for itself becomes unstageable."""
    plugin, requests = admin_session
    out = _stage_call(
        plugin,
        {
            "name": "policy.docx",
            "text": "Our engagement terms.",
            "source": {"connector": "sharepoint", "document_id": "d-9"},
        },
    )
    assert json.loads(out)["ok"] is True
    assert requests[0]["text"] == "Our engagement terms."


def test_missing_matter_id_refuses_for_smokeball(admin_session):
    """Falsifier: a key built with an empty matter misses the capture recorded
    under the real one and reports ``no_capture`` for a document that WAS read —
    a refusal naming a cause the model cannot act on."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    verdict = _stage_call(
        plugin, {"name": "letter", "source": {"connector": "smokeball", "document_id": "f-1"}}
    )
    assert verdict["action"] == "block"
    assert "source.matter_id" in verdict["message"]
    assert requests == []


def test_incomplete_coverage_refuses_and_names_the_ranges(admin_session):
    """Falsifier: a partial corpus staged as whole. Asserted through the PLUGIN,
    not just the store, so the message formatting — the offsets the model has to
    read back — is covered too."""
    plugin, requests = admin_session
    _read(plugin, "A" * 40_000, 0, 60_000)
    verdict = _stage_call(plugin, {"name": "letter", "source": _SMOKEBALL_SOURCE})
    assert verdict["action"] == "block"
    assert "40000-60000" in verdict["message"]
    assert "40000 of 60000 characters" in verdict["message"]
    assert requests == []


def test_empty_extraction_refuses_terminally(admin_session):
    """Falsifier (critique issue 5): a scanned-only PDF refuses with a
    coverage-shaped message, the model reads it again, gets nothing again, and
    the establishment run never completes. The refusal must say DROP IT."""
    plugin, requests = admin_session
    _read(plugin, "", 0, 0)
    verdict = _stage_call(plugin, {"name": "scan.pdf", "source": _SMOKEBALL_SOURCE})
    assert verdict["action"] == "block"
    assert "no text" in verdict["message"] and "Drop it" in verdict["message"]
    assert requests == []


def test_changed_document_refuses(admin_session):
    """Falsifier: pages from two versions of a document assembled into one
    staged artifact that never existed."""
    plugin, requests = admin_session
    _read(plugin, "aaa", 0, 6)
    _read(plugin, "bbb", 3, 9)
    verdict = _stage_call(plugin, {"name": "letter", "source": _SMOKEBALL_SOURCE})
    assert verdict["action"] == "block"
    assert "changed while you were reading it" in verdict["message"]
    assert requests == []


def test_a_read_from_another_session_cannot_be_staged(admin_session):
    """THE critique-7 falsifier, at the seam that matters. Without session
    scoping an establishment turn could stage a document nobody in that
    conversation opened — the admin gate, the possession ceremony, and the whole
    attribution chain would be reasoning about a document the turn never
    touched."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,", session="sess-OTHER")
    _turn(plugin, "chris@firm.com", session="sess-2")
    verdict = _stage_call(plugin, {"name": "letter", "source": _SMOKEBALL_SOURCE}, session="sess-2")
    assert verdict["action"] == "block"
    assert "holds no read" in verdict["message"]
    assert requests == []


def test_captured_name_is_preferred_over_the_supplied_name(admin_session):
    """Falsifier: the model's paraphrase reaches the demotion report, and the
    admin cannot find the document it names. The one place this design prefers a
    silent substitution to a refusal — refusing over a dash rendering would fail
    whole runs for nothing."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,", name="2026-04-02 Reyes demand.pdf")
    _stage_call(plugin, {"name": "the Reyes letter", "source": _SMOKEBALL_SOURCE})
    assert requests[0]["name"] == "2026-04-02 Reyes demand.pdf"


def test_successful_stage_forgets_the_capture(admin_session):
    """Falsifier: the capture survives, so a duplicate stage of the same document
    silently succeeds twice under two broker doc ids and the corpus manifest
    double-counts it."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    assert json.loads(_stage_call(plugin, {"name": "l", "source": _SMOKEBALL_SOURCE}))["ok"]
    again = _stage_call(plugin, {"name": "l", "source": _SMOKEBALL_SOURCE})
    assert again["action"] == "block" and "holds no read" in again["message"]
    assert len(requests) == 1


def test_handler_reassembles_when_the_gate_did_not_run(admin_session):
    """Falsifier: a dispatch path that skips this plugin's ``pre_tool_call``
    leaves no stash, and the handler falls back to... the model's text. The
    fallback must be a fresh assembly against the resolved session, so the
    reference path never degrades into the path it replaced."""
    plugin, requests = admin_session
    _read(plugin, "Dear Ms. Reyes,")
    out = plugin._stage({"name": "l", "source": _SMOKEBALL_SOURCE})
    assert json.loads(out)["ok"] is True
    assert requests[0]["text"] == "Dear Ms. Reyes,"


# --- the two latent bugs this PR also closes -------------------------------


def test_frame_pre_check_refuses_before_the_broker_sees_it(admin_session):
    """Falsifier: an oversize document reaches the socket and comes back as a
    bare ``{"ok": false, "error": "request_too_large"}`` naming no field — and a
    model told only "too large" shrinks the document, which is the ss#2247
    failure wearing a different hat. The broker's ceiling covers the WHOLE frame,
    envelope and escaping included, so it is measured here the same way."""
    plugin, requests = admin_session
    body = "x" * 1_100_000
    _read(plugin, body, 0, len(body))
    # The document IS captured whole (the store's ceiling sits above the
    # broker's on purpose) precisely so the refusal can name its size rather
    # than surface as an unsatisfiable coverage gap.
    refusal = _stage_call(plugin, {"name": "huge.pdf", "source": _SMOKEBALL_SOURCE})
    assert isinstance(refusal, str) and "larger than the seat can stage" in refusal
    assert "Do not trim, summarize, or split it" in refusal
    assert requests == []


def test_request_is_serialized_without_ascii_escaping(monkeypatch):
    """Falsifier: someone "restores" the json default and halves the effective
    document ceiling with no test failing. With ``ensure_ascii=True`` every curly
    quote costs SIX frame bytes instead of three, and the frame is the real
    limit. Asserted on the BYTES the socket receives — anything less would be
    testing json rather than the plugin, which is why this test loads the plugin
    directly instead of taking the fixture's faked ``_broker_request``."""
    plugin = load_plugin("hermes-smd-establishment")
    sent: list[bytes] = []

    class _FakeSocket:
        def __init__(self, *a, **k):
            self._replies = iter([b'{"ok": true}\n'])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, _):
            pass

        def connect(self, _):
            pass

        def sendall(self, blob):
            sent.append(blob)

        def recv(self, _n):
            return next(self._replies, b"")

    monkeypatch.setenv("SMD_WORKSPACE_BROKER_SOCKET", "/tmp/fake.sock")
    monkeypatch.setattr(plugin.socket, "socket", _FakeSocket)
    payload = {"action": "establish_stage_document", "text": "Dear Ms. Reyes — “hello”"}
    plugin._broker_request(payload)
    escaped = json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n"
    assert sent[0] == json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    assert len(sent[0]) < len(escaped)


# --- the gates still gate ---------------------------------------------------


def test_admin_gate_still_blocks_reference_mode(establishment):
    """Falsifier: a refactor that resolves the reference BEFORE the authority
    check, or gives reference mode its own entry path around the gate. A
    non-admin must get the refusal naming who CAN establish — not one about
    document coverage, which would send them chasing a read problem they do not
    have."""
    plugin, requests = establishment
    read_capture._reset_for_tests()
    _turn(plugin, "sarah@firm.com")
    verdict = _stage_call(plugin, {"name": "letter", "source": _SMOKEBALL_SOURCE})
    assert verdict["action"] == "block"
    assert "Operator admins" in verdict["message"]
    assert requests == []


def test_possession_ceremony_still_withholds_reference_mode(establishment, monkeypatch):
    """Same, for the AgentMail-custody withhold: the mailbox-possession ceremony
    must still be the thing that speaks, ahead of any staging verdict."""
    plugin, requests = establishment
    read_capture._reset_for_tests()
    _FakeCustomerConfig.connectors = {"Email": {"enabled": True, "adapter": "agentmail"}}
    _turn(plugin, "chris@firm.com", session="sess-ceremony")
    _read(plugin, "Dear Ms. Reyes,", session="sess-ceremony")
    verdict = _stage_call(
        plugin, {"name": "letter", "source": _SMOKEBALL_SOURCE}, session="sess-ceremony"
    )
    assert verdict["action"] == "block"
    assert "mailbox" in verdict["message"]
    assert requests == []
