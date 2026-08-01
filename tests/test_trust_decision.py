"""Trust-decision provenance on the audit row (ss-console #2122).

The gate decided and the ledger recorded, and nothing joined them: on the pilot
seat ``ceiling_level`` was null on 100% of 4130 live rows, ``matter_ref`` on all
4130 while ``metadata.matter_id`` sat populated on 1696 of them, and the typed
send classes the entitlements actually govern never reached a row at all.

Three layers are covered here:

  * the register itself (``shared.trust_decision``) — keyed match, the
    sequential fallback, the tool-name guard, single-use, and the bound;
  * the producer (``hermes-smd-trust``) — every decision path records, including
    the banned refusal and the fail-closed one;
  * the consumer (``hermes-smd-audit``) — the row carries the effective ceiling,
    the resolved TYPED action class, the matter reference, and how it matched.

Plus the property that makes all of it safe to ship: populating two columns that
were always in the contract changes the canonical body of NEW rows only, so the
existing ledger keeps verifying (the chain currently verifies intact over 3479
rows and must keep doing so).
"""

from __future__ import annotations

import json
import threading

import pytest

from shared.audit_chain import CHAIN_COLUMNS, GENESIS, compute_row_hash, verify_chain
from shared.trust_decision import (
    MATCH_KEYED,
    MATCH_NONE,
    MATCH_SEQUENTIAL,
    TRUST_DECISIONS,
    TrustDecision,
    TrustDecisionRegister,
)
from tests.conftest import load_plugin


def _trust():
    return load_plugin("hermes-smd-trust")


def _audit():
    return load_plugin("hermes-smd-audit")


@pytest.fixture(autouse=True)
def _clean_register():
    TRUST_DECISIONS.clear()
    yield
    TRUST_DECISIONS.clear()


def _decision(**over) -> TrustDecision:
    base = {
        "action_class": "external_send_client",
        "audit_action": "allow",
        "allowed": True,
        "authored_ceiling": "autonomous",
        "vertical_floor": None,
        "effective_ceiling": "autonomous",
        "persona": "marcus",
        "reason": "external_send_client permitted: authored exposure is autonomous",
    }
    base.update(over)
    return TrustDecision(**base)


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def test_keyed_take_matches_on_tool_call_id():
    reg = TrustDecisionRegister()
    reg.record("call-1", "send_message", _decision())
    got, match = reg.take("call-1", "send_message")
    assert match == MATCH_KEYED
    assert got is not None and got.action_class == "external_send_client"


def test_take_is_single_use():
    """A decision may authorize exactly one row. A second take gets nothing —
    never a duplicate attribution."""
    reg = TrustDecisionRegister()
    reg.record("call-1", "send_message", _decision())
    assert reg.take("call-1", "send_message")[1] == MATCH_KEYED
    assert reg.take("call-1", "send_message") == (None, MATCH_NONE)


def test_sequential_fallback_when_the_pre_hook_had_no_tool_call_id():
    """Core drops session_id on the pre_tool_call path (#141) and the same fire
    sites carry tool_call_id, so the join cannot depend on it. The post hook's
    own id is present; the register still resolves, and says how."""
    reg = TrustDecisionRegister()
    reg.record("", "send_message", _decision())
    got, match = reg.take("call-1", "send_message")
    assert match == MATCH_SEQUENTIAL
    assert got is not None


def test_sequential_fallback_is_also_single_use():
    reg = TrustDecisionRegister()
    reg.record("", "send_message", _decision())
    assert reg.take("", "send_message")[1] == MATCH_SEQUENTIAL
    assert reg.take("", "send_message") == (None, MATCH_NONE)


def test_a_tool_name_disagreement_yields_no_decision_not_the_wrong_one():
    """The guard on the fallback. A row with no trust provenance is honest; a
    row carrying someone else's decision is not."""
    reg = TrustDecisionRegister()
    reg.record("call-1", "send_message", _decision())
    assert reg.take("call-1", "email_create_draft") == (None, MATCH_NONE)
    assert reg.take("", "email_create_draft") == (None, MATCH_NONE)


def test_keyed_take_clears_the_sequential_slot_too():
    """Otherwise a keyed take would leave the same decision collectable a second
    time through the fallback."""
    reg = TrustDecisionRegister()
    reg.record("call-1", "send_message", _decision())
    assert reg.take("call-1", "send_message")[1] == MATCH_KEYED
    assert reg.take("", "send_message") == (None, MATCH_NONE)


def test_a_later_record_supersedes_the_sequential_slot():
    reg = TrustDecisionRegister()
    reg.record("call-1", "send_message", _decision(reason="first"))
    reg.record("call-2", "send_message", _decision(reason="second"))
    got, match = reg.take("", "send_message")
    assert match == MATCH_SEQUENTIAL
    assert got is not None and got.reason == "second"


def test_keyed_map_is_bounded():
    """A refused call never dispatches, so its decision is never collected. The
    register must not grow on a long-lived Machine."""
    reg = TrustDecisionRegister(max_calls=4)
    for i in range(50):
        reg.record(f"call-{i}", "send_message", _decision())
    assert len(reg._by_call) == 4
    assert "call-0" not in reg._by_call  # oldest evicted
    assert "call-49" in reg._by_call  # newest retained
    assert reg.take("call-49", "send_message")[1] == MATCH_KEYED


def test_empty_tool_name_records_and_takes_nothing():
    reg = TrustDecisionRegister()
    reg.record("call-1", "", _decision())
    assert reg.take("call-1", "") == (None, MATCH_NONE)


# ---------------------------------------------------------------------------
# Concurrency — delegate_task runs worker threads in ONE process
#
# ``/opt/hermes/model_tools.py:66-80`` (_get_worker_loop): "Each worker thread
# (e.g., delegate_task's ThreadPoolExecutor threads) gets its own long-lived
# loop stored in thread-local storage." Two threads can therefore be inside
# their own pre→dispatch→post brackets at once. A process-global last-decision
# slot would cross-attribute, and the tool-name guard does NOT close it —
# concurrent calls to the same tool are routine.
# ---------------------------------------------------------------------------


def _run_concurrently(worker, tags: tuple[str, ...]) -> tuple[dict, list]:
    """Run ``worker(tag, barrier)`` on one thread per tag, all released together."""
    barrier = threading.Barrier(len(tags), timeout=5)
    results: dict = {}
    errors: list = []

    def _target(tag: str) -> None:
        try:
            results[tag] = worker(tag, barrier)
        except BaseException as exc:  # noqa: BLE001 — surfaced by the caller
            errors.append(exc)

    threads = [threading.Thread(target=_target, args=(tag,)) for tag in tags]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread hung"
    return results, errors


def test_concurrent_threads_never_receive_each_others_decision():
    """The one that matters. Both threads record BEFORE either takes (the
    barrier makes the interleave deterministic, not lucky), and both use the
    SAME tool name so the name guard cannot save us. With a process-global slot
    at least one thread necessarily gets the other's ceiling — which for a legal
    ledger is worse than a null one, because the row would assert that something
    authorized a call it did not authorize."""
    reg = TrustDecisionRegister()

    def worker(tag: str, barrier: threading.Barrier):
        # Empty tool_call_id forces the SEQUENTIAL path — the one under test.
        reg.record("", "email_list_messages", _decision(reason=tag))
        barrier.wait()  # every decision is now recorded; nothing taken yet
        return reg.take("", "email_list_messages")

    results, errors = _run_concurrently(worker, ("thread-a", "thread-b", "thread-c"))
    assert not errors, errors
    for tag, (got, match) in results.items():
        assert match == MATCH_SEQUENTIAL, tag
        assert got is not None and got.reason == tag, (
            f"{tag} received {got.reason if got else None}"
        )


def test_a_nested_bracket_cannot_hand_its_decision_to_the_outer_row():
    """The precise mis-attribution ordering, pinned deterministically: thread A's
    pre-hook runs, thread B's pre-hook runs INSIDE A's still-open bracket, then
    A's post-hook fires. Against a process-global slot A's row received
    ``B-decision`` stamped ``sequential`` — a row asserting a ceiling that
    authorized a different call, wearing the mark of a legitimate join. That is
    the failure mode the thread-local slot exists to make unreachable."""
    reg = TrustDecisionRegister()
    a_recorded, b_recorded = threading.Event(), threading.Event()
    out: dict = {}
    errors: list = []

    def thread_a() -> None:
        try:
            reg.record("", "email_list_messages", _decision(reason="A-decision"))
            a_recorded.set()
            assert b_recorded.wait(timeout=5), "thread B never recorded"
            out["A"] = reg.take("", "email_list_messages")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def thread_b() -> None:
        try:
            assert a_recorded.wait(timeout=5), "thread A never recorded"
            reg.record("", "email_list_messages", _decision(reason="B-decision"))
            b_recorded.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            b_recorded.set()  # never hang the peer on our failure

    threads = [threading.Thread(target=thread_a), threading.Thread(target=thread_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread hung"

    assert not errors, errors
    got, match = out["A"]
    assert match == MATCH_SEQUENTIAL
    assert got is not None and got.reason == "A-decision"


def test_concurrent_keyed_takes_resolve_their_own_calls():
    """The keyed path is safe across threads by construction — the id is unique
    per call — and must stay that way once the shared map is lock-guarded."""
    reg = TrustDecisionRegister()

    def worker(tag: str, barrier: threading.Barrier):
        reg.record(f"call-{tag}", "email_list_messages", _decision(reason=tag))
        barrier.wait()
        return reg.take(f"call-{tag}", "email_list_messages")

    results, errors = _run_concurrently(worker, ("thread-a", "thread-b", "thread-c"))
    assert not errors, errors
    for tag, (got, match) in results.items():
        assert match == MATCH_KEYED, tag
        assert got is not None and got.reason == tag


def test_concurrent_records_do_not_corrupt_the_eviction_bookkeeping():
    """``record`` reads the length then evicts — an unguarded interleave can pop
    from an already-empty map. Hammer it well past the bound from many threads."""
    reg = TrustDecisionRegister(max_calls=4)

    def worker(tag: str, barrier: threading.Barrier):
        barrier.wait()
        for i in range(200):
            reg.record(f"{tag}-{i}", "email_list_messages", _decision(reason=tag))
        return True

    _, errors = _run_concurrently(worker, tuple(f"t{i}" for i in range(6)))
    assert not errors, errors
    assert len(reg._by_call) == 4


# ---------------------------------------------------------------------------
# Producer — the trust gate records every decision path
# ---------------------------------------------------------------------------


def _setup_exposure(monkeypatch, enforce, exposure, *, roster=(), typed_roster=()):
    monkeypatch.setattr(enforce, "_resolve_persona_exposure", lambda slug="": dict(exposure))
    monkeypatch.setattr(enforce, "_resolve_vertical_floors", lambda: {})
    monkeypatch.setattr(enforce, "_resolve_roster", lambda: list(roster))
    monkeypatch.setattr(enforce, "_resolve_typed_roster", lambda: list(typed_roster))
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")


def test_gate_records_the_resolved_typed_send_class(monkeypatch):
    """The whole point of the typed classes: the row must say
    ``external_send_client``, not the coarse ``external_send`` the tool NAME
    resolves to before the recipient is known."""
    enforce = _trust().enforce
    _setup_exposure(
        monkeypatch,
        enforce,
        {enforce.ActionClass.EXTERNAL_SEND_CLIENT: enforce.Ceiling.AUTONOMOUS},
        typed_roster=[("client@example.com", "client")],
    )
    assert (
        enforce.evaluate_tool_call(
            "mcp_agentmail_send_message",
            {"to": ["client@example.com"], "subject": "Hi", "text": "status update"},
            "smd",
            session_id="s1",
            tool_call_id="call-9",
        )
        is None
    )
    got, match = TRUST_DECISIONS.take("call-9", "mcp_agentmail_send_message")
    assert match == MATCH_KEYED
    assert got is not None
    assert got.action_class == "external_send_client"
    assert got.effective_ceiling == "autonomous"
    assert got.authored_ceiling == "autonomous"
    assert got.audit_action == "allow"
    assert got.allowed is True
    assert got.persona == "marcus"


def test_gate_records_a_fail_closed_refusal_with_an_unauthored_ceiling(monkeypatch):
    """The fail-closed case is the one an auditor most needs to see: refused
    because NOTHING was authored, not because a ceiling said draft."""
    enforce = _trust().enforce
    _setup_exposure(monkeypatch, enforce, {})
    result = enforce.evaluate_tool_call(
        "email_create_draft", {"body": "x"}, "smd", session_id="s1", tool_call_id="call-3"
    )
    assert result is not None and result["action"] == "block"
    got, match = TRUST_DECISIONS.take("call-3", "email_create_draft")
    assert match == MATCH_KEYED
    assert got is not None
    assert got.action_class == "internal_write"
    assert got.audit_action == "refuse"
    assert got.allowed is False
    assert got.authored_ceiling is None  # unauthored, not "refused-by-a-floor"
    assert got.effective_ceiling == "refused"


def test_gate_records_the_banned_refusal(monkeypatch):
    """A banned tool is refused by NAME before any class resolves. The audit
    plugin has its own defense-in-depth INVARIANT_VIOLATION path for one that
    reaches post_tool_call; it should carry the refusal actually made."""
    enforce = _trust().enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    result = enforce.evaluate_tool_call(
        "email_send", {}, "smd", session_id="s1", tool_call_id="call-b"
    )
    assert result is not None and result["action"] == "block"
    got, match = TRUST_DECISIONS.take("call-b", "email_send")
    assert match == MATCH_KEYED
    assert got is not None
    assert got.action_class == "banned"
    assert got.audit_action == "refuse"
    assert got.effective_ceiling == "refused"


def test_gate_records_the_indeterminate_fail_closed_path(monkeypatch):
    """A resolver fault refuses a sensitive action, and the row must say the
    ceiling was INDETERMINATE (None) rather than assert one that was never
    resolved."""
    enforce = _trust().enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")

    def _boom(slug=""):
        raise RuntimeError("customer.yaml unreadable")

    monkeypatch.setattr(enforce, "_resolve_persona_exposure", _boom)
    result = enforce.evaluate_tool_call(
        "email_create_draft", {"body": "x"}, "smd", session_id="s1", tool_call_id="call-f"
    )
    assert result is not None and result["action"] == "block"
    got, _ = TRUST_DECISIONS.take("call-f", "email_create_draft")
    assert got is not None
    assert got.audit_action == "refuse"
    assert got.allowed is False
    assert got.effective_ceiling is None


def test_gate_records_the_read_allowed_despite_a_resolver_fault(monkeypatch):
    """READ is allowed through a resolver fault by design. That allow was not
    DECIDED by the resolver, and the row says so with a null ceiling."""
    enforce = _trust().enforce
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")

    def _boom(slug=""):
        raise RuntimeError("customer.yaml unreadable")

    monkeypatch.setattr(enforce, "_resolve_persona_exposure", _boom)
    assert (
        enforce.evaluate_tool_call(
            "email_list_messages", {}, "smd", session_id="s1", tool_call_id="call-r"
        )
        is None
    )
    got, _ = TRUST_DECISIONS.take("call-r", "email_list_messages")
    assert got is not None
    assert got.action_class == "read"
    assert got.allowed is True
    assert got.effective_ceiling is None


def test_pre_tool_call_hook_threads_the_tool_call_id(monkeypatch):
    """End-to-end through the hook the dispatcher actually calls."""
    trust = _trust()
    _setup_exposure(
        monkeypatch,
        trust.enforce,
        {trust.enforce.ActionClass.INTERNAL_WRITE: trust.enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setattr(trust, "_paused_hard", lambda: False)
    assert (
        trust.on_pre_tool_call(
            tool_name="email_create_draft",
            args={"body": "x"},
            session_id="s1",
            tool_call_id="call-hook",
            customer_slug="smd",
        )
        is None
    )
    got, match = TRUST_DECISIONS.take("call-hook", "email_create_draft")
    assert match == MATCH_KEYED
    assert got is not None and got.effective_ceiling == "autonomous"


# ---------------------------------------------------------------------------
# Consumer — the audit row
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, *params) -> None:
        self.calls.append((sql, tuple(params)))

    def rows(self) -> list[dict]:
        return [dict(zip(CHAIN_COLUMNS, params, strict=True)) for _, params in self.calls]


def _emit(**over) -> dict:
    audit = _audit()
    client = _FakeClient()
    kwargs = {
        "customer": "smd",
        "tool_name": "mcp_agentmail_send_message",
        "args": {"matter_id": "2026-PI-0042"},
        "result": "{}",
        "task_id": "t1",
        "session_id": "s1",
        "tool_call_id": "call-9",
        "duration_ms": 12,
    }
    kwargs.update(over)
    audit.emit.emit_tool_event(audit.emit.AuditLogWriter(client), **kwargs)
    return client.rows()[0]


def test_row_carries_the_effective_ceiling_and_the_typed_class():
    TRUST_DECISIONS.record("call-9", "mcp_agentmail_send_message", _decision())
    row = _emit()
    md = json.loads(row["metadata"])
    assert md["ceiling_level"] == "autonomous"
    assert md["resolved_action_class"] == "external_send_client"
    # The coarse class stays — the two answer different questions and stable
    # consumers depend on ``action_class``.
    assert md["action_class"] == "external_send"
    assert md["authored_ceiling"] == "autonomous"
    assert md["vertical_floor"] is None
    assert md["trust_decision"] == "allow"
    assert md["trust_allowed"] is True
    assert md["trust_persona"] == "marcus"
    assert md["trust_reason"]
    assert md["trust_decision_match"] == MATCH_KEYED
    assert row["trust_ceiling"] == "autonomous"


def test_row_populates_matter_ref_from_the_captured_matter_id():
    """The value was captured all along; it never reached the column an auditor
    filters and indexes on."""
    row = _emit()
    assert row["matter_ref"] == "2026-PI-0042"
    assert json.loads(row["metadata"])["matter_id"] == "2026-PI-0042"


def test_matter_ref_is_null_when_the_call_carried_no_matter():
    row = _emit(args={"body": "x"})
    assert row["matter_ref"] is None


def test_a_blank_matter_id_lands_as_null_not_an_empty_string():
    """The scope extractor coerces with ``str()``. The chain canonicalizes ""
    distinctly from NULL, and a blank matter reference is not a reference."""
    row = _emit(args={"matter_id": ""})
    assert row["matter_ref"] is None


def test_a_row_with_no_trust_decision_says_so():
    """Silence is not provenance. A row the gate never recorded for must be
    distinguishable from one that predates the field."""
    row = _emit()
    md = json.loads(row["metadata"])
    assert md["trust_decision_match"] == MATCH_NONE
    assert md["ceiling_level"] is None
    assert "resolved_action_class" not in md
    assert row["trust_ceiling"] is None


def test_a_register_fault_still_writes_the_row(monkeypatch):
    """Provenance ENRICHES the row; the row is the obligation. A lookup fault
    degrades to a row with no trail, never to a missing row."""
    monkeypatch.setattr(
        TRUST_DECISIONS,
        "take",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("register broken")),
    )
    md = json.loads(_emit()["metadata"])
    assert md["trust_decision_match"] == MATCH_NONE
    assert md["tool"] == "mcp_agentmail_send_message"


def test_a_decision_for_a_different_tool_is_not_attributed():
    TRUST_DECISIONS.record("call-9", "email_create_draft", _decision())
    md = json.loads(_emit()["metadata"])
    assert md["trust_decision_match"] == MATCH_NONE
    assert md["ceiling_level"] is None


def test_row_records_a_sequential_match_as_sequential():
    TRUST_DECISIONS.record("", "mcp_agentmail_send_message", _decision())
    md = json.loads(_emit()["metadata"])
    assert md["trust_decision_match"] == MATCH_SEQUENTIAL
    assert md["ceiling_level"] == "autonomous"


def test_only_one_row_can_claim_a_decision():
    TRUST_DECISIONS.record("call-9", "mcp_agentmail_send_message", _decision())
    first = json.loads(_emit()["metadata"])
    second = json.loads(_emit()["metadata"])
    assert first["trust_decision_match"] == MATCH_KEYED
    assert second["trust_decision_match"] == MATCH_NONE


def test_draft_and_refuse_verdicts_reach_the_row():
    for audit_action, ceiling in (("draft", "draft_for_review"), ("refuse", "refused")):
        TRUST_DECISIONS.clear()
        TRUST_DECISIONS.record(
            "call-9",
            "mcp_agentmail_send_message",
            _decision(audit_action=audit_action, allowed=False, effective_ceiling=ceiling),
        )
        row = _emit()
        md = json.loads(row["metadata"])
        assert md["trust_decision"] == audit_action
        assert md["trust_allowed"] is False
        assert md["ceiling_level"] == ceiling
        assert row["trust_ceiling"] == ceiling


# ---------------------------------------------------------------------------
# The chain keeps verifying
# ---------------------------------------------------------------------------


def test_populating_the_two_columns_does_not_break_the_existing_chain():
    """``matter_ref`` and ``trust_ceiling`` were always in ``CHAIN_COLUMNS``, so
    filling them changes the canonical body of NEW rows only. Every row's hash
    is recomputed from its OWN stored values, so a ledger whose old rows carry
    NULLs and whose new rows carry values verifies end to end."""
    rows: list[dict] = []
    prev = GENESIS
    for i in range(6):
        populated = i >= 3  # the changeover point
        vals = [
            f"01JROW{i:020d}",
            f"2026-08-01T12:00:{i:02d}.000Z",
            "TOOL_CALL_COMPLETED",
            "agent",
            "agent",
            None,
            "2026-PI-0042" if populated else None,  # matter_ref
            None,
            None,
            None,
            "autonomous" if populated else None,  # trust_ceiling
            '{"per_tool_audit":true}',
        ]
        row_hash = compute_row_hash(prev, vals)
        rows.append(
            {**dict(zip(CHAIN_COLUMNS, vals, strict=True)), "prev_hash": prev, "row_hash": row_hash}
        )
        prev = row_hash

    report = verify_chain(rows)
    assert report["ok"] is True, report["breaks"]
    assert report["chained"] == 6
    assert report["legacy"] == 0
