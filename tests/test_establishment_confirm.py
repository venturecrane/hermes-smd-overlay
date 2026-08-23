"""The confirm half of read-back-and-confirm (ss-console#2529).

``shared/rule_confirm`` decides what a reply did; this file is about what the
SEAT does with that decision, which is two things and no others:

* it stashes a confirmed proposal id where the one hook that can block will
  read it, and injects a note telling the model to commit that id and to check
  the install before claiming effect;
* it refuses a submit naming any other id. That is the line between a readback
  that is a control and a readback that is advice: the model may compose the
  reply, but it does not get to decide what the person agreed to.

THE FALSIFIER, run against 349d86b (the parent commit): a "yes" email produces
no stash key at all, because nothing on the seat reads a reply for a
confirmation — ``test_a_yes_email_stashes_the_confirmed_id`` fails with a
KeyError on ``_CONFIRMED_STASH``, and the two refusal tests below fail on an
attribute that does not exist.
"""

from __future__ import annotations

import pytest

from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from tests.conftest import load_plugin

ADMIN = "chris@firm.com"
PARALEGAL = "sarah@firm.com"
RULE = "7f3a2c1d"
OTHER = "0b91ee42"
TEXT = "In client letters, be more formal and shorter."
READBACK = f"[rule {RULE}] {TEXT}"

YES_EMAIL = f"""yes

On Thu, 21 Aug 2026 at 18:04, Operator <ops@firm.com> wrote:
> {READBACK}
>
> Reply yes to confirm.
"""


class _FakeConfig:
    def __init__(self, admins):
        self._admins = admins
        self.connectors: dict = {}

    @property
    def admins(self):
        return list(self._admins)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender):
        return True


class _FakeCustomerConfig:
    admins: list[str] = [ADMIN]

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins)


def _row(proposal_id=RULE, *, instructed_by=ADMIN, for_admin=False, scope="firm_adjust"):
    return {
        "proposal_id": proposal_id,
        "scope": scope,
        "subject": {"output_class": "outbound_client", "property": "voice"},
        "text": TEXT,
        "readback": f"[rule {proposal_id}] {TEXT}",
        "instructed_by": instructed_by,
        "for_admin": for_admin,
    }


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    state: dict = {"pending": [], "requests": []}

    def fake_broker_request(payload):
        state["requests"].append(payload)
        if payload.get("action") == "establish_pending":
            return {"ok": True, "pending": list(state["pending"])}
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = [ADMIN]
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    mod._ADMIN_STASH.clear()
    mod._CONFIRMED_STASH.clear()
    mod._READBACK_OWED.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_TAINT._tainted.clear()
    yield mod, state
    SESSION_TAINT._tainted.clear()


def _turn(mod, sender, message, session="sess-1"):
    return mod.on_pre_llm_call(session_id=session, sender_id=sender, user_message=message)


def _submit_gate(mod, args, session="sess-1"):
    return mod.on_pre_tool_call(tool_name=mod.TOOL_SUBMIT, session_id=session, args=args)


# ---------------------------------------------------------------------------
# The stash and the note
# ---------------------------------------------------------------------------


def test_a_yes_email_stashes_the_confirmed_id(plugin):
    mod, state = plugin
    state["pending"] = [_row()]
    context = _turn(mod, ADMIN, YES_EMAIL)["context"]
    assert mod._CONFIRMED_STASH["sess-1"] == RULE
    assert RULE in context
    assert TEXT in context


def test_the_note_forbids_claiming_effect_before_it_is_observed(plugin):
    """Critique point 4. "Recorded" and "in effect" are different facts, and
    the second one is only sayable after ``establish_status`` says installed."""
    mod, state = plugin
    state["pending"] = [_row()]
    context = _turn(mod, ADMIN, YES_EMAIL)["context"]
    assert mod.TOOL_STATUS in context
    assert "only say it is IN EFFECT once the status says installed" in context
    assert "still converging" in context
    assert "expired" in context


def test_a_stale_confirmation_does_not_survive_the_next_message(plugin):
    """The stash says what THIS message confirmed. A later message that
    confirms nothing must not leave the previous turn's permission standing."""
    mod, state = plugin
    state["pending"] = [_row()]
    _turn(mod, ADMIN, YES_EMAIL)
    assert mod._CONFIRMED_STASH.get("sess-1") == RULE
    state["pending"] = []
    _turn(mod, ADMIN, "Can you pull the Ashton file?")
    assert "sess-1" not in mod._CONFIRMED_STASH


def test_an_ordinary_turn_costs_no_broker_round_trip(plugin):
    """This runs on every attributed turn. A message with no tag and no
    affirmative cannot be an answer, so it never asks the broker."""
    mod, state = plugin
    state["pending"] = [_row()]
    _turn(mod, ADMIN, "Please draft the demand letter for the Ashton matter.")
    assert [r for r in state["requests"] if r["action"] == "establish_pending"] == []


def test_a_declined_rule_says_so_and_commits_nothing(plugin):
    mod, state = plugin
    state["pending"] = [_row()]
    context = _turn(mod, ADMIN, f"no, leave it as it is [rule {RULE}]")["context"]
    assert "did NOT agree" in context
    assert "sess-1" not in mod._CONFIRMED_STASH


def test_an_ambiguous_answer_asks(plugin):
    mod, state = plugin
    state["pending"] = [_row(), _row(OTHER)]
    context = _turn(mod, ADMIN, "yes, thanks")["context"]
    assert RULE in context and OTHER in context
    assert "sess-1" not in mod._CONFIRMED_STASH


def test_a_broker_that_cannot_answer_costs_the_turn_nothing(plugin, monkeypatch):
    """Best-effort by contract. An unrecognized confirmation means the person
    is asked again; a raised exception means a turn that fails because somebody
    said yes."""
    mod, _state = plugin

    def broken(_payload):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(mod, "_broker_request", broken)
    result = _turn(mod, ADMIN, YES_EMAIL)
    assert result is not None and "context" in result
    assert "sess-1" not in mod._CONFIRMED_STASH


# ---------------------------------------------------------------------------
# The gate: only the id the SEAT saw confirmed
# ---------------------------------------------------------------------------


def test_a_confirmed_rule_may_be_committed(plugin):
    mod, state = plugin
    state["pending"] = [_row()]
    _turn(mod, ADMIN, YES_EMAIL)
    assert _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": RULE}) is None


def test_a_submit_naming_an_unconfirmed_id_is_refused(plugin):
    """The line between a control and advice. The model may compose the reply;
    it does not get to decide what the person agreed to."""
    mod, state = plugin
    state["pending"] = [_row(), _row(OTHER)]
    _turn(mod, ADMIN, YES_EMAIL)
    verdict = _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": OTHER})
    assert verdict["action"] == "block"
    assert "not confirmed on this turn" in verdict["message"]
    assert "Do not tell them it is in effect" in verdict["message"]


def test_a_submit_with_no_confirmation_at_all_is_refused(plugin):
    mod, state = plugin
    state["pending"] = [_row()]
    _turn(mod, ADMIN, "Be more formal in client letters.")
    assert _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": RULE})["action"] == "block"


def test_a_firm_adjust_without_a_proposal_id_is_refused(plugin):
    """There is no route by which a firm-wide rule installs without a person
    having been shown it and having said yes."""
    mod, _state = plugin
    _turn(mod, ADMIN, "x")
    verdict = _submit_gate(
        mod, {"scope": "firm_adjust", "output_class": "outbound_client", "spec_body": TEXT}
    )
    assert verdict["action"] == "block"
    assert mod.TOOL_PROPOSE in verdict["message"]


def test_a_forged_id_confirms_nothing(plugin):
    """A tag quoted out of an old thread, or invented. The stash is the only
    authority, and it holds what the seat decided this turn."""
    mod, state = plugin
    state["pending"] = [_row()]
    _turn(mod, ADMIN, YES_EMAIL)
    assert _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": "deadbeef"})["action"] == (
        "block"
    )


def test_a_non_admin_cannot_commit_a_firm_rule_even_holding_a_confirmation(plugin):
    """Belt and braces over the stash. Every legitimate path to a confirmed
    firm rule runs through an admin, so the gate makes that check itself rather
    than resting entirely on what the earlier hook decided."""
    mod, state = plugin
    state["pending"] = [_row(instructed_by=PARALEGAL)]
    _turn(mod, PARALEGAL, f"yes [rule {RULE}]")
    mod._CONFIRMED_STASH["sess-1"] = RULE  # stipulate the earlier hook was wrong
    assert _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": RULE})["action"] == "block"


def test_an_admin_releases_a_rule_that_was_waiting_for_one(plugin):
    """The non-admin leg, end to end at this seam: the paralegal stated it, the
    admin says "apply that", and the commit carries the admin's authority."""
    mod, state = plugin
    state["pending"] = [_row(instructed_by=PARALEGAL, for_admin=True)]
    context = _turn(mod, ADMIN, f"apply that [rule {RULE}]")["context"]
    assert RULE in context
    assert _submit_gate(mod, {"scope": "firm_adjust", "proposal_id": RULE}) is None


# ---------------------------------------------------------------------------
# The person lane
# ---------------------------------------------------------------------------


def test_a_person_confirms_their_own_preference(plugin):
    mod, state = plugin
    state["pending"] = [
        _row(instructed_by=PARALEGAL, scope="person") | {"subject": {"person": PARALEGAL}}
    ]
    _turn(mod, PARALEGAL, f"yes [rule {RULE}]")
    assert mod._CONFIRMED_STASH.get("sess-1") == RULE
    assert _submit_gate(mod, {"scope": "person", "proposal_id": RULE, "append": True}) is None


def test_a_person_submit_with_an_unconfirmed_id_is_refused(plugin):
    mod, state = plugin
    state["pending"] = [
        _row(instructed_by=PARALEGAL, scope="person") | {"subject": {"person": PARALEGAL}}
    ]
    _turn(mod, PARALEGAL, "Be brief with me.")
    verdict = _submit_gate(mod, {"scope": "person", "proposal_id": RULE})
    assert verdict["action"] == "block"


def test_the_direct_person_submit_is_untouched(plugin):
    """The pre-2529 path stands: a proposal is offered on this scope, not
    required."""
    mod, _state = plugin
    _turn(mod, PARALEGAL, "Be brief with me.")
    assert _submit_gate(mod, {"scope": "person", "person": PARALEGAL}) is None
    assert _submit_gate(mod, {"scope": "person", "person": ADMIN})["action"] == "block"
