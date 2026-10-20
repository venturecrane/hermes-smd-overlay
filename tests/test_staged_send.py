"""The SMD-staged send: exact words and pinned files, through the full gate.

What is pinned here:

* staging validates everything a send needs and refuses what the gate could
  never pass (a wrong sha256, an active PDF, an identifier nothing could
  verify), and the dry run writes nothing;
* ``--confirm`` must repeat the dry run's digest;
* what it writes is a routine envelope the prerendered dispatcher consumes,
  with ``attachments_required``: the files go together or nothing goes;
* the end to end falsifier: the REAL attachment scan passes the staged PDF's
  date because the staged handoff seeded it, and refuses the same PDF (so
  nothing sends) when the handoff is absent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from shared import cron_attribution, prerendered_dispatch, send_anchor, send_dispatch, staged_send
from shared.send_dispatch import DispatchResult
from tests.conftest import load_plugin
from tests.pdf_samples import make_pdf
from tests.test_attachment_scan import gate  # noqa: F401 - the shared gate fixture

PERSONA = "brokery-agent"
SKILL = "open-house-visitor-capture"
SESSION = f"cron_j-staged_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
BODY = "Tim, the two pilot flyers are attached, one for each start date.\n"


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "profiles" / PERSONA).mkdir(parents=True)
    prerendered_dispatch._IN_TURN.clear()
    yield
    send_dispatch.set_sender(None)
    prerendered_dispatch._IN_TURN.clear()


def _files(tmp_path, lines_a=None, lines_b=None):
    a = tmp_path / "flyer-oct26.pdf"
    b = tmp_path / "flyer-nov2.pdf"
    a.write_bytes(make_pdf(lines_a or ["Six weeks. Starts Monday, October 26, 2026."]))
    b.write_bytes(make_pdf(lines_b or ["Six weeks. Starts Monday, November 2, 2026."]))
    return [f"{p}={hashlib.sha256(p.read_bytes()).hexdigest()}" for p in (a, b)]


def _build(tmp_path, **overrides):
    kwargs = {
        "persona": PERSONA,
        "skill": SKILL,
        "to": ["tim@brokerage.example"],
        "cc": None,
        "subject": "Pilot flyers",
        "body": BODY,
    }
    kwargs.update(overrides)
    if "attach" not in kwargs:
        kwargs["attach"] = _files(tmp_path)
    return staged_send.build(**kwargs)


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def test_a_valid_staging_describes_everything_the_digest_covers(tmp_path):
    staged = _build(tmp_path)
    text = staged_send.describe(staged)
    assert "to:       tim@brokerage.example" in text
    assert BODY.strip() in text
    assert "flyer-oct26.pdf" in text and "flyer-nov2.pdf" in text
    assert "Starts Monday, October 26, 2026." in text  # the text the gate reads
    assert staged.digest() in text
    assert set(staged.dates) == {"October 26, 2026", "November 2, 2026"}


def test_the_digest_moves_with_any_word_file_or_recipient(tmp_path):
    base = _build(tmp_path).digest()
    assert _build(tmp_path, body=BODY + "x").digest() != base
    assert _build(tmp_path, to=["someone@brokerage.example"]).digest() != base
    other = _files(tmp_path, lines_a=["A different flyer, October 26, 2026."])
    assert _build(tmp_path, attach=other).digest() != base


@pytest.mark.parametrize(
    "overrides, match",
    [
        pytest.param({"to": ["not an address"]}, "email address", id="bad-address"),
        pytest.param({"to": []}, "at least one --to", id="no-recipient"),
        pytest.param({"subject": ""}, "subject", id="no-subject"),
        pytest.param({"body": "  "}, "body", id="no-body"),
        pytest.param({"attach": []}, "at least one --attach", id="no-files"),
        pytest.param({"persona": "../root"}, "persona", id="path-persona"),
        pytest.param(
            {"body": "Matter 2026-PI-106 is attached.\n"}, "nothing on the seat", id="case-number"
        ),
    ],
)
def test_staging_refuses_what_the_gate_could_never_pass(tmp_path, overrides, match):
    with pytest.raises(staged_send.StageError, match=match):
        _build(tmp_path, **overrides)


def test_a_file_whose_bytes_moved_after_pinning_is_refused(tmp_path):
    specs = _files(tmp_path)
    path = specs[0].rpartition("=")[0]
    with open(path, "ab") as fh:
        fh.write(b"\n% changed\n")
    with pytest.raises(staged_send.StageError, match="sha256 is"):
        _build(tmp_path, attach=specs)


def test_an_active_pdf_is_refused_at_staging(tmp_path):
    bad = tmp_path / "flyer.pdf"
    bad.write_bytes(
        make_pdf(
            catalog_extra="/OpenAction 20 0 R",
            extra_objects={20: "<< /S /JavaScript /JS (app.alert(1)) >>"},
        )
    )
    spec = f"{bad}={hashlib.sha256(bad.read_bytes()).hexdigest()}"
    with pytest.raises(staged_send.StageError, match="attachment refused"):
        _build(tmp_path, attach=[spec])


def test_a_file_that_is_neither_pdf_nor_workbook_is_refused(tmp_path):
    other = tmp_path / "flyer.png"
    other.write_bytes(b"\x89PNG")
    with pytest.raises(staged_send.StageError, match="only .pdf and .xlsx"):
        _build(tmp_path, attach=[f"{other}={hashlib.sha256(b'x').hexdigest()}"])


# ---------------------------------------------------------------------------
# Writing: the CLI's two steps
# ---------------------------------------------------------------------------


def _argv(tmp_path, *extra):
    body = tmp_path / "body.txt"
    body.write_text(BODY, encoding="utf-8")
    argv = [
        "--persona", PERSONA, "--skill", SKILL, "--to", "tim@brokerage.example",
        "--subject", "Pilot flyers", "--body-file", str(body), "--hermes-home", str(tmp_path),
    ]  # fmt: skip
    for spec in _files(tmp_path):
        argv += ["--attach", spec]
    return argv + list(extra)


def _pre_run_dir(tmp_path):
    return tmp_path / "profiles" / PERSONA / ".smd" / "pre_run"


def test_the_dry_run_writes_nothing(tmp_path, capsys):
    assert staged_send.main(_argv(tmp_path)) == 0
    assert "dry run: nothing written" in capsys.readouterr().out
    assert not _pre_run_dir(tmp_path).exists()


def test_confirm_must_repeat_the_digest(tmp_path, capsys):
    assert staged_send.main(_argv(tmp_path, "--confirm", "0" * 64)) == 2
    assert "does not match" in capsys.readouterr().err
    assert not _pre_run_dir(tmp_path).exists()


def test_confirm_writes_the_envelope_and_handoff_once(tmp_path, capsys):
    digest = _build(tmp_path).digest()
    assert staged_send.main(_argv(tmp_path, "--confirm", digest)) == 0
    out = capsys.readouterr().out
    assert f"--name op-managed:{PERSONA}:{SKILL}" in out
    directory = _pre_run_dir(tmp_path)
    envelope = json.loads((directory / f"{SKILL}.dispatch.json").read_text())
    handoff = json.loads((directory / f"{SKILL}.json").read_text())
    [entry] = envelope["dispatches"]
    assert entry["attachments_required"] is True
    assert [a["name"] for a in entry["attachments"]] == ["flyer-oct26.pdf", "flyer-nov2.pdf"]
    assert entry["full_body"] == BODY and "skeleton_body" not in entry
    assert envelope["in_turn_enforce"] is True
    assert set(handoff["dates"]) == {"October 26, 2026", "November 2, 2026"}
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    # A second staging never overwrites an unconsumed one.
    assert staged_send.main(_argv(tmp_path, "--confirm", digest)) == 2
    assert "already exists" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Dispatch: the staged envelope through the prerendered ladder
# ---------------------------------------------------------------------------


class _Sender:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0) if self.results else DispatchResult(sent=False, reason="none")


def _routine(monkeypatch):
    identity = cron_attribution.RoutineIdentity(
        job_id="j-staged", job_name=f"op-managed:{PERSONA}:{SKILL}", persona=PERSONA, skill=SKILL
    )
    monkeypatch.setattr(cron_attribution, "resolve_routine", lambda sid, **k: identity)
    monkeypatch.setattr(
        cron_attribution, "parse_cron_session_started_at", lambda sid: datetime.now(timezone.utc)
    )
    monkeypatch.setattr(send_anchor, "routine_lane", lambda skill: send_anchor.LANE_ESCALATION)
    monkeypatch.setattr(send_anchor, "routine_turn_lane", lambda sid=None: f"skill:{SKILL}")


def _stage(tmp_path):
    staged = _build(tmp_path)
    staged_send.write(staged, hermes_home=str(tmp_path))
    return staged


def test_both_files_go_in_one_send_on_the_roster_lane(monkeypatch, tmp_path):
    _routine(monkeypatch)
    staged = _stage(tmp_path)
    sender = _Sender([DispatchResult(sent=True, message_id="am-1")])
    send_dispatch.set_sender(sender)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    [call] = sender.calls
    assert [a["sha256"] for a in call["attachments"]] == [
        d["sha256"] for d, _ in staged.attachments
    ]
    assert call["to"] == ["tim@brokerage.example"] and call["text"] == BODY
    assert call["lane"] == f"skill:{SKILL}"
    assert "already delivered" in note
    # The woken turn may send only the fixed line, never a second message.
    [declared] = prerendered_dispatch._IN_TURN.values()
    assert declared["enforce"] is True
    assert [t["template"] for t in declared["templates"]] == [staged_send.DONE_LINE]
    assert not (_pre_run_dir(tmp_path) / f"{SKILL}.dispatch.json").exists()


def test_a_refused_file_sends_nothing_at_all(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _stage(tmp_path)
    sender = _Sender([DispatchResult(sent=False, reason="no", attachment_refused=True)])
    send_dispatch.set_sender(sender)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert len(sender.calls) == 1  # no rung 2, no skeleton
    assert "was not sent" in note and "attachments could not be included" in note


def test_a_stripped_file_sends_nothing_at_all(monkeypatch, tmp_path):
    _routine(monkeypatch)
    _stage(tmp_path)
    path = _pre_run_dir(tmp_path) / f"{SKILL}.dispatch.json"
    envelope = json.loads(path.read_text())
    envelope["dispatches"][0]["attachments"][1]["sha256"] = "0" * 64
    path.write_text(json.dumps(envelope))
    sender = _Sender([DispatchResult(sent=True, message_id="never")])
    send_dispatch.set_sender(sender)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert sender.calls == []
    assert "was not sent" in note


def test_a_routine_lane_that_is_authored_is_not_replaced(monkeypatch, tmp_path):
    _routine(monkeypatch)
    monkeypatch.setattr(send_anchor, "routine_lane", lambda skill: "skill:statute-watch")
    _stage(tmp_path)
    sender = _Sender([DispatchResult(sent=True, message_id="m")])
    send_dispatch.set_sender(sender)
    prerendered_dispatch.dispatch_prerendered(SESSION)
    assert sender.calls[0]["lane"] == "skill:statute-watch"


# ---------------------------------------------------------------------------
# End to end: staged handoff -> real seeding -> real attachment scan
# ---------------------------------------------------------------------------


def _wire(monkeypatch, gate):  # noqa: F811
    trust = load_plugin("hermes-smd-trust")
    monkeypatch.setattr(trust.outbound, "_AUDIT_CLIENT", gate._AUDIT_CLIENT)
    monkeypatch.setattr(trust.outbound, "_AUDIT_WIRED", True)
    monkeypatch.setattr(trust.enforce, "evaluate_tool_call", lambda *a, **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_draft", lambda **k: None)
    monkeypatch.setattr(trust.outbound, "check_outbound_send", lambda **k: None)
    monkeypatch.setattr(trust, "get_secret", lambda k: "brokery")
    monkeypatch.setattr(trust, "_seat_email_adapter", lambda: "agentmail")
    sent: list[dict] = []
    monkeypatch.setattr(
        trust.outbound_send.agentmail_broker,
        "send_message",
        lambda body, **kwargs: sent.append({"body": body, **kwargs}) or "am-msg-1",
    )
    send_dispatch.set_sender(trust._dispatch_internal_message)
    return trust, sent


def test_end_to_end_the_staged_dates_let_the_real_scan_pass(gate, monkeypatch, tmp_path):  # noqa: F811
    trust, sent = _wire(monkeypatch, gate)
    _routine(monkeypatch)
    # The flyers' dates are on no record but the staged handoff; the next test
    # is the same send without it.
    staged = _stage(tmp_path)
    trust._seed_from_pre_run_handoff(SESSION)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    [call] = sent
    shas = [d["sha256"] for d, _ in staged.attachments]
    assert [a["sha256"] for a in call["body"]["attachments"]] == shas
    assert call["audit_extra"]["attachment_sha256"] == ",".join(shas)
    assert "already delivered" in note


def test_end_to_end_without_the_handoff_the_same_files_send_nothing(gate, monkeypatch, tmp_path):  # noqa: F811
    trust, sent = _wire(monkeypatch, gate)
    _routine(monkeypatch)
    _stage(tmp_path)
    (_pre_run_dir(tmp_path) / f"{SKILL}.json").unlink()
    trust._seed_from_pre_run_handoff(SESSION)
    note = prerendered_dispatch.dispatch_prerendered(SESSION)
    assert sent == []
    assert "was not sent" in note
