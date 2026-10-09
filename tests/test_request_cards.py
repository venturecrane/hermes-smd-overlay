"""Request cards: the seat index, the decider, the hooks, and the heartbeat leg.

The feature (ss-console plan "Operator request cards + no-reply alarm"): one
email card to SMD per request a person sends a seat, a NO REPLY alarm at 30
minutes, and a follow-up card when a queued job the request started finishes.
These tests pin the seat half: what the index keeps and for how long, when a
card is due and when it must stay quiet, that every send path records the reply
opening, and that the leg marks a card done only on a 200 inside its budget.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from shared import heartbeat as hb
from shared import request_cards as rc
from shared import request_index as ri
from shared.audit_contract import CREATE_TABLE_SQL
from shared.connector_check import ConnectorCheck
from shared.scheduler_check import SchedulerCheck
from shared.spec_control_check import SpecControlCheck
from tests.conftest import load_plugin

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
VID = "graph-msg-1"
IMID = "<abc@firm.example>"


def _ts(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _epoch(minutes_ago: float) -> float:
    return (NOW - timedelta(minutes=minutes_ago)).timestamp()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Ledger:
    """An audit db with the real audit_log DDL, plus optional job tables."""

    def __init__(self, path: Path, *, job_tables: bool = True) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.execute(CREATE_TABLE_SQL)
        if job_tables:
            for table in ("demand_jobs", "drafting_jobs", "medchron_jobs"):
                self.conn.execute(
                    f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TEXT, "
                    "updated_at TEXT, state TEXT, matter_number TEXT, request_ref TEXT, "
                    "reason TEXT)"
                )
        self.conn.commit()
        self._n = 0

    def row(self, action_type: str, minutes_ago: float, *, matter_ref=None, **metadata) -> None:
        self._n += 1
        self.conn.execute(
            "INSERT INTO audit_log (id, ts, action_type, actor, matter_ref, metadata) "
            "VALUES (?,?,?,?,?,?)",
            (
                f"r{self._n:04d}",
                _ts(minutes_ago),
                action_type,
                "agent",
                matter_ref,
                json.dumps(metadata),
            ),
        )
        self.conn.commit()

    def reply(self, minutes_ago: float, *, session="s1", vid=VID, matter_ref=None) -> None:
        self.row(
            "REPLY_SENT", minutes_ago, matter_ref=matter_ref, in_reply_to=vid, session_id=session
        )

    def call(self, minutes_ago: float, tool: str, *, session="s1", **extra) -> None:
        self.row(
            "TOOL_CALL_COMPLETED",
            minutes_ago,
            tool=tool,
            session_id=session,
            outcome=extra.pop("outcome", "ok"),
            **extra,
        )

    def job(self, table: str, state: str, minutes_ago: float, *, ref=IMID, reason=None) -> None:
        self.conn.execute(
            f"INSERT INTO {table} (id, created_at, updated_at, state, matter_number, "
            "request_ref, reason) VALUES (?,?,?,?,?,?,?)",
            (f"j{state}", _ts(minutes_ago + 5), _ts(minutes_ago), state, "2024-0042", ref, reason),
        )
        self.conn.commit()

    def ro(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)


@pytest.fixture
def ledger(tmp_path) -> _Ledger:
    return _Ledger(tmp_path / "audit.db")


def _request(minutes_ago: float = 60, **overrides) -> dict:
    row = {
        "vendor_message_id": VID,
        "internet_message_id": IMID,
        "received_at": _epoch(minutes_ago),
        "sender": "paralegal@firm.example",
        "subject": "Pull the records",
        "reply_opening": "On it, pulling the file now.",
        "opening_at": None,
    }
    row.update(overrides)
    return row


def _decide(ledger: _Ledger, requests, done=(), now=NOW) -> list[dict]:
    conn = ledger.ro()
    try:
        return rc.decide(requests, conn, set(done), now)
    finally:
        conn.close()


def _kinds(cards) -> list[str]:
    return [c["kind"] for c in cards]


# ---------------------------------------------------------------------------
# request_index
# ---------------------------------------------------------------------------


def test_reply_opening_drops_the_salutation_and_collapses_whitespace() -> None:
    body = "Hi Sam,\n\nI pulled the   file.\nThe records\tare filed."
    assert ri.reply_opening(body) == "I pulled the file. The records are filed."


def test_reply_opening_cuts_long_bodies_at_a_word() -> None:
    opening = ri.reply_opening("Hello,\n" + "word " * 200)
    assert opening is not None and opening.endswith("...")
    assert len(opening) <= ri.OPENING_CHARS + 3
    assert "wor..." not in opening  # never mid-word


def test_reply_opening_keeps_a_first_line_that_is_not_a_greeting() -> None:
    assert (
        ri.reply_opening("The demand is filed in Smokeball.") == "The demand is filed in Smokeball."
    )
    assert ri.reply_opening("") is None and ri.reply_opening(None) is None


def test_record_inbound_first_write_wins_and_opening_is_first_reply_only(tmp_path) -> None:
    path = str(tmp_path / "r.db")
    assert ri.record_inbound(vendor_message_id=VID, subject="A", sender="x@y", path=path)
    assert not ri.record_inbound(vendor_message_id=VID, subject="B", sender="x@y", path=path)
    assert ri.record_reply_opening(VID, "Hi,\nfirst reply", path=path)
    assert not ri.record_reply_opening(VID, "Hi,\nsecond reply", path=path)
    assert not ri.record_reply_opening("unknown", "text", path=path)
    assert not ri.record_reply_opening("", "text", path=path)
    rows = ri.read_requests(0, path=path)
    assert len(rows) == 1
    assert rows[0]["subject"] == "A" and rows[0]["reply_opening"] == "first reply"


def test_custody_nulls_text_after_a_week_and_deletes_after_a_month(tmp_path) -> None:
    path = str(tmp_path / "r.db")
    now = time.time()
    ri.record_inbound(
        vendor_message_id="old", sender="a@b", subject="s", received_at=now - 8 * 86400, path=path
    )
    ri.record_inbound(
        vendor_message_id="gone", sender="a@b", subject="s", received_at=now - 31 * 86400, path=path
    )
    # The custody pass rides on the NEXT write.
    ri.record_inbound(vendor_message_id="new", sender="a@b", subject="s", path=path)
    rows = {r["vendor_message_id"]: r for r in ri.read_requests(0, path=path)}
    assert set(rows) == {"old", "new"}
    assert rows["old"]["sender"] is None and rows["old"]["subject"] is None
    assert rows["new"]["subject"] == "s"


def test_index_writes_wait_out_a_second_connection(tmp_path) -> None:
    """Two writers in one process (the hook thread and the sweeper thread): a
    write that arrives while another connection holds the lock waits on the
    busy timeout and lands, rather than failing the card."""
    path = str(tmp_path / "r.db")
    ri.record_inbound(vendor_message_id="seed", path=path)
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.4, holder.commit)
    release.start()
    try:
        assert ri.record_inbound(vendor_message_id="second", path=path) is True
    finally:
        release.join()
        holder.close()
    assert {r["vendor_message_id"] for r in ri.read_requests(0, path=path)} == {"seed", "second"}


def test_index_never_raises_on_an_unopenable_path(tmp_path, caplog) -> None:
    bad = str(tmp_path / "missing-dir" / "r.db")
    with caplog.at_level(logging.ERROR):
        assert ri.record_inbound(vendor_message_id=VID, path=bad) is False
        assert ri.record_reply_opening(VID, "x", path=bad) is False
    assert "request_index" in caplog.text


# ---------------------------------------------------------------------------
# The decider
# ---------------------------------------------------------------------------


def test_replied_card_after_quiet_carries_tools_matter_and_minutes(ledger) -> None:
    ledger.call(52, "mcp_smokeball_get_matter")
    ledger.call(51, "MCP_Smokeball.Search Documents")
    ledger.reply(50, matter_ref="matter-77")
    cards = _decide(ledger, [_request(60)])
    assert _kinds(cards) == ["replied"]
    card = cards[0]
    assert card["card_key"] == rc.card_key(VID, "replied")
    assert len(card["card_key"]) == 64 + len(":replied")
    assert card["minutes"] == 10
    assert card["tools"] == ["mcp_smokeball_get_matter", "mcp_smokeball.search_documents"]
    assert card["matter"] == "matter-77"
    assert card["reply_opening"] == "On it, pulling the file now."
    assert (card["replies"], card["refused"], card["failed"]) == (1, 0, 0)
    assert card["who"] == "paralegal@firm.example" and card["job"] is None


def test_replied_card_waits_for_the_reply_to_go_quiet(ledger) -> None:
    ledger.reply(2)
    assert _decide(ledger, [_request(10)]) == []


def test_ack_then_work_is_carded_once_after_the_work(ledger) -> None:
    ledger.reply(20)  # "on it"
    ledger.call(15, "smokeball_upload")
    ledger.call(2, "smokeball_upload")  # still working
    assert _decide(ledger, [_request(30)]) == []
    ledger.reply(1)  # the real answer
    later = NOW + timedelta(minutes=10)
    cards = _decide(ledger, [_request(30)], now=later)
    assert _kinds(cards) == ["replied"]
    assert cards[0]["replies"] == 2
    assert cards[0]["minutes"] == 10  # to the FIRST reply


def test_refused_and_failed_calls_are_counted_like_the_shortfall_alert(ledger) -> None:
    ledger.call(30, "send_message", trust_decision="refuse", trust_reason="exposure: no")
    ledger.call(29, "smokeball_upload", outcome="error", error_type="timeout")
    ledger.call(28, "smokeball_get", outcome="error", error_type="timeout", object_digest="d")
    ledger.call(27, "smokeball_get", object_digest="d")  # retried ok: not an event
    ledger.reply(26)
    card = _decide(ledger, [_request(40)])[0]
    assert (card["refused"], card["failed"]) == (1, 1)


def test_no_reply_alarm_once_after_thirty_minutes(ledger) -> None:
    assert _decide(ledger, [_request(29)]) == []
    cards = _decide(ledger, [_request(31)])
    assert _kinds(cards) == ["no_reply"]
    assert cards[0]["minutes"] == 31 and cards[0]["reply_opening"] is None
    assert _decide(ledger, [_request(31)], done={rc.card_key(VID, "no_reply")}) == []


def test_a_hold_suppresses_the_alarm_but_a_hold_queued_for_release_does_not(ledger) -> None:
    ledger.row("REPLY_HELD", 40, message_id=VID, held_for_release=True)
    assert _kinds(_decide(ledger, [_request(45)])) == ["no_reply"]
    ledger.row("REPLY_FAILED", 35, message_id=VID, reason="hold_expired")
    assert _decide(ledger, [_request(45)]) == []


def test_a_queued_job_suppresses_the_alarm_and_cards_when_it_ends(ledger) -> None:
    ledger.job("demand_jobs", "running", 10)
    assert _decide(ledger, [_request(60)]) == []
    ledger.conn.execute(
        "UPDATE demand_jobs SET state='failed', updated_at=?, reason=?",
        (_ts(5), "Quote check failed: page 4 of the records"),
    )
    ledger.conn.commit()
    cards = _decide(ledger, [_request(60)])
    assert _kinds(cards) == ["job_done"]
    assert cards[0]["job"] == {"lane": "demand", "state": "failed", "reason": "quote"}
    assert cards[0]["minutes"] == 55 and cards[0]["matter"] == "2024-0042"
    assert "page 4" not in json.dumps(cards[0])  # free-text reason never leaves


def test_medchron_job_joins_on_request_ref(ledger) -> None:
    ledger.reply(50)
    ledger.job("medchron_jobs", "delivered", 3)
    kinds = _kinds(_decide(ledger, [_request(60)]))
    assert sorted(kinds) == ["job_done", "replied"]


def test_a_drafting_job_suppresses_the_alarm_and_cards_when_it_ends(ledger) -> None:
    """FALSIFIER: drop "drafting" from JOB_LANES and a queued drafting job
    neither suppresses the no-reply alarm nor cards when it ends."""
    ledger.job("drafting_jobs", "running", 10)
    assert _decide(ledger, [_request(60)]) == []
    ledger.conn.execute("UPDATE drafting_jobs SET state='delivered', updated_at=?", (_ts(4),))
    ledger.conn.commit()
    cards = _decide(ledger, [_request(60)])
    assert _kinds(cards) == ["job_done"]
    assert cards[0]["job"] == {"lane": "drafting", "state": "delivered", "reason": None}
    assert cards[0]["matter"] == "2024-0042"


def _litigation_table(ledger: _Ledger) -> None:
    """The litigation ledger's columns this lane reads; no matter_number (a
    status job spans the firm's matters), so the lane must select NULL for it."""
    ledger.conn.execute(
        "CREATE TABLE litigation_jobs (id TEXT PRIMARY KEY, created_at TEXT, "
        "updated_at TEXT, state TEXT, request_ref TEXT, reason TEXT)"
    )
    ledger.conn.commit()


def _litigation_job(ledger: _Ledger, state: str, minutes_ago: float, ref: str = IMID) -> None:
    ledger.conn.execute(
        "INSERT INTO litigation_jobs (id, created_at, updated_at, state, request_ref, reason) "
        "VALUES (?,?,?,?,?,?)",
        (f"l{state}{ref}", _ts(minutes_ago + 5), _ts(minutes_ago), state, ref, None),
    )
    ledger.conn.commit()


def test_a_litigation_job_suppresses_the_alarm_and_cards_when_it_ends(ledger) -> None:
    """FALSIFIER: drop "litigation" from JOB_LANES and a queued status job
    neither suppresses the no-reply alarm nor cards when it ends."""
    _litigation_table(ledger)
    _litigation_job(ledger, "running", 10)
    assert _decide(ledger, [_request(60)]) == []
    ledger.conn.execute("UPDATE litigation_jobs SET state='delivered', updated_at=?", (_ts(4),))
    ledger.conn.commit()
    cards = _decide(ledger, [_request(60)])
    assert _kinds(cards) == ["job_done"]
    assert cards[0]["job"] == {"lane": "litigation", "state": "delivered", "reason": None}
    assert cards[0]["matter"] is None


def test_a_scheduled_litigation_job_cards_no_request(ledger) -> None:
    """A scheduled run's ref is "scheduled:<date>", never a request's id."""
    _litigation_table(ledger)
    _litigation_job(ledger, "delivered", 4, ref="scheduled:2026-10-07")
    assert _kinds(_decide(ledger, [_request(60)])) == ["no_reply"]


#: The console's reason contract (ss-console src/lib/operator/request-card.ts
#: TOKEN_RE): a card whose job reason fails it is refused outright.
_CONSOLE_TOKEN_RE = re.compile(r"^[a-z0-9_:.-]{1,64}$")

#: The shapes of reason the drafting runner records through drafting_job_record
#: (ss-console #3093, medchron/drafting/run.py + limits.py): free-text sentences,
#: a limit's setting-led sentence, and an unexpected exception's class name.
_DRAFTING_RUNNER_REASONS = [
    "the request is missing what the draft needs: the deponent's name",
    "filing refused: the upload stage refused",
    "the drafting gate refused the document (hold): text quoting Secret Client.pdf",
    "the format check refused the rendered document: caption missing",
    "compose output still unfinished after 3 continuations",
    "repair did not return 2 flagged section(s)",
    "drafting_monthly_budget_cents: the run's spend reached the monthly cost budget during compose",
    "KeyError: 'Secret Client'",
    "/opt/data/vaults/acme/drafting/firm.yaml: not valid YAML (bad indent)",
    "[Errno 2] No such file or directory: 'Secret Client.pdf'",
    "the runner exited 1 without a verdict",
    "Ünïcode leading word",
    "x" * 500,
]


@pytest.mark.parametrize("reason", _DRAFTING_RUNNER_REASONS)
def test_every_drafting_reason_leaves_as_a_console_valid_token_or_nothing(ledger, reason) -> None:
    """The overlay tokenises every lane's reason to its leading word; the free
    text after it never leaves the seat. FALSIFIER: send job["reason"] raw and
    the console refuses the card (and the tail can name a document)."""
    ledger.job("drafting_jobs", "held", 3, reason=reason)
    cards = _decide(ledger, [_request(60)])
    assert _kinds(cards) == ["job_done"]
    token = cards[0]["job"]["reason"]
    assert token is None or _CONSOLE_TOKEN_RE.match(token), token
    assert "secret" not in json.dumps(cards[0]).lower()


def test_a_limit_hold_leaves_as_its_setting(ledger) -> None:
    ledger.job(
        "drafting_jobs",
        "failed",
        3,
        reason="drafting_monthly_budget_cents: the month's spend reached the budget during compose",
    )
    job = _decide(ledger, [_request(60)])[0]["job"]
    assert job == {"lane": "drafting", "state": "failed", "reason": "drafting_monthly_budget_cents"}


def test_a_seat_with_only_the_older_job_tables_still_decides(tmp_path) -> None:
    """A seat whose ledger predates the drafting lane has no drafting_jobs table."""
    led = _Ledger(tmp_path / "audit.db", job_tables=False)
    for table in ("demand_jobs", "medchron_jobs"):
        led.conn.execute(
            f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TEXT, "
            "updated_at TEXT, state TEXT, matter_number TEXT, request_ref TEXT, "
            "reason TEXT)"
        )
    led.job("demand_jobs", "delivered", 3)
    assert _kinds(_decide(led, [_request(60)])) == ["job_done"]


def test_late_reply_after_the_alarm_is_still_carded(ledger) -> None:
    ledger.reply(10)
    cards = _decide(ledger, [_request(60)], done={rc.card_key(VID, "no_reply")})
    assert _kinds(cards) == ["replied"] and cards[0]["minutes"] == 50


def test_a_seat_without_job_tables_still_decides(tmp_path) -> None:
    led = _Ledger(tmp_path / "audit.db", job_tables=False)
    assert _kinds(_decide(led, [_request(40)])) == ["no_reply"]


def test_custody_nulled_text_degrades_to_markers_never_invented(ledger) -> None:
    card = _decide(ledger, [_request(40, sender=None, subject=None)])[0]
    assert card["who"] == "(unknown sender)" and card["subject"] == "(no subject)"


# ---------------------------------------------------------------------------
# Router + reply hooks
# ---------------------------------------------------------------------------


def _rostered_router(tmp_path, monkeypatch):
    from tests.test_inbound import _load_router_with_table

    mod, _ = _load_router_with_table(tmp_path, monkeypatch)
    (tmp_path / "customer.yaml").write_text(
        dedent(
            """
            customer_id: acme
            scope:
              inbound_allow_from:
                - colleague@firm.example
            webhook_triggers:
              - source: agentmail
                event_type: message.received
                skill: triage_inbox
                persona: assistant
            """
        ).strip()
    )
    return mod


def _mail(sender: str, message_id: str, **extra) -> dict:
    return {
        "source": "agentmail",
        "event_type": "message.received",
        "data": {
            "inbox_id": "inbox_1",
            "message_id": message_id,
            "from": sender,
            "subject": "Pull the records",
            "text": "Please pull the records.",
            **extra,
        },
    }


def test_router_indexes_a_rostered_person_and_nobody_else(tmp_path, monkeypatch) -> None:
    from tests.test_inbound import _signed_kwargs

    mod = _rostered_router(tmp_path, monkeypatch)
    mod.on_pre_gateway_dispatch(
        **_signed_kwargs(_mail("Colleague <colleague@firm.example>", "m-int"), event_id="e1")
    )
    mod.on_pre_gateway_dispatch(
        **_signed_kwargs(_mail("Stranger <stranger@evil.test>", "m-ext"), event_id="e2")
    )
    mod.on_pre_gateway_dispatch(
        **_signed_kwargs(
            _mail(
                "Colleague <colleague@firm.example>",
                "m-ooo",
                headers={"Auto-Submitted": "auto-replied"},
            ),
            event_id="e3",
        )
    )
    rows = ri.read_requests(0)
    assert [r["vendor_message_id"] for r in rows] == ["m-int"]
    assert rows[0]["sender"] == "colleague@firm.example"
    assert rows[0]["subject"] == "Pull the records"


def test_every_reply_sent_site_records_the_reply_opening() -> None:
    """GUARD: every ``action_type="REPLY_SENT"`` emission under plugins/ has a
    ``record_reply_opening`` call in the same (innermost) function. A new send
    path that forgets it would card every reply it sends with no opening."""
    root = Path(__file__).parent.parent / "plugins"
    sites: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node, func, path=path):
            for child in ast.iter_child_nodes(node):
                inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else func
                if isinstance(child, ast.Call) and any(
                    kw.arg == "action_type"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "REPLY_SENT"
                    for kw in child.keywords
                ):
                    assert func is not None, f"{path}: REPLY_SENT emitted outside a function"
                    calls = {
                        n.func.attr
                        if isinstance(n.func, ast.Attribute)
                        else getattr(n.func, "id", "")
                        for n in ast.walk(func)
                        if isinstance(n, ast.Call)
                    }
                    assert "record_reply_opening" in calls, (
                        f"{path}:{child.lineno} {func.name} emits REPLY_SENT without "
                        "record_reply_opening"
                    )
                    sites.append(f"{path.name}:{func.name}")
                visit(child, inner)

        visit(tree, None)
    # The three known send paths: live reply, seat-delivered act line, release.
    assert len(sites) >= 3, sites


def test_a_released_held_reply_records_its_opening(tmp_path) -> None:
    mod = load_plugin("hermes-smd-reply")
    ri.record_inbound(vendor_message_id="m-held", sender="greg@x.test", subject="s")
    store = mod.held_store.HeldReplyStore(str(tmp_path / "held.db"))
    store.enqueue(
        sender="greg@x.test",
        sender_class="internal",
        adapter="agentmail",
        inbox_id="inbox_x",
        message_id="m-held",
        send_text="Hi Greg,\nThe records are filed.",
        send_html="",
        body_digest="d",
        hold_reason="rate_limited_per_sender",
    )
    from tests.test_held_replies import RELEASE_ON

    result = mod.sweeper.run_sweep_once(
        store=store,
        limiter=mod.relay.RateLimiter(clock=lambda: 0.0),
        policy=RELEASE_ON,
        send_fn=lambda row: "sent-1",
        emit_fn=lambda **kw: None,
        notify_fn=None,
        internal_senders=None,
        now=None,
    )
    store.close()
    assert result.released == 1
    assert ri.read_requests(0)[0]["reply_opening"] == "The records are filed."


# ---------------------------------------------------------------------------
# The heartbeat leg
# ---------------------------------------------------------------------------


class _Poster:
    def __init__(self, statuses=None, *, clock=None, cost=0.0) -> None:
        self.calls: list[tuple[str, dict, dict, float]] = []
        self._statuses = list(statuses or [])
        self._clock = clock
        self._cost = cost

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body), timeout))
        if self._clock is not None:
            self._clock["t"] += self._cost
        return self._statuses.pop(0) if self._statuses else 200


def _leg(ledger, poster, *, now=NOW, clock=None, store=None):
    clock = clock if clock is not None else {"t": 0.0}
    store = store or rc.CardStore()
    landed = rc.send_due_cards(
        slug="pilot",
        key="k3y",
        url=rc.card_url("https://smd.services/api/internal/heartbeat"),
        audit_db_path=str(ledger.path),
        post_fn=poster,
        store=store,
        now_fn=lambda: now.timestamp(),
        monotonic_fn=lambda: clock["t"],
    )
    return landed, store


def _index(n: int, minutes_ago: float = 60) -> None:
    for i in range(n):
        ri.record_inbound(
            vendor_message_id=f"m{i:02d}",
            sender="p@firm.example",
            subject=f"r{i}",
            received_at=_epoch(minutes_ago) + i,
        )


def test_card_url_is_the_heartbeats_console() -> None:
    assert rc.card_url("https://smd.services/api/internal/heartbeat") == (
        "https://smd.services/api/internal/operator-request-card"
    )
    assert rc.card_url("http://localhost:8787/api/internal/heartbeat").startswith(
        "http://localhost:8787/"
    )


def test_leg_posts_with_heartbeat_auth_and_marks_done_only_on_200(ledger) -> None:
    _index(1)
    poster = _Poster([502])
    landed, store = _leg(ledger, poster)
    assert landed == 0 and len(poster.calls) == 1
    url, headers, body, timeout = poster.calls[0]
    assert url.endswith("/api/internal/operator-request-card")
    assert headers["Authorization"] == "Bearer k3y" and headers["X-Tenant-Slug"] == "pilot"
    assert body["kind"] == "no_reply" and timeout == rc.POST_TIMEOUT_SECONDS
    # Not done: the next tick retries and a 200 (fresh or duplicate) lands it.
    landed, _ = _leg(ledger, _Poster([200]), store=store)
    assert landed == 1
    quiet = _Poster()
    assert _leg(ledger, quiet, store=store)[0] == 0 and quiet.calls == []


def test_leg_caps_cards_per_tick(ledger) -> None:
    _index(14)
    poster = _Poster()
    landed, store = _leg(ledger, poster)
    assert landed == rc.MAX_CARDS_PER_TICK
    landed, _ = _leg(ledger, _Poster(), store=store)
    assert landed == 4


def test_leg_respects_its_time_budget(ledger) -> None:
    _index(8)
    clock = {"t": 0.0}
    poster = _Poster(clock=clock, cost=6.0)  # each POST eats 6 of the 20 seconds
    landed, _ = _leg(ledger, poster, clock=clock)
    assert landed == 4  # 0, 6, 12, 18 start; 24 >= 20 stops


def test_leg_gives_up_after_a_day_and_says_so_once(ledger, caplog) -> None:
    _index(1)
    _, store = _leg(ledger, _Poster([502]))
    later = NOW + rc.RETRY_FOR + timedelta(minutes=1)
    with caplog.at_level(logging.ERROR):
        never = _Poster()
        assert _leg(ledger, never, now=later, store=store)[0] == 0
    assert never.calls == []
    assert "did not reach the console" in caplog.text
    caplog.clear()
    _leg(ledger, _Poster(), now=later, store=store)
    assert "did not reach the console" not in caplog.text


def test_leg_sends_nothing_without_a_readable_ledger(tmp_path) -> None:
    _index(1)
    poster = _Poster()
    landed = rc.send_due_cards(
        slug="pilot",
        key="k",
        url="https://smd.services" + rc.CARD_PATH,
        audit_db_path=str(tmp_path / "absent.db"),
        post_fn=poster,
        now_fn=lambda: NOW.timestamp(),
    )
    assert landed == 0 and poster.calls == []


def test_leg_never_raises_on_a_broken_post(ledger) -> None:
    _index(1)

    def boom(*_a, **_k):
        raise OSError("connection refused")

    assert _leg(ledger, boom)[0] == 0


def _emitter(order: list[str], leg):
    def post_fn(url, headers, body):
        order.append("beat")
        return 200

    return hb.HeartbeatEmitter(
        slug="pilot",
        key="k",
        ingest_url="https://smd.services/api/internal/heartbeat",
        healthchecks_url="https://hc.example/ping",
        version="ref",
        audit_db_path_fn=lambda: None,
        post_fn=post_fn,
        ping_fn=lambda url: order.append("ping"),
        scheduler_check_fn=lambda: SchedulerCheck(ok=True, job_count=0, max_overdue_seconds=None),
        connector_check_fn=lambda: ConnectorCheck(ok=True, servers={}),
        spec_control_check_fn=lambda: SpecControlCheck(ok=True, entries={}),
        webhook_surface_check_fn=lambda: None,
        gateway_loop_check_fn=lambda: None,
        request_cards_fn=leg,
    )


def test_tick_runs_the_card_leg_after_the_beat_and_survives_it() -> None:
    order: list[str] = []

    def leg():
        order.append("cards")
        raise RuntimeError("leg exploded")

    em = _emitter(order, leg)
    em._tick()
    em._tick()
    assert order == ["beat", "ping", "cards", "beat", "ping", "cards"]


# ---------------------------------------------------------------------------
# job_done is one card per job ENDING (a resumed job reports how it ended)
# ---------------------------------------------------------------------------


def _attempt_column(ledger: _Ledger) -> None:
    ledger.conn.execute("ALTER TABLE demand_jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
    ledger.conn.commit()


def _end(ledger: _Ledger, state: str, minutes_ago: float, attempt: int) -> None:
    ledger.conn.execute(
        "UPDATE demand_jobs SET state=?, updated_at=?, attempt=?",
        (state, _ts(minutes_ago), attempt),
    )
    ledger.conn.commit()


def test_a_resumed_job_cards_its_new_ending(ledger) -> None:
    """FALSIFIER: key job_done on the request alone (the old card_key(vid,
    "job_done")) and the resumed job's delivered ending is never carded: its
    first ending's key is already done."""
    _attempt_column(ledger)
    ledger.job("demand_jobs", "failed", 30)
    first = _decide(ledger, [_request(60)])
    assert _kinds(first) == ["job_done"] and first[0]["job"]["state"] == "failed"
    _end(ledger, "delivered", 2, attempt=2)
    second = _decide(ledger, [_request(60)], done={first[0]["card_key"]})
    assert _kinds(second) == ["job_done"]
    assert second[0]["job"]["state"] == "delivered"
    assert second[0]["card_key"] == rc.job_done_key(VID, "delivered", 2)
    assert second[0]["card_key"] != first[0]["card_key"]
    # Same request hash, so the console still groups both endings under one request.
    assert second[0]["card_key"].split(":")[0] == first[0]["card_key"].split(":")[0]


def test_a_same_state_note_does_not_card_one_ending_twice(ledger) -> None:
    """FALSIFIER: key the ending on updated_at and a same-state note (which
    rewrites updated_at) re-sends the delivered card."""
    _attempt_column(ledger)
    ledger.job("demand_jobs", "delivered", 30)
    first = _decide(ledger, [_request(60)])
    ledger.conn.execute("UPDATE demand_jobs SET updated_at=?", (_ts(1),))
    ledger.conn.commit()
    assert _decide(ledger, [_request(60)], done={first[0]["card_key"]}) == []


def test_a_job_that_fails_again_after_a_resume_cards_again(ledger) -> None:
    _attempt_column(ledger)
    ledger.job("demand_jobs", "failed", 30)
    first = _decide(ledger, [_request(60)])
    _end(ledger, "failed", 5, attempt=2)
    again = _decide(ledger, [_request(60)], done={first[0]["card_key"]})
    assert _kinds(again) == ["job_done"]
    assert again[0]["card_key"] == rc.job_done_key(VID, "failed", 2)


def _decide_at(ledger: _Ledger, done: set[str], done_at: dict[str, float]) -> list[dict]:
    conn = ledger.ro()
    try:
        return rc.decide([_request(60)], conn, done, NOW, done_at=done_at)
    finally:
        conn.close()


def test_a_legacy_card_covers_the_ending_it_was_sent_for(ledger) -> None:
    """Rollout: a card that landed under the old per-request key is never re-sent."""
    _attempt_column(ledger)
    ledger.job("demand_jobs", "failed", 30)
    legacy = rc.card_key(VID, "job_done")
    landed = (NOW - timedelta(minutes=28)).timestamp()  # landed after the failure at -30
    assert _decide_at(ledger, {legacy}, {legacy: landed}) == []


def test_a_legacy_card_does_not_cover_a_later_resumed_ending(ledger) -> None:
    """FALSIFIER: treat any legacy job_done key as covering every ending and
    the resume that ended after it landed is silently dropped (the defect)."""
    _attempt_column(ledger)
    ledger.job("demand_jobs", "failed", 30)
    legacy = rc.card_key(VID, "job_done")
    landed = (NOW - timedelta(minutes=28)).timestamp()
    _end(ledger, "delivered", 2, attempt=2)
    cards = _decide_at(ledger, {legacy}, {legacy: landed})
    assert _kinds(cards) == ["job_done"] and cards[0]["job"]["state"] == "delivered"


def test_a_legacy_card_without_a_landing_time_covers_everything(ledger) -> None:
    _attempt_column(ledger)
    ledger.job("demand_jobs", "delivered", 2)
    assert _decide(ledger, [_request(60)], done={rc.card_key(VID, "job_done")}) == []


def test_a_ledger_without_the_attempt_column_reads_as_attempt_one(ledger) -> None:
    ledger.job("medchron_jobs", "delivered", 3)
    job_cards = [c for c in _decide(ledger, [_request(60)]) if c["kind"] == "job_done"]
    assert len(job_cards) == 1
    assert job_cards[0]["card_key"] == rc.job_done_key(VID, "delivered", 1)


def test_the_leg_sends_a_resumed_ending_once_after_a_legacy_card(ledger) -> None:
    """End to end through send_due_cards and the CardStore: the legacy row's
    landing time is what lets the resumed ending through, exactly once."""
    _attempt_column(ledger)
    _index(1)
    ledger.job("demand_jobs", "failed", 30, ref="m00")
    store = rc.CardStore()
    store.mark_done(rc.card_key("m00", "job_done"), (NOW - timedelta(minutes=28)).timestamp())
    poster = _Poster()
    _leg(ledger, poster, store=store)
    assert [c[2]["kind"] for c in poster.calls if c[2]["kind"] == "job_done"] == []
    _end(ledger, "delivered", 2, attempt=2)
    poster = _Poster()
    _leg(ledger, poster, store=store)
    done_cards = [c[2] for c in poster.calls if c[2]["kind"] == "job_done"]
    assert [c["job"]["state"] for c in done_cards] == ["delivered"]
    poster = _Poster()
    _leg(ledger, poster, store=store)
    assert [c for c in poster.calls if c[2]["kind"] == "job_done"] == []
