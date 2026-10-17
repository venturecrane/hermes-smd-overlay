"""A withheld send is approved only by the person who raised it, in the chat they
raised it in (the two-principal seat, 2026-10-17).

Before this binding the register held one send per Machine and any allowlisted
Telegram sender's bare "send it" released it. On a seat that allowlists a
couple, partner B could approve partner A's send, and from a group chat.

Each falsifier names the line whose removal makes it fail.
"""

from __future__ import annotations

import pytest

from shared import turn_principal
from shared.inbound import SESSION_TAINT
from shared.outbound_recipient import DRAFT_RECIPIENTS
from shared.pending_send import PENDING_SEND, PendingSendRegister
from shared.turn_principal import UNBOUND, Principal
from tests.conftest import load_plugin

A = "1001"  # partner A's Telegram user id; a Telegram DM's chat id is the user id
B = "2002"
GROUP = "-100777"  # a group both partners are in
TOOL = "mcp_agentmail_send_message"
ROSTER = ["scott@smd.services"]

_SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_CRON_SESSION",
    "HERMES_CRON_AUTO_DELIVER_PLATFORM",
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
)


def dm(user: str) -> Principal:
    return Principal(platform="telegram", user_id=user, chat_id=user, chat_type="dm")


def in_group(user: str) -> Principal:
    return Principal(platform="telegram", user_id=user, chat_id=GROUP, chat_type="group")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in _SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()
    yield
    PENDING_SEND.clear()
    DRAFT_RECIPIENTS._by_key.clear()
    SESSION_TAINT._tainted.clear()


def _turn(monkeypatch, who: Principal) -> None:
    """Bind this turn's session context the way the gateway does (env fallback)."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", who.platform)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", who.user_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", who.chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", who.chat_type)


def _hold(reg: PendingSendRegister, who: Principal, to: str = "client@example.com") -> None:
    reg.capture(TOOL, {"to": [to], "text": f"from {who.user_id}"}, {to}, requester=who)


# ---------------------------------------------------------------------------
# register: who may approve
# ---------------------------------------------------------------------------


def test_partner_b_cannot_approve_partner_as_send():
    """FALSIFIER: drop the ``approver.user_id == req.user_id`` term in
    ``_may_approve`` and B's yes releases A's send."""
    reg = PendingSendRegister()
    _hold(reg, dm(A))
    assert reg.mark_approved(f"telegram:{B}", dm(B), sole_principal=False) is False
    assert reg.peek().approved is False
    # The case only the user term catches: A raised it in the shared group and B
    # says yes in that same group.
    reg.clear()
    _hold(reg, in_group(A))
    assert reg.mark_approved(f"telegram:{B}", in_group(B), sole_principal=False) is False
    assert reg.peek().approved is False


def test_a_group_yes_never_approves_a_send_raised_in_a_dm():
    """FALSIFIER: drop ``_same_chat`` from the person-raised branch."""
    reg = PendingSendRegister()
    _hold(reg, dm(A))
    assert reg.mark_approved(f"telegram:{A}", in_group(A), sole_principal=False) is False
    # Holds on a single-principal seat too: the owner's group yes is not a DM yes.
    assert reg.mark_approved(f"telegram:{A}", in_group(A), sole_principal=True) is False
    assert reg.peek().approved is False


def test_a_dm_yes_never_approves_a_send_raised_in_a_group():
    reg = PendingSendRegister()
    _hold(reg, in_group(A))
    assert reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False) is False
    assert reg.peek().approved is False


def test_a_chat_type_disagreement_is_refused_even_on_the_same_chat_id():
    reg = PendingSendRegister()
    _hold(reg, dm(A))
    forged = Principal(platform="telegram", user_id=A, chat_id=A, chat_type="group")
    assert reg.mark_approved(f"telegram:{A}", forged, sole_principal=False) is False


def test_the_same_person_in_the_same_chat_approves():
    reg = PendingSendRegister()
    _hold(reg, dm(A))
    assert reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False) is True
    rec = reg.peek()
    assert rec.approved is True and rec.approval_source == f"telegram:{A}"


def test_expiry_still_holds_for_a_bound_send():
    reg = PendingSendRegister(ttl_seconds=900)
    _hold(reg, dm(A))
    reg.peek().created_at -= 901
    assert reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False) is False
    assert reg.pending() == []


def test_an_approval_expires_before_it_is_spent():
    reg = PendingSendRegister(ttl_seconds=900)
    _hold(reg, dm(A))
    reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False)
    reg.peek().created_at -= 901
    assert reg.take_for_send(TOOL, {"client@example.com"}, turn=dm(A)) is None


# ---------------------------------------------------------------------------
# register: two partners, two pending sends
# ---------------------------------------------------------------------------


def test_each_partner_keeps_their_own_pending_send():
    """FALSIFIER: supersede unconditionally in ``capture`` (the old single slot)
    and A's send is gone the moment B composes one."""
    reg = PendingSendRegister()
    _hold(reg, dm(A), to="a@example.com")
    _hold(reg, dm(B), to="b@example.com")
    assert len(reg.pending()) == 2

    rec = reg.approve(f"telegram:{A}", dm(A), sole_principal=False)
    assert rec is not None and rec.recipients == frozenset({"a@example.com"})
    b_rec = next(r for r in reg.pending() if r.requester.user_id == B)
    assert b_rec.approved is False


def test_a_partners_turn_cannot_spend_the_others_approved_send():
    """Same tool, same recipient, both approved-or-pending: B's turn releases only
    B's. FALSIFIER: make ``_may_consume`` return True."""
    reg = PendingSendRegister()
    _hold(reg, dm(A))
    reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False)
    assert reg.take_for_send(TOOL, {"client@example.com"}, turn=dm(B)) is None
    assert reg.take_for_send(TOOL, {"client@example.com"}, turn=in_group(A)) is None
    rec = reg.take_for_send(TOOL, {"client@example.com"}, turn=dm(A))
    assert rec is not None and rec.args["text"] == f"from {A}"
    assert reg.pending() == []


def test_a_new_compose_by_the_same_person_in_the_same_chat_supersedes():
    reg = PendingSendRegister()
    _hold(reg, dm(A), to="first@example.com")
    reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False)
    _hold(reg, dm(A), to="second@example.com")
    assert [r.recipients for r in reg.pending()] == [frozenset({"second@example.com"})]
    assert reg.peek().approved is False


# ---------------------------------------------------------------------------
# register: sends no person raised
# ---------------------------------------------------------------------------


def test_an_unbound_send_on_a_two_principal_seat_is_not_approvable():
    """Nothing says whose it is, so it fails closed. FALSIFIER: return True at the
    end of ``_may_approve``."""
    reg = PendingSendRegister()
    reg.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"}, requester=UNBOUND)
    assert reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False) is False


def test_a_cron_send_is_approvable_only_from_the_chat_it_was_delivered_to():
    reg = PendingSendRegister()
    cron = Principal(platform="telegram", chat_id=GROUP)
    reg.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"}, requester=cron)
    assert reg.mark_approved(f"telegram:{A}", dm(A), sole_principal=False) is False
    assert reg.mark_approved(f"telegram:{B}", in_group(B), sole_principal=False) is True


# ---------------------------------------------------------------------------
# capture-time binding (shared.turn_principal)
# ---------------------------------------------------------------------------


def test_requester_binds_a_telegram_turn_to_its_user_and_chat(monkeypatch):
    _turn(monkeypatch, dm(A))
    assert turn_principal.requester() == dm(A)


def test_requester_binds_a_cron_turn_to_its_delivery_chat_only(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", GROUP)
    assert turn_principal.requester() == Principal(platform="telegram", chat_id=GROUP)


def test_requester_is_unbound_without_session_context():
    assert turn_principal.requester() == UNBOUND


# ---------------------------------------------------------------------------
# end to end: the gate holds, the approval hook approves, the gate releases
# ---------------------------------------------------------------------------


def _trust():
    return load_plugin("hermes-smd-trust")


def _setup_confirm(monkeypatch, enforce):
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.CONFIRM},
    )
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(ROSTER))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "agent-crane")


def _compose(enforce, text: str, to: str = "client@example.com", session: str = "s1"):
    return enforce.evaluate_tool_call(
        TOOL, {"to": [to], "subject": "Report", "text": text}, "smd", session_id=session
    )


def test_end_to_end_partner_b_cannot_release_partner_as_send(monkeypatch):
    trust = _trust()
    enforce, approval = trust.enforce, trust.approval
    _setup_confirm(monkeypatch, enforce)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{A},{B}")

    _turn(monkeypatch, dm(A))
    held = _compose(enforce, "A's reviewed body")
    assert held is not None and held["action"] == "block"
    assert PENDING_SEND.peek().requester == dm(A)

    # B, in B's own DM, then in the shared group: neither approves.
    _turn(monkeypatch, dm(B))
    assert approval.maybe_capture_approval("telegram", B, "Yes, send it") is None
    _turn(monkeypatch, in_group(B))
    assert approval.maybe_capture_approval("telegram", B, "Yes, send it") is None
    # A in the group: still not the chat the send was raised in.
    _turn(monkeypatch, in_group(A))
    assert approval.maybe_capture_approval("telegram", A, "Yes, send it") is None
    assert PENDING_SEND.peek().approved is False

    # A in A's DM: approved, and A's turn releases exactly the stored payload.
    _turn(monkeypatch, dm(A))
    assert approval.maybe_capture_approval("telegram", A, "Yes, send it") == f"telegram:{A}"
    live = {"to": ["client@example.com"], "subject": "Report", "text": "drifted"}
    assert enforce.evaluate_tool_call(TOOL, live, "smd", session_id="s1") is None
    assert live["text"] == "A's reviewed body"
    assert PENDING_SEND.peek() is None


def test_end_to_end_each_partner_approves_only_their_own(monkeypatch):
    trust = _trust()
    enforce, approval = trust.enforce, trust.approval
    _setup_confirm(monkeypatch, enforce)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{A},{B}")

    _turn(monkeypatch, dm(A))
    _compose(enforce, "A's body", session="sa")
    _turn(monkeypatch, dm(B))
    _compose(enforce, "B's body", session="sb")
    assert len(PENDING_SEND.pending()) == 2

    # B approves; B's turn re-invokes the identical send and gets B's payload.
    rec = approval.capture_approval("telegram", B, "send it")
    assert rec is not None and rec.args["text"] == "B's body"
    live = {"to": ["client@example.com"], "subject": "Report", "text": "x"}
    assert enforce.evaluate_tool_call(TOOL, live, "smd", session_id="sb") is None
    assert live["text"] == "B's body"
    # A's send is still pending and still unapproved.
    [left] = PENDING_SEND.pending()
    assert left.requester == dm(A) and left.approved is False


def test_a_sender_that_disagrees_with_the_session_user_approves_nothing(monkeypatch):
    approval = _trust().approval
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{A},{B}")
    PENDING_SEND.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"}, requester=dm(A))
    _turn(monkeypatch, dm(B))
    assert approval.maybe_capture_approval("telegram", A, "send it") is None
    assert PENDING_SEND.peek().approved is False


# ---------------------------------------------------------------------------
# single-principal regression: today's flow on scott / A&P / pilot / brokery /
# staging is unchanged
# ---------------------------------------------------------------------------


def test_single_principal_dm_flow_is_unchanged(monkeypatch):
    """The owner holds a send in their DM, says yes in their DM, it ships with the
    stored payload, single-use; a later compose supersedes as the slot always did."""
    trust = _trust()
    enforce, approval = trust.enforce, trust.approval
    _setup_confirm(monkeypatch, enforce)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", A)
    _turn(monkeypatch, dm(A))

    _compose(enforce, "first body", to="first@example.com")
    _compose(enforce, "second body")
    assert [r.recipients for r in PENDING_SEND.pending()] == [frozenset({"client@example.com"})]

    assert approval.maybe_capture_approval("telegram", A, "Yes, send it") == f"telegram:{A}"
    live = {"to": ["client@example.com"], "subject": "Report", "text": "drifted"}
    assert enforce.evaluate_tool_call(TOOL, live, "smd", session_id="s1") is None
    assert live["text"] == "second body"
    assert PENDING_SEND.peek() is None
    # Single-use: the same re-invoke is withheld again.
    again = enforce.evaluate_tool_call(TOOL, dict(live), "smd", session_id="s1")
    assert again is not None and again["action"] == "block"


def test_single_principal_unbound_and_cron_sends_approve_from_anywhere(monkeypatch):
    """A send held on a turn with no person (no session context, or a cron that
    delivers to a group) is approved by the one principal from their DM, as it
    always was."""
    approval = _trust().approval
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", A)
    _turn(monkeypatch, dm(A))

    PENDING_SEND.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"})
    assert approval.maybe_capture_approval("telegram", A, "approve") == f"telegram:{A}"

    PENDING_SEND.clear()
    cron = Principal(platform="telegram", chat_id=GROUP)
    PENDING_SEND.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"}, requester=cron)
    assert approval.maybe_capture_approval("telegram", A, "approve") == f"telegram:{A}"


def test_single_principal_turn_without_session_context_still_approves(monkeypatch):
    """No session context at all on either turn (the env fallback is empty): the
    record is unbound and the sole principal's yes approves it, as before."""
    approval = _trust().approval
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", A)
    PENDING_SEND.capture(TOOL, {"to": ["c@example.com"]}, {"c@example.com"})
    assert approval.maybe_capture_approval("telegram", A, "send it") == f"telegram:{A}"


# ---------------------------------------------------------------------------
# out-of-band dispatch: the approved record ships, not the newest one
# ---------------------------------------------------------------------------


def test_pre_llm_call_dispatches_the_approvers_send_not_the_newest(monkeypatch):
    """A holds, then B holds (B's is now newest). A's yes must ship A's payload.
    FALSIFIER: dispatch ``PENDING_SEND.peek()`` (the old single slot) and B's
    unapproved send is picked, so nothing ships."""
    trust = _trust()
    enforce = trust.enforce
    _setup_confirm(monkeypatch, enforce)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{A},{B}")
    monkeypatch.setattr(trust, "get_secret", lambda k: "smd")
    sent: list[dict] = []

    def _send(*, payload, **_k):
        sent.append(dict(payload))
        return "msg_1"

    monkeypatch.setattr(trust.outbound_send, "send_message", _send)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)

    _turn(monkeypatch, dm(A))
    _compose(enforce, "A's body", to="a@example.com", session="sa")
    _turn(monkeypatch, dm(B))
    _compose(enforce, "B's body", to="b@example.com", session="sb")
    assert PENDING_SEND.peek().requester == dm(B)

    _turn(monkeypatch, dm(A))
    trust.on_pre_llm_call(
        session_id="sa", platform="telegram", sender_id=A, user_message="Yes, send it"
    )
    assert [p["text"] for p in sent] == ["A's body"]
    [left] = PENDING_SEND.pending()
    assert left.requester == dm(B) and left.approved is False
