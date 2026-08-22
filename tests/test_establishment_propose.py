"""The propose half of read-back-and-confirm (ss-console#2529).

Four properties, each with the input the absent control would have waved
through:

* **A proposal is taint-gated and the document verbs are not.** Both halves are
  asserted, in the same file, because they look like a contradiction and are
  not: a document establishment is tainted by doing its job, a proposed rule
  reads nothing and its content is a sentence somebody typed. A future reader
  who collapses the two breaks one of these.
* **The instructor is the speaker, and a personal rule's subject is them.** The
  hook cannot rewrite arguments, so both are exact-match refusals — which pins
  the wire value to the attribution just as a stamp would.
* **A non-admin may state a firm rule only as one that waits for an admin.**
  The seat decides that, not the model: "am I an admin" is precisely the
  question a hostile instruction would like the model to answer wrongly.
* **A reply that follows a proposal carries the readback verbatim.** The
  recipient lock's shape applied to content. Without it the model composes the
  sentence the person agrees to, and the sentence the broker commits is a
  different one, with a ledger row saying they match.

An old broker gets a sentence, never an exception: a seat whose image predates
this reports a capability that is not there and does the person's actual work.
"""

from __future__ import annotations

import json

import pytest

from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from tests.conftest import load_plugin

ADMIN = "chris@firm.com"
PARALEGAL = "sarah@firm.com"
READBACK = "[rule 7f3a2c1d] In client letters, be more formal and shorter."


class _FakeConfig:
    def __init__(self, admins, connectors=None):
        self._admins = admins
        self.connectors = dict(connectors or {})

    @property
    def admins(self):
        return list(self._admins)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender):
        return True


class _FakeCustomerConfig:
    admins: list[str] = [ADMIN]
    connectors: dict = {}

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.connectors)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    requests: list[dict] = []
    responses: dict = {"default": {"ok": True, "readback": READBACK, "proposal_id": "7f3a2c1d"}}

    def fake_broker_request(payload):
        requests.append(payload)
        return responses.get(payload.get("action"), responses["default"])

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = [ADMIN]
    _FakeCustomerConfig.connectors = {}
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    mod._ADMIN_STASH.clear()
    mod._CONFIRMED_STASH.clear()
    mod._READBACK_OWED.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_TAINT._tainted.clear()
    yield mod, requests, responses
    SESSION_TAINT._tainted.clear()


def _turn(mod, sender, session="sess-1", message="Be more formal in client letters."):
    return mod.on_pre_llm_call(session_id=session, sender_id=sender, user_message=message)


def _propose_args(**over):
    args = {
        "scope": "firm_adjust",
        "subject": {"output_class": "outbound", "property": "voice"},
        "text": "In client letters, be more formal and shorter.",
        "instructed_by": ADMIN,
        "source_ref": "msg-41",
    }
    args.update(over)
    return args


def _gate(mod, tool, args, session="sess-1"):
    return mod.on_pre_tool_call(tool_name=tool, session_id=session, args=args)


# ---------------------------------------------------------------------------
# The taint pair
# ---------------------------------------------------------------------------


def test_a_proposal_from_an_untainted_turn_passes(plugin):
    mod, _requests, _ = plugin
    _turn(mod, ADMIN)
    assert _gate(mod, mod.TOOL_PROPOSE, _propose_args()) is None


def test_a_proposal_from_a_tainted_turn_is_refused(plugin):
    mod, _requests, _ = plugin
    SESSION_TAINT.mark("sess-1", "unknown_external")
    _turn(mod, ADMIN)
    verdict = _gate(mod, mod.TOOL_PROPOSE, _propose_args())
    assert verdict["action"] == "block"
    assert "outside the firm" in verdict["message"]


def test_an_unresolvable_taint_state_refuses(plugin, monkeypatch):
    """Fail closed. The cost is that a person re-states a sentence; the cost the
    other way is a standing rule seeded by whoever can send the seat mail."""
    mod, _requests, _ = plugin
    _turn(mod, ADMIN)

    class Broken:
        @staticmethod
        def trust_class(_s):
            raise RuntimeError("register gone")

    monkeypatch.setattr(mod, "SESSION_TAINT", Broken)
    assert _gate(mod, mod.TOOL_PROPOSE, _propose_args())["action"] == "block"


# ---------------------------------------------------------------------------
# Who may state what
# ---------------------------------------------------------------------------


def test_an_unattributed_turn_records_nothing(plugin):
    mod, _requests, _ = plugin
    verdict = _gate(mod, mod.TOOL_PROPOSE, _propose_args(), session="never-seen")
    assert verdict["action"] == "block"
    assert "no verified sender" in verdict["message"]


def test_the_instructor_must_be_the_person_speaking(plugin):
    mod, _requests, _ = plugin
    _turn(mod, ADMIN)
    verdict = _gate(mod, mod.TOOL_PROPOSE, _propose_args(instructed_by=PARALEGAL))
    assert verdict["action"] == "block"


def test_a_personal_rules_subject_must_be_the_speaker(plugin):
    mod, _requests, _ = plugin
    _turn(mod, ADMIN)
    verdict = _gate(
        mod,
        mod.TOOL_PROPOSE,
        _propose_args(scope="person", subject={"person": PARALEGAL}),
    )
    assert verdict["action"] == "block"
    assert "belongs to the person themselves" in verdict["message"]


def test_a_person_may_state_their_own_preference(plugin):
    mod, _requests, _ = plugin
    _turn(mod, PARALEGAL)
    args = _propose_args(scope="person", subject={"person": PARALEGAL}, instructed_by=PARALEGAL)
    assert _gate(mod, mod.TOOL_PROPOSE, args) is None


def test_a_non_admin_may_state_a_firm_rule_only_as_one_that_waits(plugin):
    """The seat decides, not the model. A non-admin proposing a firm rule
    without ``for_admin`` is refused and told the route that works."""
    mod, _requests, _ = plugin
    _turn(mod, PARALEGAL)
    args = _propose_args(instructed_by=PARALEGAL)
    verdict = _gate(mod, mod.TOOL_PROPOSE, args)
    assert verdict["action"] == "block"
    assert "apply that" in verdict["message"]
    assert _gate(mod, mod.TOOL_PROPOSE, dict(args, for_admin=True)) is None


def test_nobody_may_list_somebody_elses_outstanding_rules(plugin):
    mod, _requests, _ = plugin
    _turn(mod, PARALEGAL)
    assert _gate(mod, mod.TOOL_PENDING, {"sender": ADMIN})["action"] == "block"
    assert _gate(mod, mod.TOOL_PENDING, {"sender": PARALEGAL}) is None


def test_only_an_admin_may_ask_for_the_rules_awaiting_an_admin(plugin):
    mod, _requests, _ = plugin
    _turn(mod, PARALEGAL)
    args = {"sender": PARALEGAL, "include_for_admin": True}
    assert _gate(mod, mod.TOOL_PENDING, args)["action"] == "block"
    _turn(mod, ADMIN, session="sess-admin")
    assert _gate(mod, mod.TOOL_PENDING, dict(args, sender=ADMIN), session="sess-admin") is None


# ---------------------------------------------------------------------------
# The readback lock (critique point 1)
# ---------------------------------------------------------------------------


def test_a_send_after_a_proposal_must_carry_the_readback(plugin):
    mod, _requests, _ = plugin
    mod._propose(_propose_args(), session_id="sess-1")
    verdict = mod.on_pre_tool_call(
        tool_name="email_send",
        session_id="sess-1",
        args={"to": ["chris@firm.com"], "body": "I have noted your preference."},
    )
    assert verdict is not None and verdict["action"] == "block"
    assert READBACK in verdict["message"]


def test_a_send_that_carries_it_goes_through_and_clears_the_debt(plugin):
    mod, _requests, _ = plugin
    mod._propose(_propose_args(), session_id="sess-1")
    body = f"Understood. {READBACK}\n\nReply yes to confirm."
    assert (
        mod.on_pre_tool_call(
            tool_name="email_send", session_id="sess-1", args={"to": ["x@y.com"], "body": body}
        )
        is None
    )
    # Cleared: the session is unencumbered once the person has been shown it.
    assert (
        mod.on_pre_tool_call(
            tool_name="email_send", session_id="sess-1", args={"to": ["x@y.com"], "body": "ok"}
        )
        is None
    )


def test_a_paraphrase_does_not_satisfy_the_lock(plugin):
    """The point of the whole mechanism, as a test. A softened readback means
    the person agrees to one sentence and the broker commits another."""
    mod, _requests, _ = plugin
    mod._propose(_propose_args(), session_id="sess-1")
    body = "Rule 7f3a2c1d: client letters should be a bit more formal. Sound right?"
    verdict = mod.on_pre_tool_call(
        tool_name="email_send", session_id="sess-1", args={"to": ["x@y.com"], "body": body}
    )
    assert verdict is not None and verdict["action"] == "block"


def test_a_session_that_proposed_nothing_sends_freely(plugin):
    mod, _requests, _ = plugin
    assert (
        mod.on_pre_tool_call(
            tool_name="email_send", session_id="sess-quiet", args={"to": ["x@y.com"], "body": "hi"}
        )
        is None
    )


def test_a_failed_proposal_owes_nothing(plugin):
    mod, _requests, responses = plugin
    responses["establish_propose"] = {"ok": False, "message": "text must not be empty"}
    mod._propose(_propose_args(text=" "), session_id="sess-1")
    assert (
        mod.on_pre_tool_call(
            tool_name="email_send", session_id="sess-1", args={"to": ["x@y.com"], "body": "hi"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# The wire, and an old broker
# ---------------------------------------------------------------------------


def test_propose_marshals_exactly_the_broker_fields(plugin):
    mod, requests, _ = plugin
    mod._propose(_propose_args(extra="dropped"), session_id="sess-1")
    assert requests[0]["action"] == "establish_propose"
    assert set(requests[0]) == {
        "action",
        "scope",
        "subject",
        "text",
        "instructed_by",
        "source_ref",
        "for_admin",
    }
    assert requests[0]["for_admin"] is False


def test_pending_marshals_exactly_the_broker_fields(plugin):
    mod, requests, _ = plugin
    mod._pending({"sender": ADMIN, "include_for_admin": True, "extra": "dropped"})
    # include_outcomes joins the closed field set at ss-console#2546. It is
    # forwarded rather than defaulted so the seat can ask the broker for rules
    # that ENDED without their author being told; it defaults to false, so the
    # confirmation path keeps seeing only rows a person could still confirm.
    assert set(requests[0]) == {
        "action",
        "sender",
        "include_for_admin",
        "include_outcomes",
        "proposal_id",
    }


@pytest.mark.parametrize(
    "message", ["unsupported broker action", "this broker has no rule store configured; nothing"]
)
def test_an_old_broker_gets_a_sentence_not_an_exception(plugin, message):
    """A seat whose image predates this must report a capability that is not
    there, and then do the person's actual work. A raw protocol error reads as
    a fault and invites a retry loop."""
    mod, _requests, responses = plugin
    responses["establish_propose"] = {"ok": False, "error": "ValueError", "message": message}
    out = mod._propose(_propose_args(), session_id="sess-1")
    assert out == mod._OLD_BROKER_MESSAGE
    assert "nothing was recorded" in out


def test_an_ordinary_refusal_is_still_relayed_verbatim(plugin):
    """Only the two old-broker frames become prose. A real refusal (a ceiling,
    a bad class) is the structured reply the model must read and act on."""
    mod, _requests, responses = plugin
    responses["establish_propose"] = {"ok": False, "message": "property must be one of"}
    out = json.loads(mod._propose(_propose_args(), session_id="sess-1"))
    assert out["ok"] is False
    assert "property must be one of" in out["message"]
