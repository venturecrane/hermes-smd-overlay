"""Tests for the intake's person-scoped install (ADR 0085 §6, ss#2067).

The load-bearing properties, each with the input the broken behavior would
have waved through (Law 12):

* NO COMPILER GATES RUN, and that is asserted positively — a person run
  installs even when every compiler is missing, because there is no corpus to
  gate; a future "hardening" that routes person runs through the gates fails
  here and must confront the design;
* the ROSTER CHECK FAILS CLOSED — an unrostered subject, an unwired config
  reader, and a raising reader all refuse, because "cannot evaluate" must not
  read as "permitted";
* the previous key holds the PRE-update bytes (the recovery invariant, in its
  falsifiable form);
* the run dir is purged on pass AND fail;
* the firm path is untouched: an unknown scope refuses, and a person run
  refuses any phase but install.
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
)
from shared.person_prefs import PREFS_MANIFEST_NAME, person_slug
from spec_applier.preferences import person_pref_key, previous_person_pref_key

BUCKET = "smd-customer-config"
SLUG = "pilot-smokeball"
PERSON = "sarah@firm.com"
PSLUG = person_slug(PERSON)
PREF_KEY = person_pref_key(SLUG, PSLUG)
PREV_KEY = previous_person_pref_key(SLUG, PSLUG)


class _NoSuchKey(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """Vault fake. When ``spec_dir`` is set, a put to the preference key
    immediately writes an applier-shaped preferences manifest whose entry hash
    matches — the 'applier converged' world."""

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
        if self.spec_dir is not None and Key == PREF_KEY:
            self.spec_dir.mkdir(parents=True, exist_ok=True)
            (self.spec_dir / PREFS_MANIFEST_NAME).write_text(
                json.dumps({"preferences": {PSLUG: {"sha256": hashlib.sha256(data).hexdigest()}}})
            )


class _RosterConfig:
    def __init__(self, roster):
        self._roster = roster

    def sender_on_roster(self, addr):
        return addr in self._roster


@pytest.fixture(autouse=True)
def _compilers_present(monkeypatch):
    """Person runs never consult the compilers, but FIRM-defaulting refusal
    paths (unknown scope, torn submissions) route through the degraded gate
    first on a box without the compiler binaries — so presence is stipulated,
    exactly as in test_establish_intake.py; the degraded-bypass test below
    un-stipulates it deliberately."""
    monkeypatch.setattr(gates, "missing_compilers", lambda *a, **k: [])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_person_run(spool: Path, *, run_id="run-p1", body="Bullets. Under 150 words.", **over):
    run_dir = spool / "runs" / run_id
    (run_dir / "docs").mkdir(parents=True)
    submission = {
        "run_id": run_id,
        "phase": "install",
        "scope": "person",
        "person": PERSON,
        "spec_body": body,
        "spec_sha256": _sha(body),
        "assertions": None,
        "instructed_by": PERSON,
        "source_ref": "msg-9",
        "created_at": "2026-08-02T00:00:00Z",
    }
    submission.update(over)
    (run_dir / "submission.json").write_text(json.dumps(submission))
    return run_dir


def make_intake(tmp_path, *, s3=None, roster=(PERSON,), config_fn="default", auto_apply=True):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    spec_dir = tmp_path / "specs"
    if s3 is None:
        s3 = FakeS3(spec_dir=spec_dir if auto_apply else None)
    if config_fn == "default":
        config_fn = lambda: _RosterConfig(list(roster))  # noqa: E731
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
            customer_config_fn=config_fn,
        ),
        s3,
        spool,
    )


def _read_result(spool: Path, run_id="run-p1") -> dict:
    return json.loads((spool / "results" / f"{run_id}.json").read_text())


# ---------------------------------------------------------------------------
# Happy path — install, converge, provenance
# ---------------------------------------------------------------------------


def test_person_install_writes_the_vault_object_and_converges(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_person_run(spool)
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_INSTALLED
    assert result["scope"] == "person"
    assert result["person"] == PERSON
    assert result["person_slug"] == PSLUG
    assert result["previous_key"] is None  # first establishment — nothing to recover
    doc = json.loads(s3.objects[PREF_KEY])
    assert doc["person"] == PERSON
    assert doc["person_slug"] == PSLUG
    assert doc["body"] == "Bullets. Under 150 words."
    assert doc["sha256"] == _sha(doc["body"])
    assert doc["instructed_by"] == PERSON
    assert not run_dir.exists()  # purged


def test_update_keeps_the_pre_update_bytes_on_the_previous_key(tmp_path):
    """The recovery invariant in its falsifiable form (firm-path parity)."""
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_person_run(spool, run_id="run-p1", body="First version."))
    before = s3.objects[PREF_KEY]
    intake.process_run(build_person_run(spool, run_id="run-p2", body="Second version."))
    result = _read_result(spool, "run-p2")
    assert result["status"] == STATUS_INSTALLED
    assert result["previous_key"] == PREV_KEY
    assert s3.objects[PREV_KEY] == before


def test_lagging_applier_reports_accepted_pending(tmp_path):
    intake, _s3, spool = make_intake(tmp_path, auto_apply=False)
    intake.process_run(build_person_run(spool))
    assert _read_result(spool)["status"] == STATUS_ACCEPTED_PENDING


def test_person_install_runs_no_gates_even_when_compilers_are_missing(tmp_path, monkeypatch):
    """THE design assertion: a person run has no corpus, so an absent compiler
    cannot mean a skipped gate — it installs while a FIRM run on the same
    degraded daemon is refused (that refusal has its own test in
    test_establish_intake.py)."""
    monkeypatch.setattr(
        gates, "missing_compilers", lambda *a, **k: ["/opt/smd/operator/bin/spec_leak_check.py"]
    )
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_person_run(spool))
    assert _read_result(spool)["status"] == STATUS_INSTALLED
    assert PREF_KEY in s3.puts


# ---------------------------------------------------------------------------
# The roster backstop — fail-closed in every direction
# ---------------------------------------------------------------------------


def test_unrostered_subject_is_refused_by_name(tmp_path):
    intake, s3, spool = make_intake(tmp_path, roster=("someone-else@firm.com",))
    intake.process_run(build_person_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("not on the organization roster" in r for r in result["reasons"])
    assert s3.puts == []


def test_unwired_config_reader_refuses(tmp_path):
    intake, s3, spool = make_intake(tmp_path, config_fn=None)
    intake.process_run(build_person_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("roster cannot be checked" in r for r in result["reasons"])
    assert s3.puts == []


def test_raising_config_reader_refuses(tmp_path):
    def broken():
        raise RuntimeError("volume gone")

    intake, s3, spool = make_intake(tmp_path, config_fn=broken)
    intake.process_run(build_person_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("roster unreadable" in r for r in result["reasons"])
    assert s3.puts == []


# ---------------------------------------------------------------------------
# Shape refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"person": "@firm.com"}, "not a valid person address"),
        ({"person": None}, "not a valid person address"),
        ({"spec_body": ""}, "spec_body missing or empty"),
        ({"spec_sha256": "0" * 64}, "does not rehash"),
        ({"assertions": "prose"}, "assertions must be"),
        ({"phase": "analyze"}, "only phase 'install'"),
    ],
)
def test_malformed_person_submissions_are_refused_and_purged(tmp_path, over, needle):
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_person_run(spool, **over)
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any(needle in r for r in result["reasons"])
    assert s3.puts == []
    assert not run_dir.exists()  # the purge holds on failure too


def test_unknown_scope_is_refused(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_person_run(spool, scope="team"))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("unknown scope" in r for r in result["reasons"])
    assert s3.puts == []
