"""The intake's ``firm_adjust`` scope (ss-console#2529, ADR 0085 §4 amended).

One sentence an Operator admin instructed and confirmed by reply, appended to a
property's standing adjustments. What this file pins:

* the append is an APPEND — the distilled body, its hash, its assertions and its
  provenance come through untouched, because "the firm said one more thing" is
  not a reason to forget what it read to learn the firm's voice;
* a retry of the same adjustment id changes nothing and says so, rather than
  rendering the firm's sentence twice;
* the ceilings refuse rather than truncate, and refuse BEFORE anything is
  written — no pointless recovery copy on a rejected run;
* NO COMPILER GATES RUN, asserted positively: a firm_adjust run installs on a
  daemon whose compilers are missing, because it has no corpus for them to
  examine. This is the design, not an oversight, and a future hardening pass
  that routes it through the gates fails here and has to confront it;
* and THE ONE THAT MATTERS MOST: a later establishment from documents keeps the
  adjustments. That is the falsifier below.

THE FALSIFIER, run against 349d86b (the parent commit): ``_put_and_converge``
wrote ``{"body": ..., "sha256": ...}`` over the whole property, so
``test_a_document_establishment_keeps_the_standing_adjustments`` fails with
``KeyError: 'adjustments'`` — every sentence the firm had confirmed was silently
deleted by the next corpus-fed run, with nothing anywhere reporting it.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from establish_intake import gates
from establish_intake.intake import (
    STATUS_ACCEPTED_PENDING,
    STATUS_INSTALLED,
    STATUS_REJECTED,
    EstablishIntake,
    spec_object_key,
)
from spec_applier.applier import MAX_ADJUSTMENTS, render_spec
from tests.test_establish_intake import FakeRunner

BUCKET = "smd-customer-config"
SLUG = "pilot-smokeball"
KEY = spec_object_key(SLUG)
PREV_KEY = f"vaults/{SLUG}/output-classes.previous.json"
CLASS = "outbound"
PROP = "voice"

TEXT = "In client letters, be more formal and shorter; no pleasantries."


class _NoSuchKey(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """Vault fake. With ``spec_dir`` set, a put to the spec key immediately
    writes an applier-shaped manifest whose source_digest matches — the
    'applier converged' world."""

    def __init__(self, objects=None, spec_dir: Path | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.puts: list[str] = []
        self.spec_dir = spec_dir

    def get_object(self, *, Bucket, Key):  # noqa: N803 — boto3 kwargs
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, **_kw):  # noqa: N803
        data = Body if isinstance(Body, bytes) else bytes(Body)
        self.objects[Key] = data
        self.puts.append(Key)
        if self.spec_dir is not None and Key == KEY:
            self.spec_dir.mkdir(parents=True, exist_ok=True)
            (self.spec_dir / "manifest.json").write_text(
                json.dumps({"source_digest": hashlib.sha256(data).hexdigest()})
            )


class _Config:
    def __init__(self, classes=(CLASS,)):
        self.output_classes = list(classes)

    def sender_on_roster(self, addr):  # pragma: no cover — firm_adjust never asks
        return True


@pytest.fixture(autouse=True)
def _compilers_present(monkeypatch):
    """Stipulated so the FIRM-defaulting refusal paths do not trip the degraded
    gate on a box without the compiler binaries. The degraded-bypass test below
    un-stipulates it deliberately."""
    monkeypatch.setattr(gates, "missing_compilers", lambda *a, **k: [])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def adjustment(**over) -> dict:
    record = {
        "id": "7f3a2c1d",
        "text": TEXT,
        "sha256": _sha(TEXT),
        "instructed_by": "christa@firm.com",
        "applied_by": "christa@firm.com",
        "at": "2026-08-21T18:04:00Z",
    }
    record.update(over)
    if "text" in over and "sha256" not in over:
        record["sha256"] = _sha(str(over["text"]))
    return record


def build_run(spool: Path, *, run_id="run-a1", **over):
    run_dir = spool / "runs" / run_id
    run_dir.mkdir(parents=True)
    submission = {
        "run_id": run_id,
        "phase": "install",
        "scope": "firm_adjust",
        "output_class": CLASS,
        "property": PROP,
        "adjustment": adjustment(),
        "instructed_by": "christa@firm.com",
        "source_ref": "msg-41",
        "created_at": "2026-08-21T18:04:00Z",
    }
    submission.update(over)
    (run_dir / "submission.json").write_text(json.dumps(submission))
    return run_dir


def make_intake(tmp_path, *, s3=None, auto_apply=True, classes=(CLASS,)):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    spec_dir = tmp_path / "specs"
    if s3 is None:
        s3 = FakeS3(spec_dir=spec_dir if auto_apply else None)
    elif auto_apply:
        s3.spec_dir = spec_dir
    return (
        EstablishIntake(
            spool_dir=spool,
            s3_client=s3,
            bucket=BUCKET,
            slug=SLUG,
            spec_dir=spec_dir,
            broker_uid=None,
            broker_gid=None,
            sleep_fn=lambda _s: None,
            converge_timeout=0.0,
            customer_config_fn=lambda: _Config(classes),
        ),
        s3,
        spool,
    )


def _result(spool: Path, run_id="run-a1") -> dict:
    return json.loads((spool / "results" / f"{run_id}.json").read_text())


def _prop(s3: FakeS3) -> dict:
    return json.loads(s3.objects[KEY])["classes"][CLASS][PROP]


def _vault_with_body(body="Write plainly.", adjustments=None) -> bytes:
    entry: dict = {
        "body": body,
        "sha256": _sha(body),
        "assertions": {"rules": [{"id": "r1", "severity": "block"}]},
        "provenance": {"run_id": "run-old", "document_count": 3, "documents": []},
    }
    if adjustments is not None:
        entry["adjustments"] = adjustments
    return json.dumps(
        {"schema_version": 1, "customer": SLUG, "classes": {CLASS: {PROP: entry}}},
        sort_keys=True,
    ).encode()


# ---------------------------------------------------------------------------
# The append
# ---------------------------------------------------------------------------


def test_the_first_adjustment_installs_onto_a_property_that_has_no_body(tmp_path):
    """A&P's shape: a declared class, no voice spec, and a partner who says how
    the letters should read. The whole point of the scope."""
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool)
    intake.process_run(run_dir)
    result = _result(spool)
    assert result["status"] == STATUS_INSTALLED
    assert result["scope"] == "firm_adjust"
    assert result["adjustment_id"] == "7f3a2c1d"
    assert result["output_class"] == CLASS
    assert result["property"] == PROP
    entry = _prop(s3)
    assert "body" not in entry
    assert [a["id"] for a in entry["adjustments"]] == ["7f3a2c1d"]
    assert entry["adjustments"][0]["text"] == TEXT
    assert not run_dir.exists()  # purged


def test_an_adjustment_leaves_the_body_and_its_provenance_untouched(tmp_path):
    intake, s3, spool = make_intake(tmp_path, s3=FakeS3({KEY: _vault_with_body()}))
    before = json.loads(s3.objects[KEY])["classes"][CLASS][PROP]
    intake.process_run(build_run(spool))
    assert _result(spool)["status"] == STATUS_INSTALLED
    after = _prop(s3)
    for field in ("body", "sha256", "assertions", "provenance"):
        assert after[field] == before[field]
    assert len(after["adjustments"]) == 1


def test_a_second_adjustment_appends_oldest_first(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool, run_id="run-a1"))
    intake.process_run(
        build_run(
            spool,
            run_id="run-a2",
            adjustment=adjustment(id="0b91ee42", text="Name the deadline first."),
        )
    )
    assert _result(spool, "run-a2")["status"] == STATUS_INSTALLED
    assert [a["id"] for a in _prop(s3)["adjustments"]] == ["7f3a2c1d", "0b91ee42"]


def test_a_repeat_of_the_same_id_changes_nothing_and_says_so(tmp_path):
    """The broker consumes a proposal once, but a result it never collected can
    bring the same run back around. Re-appending would render the firm's
    sentence twice, in a file the firm reads."""
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool, run_id="run-a1"))
    intake.process_run(build_run(spool, run_id="run-a2"))
    result = _result(spool, "run-a2")
    assert result["status"] == STATUS_INSTALLED
    assert any("already in effect" in w for w in result["warnings"])
    assert len(_prop(s3)["adjustments"]) == 1


def test_the_previous_key_holds_the_pre_update_bytes(tmp_path):
    intake, s3, spool = make_intake(tmp_path, s3=FakeS3({KEY: _vault_with_body()}))
    before = s3.objects[KEY]
    intake.process_run(build_run(spool))
    result = _result(spool)
    assert result["previous_key"] == PREV_KEY
    assert s3.objects[PREV_KEY] == before


def test_an_undeclared_class_warns_but_installs(tmp_path):
    intake, s3, spool = make_intake(tmp_path, classes=("staff",))
    intake.process_run(build_run(spool))
    result = _result(spool)
    assert result["status"] == STATUS_INSTALLED
    assert any("not declared in customer.yaml" in w for w in result["warnings"])


def test_a_lagging_applier_reports_accepted_pending(tmp_path):
    """The honest status. The reply that goes back to the firm turns on this:
    "in effect" is only sayable once the applier has adopted the object."""
    intake, _s3, spool = make_intake(tmp_path, auto_apply=False)
    intake.process_run(build_run(spool))
    assert _result(spool)["status"] == STATUS_ACCEPTED_PENDING


# ---------------------------------------------------------------------------
# The falsifier: the document path must carry the list forward
# ---------------------------------------------------------------------------


def test_a_document_establishment_keeps_the_standing_adjustments(tmp_path):
    """THE falsifier. Before this change the corpus-fed install replaced the
    whole property, so re-establishing the voice on Tuesday silently deleted
    every sentence the firm had confirmed on Monday."""
    existing = [
        {
            "id": "7f3a2c1d",
            "text": TEXT,
            "instructed_by": "christa@firm.com",
            "at": "2026-08-21T18:04:00Z",
        }
    ]
    s3 = FakeS3({KEY: _vault_with_body(adjustments=existing)})
    intake, s3, spool = make_intake(tmp_path, s3=s3)
    new_body = "Formal. Short. No hedging."
    run_dir = spool / "runs" / "run-doc"
    (run_dir / "docs").mkdir(parents=True)
    (run_dir / "docs" / "d1.json").write_text(
        json.dumps(
            {
                "doc_id": "d1",
                "name": "letter-01.docx",
                "sha256": _sha("Dear Sir, ..."),
                "text": "Dear Sir, ...",
            }
        )
    )
    (run_dir / "submission.json").write_text(
        json.dumps(
            {
                "run_id": "run-doc",
                "staging_id": "stage1",
                "phase": "install",
                "scope": "firm",
                "output_class": CLASS,
                "property": PROP,
                "spec_body": new_body,
                "spec_sha256": _sha(new_body),
                "corpus_manifest": [{"doc_id": "d1", "sha256": _sha("Dear Sir, ...")}],
                "instructed_by": "christa@firm.com",
                "source_ref": "msg-9",
                "created_at": "2026-08-22T00:00:00Z",
            }
        )
    )
    intake.gate_runner = FakeRunner()
    intake.process_run(run_dir)
    result = _result(spool, "run-doc")
    assert result["status"] == STATUS_INSTALLED, result["reasons"]
    entry = _prop(s3)
    assert entry["body"] == new_body
    assert [a["id"] for a in entry["adjustments"]] == ["7f3a2c1d"]


# ---------------------------------------------------------------------------
# No gates, by design
# ---------------------------------------------------------------------------


def test_an_adjustment_installs_on_a_daemon_whose_compilers_are_missing(tmp_path, monkeypatch):
    """No corpus means no gate could have run, so an absent compiler cannot
    mean a skipped one. Refusing here would mean a firm whose leak-check binary
    is missing can no longer tell its Operator how to write a letter."""
    monkeypatch.setattr(
        gates, "missing_compilers", lambda *a, **k: ["/opt/smd/operator/bin/spec_leak_check.py"]
    )
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool))
    assert _result(spool)["status"] == STATUS_INSTALLED
    assert KEY in s3.puts


# ---------------------------------------------------------------------------
# Refusals — nothing written, on any of them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"output_class": "Outbound"}, "outside the permitted charset"),
        ({"output_class": "../etc"}, "outside the permitted charset"),
        ({"property": "gates"}, "property must be one of"),
        ({"phase": "analyze"}, "only phase 'install'"),
        ({"adjustment": None}, "adjustment missing or not an object"),
        ({"adjustment": "be formal"}, "adjustment missing or not an object"),
    ],
)
def test_malformed_submissions_are_refused_and_purged(tmp_path, over, needle):
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool, **over)
    intake.process_run(run_dir)
    result = _result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any(needle in r for r in result["reasons"]), result["reasons"]
    assert s3.puts == []
    assert not run_dir.exists()


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"id": "7F3A2C1D"}, "eight lowercase hex"),
        ({"id": "7f3a"}, "eight lowercase hex"),
        ({"text": ""}, "text missing or empty"),
        ({"text": "Be formal.\nBe short."}, "line break"),
        ({"text": "x" * 2001}, "byte ceiling"),
        ({"sha256": "0" * 64}, "does not rehash"),
        ({"instructed_by": ""}, "instructed_by missing or empty"),
        ({"applied_by": "   "}, "applied_by must be"),
        ({"at": ""}, "at missing or empty"),
    ],
)
def test_a_malformed_adjustment_record_is_refused(tmp_path, over, needle):
    """Re-verified field by field behind the broker (design §4): root's input
    surface does not trust the spool because the permissions say only the broker
    can write it."""
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool, adjustment=adjustment(**over)))
    result = _result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any(needle in r for r in result["reasons"]), result["reasons"]
    assert s3.puts == []


def test_past_the_entry_ceiling_refuses_and_writes_nothing(tmp_path):
    """Refused BEFORE the recovery copy: a rejected run leaves no trace at all."""
    existing = [
        {
            "id": f"{i:08x}",
            "text": f"Rule {i}.",
            "instructed_by": "christa@firm.com",
            "at": "2026-08-21T18:04:00Z",
        }
        for i in range(MAX_ADJUSTMENTS)
    ]
    s3 = FakeS3({KEY: _vault_with_body(adjustments=existing)})
    intake, s3, spool = make_intake(tmp_path, s3=s3)
    intake.process_run(build_run(spool))
    result = _result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("re-establish the property from documents" in r for r in result["reasons"])
    assert s3.puts == []


def test_past_the_byte_ceiling_refuses(tmp_path):
    big = "x" * 260_000  # legal on its own; over the ceiling once a rule renders below it
    s3 = FakeS3({KEY: _vault_with_body(body=big)})
    intake, s3, spool = make_intake(tmp_path, s3=s3)
    intake.process_run(build_run(spool, adjustment=adjustment(text="y" * 1990)))
    result = _result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("over the" in r and "byte ceiling" in r for r in result["reasons"])
    assert s3.puts == []


# ---------------------------------------------------------------------------
# The two halves agree on the render
# ---------------------------------------------------------------------------


def test_the_installed_entry_renders_through_the_appliers_own_function(tmp_path):
    """The intake measures the ceiling against the string the applier will
    produce, because two renderers would drift and the drift would show up as a
    seat that accepted a rule the applier then refused."""
    intake, s3, spool = make_intake(tmp_path, s3=FakeS3({KEY: _vault_with_body()}))
    intake.process_run(build_run(spool))
    entry = _prop(s3)
    rendered = render_spec(entry["body"], entry["adjustments"])
    assert TEXT in rendered
    assert rendered.startswith("Write plainly.\n\n## Adjustments")
