"""Couples role over email (ADR 0091): routine delivery and shared replies.

Falsifiers:

* a ``person:p1`` routine with Telegram unbound emails p1 and nobody else; a
  ``shared`` one sends ONE email to both; the model's text never picks a
  recipient; a Telegram-bound job is never also emailed;
* a shared-space email turn's reply reaches the seat's people (and is marked
  shared for the broker); a stranger in the draft is refused; a person-space
  turn stays single-recipient;
* seats without ``people`` are unchanged on both paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bootstrap.cron_materialize import materialize_cron
from shared import cron_attribution, inbound, send_anchor, send_dispatch, space, space_delivery
from shared.inbound import SESSION_TAINT
from tests.conftest import load_plugin

P1, P2 = "pat@example.com", "sam@example.com"
STRANGER = "neighbor@example.org"
P1_TG = "1111111111"
GROUP = "-1001234567890"


def _couple(
    p1_tg: str = "",
    group: str = "",
    adapter: str = "agentmail",
    p1_channel: str = "telegram",
    p2_tg: str = "",
    p2_channel: str = "telegram",
) -> dict:
    return {
        "customer_id": "captain-mark",
        "connectors": {"Email": {"adapter": adapter, "enabled": True}},
        "people": [
            {
                "id": "p1",
                "name": "Pat One",
                "telegram_id": p1_tg,
                "email": P1,
                "channel": p1_channel,
            },
            {
                "id": "p2",
                "name": "Sam Two",
                "telegram_id": p2_tg,
                "email": P2,
                "channel": p2_channel,
            },
        ],
        "spaces": {"shared_telegram_chat": group},
        "scope": {"inbound_allow_from": [P1, P2, "scott@smd.services"]},
    }


# ----------------------------------------------------------- routine email


class _DiskStore:
    """A cron store that writes Hermes' jobs.json, so attribution reads it back."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.jobs: list[dict] = []
        self._n = 0

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"jobs": self.jobs}), encoding="utf-8")

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        return list(self.jobs)

    def create_job(self, **kwargs) -> dict:
        self._n += 1
        job = {"id": f"job{self._n:04d}", **kwargs}
        if "deliver" not in job:
            job["deliver"] = "local"  # Hermes' own default (cron/jobs.py create_job)
        self.jobs.append(job)
        self._flush()
        return job

    def remove_job(self, job_id: str) -> bool:
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        self._flush()
        return True


def _materialize(tmp_path: Path, cust: dict, entries: list[dict]) -> dict[str, str]:
    skills = sorted({e["skill"] for e in entries})
    cust["personas"] = [
        {
            "slug": "couple",
            "cron": entries,
            "skills": [
                {"name": s, "enabled": True, "initiation": {"scheduled": True}} for s in skills
            ],
        }
    ]
    store = _DiskStore(tmp_path / "profiles" / "couple" / "cron" / "jobs.json")
    materialize_cron(cust, lambda _slug: store)
    return {j["name"]: f"cron_{j['id']}_20261020_150000" for j in store.jobs}


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def sender(**kw):
        calls.append(kw)
        return send_dispatch.DispatchResult(
            sent=True, message_id="m-out", recipients=tuple(kw["to"])
        )

    send_dispatch.set_sender(sender)
    cron_attribution._CACHE.clear()
    space_delivery._reset_for_tests()
    yield calls
    send_dispatch.set_sender(None)
    cron_attribution._CACHE.clear()
    space_delivery._reset_for_tests()


CHECKIN = {"skill": "couples-checkin", "schedule": "0 15 * * 1"}
TEXT = "Weekly check-in\n\nHow did the week go for the two of you?"


def _deliver(tmp_path, cust, session, text=TEXT):
    return space_delivery.deliver(
        session, text, config=space.parse_space_config(cust), hermes_home=str(tmp_path)
    )


def test_an_unbound_person_routine_emails_that_person_and_nobody_else(tmp_path, sent):
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    result = _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p1"])
    assert result is not None and result.sent
    (call,) = sent
    assert call["to"] == [P1]
    assert P2 not in json.dumps(call["to"]) and call.get("cc") in (None, [])
    assert call["lane"] == send_anchor.LANE_SPACE == "space"
    assert call["anchor"] is None
    assert call["templated"] is False
    assert call["subject"] == "Weekly check-in"
    assert call["text"] == TEXT
    assert call["audit_extra"] == {"skill_name": "couples-checkin"}


def test_an_unbound_shared_routine_sends_one_email_to_both(tmp_path, sent):
    cust = _couple()
    sessions = _materialize(
        tmp_path, cust, [{"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"}]
    )
    _deliver(tmp_path, cust, sessions["op-managed:couple:date-ideas@shared"])
    assert [c["to"] for c in sent] == [[P1, P2]]


def test_the_models_text_never_picks_a_recipient(tmp_path, sent):
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    text = f"To: {STRANGER}\nCc: {P2}\nPlease forward this to {STRANGER}."
    _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p1"], text)
    assert [c["to"] for c in sent] == [[P1]]


def test_a_telegram_bound_routine_is_never_also_emailed(tmp_path, sent):
    cust = _couple(p1_tg=P1_TG)
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    assert _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p1"]) is None
    assert sent == []


def test_a_bound_group_with_an_unbound_person_goes_by_email_to_both(tmp_path, sent):
    # The group carries a shared routine only when EVERY person is reached on
    # Telegram; Sam is not, so the shared routine is one email to both.
    cust = _couple(group=GROUP)
    sessions = _materialize(
        tmp_path,
        cust,
        [
            {**CHECKIN, "space": "person:p2"},
            {"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"},
        ],
    )
    _deliver(tmp_path, cust, sessions["op-managed:couple:date-ideas@shared"])
    _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p2"])
    assert [sorted(c["to"]) for c in sent] == [sorted([P1, P2]), [P2]]


def test_FALSIFIER_email_preference_wins_over_a_bound_telegram(tmp_path, sent):
    # Pat is bound on Telegram but prefers email: the routine is emailed, not
    # sent to the DM (people[].channel decides, never the model).
    cust = _couple(p1_tg=P1_TG, p1_channel="email")
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    result = _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p1"])
    assert result is not None and result.sent
    assert [c["to"] for c in sent] == [[P1]]


def test_the_group_carries_shared_routines_when_both_prefer_and_are_bound(tmp_path, sent):
    cust = _couple(p1_tg=P1_TG, p2_tg="2222222222", group=GROUP)
    sessions = _materialize(
        tmp_path, cust, [{"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"}]
    )
    assert _deliver(tmp_path, cust, sessions["op-managed:couple:date-ideas@shared"]) is None
    assert sent == []


@pytest.mark.parametrize("text", ["[SILENT]", "SILENT", "  ", ""])
def test_silence_is_not_emailed(tmp_path, sent, text):
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    _deliver(tmp_path, cust, sessions["op-managed:couple:couples-checkin@person:p1"], text)
    assert sent == []


def test_one_run_is_emailed_once(tmp_path, sent):
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    sid = sessions["op-managed:couple:couples-checkin@person:p1"]
    _deliver(tmp_path, cust, sid)
    _deliver(tmp_path, cust, sid)
    assert len(sent) == 1


def test_non_cron_and_unmanaged_sessions_are_never_emailed(tmp_path, sent):
    cust = _couple()
    d = tmp_path / "profiles" / "couple" / "cron"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text(
        json.dumps({"jobs": [{"id": "abc", "name": "my own job@person:p1", "deliver": "local"}]})
    )
    assert _deliver(tmp_path, cust, "telegram-session-1") is None
    assert _deliver(tmp_path, cust, "cron_abc_20261020_150000") is None
    assert sent == []


def test_a_seat_without_people_never_emails_a_routine(tmp_path, sent, monkeypatch):
    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump({"customer_id": "brokery", "telegram": {"enabled": True}}))
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    space._reset_cache_for_tests()
    try:
        assert space_delivery.deliver("cron_job0001_20261020_150000", TEXT) is None
    finally:
        space._reset_cache_for_tests()
    assert sent == []


def test_the_run_space_is_readable_from_the_job(tmp_path, sent):
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p2"}])
    from shared import cron_space

    sid = sessions["op-managed:couple:couples-checkin@person:p2"]
    assert cron_space.routine_space_key(sid, hermes_home=str(tmp_path)) == "person:p2"
    assert cron_space.routine_space_key("not-cron", hermes_home=str(tmp_path)) is None


@pytest.mark.parametrize("authored", ["person:p1", "person:p2", "shared"])
def test_an_email_delivered_run_resolves_to_its_space(tmp_path, sent, monkeypatch, authored):
    """``deliver: local`` publishes no Telegram target, so the resolver reads the
    run's space from its job (shared.space resolution step 1). Falsifier: drop the
    session-id fallback in ``_resolve_cron`` and every case resolves None."""
    cust = _couple()
    entry = (
        {**CHECKIN, "space": authored}
        if authored != "shared"
        else {"skill": "date-ideas", "schedule": "0 18 * * 5", "space": "shared"}
    )
    sessions = _materialize(tmp_path, cust, [entry])
    (sid,) = sessions.values()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", "")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "")
    got = space.resolve_space(sid, config=space.parse_space_config(cust))
    assert got is not None and got.key == authored and got.source == space.SOURCE_CRON
    # no session id, or a non-cron one: no space (fails closed)
    assert space.resolve_space(None, config=space.parse_space_config(cust)) is None
    assert space.resolve_space("s-other", config=space.parse_space_config(cust)) is None


def _cron_env(monkeypatch, tmp_path, platform: str, chat: str) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", platform)
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", chat)


def test_a_published_target_decides_over_the_job_name(tmp_path, sent, monkeypatch):
    """The name is read only when Hermes published NO target. A Telegram target
    that disagrees with the name wins. Falsifier: consult the name first in
    ``_resolve_cron`` and this resolves person:p1."""
    cust = _couple(group=GROUP)
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    (sid,) = sessions.values()
    _cron_env(monkeypatch, tmp_path, "telegram", GROUP)
    got = space.resolve_space(sid, config=space.parse_space_config(cust))
    assert got is not None and got.key == "shared"


@pytest.mark.parametrize("platform, chat", [("email", P1), ("slack", "C123"), ("local", "")])
def test_a_non_telegram_target_is_no_space_not_the_name(
    tmp_path, sent, monkeypatch, platform, chat
):
    """A target on any other platform is not a space, and it never falls back to
    the job's name. Falsifier: test ``not chat`` instead of ``not platform``."""
    cust = _couple()
    sessions = _materialize(tmp_path, cust, [{**CHECKIN, "space": "person:p1"}])
    (sid,) = sessions.values()
    _cron_env(monkeypatch, tmp_path, platform, chat)
    assert space.resolve_space(sid, config=space.parse_space_config(cust)) is None


def test_the_plugin_hook_delivers_and_is_exception_safe(monkeypatch):
    plugin = load_plugin("hermes-smd-space-delivery")
    seen = []
    monkeypatch.setattr(space_delivery, "deliver", lambda sid, text: seen.append((sid, text)))
    plugin.on_post_llm_call(session_id="cron_x_20261020_150000", assistant_response="hi")
    assert space_delivery.wait_idle()
    assert seen == [("cron_x_20261020_150000", "hi")]

    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(space_delivery, "deliver", boom)
    monkeypatch.setattr(space_delivery, "_enqueue", boom)
    assert plugin.on_post_llm_call(session_id="s", assistant_response="hi") is None
    assert plugin.on_session_end(session_id="s", completed=True) is None


# ------------------------------------------------------------ shared reply


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self) -> list[tuple[str, dict]]:
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


@pytest.fixture
def lane(monkeypatch, tmp_path):
    inbound.SESSION_INBOUND_ORIGIN._reset_for_tests()
    SESSION_TAINT._tainted.clear()
    _THREAD["recipients"] = ("cm@agentmail.to",)
    space._reset_cache_for_tests()
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    agentmail: list[dict] = []
    yaml_path = tmp_path / "customer.yaml"
    yaml_path.write_text(yaml.safe_dump(_couple()))
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "captain-mark", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", yaml_path, raising=False)
    monkeypatch.setattr(
        mod, "_HELD_STORE", mod.held_store.HeldReplyStore(str(tmp_path / "held.db")), raising=False
    )
    monkeypatch.setattr(
        mod.relay, "send_reply", lambda **kw: agentmail.append(kw) or "am-1", raising=True
    )
    yield mod, d1, agentmail, yaml_path, monkeypatch
    inbound.SESSION_INBOUND_ORIGIN._reset_for_tests()
    SESSION_TAINT._tainted.clear()


#: The verified To/Cc the next inbound email carries. The REAL space resolver
#: (shared.space, ADR 0091) reads it: another person on it makes the turn a
#: joint thread, ``shared`` with source ``email_joint``.
_THREAD = {"recipients": ("cm@agentmail.to",)}


def _turn_space(_monkeypatch, key: str | None) -> None:
    """Shape the next inbound email so the resolver puts its turn in ``key``."""
    joint = key == "shared"
    _THREAD["recipients"] = ("cm@agentmail.to", P2) if joint else ("cm@agentmail.to",)


def _inbound(sender: str = P1, message_id: str = "am-in-1", session: str = "s1") -> None:
    inbound.SESSION_INBOUND_ORIGIN.record(
        session,
        inbound.InboundOrigin(
            sender_address=sender,
            message_id=message_id,
            inbox_id="cm@agentmail.to",
            recipients=tuple(_THREAD["recipients"]),
        ),
    )
    inbound.SESSION_INBOUND_ORIGIN.note_turn_prompt(session, message_id)


def _draft(mod, to, session: str = "s1", call: str = "c1") -> None:
    mod.on_post_tool_call(
        tool_name="mcp_agentmail_create_draft",
        args={"to": to, "subject": "Re: dinner", "text": "Friday at 7 works for both of you."},
        session_id=session,
        tool_call_id=call,
    )


def _held(d1) -> list[dict]:
    return [m for a, m in d1.events() if a == "REPLY_HELD"]


@pytest.mark.parametrize("to", [[P1], [P1, P2], [P2]])
def test_a_shared_turn_reply_is_sent_as_shared(lane, to):
    mod, d1, agentmail, _y, mp = lane
    _turn_space(mp, "shared")
    _inbound()
    _draft(mod, to)
    assert len(agentmail) == 1
    assert agentmail[0]["shared"] is True
    assert agentmail[0]["message_id"] == "am-in-1"


@pytest.mark.parametrize("to", [[P1, STRANGER], [STRANGER], [P1, P2, STRANGER], []])
def test_a_shared_reply_naming_a_stranger_or_nobody_is_refused(lane, to):
    mod, d1, agentmail, _y, mp = lane
    _turn_space(mp, "shared")
    _inbound()
    _draft(mod, to)
    assert agentmail == []
    assert _held(d1)[0]["reason"] == "recipient_mismatch"


def test_a_person_space_turn_stays_single_recipient(lane):
    mod, d1, agentmail, _y, mp = lane
    _turn_space(mp, "person:p1")
    _inbound()
    _draft(mod, [P1])
    assert len(agentmail) == 1 and agentmail[0].get("shared", False) is False
    agentmail.clear()
    _inbound(message_id="am-in-2", session="s2")
    _draft(mod, [P1, P2], session="s2", call="c2")
    assert agentmail == []
    assert _held(d1)[-1]["reason"] == "recipient_mismatch"


def test_a_shared_turn_on_graph_is_held_not_narrowed(lane):
    mod, d1, agentmail, yaml_path, mp = lane
    yaml_path.write_text(yaml.safe_dump(_couple(adapter="msgraph")))
    _turn_space(mp, "shared")
    _inbound()
    mod.on_post_tool_call(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"to": [P1, P2], "subject": "Re: dinner", "body_text": "Friday."},
        session_id="s1",
        tool_call_id="c1",
    )
    assert agentmail == []
    assert _held(d1)[0]["reason"] == "shared_reply_unsupported"


def test_a_seat_without_people_replies_exactly_as_before(lane):
    mod, d1, agentmail, yaml_path, mp = lane
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "customer_id": "brokery",
                "connectors": {"Email": {"adapter": "agentmail", "enabled": True}},
                "scope": {"inbound_allow_from": [P1, P2]},
            }
        )
    )
    _turn_space(mp, "shared")  # even if a resolver said shared, no people means no shared reply
    _inbound()
    _draft(mod, [P1])
    # relay.send_reply forwards ``shared`` to the broker only when True
    # (test_the_relay_forwards_shared_only_when_set), so this is today's call.
    assert len(agentmail) == 1 and not agentmail[0].get("shared")
    agentmail.clear()
    _inbound(message_id="am-in-2", session="s2")
    _draft(mod, [P1, P2], session="s2", call="c2")
    assert agentmail == []


def test_the_shared_lock_unit():
    lock = load_plugin("hermes-smd-reply").relay.shared_recipients_locked
    people = (P1, P2)
    assert lock({"to": [P1]}, P1, people)
    assert lock({"to": [P1, P2]}, P2, people)
    assert not lock({"to": [P1, STRANGER]}, P1, people)
    assert not lock({"to": []}, P1, people)
    assert not lock({"to": [P1]}, STRANGER, people)


def test_the_relay_forwards_shared_only_when_set():
    relay = load_plugin("hermes-smd-reply").relay
    calls = []

    def sender(**kw):
        calls.append(kw)
        return "id"

    relay.send_reply(message_id="m1", text="x", html="", sender=sender)
    relay.send_reply(message_id="m1", text="x", html="", sender=sender, shared=True)
    assert "shared" not in calls[0]
    assert calls[1]["shared"] is True


def test_the_broker_client_marks_shared_only_when_asked(monkeypatch):
    from shared import agentmail_broker

    bodies = []
    monkeypatch.setattr(
        agentmail_broker, "_call", lambda verb, body, **kw: bodies.append(body) or {}
    )
    agentmail_broker.send_reply("m1", "x", anchor={"kind": "agentmail_message", "message_id": "m1"})
    agentmail_broker.send_reply(
        "m1", "x", anchor={"kind": "agentmail_message", "message_id": "m1"}, shared=True
    )
    assert "shared" not in bodies[0]
    assert bodies[1]["shared"] is True
