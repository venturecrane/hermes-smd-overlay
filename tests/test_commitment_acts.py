"""A commitment an Operator administrator confirmed in writing (ss-console
operator-own-matter).

WHAT THIS FILE IS ABOUT. The Operator can now create the firm's internal matter
itself, on one condition: an administrator of the firm read a sentence and
answered it. Everything below is an attempt to make that condition fail. The
invariants each test tries to break:

* it is never autonomous. Nothing proposes and nothing executes except behind an
  administrator's own words;
* it may only be PROPOSED on a turn an administrator opened by email. A Telegram
  message, a cron wake, a turn nobody can be named for, and a colleague who is
  not an administrator all leave no trace: no broker row, no register entry,
  nothing a later "yes" could land on;
* what is proposed is the block the FIRM authored, never the arguments the model
  composed;
* one outstanding question at a time, across rules, acts, and withheld sends,
  because a bare "yes" is worth nothing when two things are waiting on it;
* what executes is the stored payload, not whatever the model composes on the
  confirming turn;
* the approval is spent once, and the ledger row names the person and the
  message that authorized it.

THE FALSIFIER, run against 991044a (the parent commit): every gate test below
fails there, because ``Ceiling.CONFIRM`` has no branch for a COMMITMENT and the
call is simply REFUSED. There is no proposal, no read-back, no approval, and
``shared.pending_acts`` does not exist, so the register tests fail on the import.
"""

from __future__ import annotations

import json

import pytest

from shared import act_broker, customer_config, rule_confirm
from shared.action_classes import ActionClass, classify_tool
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT, InboundOrigin
from shared.pending_acts import PENDING_ACTS, ConfirmedAct
from shared.pending_send import PENDING_SEND
from tests.conftest import load_plugin

ADMIN = "christa@firm.com"
PARALEGAL = "sarah@firm.com"
TOOL = "mcp_smokeball_create_matter"
OTHER_COMMITMENT = "mcp_clio_oktopeak_create_matter"
SESSION = "sess-act"
PROPOSAL = "5c1d9f02"
MESSAGE_ID = "msg-inbound-1"

#: The block the firm authored, whole. The model never supplies any of it. This
#: is what is proposed and what is committed: six keys, names included, because
#: the names are what the administrator reads and therefore part of what they
#: agreed to.
AUTHORED = {
    "description": "Operator Library",
    "matter_type_id": "42cc724c-f046-451c-8452-4284f7a82b66_CA",
    "client_contact_id": "0ac0f746-bf92-462d-b4e5-d133070314fa",
    "number": "OPS-OPERATOR-LIBRARY",
    "client_contact_name": "Ashton and Price",
    "matter_type_name": "Personal Injury - Plaintiff",
}

#: What the TOOL is called with: the same block minus the two authored names,
#: which are read-back labels and not fields the connector has.
ARGS = {
    "description": "Operator Library",
    "matter_type_id": "42cc724c-f046-451c-8452-4284f7a82b66_CA",
    "client_contact_id": "0ac0f746-bf92-462d-b4e5-d133070314fa",
    "number": "OPS-OPERATOR-LIBRARY",
}

READBACK = (
    f'[act {PROPOSAL}] Create Smokeball matter "Operator Library" '
    "(number OPS-OPERATOR-LIBRARY; client: Ashton and Price; "
    'type: Personal Injury - Plaintiff). Reply "yes, create it" to proceed.'
)

YES_EMAIL = f"""yes, create it

On Fri, 21 Aug 2026 at 09:12, Operator <ops@firm.com> wrote:
> {READBACK}
"""


class _FakeConfig:
    """The trusted customer.yaml, as the two readers see it."""

    def __init__(self, admins: list[str], authored: dict | None) -> None:
        self._admins = [a.lower() for a in admins]
        self._authored = authored
        self.connectors: dict = {}

    @property
    def admins(self) -> list[str]:
        return list(self._admins)

    def sender_is_admin(self, sender: object) -> bool:
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender: object) -> bool:
        return True

    @property
    def raw(self) -> dict:
        if self._authored is None:
            return {"self_initiation": {"document_library": {}}}
        return {"self_initiation": {"document_library": {"operator_matter": self._authored}}}


class _FakeCustomerConfig:
    admins: list[str] = [ADMIN]
    authored: dict | None = None

    @classmethod
    def from_volume(cls, path=None):  # noqa: ANN001 - mirrors the real signature
        return _FakeConfig(cls.admins, cls.authored)


def _authored_block() -> dict:
    return dict(AUTHORED)


@pytest.fixture(autouse=True)
def _clean_state():
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    _FakeCustomerConfig.admins = [ADMIN]
    _FakeCustomerConfig.authored = _authored_block()
    yield
    PENDING_ACTS.clear()
    PENDING_SEND.clear()
    SESSION_TAINT._tainted.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()


@pytest.fixture
def gate(monkeypatch):
    """The trust gate with a seat that authors ``commitment: confirm``."""
    trust = load_plugin("hermes-smd-trust")
    enforce = trust.enforce
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.COMMITMENT: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "operator")
    monkeypatch.setattr(customer_config, "CustomerConfig", _FakeCustomerConfig)
    calls: list[dict] = []

    def fake_verdict(payload: dict) -> dict:
        calls.append(payload)
        if payload.get("action") == act_broker.ACTION_PROPOSE:
            return {
                "ok": True,
                "proposal_id": PROPOSAL,
                "kind": act_broker.KIND_TOOL_CALL,
                "tool": payload.get("tool"),
                "for_admin": True,
                "expires_at": "2026-08-22T09:12:00Z",
                "readback": READBACK,
            }
        return {"ok": True, "committed": True}

    monkeypatch.setattr(act_broker, "verdict", fake_verdict)
    return trust, enforce, calls


def _admin_turn(session: str = SESSION, sender: str = ADMIN) -> None:
    """Record the verified inbound origin an email turn arrives with."""
    SESSION_INBOUND_ORIGIN.record(
        session, InboundOrigin(sender_address=sender, message_id=MESSAGE_ID, inbox_id="inbox-1")
    )


def _propose_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("action") == act_broker.ACTION_PROPOSE]


def _commit_calls(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("action") == act_broker.ACTION_COMMIT]


def _confirm_on_seat(session: str = SESSION, tool: str = TOOL) -> None:
    """The state the establishment plugin leaves after an administrator's yes."""
    PENDING_ACTS.mark_confirmed(
        session,
        ConfirmedAct(
            proposal_id=PROPOSAL,
            tool=tool,
            payload=dict(AUTHORED),
            instructed_by=ADMIN,
            confirmed_by=ADMIN,
            confirmed_message_id=MESSAGE_ID,
            confirmed_at=1_000_000.0,
        ),
    )


# ---------------------------------------------------------------------------
# The classification this whole file rests on
# ---------------------------------------------------------------------------


def test_creating_a_matter_is_still_a_commitment():
    """Nothing here reclassifies the tool. It is a COMMITMENT before and after."""
    assert classify_tool(TOOL).action_class is ActionClass.COMMITMENT


def test_the_authored_names_are_agreed_to_but_never_passed_on():
    """The two halves of the payload, and the seam between them.

    The names are part of what the administrator agreed to, so they are in the
    payload that is proposed and committed. They are not fields the connector
    has, so they are not in what the tool is called with.
    """
    assert act_broker.tool_arguments(TOOL, AUTHORED) == ARGS
    assert set(AUTHORED) - set(ARGS) == {"client_contact_name", "matter_type_name"}
    # An act tool this module does not know projects to nothing, never through.
    assert act_broker.tool_arguments(OTHER_COMMITMENT, AUTHORED) == {}


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def test_a_second_act_never_supersedes_the_one_already_asked():
    assert PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK) is True
    assert (
        PENDING_ACTS.note_proposed(SESSION, "aaaa1111", TOOL, "[act aaaa1111] something else")
        is False
    )
    assert PENDING_ACTS.peek(SESSION).proposal_id == PROPOSAL


def test_a_confirmation_of_one_tool_does_not_approve_another():
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is not None
    assert PENDING_ACTS.peek_confirmed(SESSION, OTHER_COMMITMENT) is None


def test_a_confirmation_does_not_reach_another_conversation():
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    assert PENDING_ACTS.peek_confirmed("sess-somebody-else", TOOL) is None


def test_an_expired_record_is_not_an_approval(monkeypatch):
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    monkeypatch.setattr(PENDING_ACTS, "_now", lambda: __import__("time").time() + 601)
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is None
    assert PENDING_ACTS.has_open(SESSION) is False


def test_the_stored_payload_cannot_be_mutated_through_the_register():
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    handed_out = PENDING_ACTS.peek_confirmed(SESSION, TOOL)
    handed_out.payload["number"] = "SOMETHING-ELSE"
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL).payload["number"] == "OPS-OPERATOR-LIBRARY"


def test_taking_the_approval_leaves_the_record_for_the_outcome():
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    assert PENDING_ACTS.take_in_flight(SESSION, TOOL) is not None
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is None  # spent
    assert PENDING_ACTS.finish(SESSION, TOOL).proposal_id == PROPOSAL
    assert PENDING_ACTS.has_open(SESSION) is False


# ---------------------------------------------------------------------------
# Proposing: who may, and what gets proposed
# ---------------------------------------------------------------------------


def test_the_authored_act_is_proposed_even_when_the_model_asked_for_another(gate):
    """The model's arguments are not the act. The firm's authored block is."""
    _trust, enforce, calls = gate
    _admin_turn()
    result = enforce.evaluate_tool_call(
        TOOL,
        {
            "description": "Smith v. Jones",
            "number": "2026-0042",
            "client_contact_id": "a-client-of-the-firm",
            "matter_type_id": "whatever-the-model-picked",
        },
        "smd",
        session_id=SESSION,
    )
    assert result is not None and result["action"] == "block"
    assert READBACK in result["message"]
    proposals = _propose_calls(calls)
    assert len(proposals) == 1
    # The authored block WHOLE, names included: the broker renders the sentence
    # the administrator judges from these same keys, so nothing composed in the
    # hook sits between the file and what they read.
    assert proposals[0]["payload"] == AUTHORED
    assert proposals[0]["instructed_by"] == ADMIN
    assert proposals[0]["source_ref"] == MESSAGE_ID
    # Withheld, not done: the register holds a question, not an approval.
    assert PENDING_ACTS.peek(SESSION).confirmed is None


def test_a_seat_with_no_authored_matter_proposes_nothing(gate):
    _trust, enforce, calls = gate
    _FakeCustomerConfig.authored = None
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert "no Operator matter is authored" in result["message"]
    assert _propose_calls(calls) == []
    assert PENDING_ACTS.has_open(SESSION) is False


def test_an_incomplete_authored_block_proposes_nothing(gate):
    """Half a matter is not a matter. Missing the client contact, it cannot go."""
    _trust, enforce, calls = gate
    _FakeCustomerConfig.authored = {"description": "Operator Library"}
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert _propose_calls(calls) == []


def test_a_turn_nobody_opened_by_email_proposes_nothing(gate):
    """Cron, a self-wake, a tool-driven turn: no verified inbound, no proposal."""
    _trust, enforce, calls = gate
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id="sess-cron")
    assert result["action"] == "block"
    assert "opened by email" in result["message"]
    assert _propose_calls(calls) == []
    assert PENDING_ACTS.has_open("sess-cron") is False


def test_a_telegram_turn_proposes_nothing(gate):
    """Telegram records no inbound origin, so it cannot reach the propose path.

    The channel that approves a SEND is deliberately not the channel that
    proposes an ACT: a commitment against the firm's system of record is
    instructed in writing, by a named administrator, on the record.
    """
    _trust, enforce, calls = gate
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id="sess-telegram")
    assert result["action"] == "block"
    assert _propose_calls(calls) == []


def test_a_colleague_who_is_not_an_administrator_proposes_nothing(gate):
    _trust, enforce, calls = gate
    _admin_turn(sender=PARALEGAL)
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert "opened by email" in result["message"]
    assert _propose_calls(calls) == []
    assert PENDING_ACTS.has_open(SESSION) is False


def test_a_withheld_send_blocks_a_new_proposal(gate):
    """One outstanding question at a time, across registers."""
    _trust, enforce, calls = gate
    _admin_turn()
    PENDING_SEND.capture("mcp_agentmail_send_message", {"to": ["x@y.com"]}, {"x@y.com"})
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert "one outstanding question at a time" in result["message"]
    assert _propose_calls(calls) == []


def test_a_second_act_on_the_same_conversation_is_refused(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert "one outstanding question at a time" in result["message"]
    assert len(_propose_calls(calls)) == 1


def test_a_broker_refusal_is_passed_on_and_nothing_is_registered(gate, monkeypatch):
    _trust, enforce, _calls = gate
    monkeypatch.setattr(
        act_broker,
        "verdict",
        lambda payload: {"ok": False, "refused": "that payload is not what this seat authored"},
    )
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert "not what this seat authored" in result["message"]
    assert PENDING_ACTS.has_open(SESSION) is False


def test_an_unreachable_broker_withholds_rather_than_allows(gate, monkeypatch):
    _trust, enforce, _calls = gate

    def boom(payload):
        raise RuntimeError("no socket")

    monkeypatch.setattr(act_broker, "verdict", boom)
    _admin_turn()
    result = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert result["action"] == "block"
    assert PENDING_ACTS.has_open(SESSION) is False


# ---------------------------------------------------------------------------
# Executing: what runs once an administrator has said yes
# ---------------------------------------------------------------------------


def test_the_confirmed_payload_replaces_whatever_the_model_composed(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    _confirm_on_seat()
    live = {"description": "Something Else", "number": "2026-9999", "extra": "injected"}
    assert enforce.evaluate_tool_call(TOOL, live, "smd", session_id=SESSION) is None
    # The stored payload, projected onto the keys the connector accepts: nothing
    # the model added survives, and the two read-back names are not passed on.
    assert live == ARGS


def test_the_approval_is_spent_by_the_call_it_authorized(gate):
    _trust, enforce, _calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    _confirm_on_seat()
    assert enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION) is None
    second = enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    assert second is not None and second["action"] == "block"


def test_a_confirmed_act_does_not_authorize_a_different_commitment(gate):
    _trust, enforce, calls = gate
    _admin_turn()
    enforce.evaluate_tool_call(TOOL, dict(AUTHORED), "smd", session_id=SESSION)
    _confirm_on_seat()
    result = enforce.evaluate_tool_call(
        OTHER_COMMITMENT, {"description": "Operator Library"}, "smd", session_id=SESSION
    )
    assert result is not None and result["action"] == "block"
    assert len(_propose_calls(calls)) == 1  # no second proposal either


def test_an_agent_supplied_approval_flag_is_stripped(gate, monkeypatch):
    """SEC-36: the model cannot approve its own commitment by stamping the args."""
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    _admin_turn()
    result = trust.on_pre_tool_call(
        tool_name=TOOL,
        args={**AUTHORED, "_current_turn_approval": True},
        session_id=SESSION,
        customer_slug="smd",
    )
    assert result is not None and result["action"] == "block"
    assert READBACK in result["message"]  # withheld and put to the administrator
    assert len(_propose_calls(calls)) == 1


# ---------------------------------------------------------------------------
# Committing the row: only on an outcome the vendor actually reported
# ---------------------------------------------------------------------------


def test_a_successful_act_commits_the_row_naming_the_approving_message(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=dict(AUTHORED),
        result=json.dumps({"id": "matter-9911", "number": "OPS-OPERATOR-LIBRARY"}),
        session_id=SESSION,
        status="ok",
    )
    commits = _commit_calls(calls)
    assert len(commits) == 1
    assert commits[0]["proposal_id"] == PROPOSAL
    assert commits[0]["payload"] == AUTHORED
    assert commits[0]["confirmed_by"] == ADMIN
    assert commits[0]["confirmed_message_id"] == MESSAGE_ID
    assert commits[0]["outcome"] == {"ok": True, "ref": "matter-9911"}


def test_a_failed_call_commits_nothing(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _confirm_on_seat()
    PENDING_ACTS.take_in_flight(SESSION, TOOL)
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=dict(AUTHORED),
        result='{"error": "vendor refused"}',
        session_id=SESSION,
        status="error",
    )
    assert _commit_calls(calls) == []
    assert PENDING_ACTS.has_open(SESSION) is False  # and no stale approval survives


def test_a_call_nobody_confirmed_commits_nothing(gate, monkeypatch):
    trust, _enforce, calls = gate
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    trust.on_post_tool_call(
        tool_name=TOOL,
        args=dict(AUTHORED),
        result='{"id": "matter-0001"}',
        session_id=SESSION,
        status="ok",
    )
    assert _commit_calls(calls) == []


# ---------------------------------------------------------------------------
# The answer: who may say yes, and to what
# ---------------------------------------------------------------------------


def _act_row(proposal_id: str = PROPOSAL, *, instructed_by: str = ADMIN) -> dict:
    return {
        "proposal_id": proposal_id,
        "kind": act_broker.KIND_TOOL_CALL,
        "tool": TOOL,
        "payload": dict(AUTHORED),
        "readback": READBACK,
        "instructed_by": instructed_by,
        "for_admin": True,
    }


def _rule_row(proposal_id: str = "0b91ee42") -> dict:
    return {
        "proposal_id": proposal_id,
        "kind": "rule",
        "scope": "firm_adjust",
        "subject": {"output_class": "outbound_client", "property": "voice"},
        "text": "In client letters, be more formal and shorter.",
        "readback": f"[rule {proposal_id}] In client letters, be more formal and shorter.",
        "instructed_by": ADMIN,
        "for_admin": False,
    }


@pytest.fixture
def establishment(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    state: dict = {"pending": [], "requests": []}

    def fake_broker_request(payload):
        state["requests"].append(payload)
        if payload.get("action") == mod.TOOL_PENDING:
            wanted = payload.get("proposal_id")
            rows = list(state["pending"])
            if wanted:
                rows = [r for r in rows if str(r.get("proposal_id")) == str(wanted)]
            return {"ok": True, "pending": rows}
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    mod._ADMIN_STASH.clear()
    mod._CONFIRMED_STASH.clear()
    mod._READBACK_OWED.clear()
    return mod, state


def _turn(mod, sender, message, session=SESSION):
    return mod.on_pre_llm_call(session_id=session, sender_id=sender, user_message=message)


def test_an_administrators_yes_confirms_the_act_and_names_the_call(establishment):
    mod, state = establishment
    state["pending"] = [_act_row()]
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _admin_turn()
    context = _turn(mod, ADMIN, YES_EMAIL)["context"]
    assert f"[act {PROPOSAL}]" in context
    assert TOOL in context
    # The model is told the tool's own arguments, not the whole authored block.
    assert json.dumps(ARGS, ensure_ascii=False, sort_keys=True) in context
    assert "client_contact_name" not in context
    act = PENDING_ACTS.peek_confirmed(SESSION, TOOL)
    assert act is not None
    assert act.payload == AUTHORED
    assert act.confirmed_by == ADMIN
    assert act.confirmed_message_id == MESSAGE_ID
    # An act is not a rule: it must never become submittable as one.
    assert SESSION not in mod._CONFIRMED_STASH


def test_an_administrator_may_answer_on_a_later_conversation(establishment):
    """The ordinary case. The read-back went out on one turn; the reply is a new
    inbound with its own session, and the broker row is what carries the act."""
    mod, state = establishment
    state["pending"] = [_act_row()]
    _admin_turn(session="sess-later")
    _turn(mod, ADMIN, YES_EMAIL, session="sess-later")
    assert PENDING_ACTS.peek_confirmed("sess-later", TOOL) is not None


def test_a_colleagues_yes_confirms_nothing(establishment):
    mod, state = establishment
    state["pending"] = [_act_row()]
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _admin_turn(sender=PARALEGAL)
    context = _turn(mod, PARALEGAL, YES_EMAIL)["context"]
    assert "only an Operator administrator" in context
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is None
    assert PENDING_ACTS.peek(SESSION).confirmed is None


def test_a_bare_yes_with_a_rule_and_an_act_open_asks_which(establishment):
    mod, state = establishment
    state["pending"] = [_act_row(), _rule_row()]
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _admin_turn()
    context = _turn(mod, ADMIN, "yes")["context"]
    assert "Ask which" in context
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is None
    assert SESSION not in mod._CONFIRMED_STASH


def test_naming_the_act_resolves_the_ambiguity(establishment):
    mod, state = establishment
    state["pending"] = [_act_row(), _rule_row()]
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _admin_turn()
    _turn(mod, ADMIN, f"[act {PROPOSAL}] yes, create it")
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is not None


def test_a_qualified_yes_confirms_nothing(establishment):
    mod, state = establishment
    state["pending"] = [_act_row()]
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    _admin_turn()
    context = _turn(mod, ADMIN, f"[act {PROPOSAL}] yes but call it something else")["context"]
    assert "change or a condition" in context
    assert PENDING_ACTS.peek_confirmed(SESSION, TOOL) is None


def test_an_affirmative_with_only_a_withheld_send_open_releases_nothing(establishment):
    mod, state = establishment
    state["pending"] = []
    PENDING_SEND.capture("mcp_agentmail_send_message", {"to": ["x@y.com"]}, {"x@y.com"})
    _admin_turn()
    context = _turn(mod, ADMIN, "yes")["context"]
    assert "withheld for approval" in context
    assert PENDING_SEND.peek().approved is False


# ---------------------------------------------------------------------------
# The read-back lock: the reply has to carry the sentence
# ---------------------------------------------------------------------------


def test_the_proposing_turn_cannot_send_without_the_act_readback(establishment):
    mod, _state = establishment
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    verdict = mod.on_pre_tool_call(
        tool_name="mcp_agentmail_send_message",
        session_id=SESSION,
        args={"to": ["christa@firm.com"], "text": "I will go and create that matter now."},
    )
    assert verdict is not None and verdict["action"] == "block"
    assert READBACK in verdict["message"]


def test_a_reply_carrying_the_act_readback_goes_out(establishment):
    mod, _state = establishment
    PENDING_ACTS.note_proposed(SESSION, PROPOSAL, TOOL, READBACK)
    assert (
        mod.on_pre_tool_call(
            tool_name="mcp_agentmail_send_message",
            session_id=SESSION,
            args={"to": ["christa@firm.com"], "text": f"Happy to. {READBACK}"},
        )
        is None
    )
    assert PENDING_ACTS.proposed(SESSION) == []  # the debt is settled


# ---------------------------------------------------------------------------
# The matcher itself
# ---------------------------------------------------------------------------


def test_the_tag_matcher_reads_both_kinds():
    assert rule_confirm.find_tags(f"[act {PROPOSAL}] yes") == (PROPOSAL,)
    assert rule_confirm.find_tags("[rule 0b91ee42] yes") == ("0b91ee42",)


def test_only_an_administrator_may_confirm_an_act():
    row = _act_row()
    assert rule_confirm.resolve(f"[act {PROPOSAL}] yes", [row], ADMIN, True).kind == "confirmed"
    verdict = rule_confirm.resolve(f"[act {PROPOSAL}] yes", [row], PARALEGAL, False)
    assert verdict.kind == "ask"
    assert verdict.reason == rule_confirm.ASK_NOT_THEIRS
