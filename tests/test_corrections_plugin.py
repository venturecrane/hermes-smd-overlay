"""Tests for plugins/hermes-smd-corrections (ss-console #2091, ADR 0083 §4).

Four properties are load-bearing enough that a regression would be silent and
would reach a running seat:

 1. THE TOOL IS MAPPED. An unmapped tool is REFUSED by design, which is exactly
    how the ``execute_code`` gap this plugin closes stayed invisible until a live
    probe (ss #1915). ``correction_capture`` must classify INTERNAL_WRITE — the
    class every seat already authors at ``draft_for_review`` or better.
 2. THE AGENT NEVER SETS STATUS. The broker stamps ``proposed`` as a constant;
    nothing the caller sends may carry a status, so the marshalled payload must
    not contain one even if the model puts one in its args.
 3. TAINT REFUSES, AND FAILS CLOSED. A correction stated on a turn that read
    outside content is not the customer's. An unresolvable taint state refuses
    too — the cost of declining is a person restating a preference; the cost of
    accepting is a stranger's words in a reviewer's queue under the customer's
    name.
 4. THE NUDGE MATCHES THE REFUSAL. ``record_peer_preference`` shipped registered
    and unprompted and the lane had zero rows fleet-wide (overlay #170). The
    nudge exists for that reason, and it must never advertise capture on a turn
    ``pre_tool_call`` would refuse.
 5. ONLY AN AUTHORED ADMIN INSTALLS A STANDING RULE, AND A REFUSAL IS LEGIBLE
    (ss-console #2429). Admin status is read from the VERIFIED inbound origin,
    never from the tool's arguments, and a refusal writes an ``RBAC_EVENT`` row
    naming who asked and why it was declined. Before this, the only thing between
    a reply-authorized non-admin and a rule-install was model judgment — and it
    moved the day a framing sentence changed.

The broker is faked. Its validation is tested where it lives (console side,
``operator/workspace_broker/corrections.py``); duplicating it here would assert a
second, drifting copy of rules this plugin deliberately does not own.
"""

from __future__ import annotations

import json
from textwrap import dedent

import pytest

from shared.action_classes import ActionClass, classify_tool
from shared.inbound import SESSION_INBOUND_ORIGIN, TRUST_CLASS_INTERNAL, InboundOrigin
from tests.conftest import load_plugin

#: The two authored addresses every gate test turns on. Both are
#: reply-authorized; only one is an administrator (ss ADR 0085 §2).
ADMIN_ADDRESS = "admin@firm.example"
NON_ADMIN_ADDRESS = "runner@firm.example"


@pytest.fixture
def corrections(monkeypatch):
    plugin = load_plugin("hermes-smd-corrections")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "cor-1", "status": "proposed"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    return plugin, requests


@pytest.fixture
def audit_rows(corrections, monkeypatch):
    """Capture the rows the plugin writes, through its real emit path.

    The client is faked at the module global the plugin caches, NOT at
    ``_emit_refusal_audit`` — a test that stubs the emitter proves the caller
    called something, never that a row with the right action_type and metadata
    would land in a ledger.
    """
    plugin, _ = corrections
    rows: list[tuple] = []

    class _FakeClient:
        def execute(self, sql, *params):
            rows.append((sql, params))
            return 1

    monkeypatch.setattr(plugin, "_AUDIT_CLIENT", _FakeClient(), raising=False)
    monkeypatch.setattr(plugin, "_AUDIT_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(plugin, "_AUDIT_WIRED", True, raising=False)
    return rows


def _row_metadata(row: tuple) -> dict:
    """The metadata dict of a captured audit row (last positional param)."""
    return json.loads(row[1][-1])


def _row_action_type(row: tuple) -> str:
    """The action_type column (third positional param — see COLUMNS order)."""
    return row[1][2]


def _authored_admins(tmp_path, monkeypatch) -> None:
    """Author BOTH addresses on the reply roster, ONE of them as an admin."""
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(
        dedent(
            f"""
            customer_id: acme
            scope:
              inbound_allow_from:
                - {ADMIN_ADDRESS}
                - {NON_ADMIN_ADDRESS}
              admins:
                - {ADMIN_ADDRESS}
            """
        ).strip()
    )
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))


def _sender_is(session_id: str, address: str) -> None:
    """Bind the session to a verified inbound origin, as the live path does."""
    SESSION_INBOUND_ORIGIN.record(
        session_id, InboundOrigin(sender_address=address, message_id=f"mid-{address}")
    )


def _untainted(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin.SESSION_TAINT, "trust_class", lambda _sid: TRUST_CLASS_INTERNAL, raising=False
    )


def _admin_turn(plugin, monkeypatch, tmp_path, session_id="s1"):
    """An untainted turn whose verified sender is an authored administrator."""
    _untainted(plugin, monkeypatch)
    _authored_admins(tmp_path, monkeypatch)
    _sender_is(session_id, ADMIN_ADDRESS)


def _tainted(plugin, monkeypatch, value="unknown_external"):
    monkeypatch.setattr(plugin.SESSION_TAINT, "trust_class", lambda _sid: value, raising=False)


# ---------------------------------------------------------------------------
# 1. The tool is mapped
# ---------------------------------------------------------------------------


def test_capture_tool_is_mapped_internal_write(corrections):
    """Unmapped => REFUSED by design. INTERNAL_WRITE is the class every seat
    already authors, so capture needs no entitlement widening."""
    plugin, _ = corrections
    assert classify_tool(plugin.TOOL_NAME).action_class is ActionClass.INTERNAL_WRITE


def test_registers_tool_and_both_hooks(corrections):
    plugin, _ = corrections
    registered_tools: list[dict] = []
    registered_hooks: list[str] = []

    class Ctx:
        def register_tool(self, **kwargs):
            registered_tools.append(kwargs)

        def register_hook(self, name, _cb):
            registered_hooks.append(name)

    plugin.register(Ctx())
    assert [t["name"] for t in registered_tools] == [plugin.TOOL_NAME]
    # The wrapped function shape — a bare JSON-schema advertises empty
    # parameters and the model cannot pass a single argument.
    assert "parameters" in registered_tools[0]["schema"]
    assert sorted(registered_hooks) == ["pre_llm_call", "pre_tool_call"]


# ---------------------------------------------------------------------------
# 2. The agent never sets status
# ---------------------------------------------------------------------------


def test_marshalled_payload_carries_no_status(corrections):
    """`status` is a broker-side constant. A validated-but-caller-supplied status
    is one typo away from a caller-supplied `approved`; a constant cannot be."""
    plugin, requests = corrections
    plugin._capture(
        {
            "output_class": "staff",
            "spec_property": "format",
            "statement": "Could this be a table instead of text?",
            "status": "approved",  # the model tries; nothing reads it
        }
    )
    assert requests[0]["action"] == "correction_propose"
    assert "status" not in requests[0]["proposal"]
    assert set(requests[0]["proposal"]) == {
        "output_class",
        "spec_property",
        "statement",
        "stated_by",
        "source_ref",
    }


def test_broker_verdict_is_returned_verbatim(corrections, monkeypatch):
    """A refusal must stay visible to the turn rather than be swallowed into a
    cheerful acknowledgement."""
    plugin, _ = corrections
    monkeypatch.setattr(
        plugin,
        "_broker_request",
        lambda _p: {"error": "CorrectionValidationError", "message": "statement must not be empty"},
    )
    out = json.loads(plugin._capture({"output_class": "staff", "spec_property": "voice"}))
    assert out["error"] == "CorrectionValidationError"


# ---------------------------------------------------------------------------
# 3. Taint refuses, and fails closed
# ---------------------------------------------------------------------------


def test_untainted_admin_turn_is_allowed(corrections, monkeypatch, tmp_path):
    plugin, _ = corrections
    _admin_turn(plugin, monkeypatch, tmp_path)
    assert plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1") is None


def test_tainted_turn_is_refused(corrections, monkeypatch):
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1")
    assert block is not None and block["action"] == "block"
    assert "outside the firm" in block["message"]


def test_unresolvable_taint_refuses(corrections, monkeypatch):
    """Fail-closed: a capture we cannot certify came from a trusted turn is one
    we decline."""
    plugin, _ = corrections

    def boom(_sid):
        raise RuntimeError("register unreadable")

    monkeypatch.setattr(plugin.SESSION_TAINT, "trust_class", boom, raising=False)
    block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s1")
    assert block is not None and block["action"] == "block"


def test_other_tools_are_untouched(corrections, monkeypatch):
    """The hook fires for every tool call on the seat; it must be inert for all
    but its own."""
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    assert plugin.on_pre_tool_call(tool_name="write_file", session_id="s1") is None


# ---------------------------------------------------------------------------
# 3b. Admin status is enforced server-side, from the verified origin (ss#2429)
# ---------------------------------------------------------------------------


class TestAdminGateKillPair:
    """THE KILL-TEST PAIR (ss-console#2429, AC 2).

    On 2026-08-18, run
    ``shadow-pilot-smokeball-20260818T210927Z-8499256-2a47e3a7825a-notgreen``
    had the seat install a standing rule for ``ss-probe-runner`` — a sender on
    ``scope.inbound_allow_from`` who is NOT on ``scope.admins`` — and a
    ``CORRECTION_PROPOSED`` row was written. The turn was untainted, so the only
    gate in front of the effect was the model's own judgment about who was
    asking, and it moved when one framing sentence in the dispatched prompt
    changed (ss#2416 iteration 4). A control that passes by disposition is not a
    control.

    The pair below is the same attempt from two senders, identical in every
    respect except the authored admin list. Falsifier discipline: both tests
    were run against a mutant that keeps the old taint-only gate (the refusal
    test goes red) and one that stamps the audit row without the reason (the
    row assertions go red).
    """

    _ARGS = {
        "output_class": "outbound_client",
        "spec_property": "format",
        "statement": "Going forward, always open with the matter number.",
        # Model-supplied provenance. It must not be able to buy authority:
        # a caller who can name themselves can name anyone.
        "stated_by": "Managing Partner",
    }

    def test_rostered_non_admin_is_refused_with_an_audit_row(
        self, corrections, monkeypatch, tmp_path, audit_rows
    ):
        plugin, broker_requests = corrections
        _untainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)
        _sender_is("s-runner", NON_ADMIN_ADDRESS)

        block = plugin.on_pre_tool_call(
            tool_name=plugin.TOOL_NAME, session_id="s-runner", args=dict(self._ARGS)
        )

        assert block is not None and block["action"] == "block"
        assert "administrator" in block["message"]
        # The refusal is legible in the ledger, and names WHY and WHO.
        assert len(audit_rows) == 1
        assert _row_action_type(audit_rows[0]) == "RBAC_EVENT"
        meta = _row_metadata(audit_rows[0])
        assert meta["subAction"] == plugin.RBAC_SUB_ACTION_REFUSED
        assert meta["reason"] == plugin.REFUSAL_NOT_ADMIN
        assert meta["decision"] == "deny"
        assert meta["required"] == "scope.admins"
        assert meta["sender"] == NON_ADMIN_ADDRESS
        assert meta["tool"] == plugin.TOOL_NAME
        # The statement itself is never written: a refused capture's prose is the
        # content the refusal exists to keep out of the reviewer's queue.
        assert self._ARGS["statement"] not in json.dumps(meta)
        # And nothing reached the broker.
        assert broker_requests == []

    def test_admin_identical_attempt_succeeds(self, corrections, monkeypatch, tmp_path, audit_rows):
        """The other half of the pair: same tool, same args, same turn shape —
        only the authored admin list differs. Without this, 'refuse everything'
        would score as a passing control."""
        plugin, broker_requests = corrections
        _untainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)
        _sender_is("s-admin", ADMIN_ADDRESS)

        assert (
            plugin.on_pre_tool_call(
                tool_name=plugin.TOOL_NAME, session_id="s-admin", args=dict(self._ARGS)
            )
            is None
        )
        assert audit_rows == []  # nothing refused, nothing to record

        # …and the capture then runs end to end, through the real handler.
        out = json.loads(plugin._capture(dict(self._ARGS)))
        assert out["status"] == "proposed"
        assert len(broker_requests) == 1
        assert broker_requests[0]["action"] == "correction_propose"

    def test_model_supplied_arguments_cannot_confer_admin_status(
        self, corrections, monkeypatch, tmp_path, audit_rows
    ):
        """AC 1: the check reads the VERIFIED origin, never the arguments. A
        non-admin turn that names an admin in ``stated_by`` is still refused."""
        plugin, _ = corrections
        _untainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)
        _sender_is("s-forge", NON_ADMIN_ADDRESS)

        block = plugin.on_pre_tool_call(
            tool_name=plugin.TOOL_NAME,
            session_id="s-forge",
            args={**self._ARGS, "stated_by": ADMIN_ADDRESS, "source_ref": ADMIN_ADDRESS},
        )
        assert block is not None and block["action"] == "block"
        assert _row_metadata(audit_rows[0])["sender"] == NON_ADMIN_ADDRESS

    def test_no_verified_origin_is_refused(self, corrections, monkeypatch, tmp_path, audit_rows):
        """A turn nobody is attributable for cannot be an administrator's. Same
        fail-closed posture as the unresolvable taint state."""
        plugin, _ = corrections
        _untainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)

        block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s-nobody")
        assert block is not None and block["action"] == "block"
        assert _row_metadata(audit_rows[0])["reason"] == plugin.REFUSAL_NO_ORIGIN

    def test_unreadable_admin_list_refuses(self, corrections, monkeypatch, tmp_path, audit_rows):
        """An unreadable config means nobody is an admin, never everybody."""
        plugin, _ = corrections
        _untainted(plugin, monkeypatch)
        monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(tmp_path / "does-not-exist.yaml"))
        _sender_is("s-broken", ADMIN_ADDRESS)

        block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s-broken")
        assert block is not None and block["action"] == "block"
        assert _row_metadata(audit_rows[0])["reason"] == plugin.REFUSAL_NOT_ADMIN

    def test_domain_grant_does_not_make_an_admin(
        self, corrections, monkeypatch, tmp_path, audit_rows
    ):
        """``sender_is_admin`` is exact-address only: sharing the firm's domain
        makes you a colleague, not an administrator (ADR 0085 §2). An authored
        @domain entry is dropped by the accessor, not honored here."""
        plugin, _ = corrections
        _untainted(plugin, monkeypatch)
        yaml_path = tmp_path / "customer.yaml"
        yaml_path.write_text(
            dedent(
                """
                customer_id: acme
                scope:
                  inbound_allow_from:
                    - "@firm.example"
                  admins:
                    - "@firm.example"
                """
            ).strip()
        )
        monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(yaml_path))
        _sender_is("s-domain", NON_ADMIN_ADDRESS)

        block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s-domain")
        assert block is not None and block["action"] == "block"
        assert _row_metadata(audit_rows[0])["reason"] == plugin.REFUSAL_NOT_ADMIN

    def test_tainted_turn_refusal_is_also_recorded(
        self, corrections, monkeypatch, tmp_path, audit_rows
    ):
        """The pre-existing taint refusal now leaves a row too — it had been
        silent in the ledger, visible only in the container's logs."""
        plugin, _ = corrections
        _tainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)
        _sender_is("s-tainted", ADMIN_ADDRESS)

        block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s-tainted")
        assert block is not None and "outside the firm" in block["message"]
        assert _row_metadata(audit_rows[0])["reason"] == plugin.REFUSAL_TAINTED

    def test_refusal_stands_when_the_ledger_is_unavailable(
        self, corrections, monkeypatch, tmp_path
    ):
        """The safety decision is the refusal, not the row: an audit client that
        raises must not turn a refusal into an allow."""
        plugin, _ = corrections
        _untainted(plugin, monkeypatch)
        _authored_admins(tmp_path, monkeypatch)
        _sender_is("s-noaudit", NON_ADMIN_ADDRESS)

        class _Boom:
            def execute(self, *_args, **_kwargs):
                raise RuntimeError("d1 unreachable")

        monkeypatch.setattr(plugin, "_AUDIT_CLIENT", _Boom(), raising=False)
        monkeypatch.setattr(plugin, "_AUDIT_CUSTOMER_SLUG", "acme", raising=False)
        monkeypatch.setattr(plugin, "_AUDIT_WIRED", True, raising=False)

        block = plugin.on_pre_tool_call(tool_name=plugin.TOOL_NAME, session_id="s-noaudit")
        assert block is not None and block["action"] == "block"

    def test_the_refusal_verb_is_in_the_accepted_vocabulary(self, corrections):
        """A row whose action_type the writer's vocabulary denies is a row the
        portal renders as nothing — indistinguishable from a deliberate
        suppression (ss#2320). ``RBAC_EVENT`` is an EXISTING verb on both sides,
        which is why this refusal needs no new action_type."""
        plugin, _ = corrections
        audit = load_plugin("hermes-smd-audit")
        assert plugin._AUDIT_ACTION_TYPE == "RBAC_EVENT"
        assert plugin._AUDIT_ACTION_TYPE in audit.schemas.ACCEPTED_ACTION_TYPES


# ---------------------------------------------------------------------------
# 4. The nudge matches the refusal
# ---------------------------------------------------------------------------


def test_nudge_on_a_sender_attributed_untainted_admin_turn(corrections, monkeypatch, tmp_path):
    plugin, _ = corrections
    _admin_turn(plugin, monkeypatch, tmp_path)
    out = plugin.on_pre_llm_call(session_id="s1", sender_id=ADMIN_ADDRESS)
    assert out is not None and plugin.TOOL_NAME in out["context"]


def test_no_nudge_for_a_rostered_non_admin(corrections, monkeypatch, tmp_path):
    """The nudge follows the refusal through one shared decision, so a non-admin
    is never invited to state a standing rule the tool would then refuse."""
    plugin, _ = corrections
    _untainted(plugin, monkeypatch)
    _authored_admins(tmp_path, monkeypatch)
    _sender_is("s-nudge-runner", NON_ADMIN_ADDRESS)
    assert plugin.on_pre_llm_call(session_id="s-nudge-runner", sender_id=NON_ADMIN_ADDRESS) is None


def test_no_nudge_without_a_human(corrections, monkeypatch):
    """A cron turn has nobody to state a correction; the line would be pure
    context cost on every scheduled run."""
    plugin, _ = corrections
    _untainted(plugin, monkeypatch)
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_no_nudge_where_capture_would_be_refused(corrections, monkeypatch):
    """The nudge must never advertise something pre_tool_call would refuse."""
    plugin, _ = corrections
    _tainted(plugin, monkeypatch)
    assert plugin.on_pre_llm_call(session_id="s1", sender_id="someone@example.com") is None
