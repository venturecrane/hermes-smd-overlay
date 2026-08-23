"""The commit half of read-back-and-confirm (ss-console#2529).

Two controls, both authored from one live run on the pilot seat
(2026-08-21T23:30Z, overlay 07ed486), in which a person confirmed
``[rule 811e5a68]`` and the firm was then told the rule was in effect while
nothing had been committed and the seat's preference manifest was empty.

* **The commit carries the ID and nothing describing the rule.** The model
  called ``establish_submit`` five times; the broker refused all five, because
  the plugin forwarded the model's paraphrase in ``spec_body`` and the broker
  refuses a submit that restates the confirmed sentence (console
  ``operator/workspace_broker/establishment.py`` ``_refuse_restated``). The
  refusal was correct and unfixable from the model's side: the text was never
  its to change. So the field stops going on the wire.
* **"In effect" is a gate, not a paragraph.** The instruction forbidding that
  sentence until an install is observed was already in ``_CONFIRMED_NOTE`` and
  did not hold. An instruction that has failed on the surface it exists to
  protect does not get louder; it gets a seam.

THE FALSIFIER, run against origin/main (b782926):

* ``test_a_confirmed_commit_sends_only_the_id`` fails on the forwarded
  ``spec_body`` -- ``AssertionError: assert 'spec_body' not in {...}`` -- which
  is exactly the byte the broker refused.
* ``test_a_false_in_effect_reply_is_blocked`` fails with the gate returning
  ``None``: on main the false claim ships.
"""

from __future__ import annotations

import json

import pytest

from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from tests.conftest import load_plugin

ADMIN = "chris@firm.com"
PERSON = "sarah@firm.com"
RULE = "811e5a68"
TEXT = "In client letters, be more formal and shorter."
READBACK = f"[rule {RULE}] {TEXT}"
PARAPHRASE = "Client letters should read a bit more formally from here on."

YES_EMAIL = f"""yes

On Thu, 21 Aug 2026 at 23:04, Operator <ops@firm.com> wrote:
> {READBACK}
>
> Reply yes to confirm.
"""

REFUSAL = (
    "spec_body does not match rule 811e5a68 as it was proposed and confirmed; "
    "the committed rule comes from the proposal, not from this request"
)


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


def _row(*, scope="firm_adjust", instructed_by=ADMIN, subject=None):
    return {
        "proposal_id": RULE,
        "scope": scope,
        "subject": subject or {"output_class": "outbound_client", "property": "voice"},
        "text": TEXT,
        "readback": READBACK,
        "instructed_by": instructed_by,
        "for_admin": False,
    }


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    state: dict = {"pending": [], "requests": [], "submit": {"ok": True, "run_id": "run-1"}}

    def fake_broker_request(payload):
        state["requests"].append(payload)
        if payload.get("action") == "establish_pending":
            return {"ok": True, "pending": list(state["pending"])}
        if payload.get("action") == mod.TOOL_SUBMIT:
            return state["submit"]
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = [ADMIN]
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    for register in (
        mod._ADMIN_STASH,
        mod._CONFIRMED_STASH,
        mod._READBACK_OWED,
        mod._SUBMIT_RUNS,
        mod._INSTALLED_RULES,
        mod._LAST_SUBMIT_REFUSAL,
    ):
        register.clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_TAINT._tainted.clear()
    yield mod, state
    SESSION_TAINT._tainted.clear()


def _confirm(mod, state, *, sender=ADMIN, scope="firm_adjust", session="sess-1"):
    """Put the session in the state the pilot was in: one rule, just confirmed."""
    subject = {"person": sender} if scope == "person" else None
    state["pending"] = [_row(scope=scope, instructed_by=sender, subject=subject)]
    mod.on_pre_llm_call(session_id=session, sender_id=sender, user_message=YES_EMAIL)
    assert mod._CONFIRMED_STASH.get(session) == RULE
    state["pending"] = []


def _send(mod, body, session="sess-1"):
    return mod.on_pre_tool_call(
        tool_name="email_send", session_id=session, args={"to": [ADMIN], "body": body}
    )


# ---------------------------------------------------------------------------
# The wire: a confirmed commit names the rule and does not describe it
# ---------------------------------------------------------------------------


def test_a_confirmed_commit_sends_only_the_id(plugin):
    """The live defect, as a test. The model wrote its own sentence; the broker
    refused; the sentence never reaches the broker now."""
    mod, state = plugin
    mod._submit(
        {
            "scope": "firm_adjust",
            "proposal_id": RULE,
            "phase": "install",
            "spec_body": PARAPHRASE,
            "text": PARAPHRASE,
            "output_class": "outbound_client",
            "property": "voice",
            "instructed_by": ADMIN,
            "source_ref": "msg-41",
        },
        session_id="sess-1",
    )
    sent = state["requests"][0]
    assert set(sent) == {
        "action",
        "scope",
        "proposal_id",
        "instructed_by",
        "source_ref",
        "append",
    }
    assert sent["proposal_id"] == RULE
    for described in ("spec_body", "text", "person", "output_class", "property"):
        assert described not in sent


def test_a_confirmed_personal_commit_sends_only_the_id(plugin):
    """Same on the person lane: the broker takes the subject and the body from
    the row (console ``_submit_person``), so a supplied ``person`` is one more
    value that can disagree with it."""
    mod, state = plugin
    mod._submit(
        {
            "scope": "person",
            "proposal_id": RULE,
            "phase": "install",
            "person": PERSON,
            "spec_body": PARAPHRASE,
            "append": True,
            "instructed_by": PERSON,
            "source_ref": "msg-9",
        },
        session_id="sess-1",
    )
    sent = state["requests"][0]
    assert "person" not in sent and "spec_body" not in sent
    assert sent["append"] is True


def test_the_direct_person_submit_still_carries_its_own_body(plugin):
    """No proposal, no row to source from. The pre-2529 path is untouched."""
    mod, state = plugin
    mod._submit(
        {
            "scope": "person",
            "person": PERSON,
            "phase": "install",
            "spec_body": "Bullets. Short emails.",
            "instructed_by": PERSON,
            "source_ref": "msg-9",
        },
        session_id="sess-1",
    )
    sent = state["requests"][0]
    assert sent["person"] == PERSON
    assert sent["spec_body"] == "Bullets. Short emails."


def test_a_firm_corpus_run_keeps_its_staged_payload(plugin):
    """Narrowing binds on the scopes that HAVE a pending row. A staged firm
    establishment has none, so a stray id must not empty the submission."""
    mod, state = plugin
    mod._submit(
        {
            "staging_id": "set-1",
            "phase": "install",
            "output_class": "work_product",
            "property": "voice",
            "spec_body": "Plain sentences.",
            "proposal_id": RULE,
            "instructed_by": ADMIN,
            "source_ref": "msg-1",
        },
        session_id="sess-1",
    )
    sent = state["requests"][0]
    assert sent["spec_body"] == "Plain sentences."
    assert sent["staging_id"] == "set-1"


def test_a_refused_commit_tells_the_model_not_to_rewrite_it(plugin):
    """Five submits, five refusals, each with fresh wording. The tool result now
    says the wording is not the thing that failed."""
    mod, state = plugin
    state["submit"] = {"ok": False, "message": REFUSAL}
    out = json.loads(
        mod._submit(
            {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
            session_id="sess-1",
        )
    )
    assert out["ok"] is False
    assert out["message"] == REFUSAL  # the broker's verdict, verbatim
    assert "NOT submit again with different wording" in out["seat_note"]
    assert mod._LAST_SUBMIT_REFUSAL["sess-1"] == REFUSAL


# ---------------------------------------------------------------------------
# The gate: "in effect" is said after it is observed
# ---------------------------------------------------------------------------


def test_a_false_in_effect_reply_is_blocked(plugin):
    """The pilot's last beat. Nothing committed, no status read, and the firm
    told the rule was live."""
    mod, state = plugin
    _confirm(mod, state)
    state["submit"] = {"ok": False, "message": REFUSAL}
    mod._submit(
        {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
        session_id="sess-1",
    )
    verdict = _send(mod, f"Rule {RULE} is in effect. Client letters will read formally.")
    assert verdict is not None and verdict["action"] == "block"
    assert RULE in verdict["message"]
    assert REFUSAL in verdict["message"]  # the reply has to say WHY
    assert mod.TOOL_STATUS in verdict["message"]


def test_the_honest_refusal_reply_goes_out(plugin):
    """The gate names the reply it wants; that reply must not then be blocked,
    or the model learns to say nothing at all."""
    mod, state = plugin
    _confirm(mod, state)
    state["submit"] = {"ok": False, "message": REFUSAL}
    mod._submit(
        {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
        session_id="sess-1",
    )
    body = (
        f"You confirmed the rule [rule {RULE}], but it could not be committed. "
        f"The seat refused it: {REFUSAL}. It is not in force, and I have not "
        "changed anything."
    )
    assert _send(mod, body) is None


def test_the_still_converging_reply_goes_out(plugin):
    """``_CONFIRMED_NOTE`` asks for exactly this sentence on an accepted run.
    Hedged, so it is a promise rather than a claim."""
    mod, state = plugin
    _confirm(mod, state)
    body = (
        f"Recorded. The rule [rule {RULE}] will be in effect within a minute "
        "and I will confirm when it is."
    )
    assert _send(mod, body) is None


def test_an_observed_install_unlocks_the_claim(plugin):
    """The whole point is not silence. Once the seat has SEEN it install, the
    sentence is true and ships."""
    mod, state = plugin
    _confirm(mod, state)
    mod._submit(
        {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
        session_id="sess-1",
    )
    mod.on_post_tool_call(
        tool_name=mod.TOOL_STATUS,
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(
            {
                "ok": True,
                "run_id": "run-1",
                "status": "complete",
                "result": {"status": "installed", "scope": "firm_adjust", "adjustment_id": RULE},
            }
        ),
    )
    assert _send(mod, f"Rule {RULE} is now in effect.") is None


def test_a_person_install_is_observed_through_the_run_id(plugin):
    """A personal preference's result names the person and the digest and
    nothing about the proposal, so the run id recorded at submit is the only
    link back to the rule."""
    mod, state = plugin
    _confirm(mod, state, sender=PERSON, scope="person")
    mod._submit(
        {"scope": "person", "proposal_id": RULE, "instructed_by": PERSON},
        session_id="sess-1",
    )
    mod.on_post_tool_call(
        tool_name=mod.TOOL_STATUS,
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(
            {
                "ok": True,
                "run_id": "run-1",
                "status": "complete",
                "result": {"status": "installed", "scope": "person", "person": PERSON},
            }
        ),
    )
    assert _send(mod, f"Done: the rule [rule {RULE}] is in effect for you.") is None


def test_a_run_still_converging_does_not_unlock_the_claim(plugin):
    """``accepted_pending_install`` is not ``installed``. The honest sentence
    for that state is the hedged one, which passes; this one does not."""
    mod, state = plugin
    _confirm(mod, state)
    mod._submit(
        {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
        session_id="sess-1",
    )
    mod.on_post_tool_call(
        tool_name=mod.TOOL_STATUS,
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(
            {
                "ok": True,
                "run_id": "run-1",
                "status": "complete",
                "result": {"status": "accepted_pending_install", "adjustment_id": RULE},
            }
        ),
    )
    assert _send(mod, f"Rule {RULE} is in effect.")["action"] == "block"


def test_an_already_committed_refusal_is_the_seat_saying_it_is_in_force(plugin):
    """The one refusal that is a report of success. Reading it any other way
    would leave the gate blocking a true sentence for the rest of the session,
    with the model quoting the refusal that names the effect it may not claim."""
    mod, state = plugin
    _confirm(mod, state)
    state["submit"] = {
        "ok": False,
        "message": f"rule {RULE} was already committed; it is in effect",
    }
    mod._submit(
        {"scope": "firm_adjust", "proposal_id": RULE, "instructed_by": ADMIN},
        session_id="sess-1",
    )
    assert _send(mod, f"Rule {RULE} is in effect.") is None


def test_a_session_that_confirmed_nothing_is_not_gated(plugin):
    """Narrow by construction. A reply about a firm rule on an ordinary turn is
    none of this gate's business."""
    mod, _state = plugin
    assert _send(mod, "The rule about formal letters is in effect.", session="sess-quiet") is None


def test_a_reply_that_names_no_rule_is_not_gated(plugin):
    """The anchor half. "From now on" about the work in front of the model is
    not a claim about a standing rule."""
    mod, state = plugin
    _confirm(mod, state)
    assert _send(mod, "From now on I will send the Ashton updates on Fridays.") is None


def test_a_reply_that_claims_nothing_goes_out(plugin):
    mod, state = plugin
    _confirm(mod, state)
    assert _send(mod, f"Understood on [rule {RULE}]. I am working on it now.") is None


def test_the_claim_survives_being_split_across_the_body(plugin):
    """The phrase and the anchor need only share the message, because that is
    the unit the person reads."""
    mod, state = plugin
    _confirm(mod, state)
    body = f"I have applied what you asked for.\n\nReference: [rule {RULE}]."
    assert _send(mod, body)["action"] == "block"


def test_a_will_belonging_to_another_verb_does_not_launder_the_claim(plugin):
    """The hedge window is one short clause, not a sentence. "I will write to
    you again" earlier in the same breath does not make a flat "is in effect"
    into a promise."""
    mod, state = plugin
    _confirm(mod, state)
    body = f"I will write again shortly. The rule [rule {RULE}] is in effect."
    assert _send(mod, body)["action"] == "block"


def test_the_rules_own_sentence_is_quoted_not_asserted(plugin):
    """A rule may say "from now on" in its own body. Reading the sentence the
    person confirmed as the model's claim about that sentence would block every
    reply that quotes it back, which is most honest ones."""
    mod, state = plugin
    state["pending"] = [
        _row() | {"text": "From now on, client letters are formal.", "readback": READBACK}
    ]
    mod.on_pre_llm_call(session_id="sess-1", sender_id=ADMIN, user_message=YES_EMAIL)
    assert mod._CONFIRMED_STASH.get("sess-1") == RULE
    state["pending"] = []
    body = (
        f'You confirmed [rule {RULE}]: "From now on, client letters are '
        'formal." I am recording it now and will confirm when it is live.'
    )
    assert _send(mod, body) is None


def test_the_gate_reads_the_subject_line_too(plugin):
    """The args blob, not one named field: a claim in a subject is a claim."""
    mod, state = plugin
    _confirm(mod, state)
    verdict = mod.on_pre_tool_call(
        tool_name="email_send",
        session_id="sess-1",
        args={
            "to": [ADMIN],
            "subject": f"Rule {RULE} is now in effect",
            "body": "See below.",
        },
    )
    assert verdict is not None and verdict["action"] == "block"
