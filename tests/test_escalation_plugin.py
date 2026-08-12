"""Tests for plugins/hermes-smd-escalation (ss #1915).

The append validation lives in the broker (console side); here we prove the
tool handlers marshal the event correctly (broker faked), the state read folds
a real ledger file via the vendored shared/escalation_ledger twin, both tools
register well-formed, and both carry TOOL_ACTION_CLASS_MAP entries (the
unmapped-tool REFUSED fallback is exactly how the execute_code gap this plugin
closes was surfaced).
"""

from __future__ import annotations

import json
import time

import pytest

from shared import escalation_ledger
from shared.action_classes import ActionClass, classify_tool
from tests.conftest import load_plugin


@pytest.fixture
def escalation(monkeypatch):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "evt-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    return plugin, requests


def _derive_then_append(plugin, components: dict) -> tuple[dict, dict]:
    """The two-step contract (ss #2304). Identity components are supplied ONCE, on
    the derive; the write presents the single-use handle that derive returned and
    names no identity of its own."""
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    written = json.loads(
        plugin._escalation_append(
            {
                "skill": components["skill"],
                "event": components["event"],
                "attempt": components["attempt"],
                "append_handle": derived["append_handle"],
            }
        )
    )
    return derived, written


def test_append_derives_item_key_and_token_from_components(escalation):
    """The tool hashes the identity tuple ITSELF (the first live probe proved
    a model-authored item_key forks the pre_run join: it wrote a colon-joined
    composite the sha256 join never matched)."""
    plugin, requests = escalation
    _, out = _derive_then_append(
        plugin,
        {
            "skill": "client-verification-tracker",
            "matter_id": "m-1",
            "source_id": "task-1",
            "label": "client-verification",
            "authored_date": None,
            "event": "chased",
            "attempt": 2,
        },
    )
    expected_key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    expected_token = escalation_ledger.token_for(expected_key)
    assert out["ok"] is True
    assert out["item_key"] == expected_key  # echoed for the turn
    assert out["token"] == expected_token
    assert len(requests) == 1
    req = requests[0]
    assert req["action"] == "escalation_event_append"
    event = req["event"]
    assert event["item_key"] == expected_key  # EXACTLY the pre_run gate's key
    assert event["token"] == expected_token
    assert event["event"] == "chased"
    assert event["attempt"] == 2
    assert event["ts"] is None  # broker stamps server-side; agent cannot backdate
    assert event["v"] == escalation_ledger.SCHEMA_VERSION


def test_derive_only_returns_identity_and_writes_nothing(escalation):
    """ss #1935: the alert body must quote real broker-derived ACK codes, but the
    safe failure direction (send fails -> nothing recorded -> re-fires next run)
    requires the raise to be appended AFTER the send. derive_only=true is the
    first step of that ordering: identity out, zero events written."""
    plugin, requests = escalation
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-1",
                "source_id": "task-1",
                "label": "records-outstanding",
                "authored_date": "2026-07-11",
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
            }
        )
    )
    expected_key = escalation_ledger.item_key("m-1", "task-1", "records-outstanding", "2026-07-11")
    assert out["ok"] is True
    assert out["written"] is False
    assert out["item_key"] == expected_key
    assert out["token"] == escalation_ledger.token_for(expected_key)
    assert requests == []  # NOTHING reached the broker


def test_derive_only_matches_the_later_real_append(escalation):
    """The token quoted in the alert (derive_only) and the token recorded by the
    post-send append are the same value — since ss #2304 by construction, not by
    two derivations agreeing: the append re-derives nothing."""
    plugin, requests = escalation
    derived, appended = _derive_then_append(
        plugin,
        {
            "skill": "deadline-miss-escalator",
            "matter_id": "m-1",
            "source_id": "task-9",
            "label": "lien-payoff",
            "authored_date": None,
            "event": "fired",
            "attempt": 1,
        },
    )
    assert derived["token"] == appended["token"]
    assert derived["item_key"] == appended["item_key"]
    assert len(requests) == 1  # only the second call wrote
    assert requests[0]["event"]["item_key"] == derived["item_key"]


def test_a_handle_writes_exactly_one_row(escalation):
    """Single use. A retried append on a spent handle is refused rather than
    writing the item twice — a second raise would inflate the attempt count the
    chase ceiling reads."""
    plugin, requests = escalation
    derived, _ = _derive_then_append(
        plugin,
        {
            "skill": "deadline-miss-escalator",
            "matter_id": "m-1",
            "source_id": "task-9",
            "label": "lien-payoff",
            "authored_date": None,
            "event": "fired",
            "attempt": 1,
        },
    )
    with pytest.raises(ValueError, match="already used to write a row"):
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "fired",
                "attempt": 1,
                "append_handle": derived["append_handle"],
            }
        )
    assert len(requests) == 1


def test_a_handle_never_issued_is_refused(escalation):
    """A fabricated or remembered handle writes nothing. The turn cannot invent
    its way past the derive."""
    plugin, requests = escalation
    with pytest.raises(ValueError, match="was not issued by a derive_only call"):
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "fired",
                "attempt": 1,
                "append_handle": "EDH-0000000000000000000000000000000",
            }
        )
    assert requests == []


def test_an_expired_handle_is_refused_not_written(escalation, monkeypatch):
    """A handle that outlived its window refuses. The turn re-derives, which is
    free; the failure direction stays the safe one (nothing written, ss #1935)."""
    plugin, requests = escalation
    derived = json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-1",
                "source_id": "task-9",
                "label": "lien-payoff",
                "authored_date": None,
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
            }
        )
    )
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        plugin.time, "monotonic", lambda: real_monotonic() + plugin._HANDLE_TTL_SECONDS + 1
    )
    with pytest.raises(ValueError, match="expired"):
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "fired",
                "attempt": 1,
                "append_handle": derived["append_handle"],
            }
        )
    assert requests == []


def test_identity_components_on_an_append_are_refused_even_when_they_agree(escalation):
    """The refusal is on the SHAPE, not on a comparison. A tool that only refused
    disagreeing components would still accept the case the issue names as
    uncatchable — a turn that re-reads the wrong row and copies every field off it
    consistently. Identity is typed once per row or not at all."""
    plugin, requests = escalation
    components = {
        "skill": "deadline-miss-escalator",
        "matter_id": "m-7",
        "source_id": "task-42",
        "label": "task-deadline",
        "authored_date": "2026-08-11",
        "event": "fired",
        "attempt": 1,
    }
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    with pytest.raises(ValueError, match="must carry NO identity components"):
        plugin._escalation_append({**components, "append_handle": derived["append_handle"]})
    assert requests == []


def test_a_handle_cannot_be_spent_under_a_different_skill_or_event(escalation):
    """The handle names an item AND the row it was derived for. A raise filed
    under the wrong skill is invisible to that skill's state fold, and a derive
    for a `fired` spent on a `handed_off` is a terminal write the turn did not
    look up."""
    plugin, requests = escalation
    components = {
        "skill": "client-verification-tracker",
        "matter_id": "m-1",
        "source_id": "task-1",
        "label": "client-verification",
        "authored_date": None,
        "event": "chased",
        "attempt": 2,
    }
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    with pytest.raises(ValueError, match="filed under the wrong skill"):
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "chased",
                "attempt": 2,
                "append_handle": derived["append_handle"],
            }
        )
    with pytest.raises(ValueError, match="derive the event you intend to write"):
        plugin._escalation_append(
            {
                "skill": "client-verification-tracker",
                "event": "handed_off",
                "attempt": 2,
                "append_handle": derived["append_handle"],
            }
        )
    assert requests == []


def test_derive_only_rejects_an_append_handle(escalation):
    plugin, requests = escalation
    with pytest.raises(ValueError, match="what a derive RETURNS"):
        plugin._escalation_append(
            {
                "skill": "s",
                "matter_id": "m-1",
                "source_id": "t-1",
                "label": "x",
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
                "append_handle": "EDH-abc",
            }
        )
    assert requests == []


def test_acked_refuses_identity_components_and_handles(escalation, tmp_path, monkeypatch):
    """An acked event identifies by the quoted code and nothing else. Components
    alongside it used to be accepted and silently ignored, which reads to a turn
    as though they were honoured."""
    plugin, requests = escalation
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(tmp_path / "empty.jsonl"))
    with pytest.raises(ValueError, match="nothing else"):
        plugin._escalation_append(
            {
                "skill": "s",
                "event": "acked",
                "attempt": 1,
                "ack_token": "ACK-ABCDEF",
                "source_id": "t-1",
            }
        )
    assert requests == []


def test_derive_only_rejects_ack_token(escalation):
    plugin, requests = escalation
    with pytest.raises(ValueError, match="one or the other"):
        plugin._escalation_append(
            {
                "skill": "s",
                "event": "acked",
                "attempt": 1,
                "ack_token": "ACK-ABCDEF",
                "derive_only": True,
            }
        )
    assert requests == []


def test_append_idless_item_gets_no_token(escalation):
    plugin, requests = escalation
    _derive_then_append(
        plugin,
        {
            "skill": "deadline-miss-escalator",
            "matter_id": "m-1",
            "source_id": None,
            "label": "sol-date",
            "authored_date": "2026-08-01",
            "event": "fired",
            "attempt": 1,
        },
    )
    assert requests[0]["event"]["token"] is None  # blanket-ack-only group


def test_sentinel_matter_item_gets_no_token(escalation):
    """ss #2289 fix 2, at the seam that actually decides. ``pre_run``'s
    ``_matter_id_of`` returns "unknown-matter" when the Smokeball payload carries
    no resolvable matter link, and the agent passes that value straight through.
    The item has a real task GUID, so the old ``source_id is not None`` test
    handed it a per-item ACK code — but half its key is a placeholder, so the key
    (and therefore the code) changes the instant the matter resolves. The alert
    would print a code that names nothing by the time anyone types it."""
    plugin, requests = escalation
    _, out = _derive_then_append(
        plugin,
        {
            "skill": "deadline-miss-escalator",
            "matter_id": "unknown-matter",
            "source_id": "3c191bed-cdda-48b9-a6ed-a51a349f3f94",
            "label": "task-deadline",
            "authored_date": "2026-08-11",
            "event": "fired",
            "attempt": 1,
        },
    )
    assert out["token"] is None  # blanket-ack-only
    assert requests[0]["event"]["token"] is None
    # The item still FIRES — the sentinel costs it a per-item code, not its alarm.
    assert requests[0]["event"]["item_key"]


def test_one_deadline_two_date_spellings_is_one_append_identity(escalation):
    """ss #2289 fix 1 through the tool the model actually calls. ``authored_date``
    is a schema string; across runs the same deadline arrives spelled differently
    and each spelling used to be its own item — fire-once counted them apart and
    each carried a different ACK code."""
    plugin, requests = escalation
    for spelling in ("2026-08-11", "2026-08-11T00:00:00Z", " 2026-08-11 "):
        _derive_then_append(
            plugin,
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-7",
                "source_id": "task-42",
                "label": "task-deadline",
                "authored_date": spelling,
                "event": "fired",
                "attempt": 1,
            },
        )
    keys = {r["event"]["item_key"] for r in requests}
    tokens = {r["event"]["token"] for r in requests}
    assert len(keys) == 1, f"one deadline, {len(keys)} identities: {sorted(keys)}"
    assert len(tokens) == 1


def test_unparseable_authored_date_is_refused_before_the_broker(escalation):
    """A date the module cannot canonicalize must not reach the ledger verbatim.
    The turn sees the error on the derive — while the argument is still fixable,
    and before any code has been quoted to anyone."""
    plugin, requests = escalation
    with pytest.raises(ValueError, match="authored_date"):
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "matter_id": "m-7",
                "source_id": "task-42",
                "label": "task-deadline",
                "authored_date": "next Tuesday",
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
            }
        )
    assert requests == []


def test_state_never_offers_a_token_that_cannot_resolve(escalation, tmp_path, monkeypatch):
    """ss #2289 fix 3. ``escalation_state`` used to backfill ``token_for(key)``
    for any item whose ledger rows carried no token — precisely the
    blanket-ack-only items, which ``_resolve_token_identity`` refuses by design.
    The turn was handed an ACK code that structurally could not be acked; quote it
    in an alert and the human types a code that comes back "an alarm that never
    rang cannot be acked".

    Every token this tool reports must round-trip. Asserted here by actually
    resolving it, not by inspecting the shape."""
    plugin, _ = escalation
    idless_key = escalation_ledger.item_key("m-1", None, "sol-date", "2026-08-01")
    ledger_file = tmp_path / "ledger.jsonl"
    ledger_file.write_text(
        json.dumps(
            escalation_ledger.make_event(
                skill="deadline-miss-escalator",
                matter_id="m-1",
                item_key=idless_key,
                event="fired",
                attempt=1,
                token=None,  # blanket-ack-only: the append wrote no token
                ts="2026-08-01T09:00:00.000Z",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(plugin._escalation_state({}))
    row = out["items"][idless_key]
    assert row["token"] is None
    assert row["ackable"] is False
    for key, item in out["items"].items():
        if item["token"] is None:
            continue
        # Anything still offered as a token must resolve to this very item.
        assert plugin._resolve_token_identity(item["token"])[0] == key


def test_acked_resolves_identity_from_token(escalation, tmp_path, monkeypatch):
    """The acker knows the ACK code from the reply, not the identity tuple —
    the tool resolves the token against the ledger's prior raises."""
    plugin, requests = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "ledger.jsonl"
    fired = escalation_ledger.make_event(
        skill="deadline-miss-escalator",
        matter_id="m-1",
        item_key=key,
        event="fired",
        attempt=1,
        token=token,
        ts="2026-07-14T09:00:00.000Z",
    )
    ledger_file.write_text(json.dumps(fired) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(
        plugin._escalation_append(
            {
                "skill": "deadline-miss-escalator",
                "event": "acked",
                "attempt": 1,
                "ack_token": token,
            }
        )
    )
    assert out["ok"] is True
    assert requests[0]["event"]["item_key"] == key
    assert requests[0]["event"]["matter_id"] == "m-1"


def test_acked_unknown_token_is_rejected_before_the_broker(escalation, tmp_path, monkeypatch):
    plugin, requests = escalation
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(tmp_path / "empty.jsonl"))
    with pytest.raises(ValueError, match="never rang"):
        plugin._escalation_append(
            {"skill": "s", "event": "acked", "attempt": 1, "ack_token": "ACK-XXXXXX"}
        )
    assert requests == []  # nothing shipped


def test_acked_against_pre_epoch_raise_is_refused_before_the_broker(
    escalation, tmp_path, monkeypatch
):
    """ss #2151. A human replying with an ACK code from an old alert must be told
    the code is superseded. Resolving it would ship an ack for a phantom item and
    report a silenced alarm while the real deadline kept firing."""
    plugin, requests = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "settlement-offer-lapsed", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "ledger.jsonl"
    fired = escalation_ledger.make_event(
        skill="deadline-miss-escalator",
        matter_id="m-1",
        item_key=key,
        event="fired",
        attempt=1,
        token=token,
        ts="2026-08-11T14:05:18.711Z",
    )
    fired["v"] = 1  # written under the superseded derivation
    ledger_file.write_text(json.dumps(fired) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    with pytest.raises(ValueError, match="ss #2151"):
        plugin._escalation_append(
            {"skill": "deadline-miss-escalator", "event": "acked", "attempt": 1, "ack_token": token}
        )
    assert requests == []  # nothing shipped to the broker


def test_acked_resolves_past_a_pre_epoch_raise_to_a_current_one(escalation, tmp_path, monkeypatch):
    """The epoch guard must not block a legitimate ack when a current raise for
    the same token also exists."""
    plugin, requests = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "task-deadline", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "ledger.jsonl"

    def _row(ts, version):
        row = escalation_ledger.make_event(
            skill="deadline-miss-escalator",
            matter_id="m-1",
            item_key=key,
            event="fired",
            attempt=1,
            token=token,
            ts=ts,
        )
        row["v"] = version
        return json.dumps(row)

    ledger_file.write_text(
        _row("2026-08-11T14:05:18.711Z", 1) + "\n" + _row("2026-08-12T14:00:00.000Z", 2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(
        plugin._escalation_append(
            {"skill": "deadline-miss-escalator", "event": "acked", "attempt": 1, "ack_token": token}
        )
    )
    assert out["ok"] is True
    assert requests[0]["event"]["item_key"] == key


def test_append_returns_broker_rejection_verbatim(escalation, monkeypatch):
    plugin, _ = escalation
    derived = json.loads(
        plugin._escalation_append(
            {
                "skill": "s",
                "matter_id": "m-1",
                "source_id": "t-1",
                "label": "x",
                "event": "fired",
                "attempt": 1,
                "derive_only": True,
            }
        )
    )
    monkeypatch.setattr(
        plugin,
        "_broker_request",
        lambda payload: {"ok": False, "error": "ValueError", "message": "no prior raise"},
    )
    write = {
        "skill": "s",
        "event": "fired",
        "attempt": 1,
        "append_handle": derived["append_handle"],
    }
    out = json.loads(plugin._escalation_append(write))
    assert out["ok"] is False
    assert "no prior raise" in out["message"]
    # A refused write leaves the handle alive: no row exists, so the turn can
    # retry the SAME identity without re-deriving a code it has already quoted.
    monkeypatch.setattr(plugin, "_broker_request", lambda payload: {"ok": True, "id": "evt-2"})
    retried = json.loads(plugin._escalation_append(write))
    assert retried["ok"] is True
    assert retried["item_key"] == derived["item_key"]


def test_a_code_shown_for_one_item_cannot_be_written_against_another(escalation):
    """ss #2304, the defect itself. The turn derives item A, quotes A's ACK code
    in the alert it sends, and then appends -- and the append used to re-derive
    identity from whatever tuple it was handed THAT call. A transposition
    (``task-42`` -> ``task-43``, one row off in a batch of nine) wrote a row the
    human's code does not name: the ack is refused, or in a batch it resolves to a
    DIFFERENT open item and silences the wrong deadline. Both calls are
    individually well-formed, so nothing in the tool, the broker or the ledger
    could see it.

    The append no longer accepts identity at all -- it presents the handle its
    derive returned -- so the divergence is not detected, it is unrepresentable."""
    plugin, requests = escalation
    item_a = {
        "skill": "deadline-miss-escalator",
        "matter_id": "m-7",
        "source_id": "task-42",
        "label": "task-deadline",
        "authored_date": "2026-08-11",
        "event": "fired",
        "attempt": 1,
    }
    item_b = {**item_a, "source_id": "task-43"}
    derived = json.loads(plugin._escalation_append({**item_a, "derive_only": True}))
    shown_to_the_human = derived["token"]
    try:
        written = json.loads(plugin._escalation_append(item_b))
    except ValueError as refusal:
        assert "append_handle" in str(refusal)
        assert requests == []  # nothing reached the broker
        return
    pytest.fail(
        "the append was accepted and wrote a DIFFERENT item than the code the "
        f"human was shown: shown {shown_to_the_human} ({derived['item_key']}), "
        f"written {written['token']} ({written['item_key']}) -- two well-formed "
        "calls, no derivation binds them, and nothing noticed"
    )


def test_the_single_item_path_still_round_trips_through_the_ack_resolver(
    escalation, tmp_path, monkeypatch
):
    """Control for the test above: the normal derive -> send -> append path still
    yields a code a human can actually type back. The row the append wrote is
    replayed into a ledger file and the code shown in the alert is resolved
    through ``_resolve_token_identity`` -- the same resolver the ack turn uses."""
    plugin, requests = escalation
    components = {
        "skill": "deadline-miss-escalator",
        "matter_id": "m-7",
        "source_id": "task-42",
        "label": "task-deadline",
        "authored_date": "2026-08-11",
        "event": "fired",
        "attempt": 1,
    }
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    shown_to_the_human = derived["token"]
    appended = json.loads(
        plugin._escalation_append(
            {
                "skill": components["skill"],
                "event": components["event"],
                "attempt": components["attempt"],
                "append_handle": derived["append_handle"],
            }
        )
    )
    assert appended["item_key"] == derived["item_key"]
    assert appended["token"] == shown_to_the_human
    row = requests[0]["event"]
    ledger_file = tmp_path / "ledger.jsonl"
    ledger_file.write_text(
        json.dumps({**row, "ts": "2026-08-11T09:00:00.000Z", "id": "evt-1"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    resolved_key, resolved_matter = plugin._resolve_token_identity(shown_to_the_human)
    assert resolved_key == derived["item_key"]
    assert resolved_matter == "m-7"


def test_state_folds_ledger_file(escalation, tmp_path, monkeypatch):
    plugin, _ = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "client-verification", None)
    token = escalation_ledger.token_for(key)
    ledger_file = tmp_path / "escalation-ledger.jsonl"
    events = [
        escalation_ledger.make_event(
            skill="client-verification-tracker",
            matter_id="m-1",
            item_key=key,
            event="chased",
            attempt=1,
            token=token,
            ts="2026-07-14T09:00:00.000Z",
        ),
    ]
    ledger_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(plugin._escalation_state({}))
    assert out["event_count"] == 1
    assert out["item_count"] == 1
    row = out["items"][key]
    assert row["attempts"] == 1
    assert row["token"] == token
    assert row["last_raised_date"] == "2026-07-14"


def test_state_filters_by_skill(escalation, tmp_path, monkeypatch):
    plugin, _ = escalation
    key = escalation_ledger.item_key("m-1", "task-1", "deadline", None)
    lines = [
        json.dumps(
            escalation_ledger.make_event(
                skill=skill,
                matter_id="m-1",
                item_key=key + suffix,
                event="fired",
                attempt=1,
                ts="2026-07-14T09:00:00.000Z",
            )
        )
        for skill, suffix in (
            ("deadline-miss-escalator", ""),
            ("client-verification-tracker", "x"),
        )
    ]
    ledger_file = tmp_path / "ledger.jsonl"
    ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(ledger_file))
    out = json.loads(plugin._escalation_state({"skill": "deadline-miss-escalator"}))
    assert out["event_count"] == 1
    assert out["item_count"] == 1


def test_state_missing_file_is_empty_not_error(escalation, monkeypatch, tmp_path):
    plugin, _ = escalation
    monkeypatch.setenv("SMD_ESCALATION_LEDGER_PATH", str(tmp_path / "absent.jsonl"))
    out = json.loads(plugin._escalation_state({}))
    assert out == {"event_count": 0, "item_count": 0, "items": {}}


def test_register_registers_both_tools(escalation):
    plugin, _ = escalation
    registered: list[dict] = []

    class Ctx:
        def register_tool(self, **kw):
            registered.append(kw)

    plugin.register(Ctx())
    names = {r["name"] for r in registered}
    assert names == {"escalation_append", "escalation_state"}
    for r in registered:
        assert "parameters" in r["schema"]  # function shape, not bare JSON-schema


def test_tools_are_mapped_in_action_class_registry():
    assert classify_tool("escalation_append").action_class is ActionClass.INTERNAL_WRITE
    assert classify_tool("escalation_state").action_class is ActionClass.READ
    assert classify_tool("escalation_append").unmapped is False


def test_vendored_ledger_twin_matches_reference_shapes():
    # Guard the vendored twin's load-bearing API (the console-side sync test
    # guards byte-identity; this guards the plugin's import surface).
    for name in (
        "read_ledger",
        "derive_state",
        "token_for",
        "item_key",
        "make_event",
        "SCHEMA_VERSION",
        "DEFAULT_LEDGER_PATH",
    ):
        assert hasattr(escalation_ledger, name)
