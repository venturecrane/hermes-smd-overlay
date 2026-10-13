"""The lane-proof gate's page (shared/lane_proofs.py, the lane_unproven cards in
shared/request_cards.py).

The seat half of "SMD hears about a new kind of ask immediately": a job the
root daemon holds because its kind is not proven on the seat (the hold note on
its ledger row) is carded at once, under its request or on its own when it has
none; an in-turn skill read by a replying session that is not proven is carded
once per seat; a proven lane is never carded."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from shared import lane_proofs
from shared import request_cards as rc
from tests.test_request_cards import IMID, NOW, VID, _Ledger, _request

HELD = "01M4RW99P9XHWPJ228MSSWEXGG"
# The console's key pattern (ss-console src/lib/operator/request-card.ts).
CONSOLE_KEY_RE = re.compile(
    r"^[0-9a-f]{64}:(?:(replied|no_reply)|(job_done)(?::(?:delivered|failed|held)-[1-9]\d{0,3})?"
    r"|(lane_unproven):([a-z0-9][a-z0-9-]{0,63}(?:\.[a-z0-9_]{1,40})?))$"
)


def _proofs(
    tmp_path: Path, lanes: dict | None = None, events: list | None = None
) -> lane_proofs.Proofs:
    d = tmp_path / "proofs"
    d.mkdir(exist_ok=True)
    (d / "lane-proofs.yaml").write_text(
        yaml.safe_dump({"schema": 1, "lanes": lanes or {}, "revoked": []})
    )
    if events is not None:
        (d / "lane-proofs.runtime.json").write_text(json.dumps({"events": events}))
    return lane_proofs.load(d)


@pytest.fixture
def ledger(tmp_path) -> _Ledger:
    led = _Ledger(tmp_path / "audit.db")
    for table in ("litigation_jobs", "negotiation_jobs"):
        led.conn.execute(
            f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, state TEXT, "
            "request_ref TEXT, reason TEXT)"
        )
    led.conn.commit()
    return led


def _hold(led: _Ledger, table: str, key: str, *, ref=IMID, state="submitted", job_id=HELD) -> None:
    led.conn.execute(
        f"INSERT INTO {table} (id, created_at, updated_at, state, request_ref, reason) VALUES (?,?,?,?,?,?)",
        (
            job_id,
            "2026-10-07T11:58:00Z",
            "2026-10-07T11:58:30Z",
            state,
            ref,
            f"lane_unproven: {key}",
        ),
    )
    if table in ("demand_jobs", "drafting_jobs", "medchron_jobs"):
        led.conn.execute(f"UPDATE {table} SET matter_number = '2024-0042' WHERE id = ?", (job_id,))
    led.conn.commit()


def _decide(led, requests, proofs, done=()):
    conn = led.ro()
    try:
        return rc.decide(requests, conn, set(done), NOW, proofs=proofs)
    finally:
        conn.close()


def _lane_cards(cards):
    return [c for c in cards if c["kind"] == "lane_unproven"]


def test_a_held_job_is_carded_under_its_request_at_once(ledger, tmp_path):
    _hold(ledger, "drafting_jobs", "document-drafter.discovery_set")
    cards = _lane_cards(_decide(ledger, [_request(minutes_ago=2)], _proofs(tmp_path)))
    assert len(cards) == 1
    c = cards[0]
    assert c["lane"] == {
        "key": "document-drafter.discovery_set",
        "job_lane": "drafting",
        "held_job": HELD,
        "why": "never_proven",
    }
    assert c["card_key"].startswith(hashlib.sha256(VID.encode()).hexdigest() + ":lane_unproven:")
    assert CONSOLE_KEY_RE.match(c["card_key"]) and c["job"] is None
    assert c["who"] == "paralegal@firm.example" and c["matter"] == "2024-0042"
    # done: never again; proven since: never at all
    assert (
        _lane_cards(
            _decide(ledger, [_request(minutes_ago=2)], _proofs(tmp_path), done={c["card_key"]})
        )
        == []
    )
    proven = _proofs(
        tmp_path, events=[{"event": "proven", "key": "document-drafter.discovery_set"}]
    )
    assert _lane_cards(_decide(ledger, [_request(minutes_ago=2)], proven)) == []


def test_a_scheduled_hold_with_no_request_is_carded_on_its_own(ledger, tmp_path):
    _hold(ledger, "negotiation_jobs", "negotiation-watch", ref=None, state="queued")
    cards = _lane_cards(_decide(ledger, [], _proofs(tmp_path)))
    assert len(cards) == 1
    assert (
        cards[0]["card_key"]
        == hashlib.sha256(f"job:{HELD}".encode()).hexdigest() + ":lane_unproven:negotiation-watch"
    )
    assert cards[0]["lane"]["job_lane"] == "negotiation" and CONSOLE_KEY_RE.match(
        cards[0]["card_key"]
    )


def test_an_unreadable_record_still_pages_a_hold_and_says_so(ledger, tmp_path):
    _hold(ledger, "demand_jobs", "demand-letter-drafter")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "lane-proofs.yaml").write_text("lanes: [x]")
    cards = _lane_cards(_decide(ledger, [_request()], lane_proofs.load(broken)))
    assert cards and cards[0]["lane"]["why"] == "unreadable"


def test_a_note_that_is_not_a_hold_is_not_a_card(ledger, tmp_path):
    ledger.job("demand_jobs", "submitted", 1, reason="refused: something else")
    assert _lane_cards(_decide(ledger, [_request()], _proofs(tmp_path))) == []


def _in_turn(ledger: _Ledger, skill: str, *, session="s1", vid=VID) -> None:
    ledger.reply(3, session=session, vid=vid)
    ledger.call(4, "skill_view", session=session, skill_procedure=skill)


def test_an_unproven_in_turn_skill_is_carded_once_per_seat(ledger, tmp_path):
    _in_turn(ledger, "rep-letter-request")
    _in_turn(ledger, "rep-letter-request", session="s2", vid="graph-msg-2")
    reqs = [_request(minutes_ago=10), _request(minutes_ago=8, vendor_message_id="graph-msg-2")]
    cards = _lane_cards(_decide(ledger, reqs, _proofs(tmp_path)))
    assert len(cards) == 1
    assert cards[0]["lane"] == {
        "key": "rep-letter-request",
        "job_lane": None,
        "held_job": None,
        "why": "never_proven",
    }
    assert (
        cards[0]["card_key"]
        == hashlib.sha256(b"lane:rep-letter-request").hexdigest()
        + ":lane_unproven:rep-letter-request"
    )


def test_a_proven_skill_or_a_proven_kind_of_it_is_not_carded(ledger, tmp_path):
    _in_turn(ledger, "document-drafter")
    proofs = _proofs(tmp_path, lanes={"document-drafter.mediation_brief": {"evidence": "x"}})
    assert _lane_cards(_decide(ledger, [_request(minutes_ago=10)], proofs)) == []


def test_in_turn_cards_wait_for_a_readable_record(ledger, tmp_path):
    _in_turn(ledger, "rep-letter-request")
    assert (
        _lane_cards(
            _decide(ledger, [_request(minutes_ago=10)], lane_proofs.load(tmp_path / "absent"))
        )
        == []
    )


def test_without_proofs_the_decider_cards_no_lanes(ledger, tmp_path):
    _hold(ledger, "drafting_jobs", "document-drafter.discovery_set")
    conn = ledger.ro()
    try:
        assert _lane_cards(rc.decide([_request()], conn, set(), NOW)) == []
    finally:
        conn.close()


def test_the_leg_pages_a_scheduled_hold_with_an_empty_index(ledger, tmp_path, monkeypatch):
    """The leg used to return before reading anything when no request was
    indexed; a held scheduled run would never have paged."""
    _hold(ledger, "negotiation_jobs", "negotiation-watch", ref=None, state="queued")
    _proofs(tmp_path)
    monkeypatch.setattr(rc.request_index, "read_requests", lambda *a, **k: [])
    posted = []

    def post(url, headers, body, timeout):
        posted.append(json.loads(body))
        return 200

    landed = rc.send_due_cards(
        slug="example",
        key="k",
        url="https://smd.services/api/internal/operator-request-card",
        audit_db_path=str(ledger.path),
        post_fn=post,
        store=rc.CardStore(str(tmp_path / "cards.db")),
        now_fn=lambda: NOW.timestamp(),
        proofs_dir=str(tmp_path / "proofs"),
    )
    assert landed == 1 and posted[0]["kind"] == "lane_unproven"


def test_the_rule_matches_the_contract_cases_ss_console_also_reads(tmp_path):
    cases = json.loads((Path(__file__).parent / "contract" / "lane_proofs_cases.json").read_text())
    for i, case in enumerate(cases["cases"]):
        d = tmp_path / f"c{i}"
        d.mkdir()
        if case["authored"] is not None:
            (d / "lane-proofs.yaml").write_text(yaml.safe_dump(case["authored"]))
        (d / "lane-proofs.runtime.json").write_text(json.dumps({"events": case["events"]}))
        p = lane_proofs.load(d)
        assert {k: p.is_proven(k) for k in case["expect"]} == case["expect"], case["name"]
