"""The rule-request loop closing (ss-console#2546).

WHAT WAS BROKEN, restated because every test here is about one of the silences:
under ss-console#2529 a person who is NOT an Operator admin could state a
firm-level rule and be told an admin could apply it. No admin was told. An
admin's "no" did nothing. A rule nobody answered was deleted and its author was
never told it had lapsed.

THE FOUR THINGS THESE TESTS PIN:

1. a paralegal's rule is EMAILED to the administrators the firm named on
   ``scope.rule_requests_to``, deterministically, from the tool handler and not
   by the model deciding to send. Administrators NOT named receive nothing, and
   the negative is asserted directly rather than implied;
2. every one of those sends goes through the seat's own gate, so a refusal
   produces an HONEST note. The failure this forecloses is the Operator saying
   an administrator was asked when nothing left the building, which is worse
   than the original silence;
3. a decline is an explicit refusal, over exactly one rule, by an administrator
   who is not the person who asked. "Wait, which letters?" is a question;
4. an operations request reaches SMD or the Operator says it did not.

THE FALSIFIER for the file, run at origin/main (241df3b): every test that
touches new behaviour fails on an AttributeError or an ImportError for a symbol
that does not exist there, and the handful that pin PRESERVED behaviour pass on
both sides, which is what they are for.
"""

from __future__ import annotations

import json

import pytest

from shared import operations_request as ops_request
from shared import rule_confirm as rc
from shared import rule_dispatch, send_dispatch
from shared.customer_config import CustomerConfig
from shared.inbound import SESSION_INBOUND_ORIGIN, SESSION_TAINT
from tests.conftest import load_plugin

ADMIN = "christa@firm.com"
OTHER_ADMIN = "chris@firm.com"
PARALEGAL = "sarah@firm.com"
RULE = "7f3a2c1d"
TEXT = "In client letters, no pleasantries; keep that."


class _Result:
    """A send outcome, shaped like ``shared.send_dispatch.DispatchResult``."""

    def __init__(self, sent=True, reason="", message_id="msg-1"):
        self.sent = sent
        self.reason = reason
        self.message_id = message_id
        self.recipients = ()


class _Recorder:
    """A send double that records every call and answers a fixed way."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self._result = result or _Result()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self._result

    @property
    def recipients(self) -> list[str]:
        return [a for call in self.calls for a in call.get("to", [])]

    @property
    def copied(self) -> list[str]:
        return [a for call in self.calls for a in call.get("cc", [])]


# ---------------------------------------------------------------------------
# 1. the request reaches the named administrators, and nobody else
# ---------------------------------------------------------------------------


def test_a_paralegals_rule_is_emailed_to_the_named_administrators():
    send = _Recorder()
    result = rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[ADMIN],
        send=send,
    )

    assert result.sent is True
    assert send.recipients == [ADMIN]
    # The requester is copied so they can see that it was asked, by name, and so
    # the thread the administrator answers on is the one they started.
    assert send.copied == [PARALEGAL]
    body = send.calls[0]["text"]
    assert f"[rule {RULE}] {TEXT}" in body
    assert '"apply that"' in body and '"no"' in body
    # The tag rides the SUBJECT too: reply chains vary wildly in what they
    # quote, and an answer with no tag cannot be bound to a rule.
    assert f"[rule {RULE}]" in send.calls[0]["subject"]


def test_an_administrator_the_firm_did_not_name_receives_nothing():
    """THE PRIVILEGE ASSERTION, and it is a negative, so it is asserted directly
    rather than implied by the positive above. Authority and traffic are
    separate lists precisely so a partner is not paged for every request; if
    this sent to ``admins`` the split would be decorative."""
    send = _Recorder()
    rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[ADMIN],
        send=send,
    )
    assert OTHER_ADMIN not in send.recipients
    assert OTHER_ADMIN not in send.copied


def test_the_note_names_who_was_asked_and_forbids_a_second_send():
    send = _Recorder()
    note = rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[ADMIN, OTHER_ADMIN],
        send=send,
    ).note
    assert ADMIN in note and OTHER_ADMIN in note
    assert "ALREADY" in note
    assert "do not call a send tool" in note


def test_a_refused_send_produces_an_honest_note_and_no_audit_row():
    """The failure this whole feature has to survive. An Operator that says an
    administrator was asked, when the gate refused the send, is worse than the
    silence the issue set out to fix: the person stops waiting."""
    send = _Recorder(_Result(sent=False, reason="Refused: this turn read outside content"))
    rows: list[dict] = []
    result = rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[ADMIN],
        send=send,
        emit=lambda **kw: rows.append(kw),
    )
    assert result.sent is False
    assert "COULD NOT" in result.note
    assert "outside content" in result.note
    assert "forward it to an administrator themselves" in result.note
    assert "Do not say an administrator was asked" in result.note
    assert rows == []


def test_a_firm_that_named_nobody_is_said_so_rather_than_implied():
    send = _Recorder()
    result = rule_dispatch.notify_admins(
        proposal_id=RULE, text=TEXT, requester=PARALEGAL, rule_requests_to=[], send=send
    )
    assert result.sent is False
    assert send.calls == []
    assert "names nobody to receive rule requests" in result.note
    assert "Do not claim anyone was asked" in result.note


def test_the_audit_row_names_who_was_notified_and_never_the_rule():
    """Same posture as RULE_PROPOSED: the ledger keeps who and which, not what.
    The row exists at all because it answers a question the send's own row
    cannot: a specific request reached the people authorized to answer it."""
    rows: list[dict] = []
    rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[ADMIN, OTHER_ADMIN],
        send=_Recorder(),
        emit=lambda **kw: rows.append(kw),
        session_id="sess-1",
    )
    assert len(rows) == 1
    assert rows[0]["action_type"] == rule_dispatch.RULE_REQUEST_NOTIFIED
    metadata = rows[0]["metadata"]
    assert metadata["notified_to"] == [ADMIN, OTHER_ADMIN]
    assert metadata["notified_count"] == 2
    assert metadata["instructed_by"] == PARALEGAL
    assert TEXT not in str(metadata)


def test_the_fan_out_is_bounded():
    """A single inbound message must not be a send amplifier."""
    send = _Recorder()
    rule_dispatch.notify_admins(
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        rule_requests_to=[f"a{i}@firm.com" for i in range(50)],
        send=send,
    )
    assert len(send.recipients) == rule_dispatch.MAX_NOTIFIED


# ---------------------------------------------------------------------------
# 2. the three outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("installed", "in effect from now"),
        ("declined", "declined the rule you asked for"),
        ("lapsed", "No administrator answered"),
    ],
)
def test_each_outcome_says_which_thing_happened(kind, needle):
    """Three sentences rather than one parameterised sentence: the three are
    different news, and a person must know which without parsing a clause."""
    send = _Recorder()
    result = rule_dispatch.notify_outcome(
        kind=kind,
        proposal_id=RULE,
        text=TEXT,
        requester=PARALEGAL,
        by=ADMIN,
        send=send,
    )
    assert result.sent is True
    assert send.recipients == [PARALEGAL]
    assert needle in send.calls[0]["text"]
    assert f"[rule {RULE}] {TEXT}" in send.calls[0]["text"]


def test_an_outcome_goes_to_the_person_who_asked_and_to_nobody_else():
    send = _Recorder()
    rule_dispatch.notify_outcome(
        kind="declined", proposal_id=RULE, text=TEXT, requester=PARALEGAL, by=ADMIN, send=send
    )
    assert send.recipients == [PARALEGAL]
    assert send.copied == []


def test_a_refused_outcome_send_reports_not_sent_so_the_row_stays_unmarked():
    """The ordering the caller depends on: the broker is marked only after this
    says sent, so a failed send retries rather than going silent."""
    send = _Recorder(_Result(sent=False, reason="transport down"))
    result = rule_dispatch.notify_outcome(
        kind="lapsed", proposal_id=RULE, text=TEXT, requester=PARALEGAL, send=send
    )
    assert result.sent is False
    assert result.reason == "transport down"


# ---------------------------------------------------------------------------
# 3. scope.rule_requests_to
# ---------------------------------------------------------------------------


def _cfg(data: dict) -> CustomerConfig:
    config = CustomerConfig.__new__(CustomerConfig)
    config._data = data
    return config


def test_routing_is_read_as_the_subset_of_admins_it_is_authored_as():
    config = _cfg({"scope": {"admins": [ADMIN, OTHER_ADMIN], "rule_requests_to": [ADMIN]}})
    assert config.rule_requests_to == [ADMIN]


def test_a_routed_address_that_is_not_an_admin_is_dropped():
    """The runtime backstop for the console validator. A person asked to answer
    a question they cannot answer would also be a send the broker's own
    recipient fence refuses, so the request would reach nobody and nothing
    would say so."""
    config = _cfg({"scope": {"admins": [ADMIN], "rule_requests_to": [ADMIN, PARALEGAL]}})
    assert config.rule_requests_to == [ADMIN]


def test_unauthored_routing_is_empty_rather_than_every_admin():
    """Fail-closed in the HONEST direction: nobody is emailed, and the caller's
    contract is then to say so. Defaulting to every admin would page a partner
    for every request, which is the thing the key exists to prevent."""
    assert _cfg({"scope": {"admins": [ADMIN]}}).rule_requests_to == []
    assert _cfg({"scope": {"admins": [ADMIN], "rule_requests_to": "nope"}}).rule_requests_to == []
    assert _cfg({}).rule_requests_to == []


# ---------------------------------------------------------------------------
# 4. an explicit decline, and the questions that are not one
# ---------------------------------------------------------------------------


def _pending_row(*, instructed_by=PARALEGAL, for_admin=True, proposal_id=RULE):
    return {
        "proposal_id": proposal_id,
        "scope": "firm_adjust",
        "kind": "rule",
        "subject": {"output_class": "outbound", "property": "voice"},
        "text": TEXT,
        "instructed_by": instructed_by,
        "for_admin": for_admin,
        "state": "open",
    }


def test_an_explicit_no_over_one_rule_declines_it():
    verdict = rc.resolve(f"[rule {RULE}] no", [_pending_row()], ADMIN, is_admin=True)
    assert verdict.kind == rc.DECLINED
    assert verdict.proposal_id == RULE


def test_a_question_that_happens_to_contain_a_defeater_is_not_a_decline():
    """THE CASE ss-console#2546 EXISTS TO STOP. "wait" is a defeater, so before
    this a question closed somebody else's request, wrote a row, and emailed
    them to say it was refused. It was harmless while a decline only shaped a
    sentence; it is not harmless now."""
    verdict = rc.resolve(
        f"wait, which letters? [rule {RULE}]", [_pending_row()], ADMIN, is_admin=True
    )
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_UNCLEAR_REFUSAL


def test_a_refusal_over_two_named_rules_asks_which():
    other = _pending_row(proposal_id="0b91ee42")
    verdict = rc.resolve(
        f"no [rule {RULE}] [rule 0b91ee42]", [_pending_row(), other], ADMIN, is_admin=True
    )
    assert verdict.kind == rc.ASK
    assert verdict.reason == rc.ASK_UNCLEAR_REFUSAL


def test_a_person_refusing_their_own_rule_is_still_a_decline():
    """PRESERVED behaviour, and the reason the standing check is not in the
    verdict: their "no" spends nothing, and reporting it as a question would
    ask somebody to repeat themselves."""
    row = _pending_row(instructed_by=ADMIN, for_admin=False)
    verdict = rc.resolve(f"[rule {RULE}] no, leave it", [row], ADMIN, is_admin=True)
    assert verdict.kind == rc.DECLINED


@pytest.mark.parametrize(
    ("row", "sender", "is_admin", "expected"),
    [
        (_pending_row(), ADMIN, True, True),
        # Not for_admin: nobody else's to refuse.
        (_pending_row(for_admin=False), ADMIN, True, False),
        # Not an admin: refusing a firm-level request is an act of authority.
        (_pending_row(), PARALEGAL, False, False),
        # The person who asked: a loop with no second person in it.
        (_pending_row(instructed_by=ADMIN), ADMIN, True, False),
    ],
)
def test_may_decline_answers_the_standing_question(row, sender, is_admin, expected):
    assert rc.may_decline(row, sender, is_admin) is expected


def test_an_act_is_never_declined_through_this_path():
    """Declining an act is "do not do it", which is what happens anyway when
    nobody confirms it."""
    row = dict(_pending_row(), kind="tool_call")
    assert rc.may_decline(row, ADMIN, True) is False


# ---------------------------------------------------------------------------
# 5. the operations request
# ---------------------------------------------------------------------------


def test_an_operations_request_carries_the_verbatim_sender_and_message():
    message = ops_request.build(
        sender=PARALEGAL, summary="a digest every Monday", message_id="m-42", customer_slug="ap"
    )
    assert message["to"] == [ops_request.SMD_OPERATIONS_DESK]
    assert PARALEGAL in message["subject"]
    assert PARALEGAL in message["text"]
    assert "message m-42" in message["text"]
    assert "a digest every Monday" in message["text"]
    # The desk is told to read the person's own words rather than the summary.
    assert "Read that rather" in message["text"]


def test_a_missing_message_id_says_so_rather_than_going_quiet():
    """A desk that cannot find the original needs to know that is why, not to
    wonder whether it looked properly."""
    text = ops_request.build(sender=PARALEGAL, summary="x")["text"]
    assert "not identified by the seat" in text


def test_the_fixed_reply_promises_nothing_about_the_future():
    assert "passed your request" in ops_request.FIXED_REPLY
    assert "do NOT say when it will happen" in ops_request.FIXED_REPLY
    assert "whether it will happen" in ops_request.FIXED_REPLY


def test_the_refused_reply_says_it_was_not_passed_on():
    text = ops_request.REFUSED_REPLY.format(reason="the turn was tainted")
    assert "COULD NOT pass the request on" in text
    assert "the turn was tainted" in text
    assert "Do not say it was passed on" in text


def test_a_summary_is_bounded_and_folded():
    assert ops_request.summarize("  a\n  b  ") == "a b"
    assert len(ops_request.summarize("x" * 9000)) == ops_request.MAX_SUMMARY_CHARS
    assert ops_request.summarize(None) == ""


# ---------------------------------------------------------------------------
# 6. the send registry
# ---------------------------------------------------------------------------


def test_an_unwired_seat_reports_that_it_cannot_send():
    """UNREGISTERED IS A FIRST-CLASS ANSWER. The caller is then required to say
    the notification did not go, which is the whole contract."""
    send_dispatch.set_sender(None)
    try:
        result = send_dispatch.dispatch(to=[ADMIN], subject="s", text="t")
        assert result.sent is False
        assert "no send path wired" in result.reason
        assert result.recipients == (ADMIN,)
    finally:
        send_dispatch.set_sender(None)


def test_a_raising_sender_is_reported_rather_than_propagated():
    """Two of the three callers are hooks and the third is a daemon thread; a
    raise from any of them costs a turn or kills the sweeper."""

    def boom(**_kwargs):
        raise RuntimeError("socket gone")

    send_dispatch.set_sender(boom)
    try:
        result = send_dispatch.dispatch(to=[ADMIN], subject="s", text="t")
        assert result.sent is False
        assert "socket gone" in result.reason
    finally:
        send_dispatch.set_sender(None)


# ---------------------------------------------------------------------------
# 7. the plugin: propose dispatches, the gate refuses, the tool answers
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, admins, routing):
        self._admins = admins
        self._routing = routing
        self.connectors: dict = {}

    @property
    def admins(self):
        return list(self._admins)

    @property
    def rule_requests_to(self):
        return list(self._routing)

    def sender_is_admin(self, sender):
        return isinstance(sender, str) and sender.strip().lower() in self._admins

    def sender_on_roster(self, sender):
        return True


class _FakeCustomerConfig:
    admins = [ADMIN]
    routing = [ADMIN]

    @classmethod
    def from_volume(cls, path=None):
        return _FakeConfig(cls.admins, cls.routing)


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    mod = load_plugin("hermes-smd-establishment")
    state: dict = {"requests": [], "pending": [], "sends": [], "status": [], "marked": []}

    def fake_broker_request(payload):
        state["requests"].append(payload)
        action = payload.get("action")
        if action == "establish_pending":
            return {"ok": True, "pending": list(state["pending"])}
        if action == "establish_propose":
            return {
                "ok": True,
                "proposal_id": RULE,
                "duplicate_of": state.get("duplicate_of"),
                "readback": f"[rule {RULE}] {TEXT}",
            }
        if action == "establish_decline":
            return {
                "ok": True,
                "proposal_id": RULE,
                "instructed_by": PARALEGAL,
                "text": TEXT,
                "declined_by": ADMIN,
            }
        if action == "establish_status":
            queue = state.get("status") or []
            return queue.pop(0) if queue else {"ok": False, "error": "unknown run_id"}
        if action == "establish_lapse_notified":
            state["marked"].append(payload.get("proposal_id"))
            for row in state["pending"]:
                if row.get("proposal_id") == payload.get("proposal_id"):
                    row["lapse_notified"] = True
            return {"ok": True}
        return {"ok": True, "run_id": "run-1"}

    def fake_send(**kwargs):
        state["sends"].append(kwargs)
        return _Result(sent=state.get("send_ok", True), reason=state.get("send_reason", ""))

    monkeypatch.setattr(mod, "_broker_request", fake_broker_request)
    monkeypatch.setattr(mod.send_dispatch, "dispatch", fake_send)
    monkeypatch.setattr(mod, "_emit_audit", lambda **kw: None)
    monkeypatch.setenv("SMD_ADMIN_POSSESSION_DB_PATH", str(tmp_path / "possession.db"))
    _FakeCustomerConfig.admins = [ADMIN]
    _FakeCustomerConfig.routing = [ADMIN]
    monkeypatch.setattr(mod, "CustomerConfig", _FakeCustomerConfig)
    # The converge wait is real time in production and nothing in a test; the
    # SHAPE under test is "more than one read", so the schedule keeps its length
    # and loses its sleeps.
    #
    # TOLERANT ON PURPOSE (raising=False, and getattr below). Every register the
    # install notice added is reset here, and a fixture that DEMANDED them would
    # make the whole file error out at origin/main -- which would say only that
    # a name is missing, and would say it just as loudly for the nineteen tests
    # in this file that have nothing to do with the install notice. The falsifier
    # is supposed to show each new test failing on its own assertion, so the
    # fixture is built to survive the ref it is falsified against.
    monkeypatch.setattr(mod, "_INSTALL_POLL_DELAYS", (0.0, 0.0, 0.0, 0.0), raising=False)
    mod._ADMIN_STASH.clear()
    mod._CONFIRMED_STASH.clear()
    mod._OPERATIONS_SENT.clear()
    mod._OUTCOMES_REPORTED.clear()
    mod._SUBMIT_RUNS.clear()
    mod._INSTALLED_RULES.clear()
    for register in ("_INSTALL_NOTIFIED", "_STATUS_CACHE"):
        getattr(mod, register, {}).clear()
    SESSION_INBOUND_ORIGIN._origins.clear()
    SESSION_TAINT._tainted.clear()
    yield mod, state
    SESSION_TAINT._tainted.clear()


def _propose_args(**over):
    args = {
        "scope": "firm_adjust",
        "subject": {"output_class": "outbound", "property": "voice"},
        "text": TEXT,
        "instructed_by": PARALEGAL,
        "source_ref": "m-1",
        "for_admin": True,
    }
    args.update(over)
    return args


def test_recording_a_paralegals_rule_sends_the_request_from_the_handler(plugin):
    """DETERMINISTIC: the send happens in the tool handler, the moment the
    broker says a for_admin row exists, without the model deciding to send."""
    mod, state = plugin
    mod._propose(_propose_args(), session_id="sess-1")
    assert [s["to"] for s in state["sends"]] == [[ADMIN]]
    assert f"[rule {RULE}]" in state["sends"][0]["subject"]


def test_the_tool_result_tells_the_model_who_was_asked(plugin):
    mod, state = plugin
    result = mod._propose(_propose_args(), session_id="sess-1")
    assert ADMIN in result
    assert "ALREADY" in result


def test_a_duplicate_does_not_page_an_administrator_a_second_time(plugin):
    """Two tags in front of one administrator, only one of which answering
    would close, is worse than one."""
    mod, state = plugin
    state["duplicate_of"] = RULE
    mod._propose(_propose_args(), session_id="sess-1")
    assert state["sends"] == []


def test_an_admins_own_rule_pages_nobody(plugin):
    """for_admin false is the ordinary admin path: they confirm it themselves."""
    mod, state = plugin
    mod._propose(_propose_args(instructed_by=ADMIN, for_admin=False), session_id="sess-1")
    assert state["sends"] == []


def test_a_refused_dispatch_makes_the_tool_result_say_so(plugin):
    mod, state = plugin
    state["send_ok"] = False
    state["send_reason"] = "Refused: this turn is tainted"
    result = mod._propose(_propose_args(), session_id="sess-1")
    assert "COULD NOT" in result
    assert "tainted" in result


def test_an_admin_may_not_mark_their_own_rule_as_waiting_for_an_admin(plugin):
    """It used to be merely pointless. It would now email the routing list a
    request from somebody who could simply have said yes, and would let one
    address both raise and answer a request with nobody else involved."""
    mod, _state = plugin
    mod._ADMIN_STASH["sess-1"] = {"sender": ADMIN, "is_admin": True}
    block = mod.on_pre_tool_call(
        tool_name=mod.TOOL_PROPOSE,
        session_id="sess-1",
        args=_propose_args(instructed_by=ADMIN, for_admin=True),
    )
    assert block is not None
    assert "for_admin marks a rule as WAITING" in block["message"]


def test_a_non_admins_rule_still_proposes_with_for_admin(plugin):
    """PRESERVED. The refusal above must not close the path the loop runs on."""
    mod, _state = plugin
    mod._ADMIN_STASH["sess-1"] = {"sender": PARALEGAL, "is_admin": False}
    assert (
        mod.on_pre_tool_call(tool_name=mod.TOOL_PROPOSE, session_id="sess-1", args=_propose_args())
        is None
    )


def test_an_operations_request_reaches_smd_and_returns_the_fixed_sentence(plugin):
    mod, state = plugin
    mod._ADMIN_STASH["sess-1"] = {"sender": PARALEGAL, "is_admin": False}
    reply = mod._operations_request({"summary": "a digest every Monday"}, session_id="sess-1")
    assert reply == ops_request.FIXED_REPLY
    assert state["sends"][0]["to"] == [ops_request.SMD_OPERATIONS_DESK]
    assert "a digest every Monday" in state["sends"][0]["text"]


def test_an_operations_request_that_could_not_go_says_it_did_not(plugin):
    """The two sentences are the design: which one the model gets is decided by
    whether the message went, not by the model."""
    mod, state = plugin
    state["send_ok"] = False
    state["send_reason"] = "Refused: this turn is tainted"
    mod._ADMIN_STASH["sess-1"] = {"sender": PARALEGAL, "is_admin": False}
    reply = mod._operations_request({"summary": "a digest"}, session_id="sess-1")
    assert "COULD NOT pass the request on" in reply
    assert "tainted" in reply
    assert "send it to SMD themselves" in reply


def test_an_unattributed_turn_cannot_pass_a_request_on(plugin):
    mod, state = plugin
    reply = mod._operations_request({"summary": "a digest"}, session_id="sess-1")
    assert "no verified sender" in reply
    assert state["sends"] == []


# ---------------------------------------------------------------------------
# 8. the send-time gate on a promised routine change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Sure, I will start sending you a digest every Monday.",
        "I have scheduled the weekly summary for you.",
        "From now on, I'll run that daily.",
    ],
)
def test_a_reply_promising_a_routine_change_is_withheld(plugin, body):
    mod, _state = plugin
    block = mod._operations_gate("sess-1", "smd_send_message", {"text": body})
    assert block is not None
    assert "promises that a routine will start" in block["message"]


@pytest.mark.parametrize(
    "body",
    [
        "I will send you the draft this afternoon.",
        "The digest runs on Mondays, as things stand.",
        "I have set out the three options below.",
    ],
)
def test_an_ordinary_reply_is_not_withheld(plugin, body):
    """THE FALSIFIER FOR THE GATE. It is a conjunction (a first-person promise
    AND a routine object) precisely so ordinary work is untouched; a gate that
    fired on either half alone would withhold half the seat's mail."""
    mod, _state = plugin
    assert mod._operations_gate("sess-1", "smd_send_message", {"text": body}) is None


def test_the_promise_is_allowed_once_the_request_has_actually_been_passed_on(plugin):
    mod, _state = plugin
    mod._ADMIN_STASH["sess-1"] = {"sender": PARALEGAL, "is_admin": False}
    mod._operations_request({"summary": "a digest every Monday"}, session_id="sess-1")
    assert (
        mod._operations_gate(
            "sess-1", "smd_send_message", {"text": "I will start sending a weekly digest."}
        )
        is None
    )


def test_a_refused_request_leaves_the_gate_armed(plugin):
    """The register holds the FACT, never the model's account of it: the entry
    is written after the send returns sent, so a refused send leaves the gate
    armed and the reply has to say the request did not go."""
    mod, state = plugin
    state["send_ok"] = False
    mod._ADMIN_STASH["sess-1"] = {"sender": PARALEGAL, "is_admin": False}
    mod._operations_request({"summary": "a digest every Monday"}, session_id="sess-1")
    block = mod._operations_gate(
        "sess-1", "smd_send_message", {"text": "I will start sending a weekly digest."}
    )
    assert block is not None


# ---------------------------------------------------------------------------
# 9. the lapse sweeper
# ---------------------------------------------------------------------------


def test_the_sweeper_reports_then_marks(monkeypatch):
    """The ordering, and it is the whole design: a mark written first would
    trade a duplicate note for a silence."""
    sweeper = load_plugin("hermes-smd-establishment").__dict__["lapse_sweeper"]
    order: list[str] = []

    def notify(*, kind, row, by):
        order.append(f"notify:{row['proposal_id']}")
        return True

    def mark(proposal_id):
        order.append(f"mark:{proposal_id}")

    result = sweeper.run_sweep_once(
        fetch=lambda: [{"proposal_id": RULE, "state": "lapsed"}],
        notify=notify,
        mark=mark,
    )
    assert order == [f"notify:{RULE}", f"mark:{RULE}"]
    assert result.reported == 1


def test_a_row_the_sweeper_could_not_send_is_left_unmarked():
    """It comes back next pass. There is no give-up: the failure modes are a
    refused gate and an unreachable transport, and both clear on their own."""
    sweeper = load_plugin("hermes-smd-establishment").__dict__["lapse_sweeper"]
    marked: list[str] = []
    result = sweeper.run_sweep_once(
        fetch=lambda: [{"proposal_id": RULE, "state": "lapsed"}],
        notify=lambda **_kw: False,
        mark=marked.append,
    )
    assert marked == []
    assert result.failed == 1
    assert result.reported == 0


def test_the_sweeper_ignores_anything_that_has_not_ended():
    """FALSIFIER for the widening. An open row reaching the sweeper would mean
    telling somebody their rule lapsed while an administrator still has it."""
    sweeper = load_plugin("hermes-smd-establishment").__dict__["lapse_sweeper"]
    sent: list[str] = []
    result = sweeper.run_sweep_once(
        fetch=lambda: [
            {"proposal_id": RULE, "state": "open"},
            {"proposal_id": "x", "state": ""},
            "not a row",
        ],
        notify=lambda **kw: sent.append(kw["row"]["proposal_id"]) or True,
        mark=lambda _p: None,
    )
    assert sent == []
    assert result.skipped == 3


def test_one_pass_is_bounded_so_a_backlog_drains_rather_than_bursts():
    sweeper = load_plugin("hermes-smd-establishment").__dict__["lapse_sweeper"]
    rows = [{"proposal_id": f"{i:08x}", "state": "lapsed"} for i in range(100)]
    result = sweeper.run_sweep_once(
        fetch=lambda: rows, notify=lambda **_kw: True, mark=lambda _p: None
    )
    assert result.reported == sweeper.MAX_PER_PASS


# ---------------------------------------------------------------------------
# 10. the templated spec-gate path, and the leak it must not open
# ---------------------------------------------------------------------------


def test_a_templated_body_skips_only_the_voice_read_branch(monkeypatch):
    """The notifications are bytes this repo wrote. Asking whether the MODEL
    read the firm's voice spec before writing them is a question with no
    meaning, and on a seat that declares one it would refuse every
    notification."""
    from shared import spec_gate

    monkeypatch.setattr(spec_gate, "_spec_expected", lambda _c: True)
    monkeypatch.setattr(spec_gate, "_declared", lambda _c, _p: False)
    monkeypatch.setattr(spec_gate, "_control_state", lambda _c, _p: spec_gate._STATE_PRESENT)
    monkeypatch.setattr(spec_gate.SPEC_STATUS, "was_read", lambda *a, **k: False)

    assert (
        spec_gate.check_spec_gate(
            tool_name="smd_send_message",
            action_class_value="external_send_internal",
            session_id="sess-1",
            body="a fixed template",
            templated=True,
        )
        is None
    )


def test_the_templated_path_grants_the_session_nothing(monkeypatch):
    """THE LEAK TEST, and the reason the implementation does not simply mark the
    spec read for the session: doing that would leave the session holding a read
    it never performed, and the NEXT model-composed send would sail through."""
    from shared import spec_gate

    monkeypatch.setattr(spec_gate, "_spec_expected", lambda _c: True)
    monkeypatch.setattr(spec_gate, "_declared", lambda _c, _p: False)
    monkeypatch.setattr(spec_gate, "_control_state", lambda _c, _p: spec_gate._STATE_PRESENT)
    monkeypatch.setattr(spec_gate.SPEC_STATUS, "was_read", lambda *a, **k: False)
    monkeypatch.setattr(spec_gate, "_emit_spec_gate_audit", lambda **_kw: None)

    spec_gate.check_spec_gate(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="sess-1",
        body="a fixed template",
        templated=True,
    )
    composed = spec_gate.check_spec_gate(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="sess-1",
        body="prose the model wrote",
    )
    assert composed is not None
    assert composed["action"] == "block"


def test_a_tampered_spec_still_refuses_a_templated_body(monkeypatch):
    """Only ONE branch is skipped. A control that cannot be proven intact
    refuses whatever wrote the body."""
    from shared import spec_gate

    monkeypatch.setattr(spec_gate, "_spec_expected", lambda _c: True)
    monkeypatch.setattr(spec_gate, "_declared", lambda _c, _p: False)
    monkeypatch.setattr(spec_gate, "_control_state", lambda _c, _p: spec_gate._STATE_TAMPERED)
    monkeypatch.setattr(spec_gate, "_emit_spec_gate_audit", lambda **_kw: None)

    block = spec_gate.check_spec_gate(
        tool_name="smd_send_message",
        action_class_value="external_send_internal",
        session_id="sess-1",
        body="a fixed template",
        templated=True,
    )
    assert block is not None


def test_the_dispatcher_marks_its_payload_templated(monkeypatch):
    """The key is on the PAYLOAD rather than a module flag, because a sweeper
    thread and a live turn can both be in flight and a flag would let one turn's
    posture leak into another's."""
    from shared.spec_gate import TEMPLATED_BODY_ARG

    trust = load_plugin("hermes-smd-trust")
    seen: dict = {}

    def fake_evaluate(tool_name, args, slug, session_id=""):
        seen.update(args)
        return {"action": "block", "message": "stop here"}

    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", fake_evaluate)
    result = trust._dispatch_internal_message(
        to=[ADMIN], subject="s", text="t", session_id="sess-1"
    )
    assert result.sent is False
    assert seen[TEMPLATED_BODY_ARG] is True
    assert result.reason == "stop here"


# ---------------------------------------------------------------------------
# 11. the rule goes into force and the person who asked HEARS SO
#     (ss-console#2546 follow-up)
#
# LIVE DEFECT (pilot, 2026-08-22T20:31Z, overlay 119f6bf). ss-probe-runner, a
# non-admin, stated a firm rule. ss-probe-admin replied "apply that". The submit
# was accepted at 20:31:32Z and the intake installed the rule twenty seconds
# later; the seat's own gate correctly blocked one premature draft and released
# the reply once the install was real. Every part of that worked. The runner was
# never told, and her inbox is the only place the whole feature is visible.
#
# The seat asked the broker ONCE, immediately after the submit returned, whether
# the run had installed. It had not yet -- a converge window read at its start
# measures nothing -- so the notification path returned, and the answer it was
# waiting for arrived into a one-shot result that some other caller consumed.
#
# Three observers now, because the fact can surface on three different paths and
# the person is owed it whichever one sees it first: the handler waits out the
# window, the model's own status call is watched, and the sweeper picks up
# anything that got as far as being observed but not sent. Exactly one letter
# across the three, and the lock is the broker's conditional mark.
# ---------------------------------------------------------------------------


def _installed_row(**over):
    row = {
        "proposal_id": RULE,
        "text": TEXT,
        "instructed_by": PARALEGAL,
        "for_admin": True,
        "state": "committed",
        "installed": True,
        "lapse_notified": False,
    }
    row.update(over)
    return row


def _pending_answer():
    return {"ok": True, "run_id": "run-1", "status": "pending"}


def _installed_answer():
    return {
        "ok": True,
        "run_id": "run-1",
        "status": "complete",
        "result": {"status": "installed", "adjustment_id": RULE},
    }


def _apply(mod, state, *, sender=ADMIN, row=None, status=None, session="sess-1"):
    """An administrator's confirmed submit, with the broker's answers staged."""
    state["pending"] = [row if row is not None else _installed_row()]
    state["status"] = list(status if status is not None else [_installed_answer()])
    mod._ADMIN_STASH[session] = {"sender": sender, "is_admin": True}
    return mod._submit({"scope": "firm_adjust", "proposal_id": RULE}, session_id=session)


def _install_notes(state):
    return [s for s in state["sends"] if "in effect" in s["subject"]]


def test_a_rule_that_installs_on_the_third_poll_is_still_reported(plugin):
    """THE LIVE DEFECT. One poll fired at the start of a 90 s converge window
    reads 'pending' and learns nothing, which is exactly what happened to the
    run that installed twenty seconds later."""
    mod, state = plugin
    _apply(mod, state, status=[_pending_answer(), _pending_answer(), _installed_answer()])

    notes = _install_notes(state)
    assert len(notes) == 1
    assert notes[0]["to"] == [PARALEGAL]
    assert TEXT in notes[0]["text"]
    assert ADMIN in notes[0]["text"]
    assert state["marked"] == [RULE]


def test_a_run_that_never_converges_sends_nothing(plugin):
    """The falsifier. The seat may not say a rule is in force because it waited
    a while and stopped asking."""
    mod, state = plugin
    _apply(mod, state, status=[_pending_answer()] * 8)

    assert _install_notes(state) == []
    assert state["marked"] == []


def test_the_seats_wait_leaves_the_reply_gate_satisfied(plugin):
    """The seat saw the word itself, so the reply may say the rule is in force
    even if the model's own status call never lands."""
    mod, state = plugin
    _apply(mod, state)
    assert RULE in mod._INSTALLED_RULES.get("sess-1", set())


def test_the_models_status_call_reports_an_install_the_poll_missed(plugin):
    """The second observer. The poll gave up; the model asked; the person is
    told anyway."""
    mod, state = plugin
    _apply(mod, state, status=[_pending_answer()] * 8)
    assert _install_notes(state) == []

    mod._SUBMIT_RUNS["run-1"] = RULE
    mod.on_post_tool_call(
        tool_name="establish_status",
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(_installed_answer()),
    )

    notes = _install_notes(state)
    assert len(notes) == 1
    assert notes[0]["to"] == [PARALEGAL]
    assert state["marked"] == [RULE]


def test_two_observers_of_one_install_send_one_letter(plugin):
    """The handler waited it out AND the model asked. One rule, one letter."""
    mod, state = plugin
    _apply(mod, state)
    assert len(_install_notes(state)) == 1

    mod.on_post_tool_call(
        tool_name="establish_status",
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(_installed_answer()),
    )

    assert len(_install_notes(state)) == 1
    assert state["marked"] == [RULE]


def test_the_broker_mark_is_what_stops_the_second_letter(plugin):
    """Not process memory. A seat that restarted between the send and the next
    observation still sends nothing, because the row says it was reported."""
    mod, state = plugin
    _apply(mod, state)
    mod._INSTALL_NOTIFIED.clear()  # the restart
    before = len(_install_notes(state))

    mod.on_post_tool_call(
        tool_name="establish_status",
        session_id="sess-1",
        args={"run_id": "run-1"},
        result=json.dumps(_installed_answer()),
    )

    assert len(_install_notes(state)) == before


def test_an_administrator_confirming_their_own_rule_is_sent_nothing(plugin):
    """No letter, and no wait either: the row is read BEFORE the converge
    window, so an administrator's own confirmation does not hold the turn open
    for a hundred seconds to learn something nobody is waiting for."""
    mod, state = plugin
    _apply(mod, state, sender=ADMIN, row=_installed_row(instructed_by=ADMIN))

    assert _install_notes(state) == []
    assert state["status"] == [_installed_answer()]  # untouched: never polled


def test_a_rule_nobody_was_waiting_on_is_not_polled_for(plugin):
    """Same ordering, the other reason: a rule that was never for_admin has no
    requester to tell."""
    mod, state = plugin
    _apply(mod, state, row=_installed_row(for_admin=False))

    assert _install_notes(state) == []
    assert state["status"] == [_installed_answer()]


def test_a_note_that_did_not_go_leaves_the_row_for_the_sweeper(plugin):
    """Mark AFTER the send, never before. A refused gate is a condition that
    clears, and the row has to come back."""
    mod, state = plugin
    state["send_ok"] = False
    _apply(mod, state)

    assert state["marked"] == []
    assert RULE not in mod._INSTALL_NOTIFIED

    state["send_ok"] = True
    assert mod._notify_install_observed("sess-1", RULE) is True
    assert state["marked"] == [RULE]


def test_the_seats_own_read_does_not_blind_the_model(plugin):
    """A result is a ONE-SHOT read. The seat now polls runs itself, so when its
    poll wins the race the model asking a moment later would be told 'unknown
    run_id' about a run that had just succeeded -- the seat destroying the
    model's evidence in the act of gathering its own."""
    mod, state = plugin
    _apply(mod, state)
    assert state["status"] == []  # consumed by the seat's poll

    answered = json.loads(mod._status({"run_id": "run-1"}))
    assert answered["result"]["status"] == "installed"


def test_a_live_answer_always_wins_over_the_cache(plugin):
    """The cache is the broker's own answer handed back, never a second source
    of truth: it is consulted only when the broker no longer has the run."""
    mod, state = plugin
    _apply(mod, state)
    state["status"] = [{"ok": True, "run_id": "run-1", "status": "pending"}]

    assert json.loads(mod._status({"run_id": "run-1"}))["status"] == "pending"


def test_the_sweeper_reports_an_installed_rule(plugin):
    """The third observer, for the run whose send failed while somebody was in
    front of it and whose person is now owed a letter with nobody there."""
    mod, state = plugin
    state["pending"] = [_installed_row()]
    result = mod._sweep_lapses_once()

    assert result.reported == 1
    assert [s["to"] for s in _install_notes(state)] == [[PARALEGAL]]
    assert state["marked"] == [RULE]


def test_the_sweeper_leaves_a_committed_rule_nobody_has_seen_install(plugin):
    """THE HONESTY OF IT. Committed is not in force: the run can still be
    converging and can still fail. A sweeper that read consumed_at as an outcome
    would mail 'your rule is in effect' about a rule that never landed."""
    mod, state = plugin
    state["pending"] = [_installed_row(installed=False)]
    result = mod._sweep_lapses_once()

    assert result.reported == 0
    assert _install_notes(state) == []


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"state": "lapsed"}, "lapsed"),
        ({"state": "declined"}, "declined"),
        ({"state": "committed", "installed": True}, "installed"),
        ({"state": "committed", "installed": False}, ""),
        ({"state": "committed"}, ""),
        ({"state": "open"}, ""),
        ({}, ""),
    ],
)
def test_which_outcome_a_row_is(row, expected):
    sweeper = load_plugin("hermes-smd-establishment").__dict__["lapse_sweeper"]
    assert sweeper.outcome_kind(row) == expected


def test_the_nudge_names_the_list_the_request_reaches(plugin):
    """THE COSMETIC DEFECT, live on the pilot: the Operator named the ADMINS
    while the request had gone to scope.rule_requests_to. The two lists are
    separate by design, so naming the wrong one sends the person to chase an
    answer from somebody who was never asked."""
    mod, state = plugin
    _FakeCustomerConfig.admins = [ADMIN, "partner@firm.com"]
    _FakeCustomerConfig.routing = [ADMIN]
    context = mod.on_pre_llm_call(
        session_id="sess-9", sender_id=PARALEGAL, user_message="be more formal"
    )["context"]

    assert ADMIN in context
    assert "partner@firm.com" not in context


def test_the_propose_result_names_the_list_the_request_reached(plugin):
    """And so does the sentence handed back from the tool, for the same reason
    and against the same falsifier."""
    mod, state = plugin
    _FakeCustomerConfig.admins = [ADMIN, "partner@firm.com"]
    _FakeCustomerConfig.routing = [ADMIN]
    result = mod._propose(_propose_args(), session_id="sess-1")

    assert ADMIN in result
    assert "partner@firm.com" not in result
