"""Tests for ``establish_intake`` — the root-side establishment daemon (ADR 0085).

Every side effect is faked: a FakeS3 vault, a FakeRunner standing in for the
compiler subprocesses, ``tmp_path`` spools, uid checks skipped (``broker_uid``
injected — the off-box posture, with a mismatch test where it is enforced).

The tests that matter most are the refusals and the recovery invariant
(Law 12 — each one fails on an input the broken behavior would wave through):

* a leak-check rejection leaves the prior spec STANDING (no put, no previous-
  key write, corpus purged anyway);
* the previous key holds the PRE-update body after an update — the falsifiable
  form of "the prior spec is recoverable";
* zero assertions record the selftest NOT_RUN, never "pass";
* demotions ride the result so the Operator's reply can name them;
* the run dir is purged on pass AND fail (the corpus-discard guarantee);
* a submission whose files did not come from the broker uid is refused.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from establish_intake import gates
from establish_intake.intake import (
    STATUS_ACCEPTED_PENDING,
    STATUS_ANALYZED,
    STATUS_ERROR,
    STATUS_INSTALLED,
    STATUS_REJECTED,
    EstablishIntake,
    previous_object_key,
    spec_object_key,
)

BUCKET = "smd-customer-config"
SLUG = "pilot-smokeball"
MAIN_KEY = spec_object_key(SLUG)
PREV_KEY = previous_object_key(SLUG)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _NoSuchKey(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """Vault fake. When ``spec_dir`` is set, a put to the main key immediately
    writes an applier-shaped manifest whose ``source_digest`` matches — the
    'applier converged' world. Leave it None for the applier-lagging world."""

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
        if self.spec_dir is not None and Key == MAIN_KEY:
            self.spec_dir.mkdir(parents=True, exist_ok=True)
            (self.spec_dir / "manifest.json").write_text(
                json.dumps({"source_digest": hashlib.sha256(data).hexdigest()})
            )


class FakeRunner:
    """Stands in for ``subprocess.run`` over the four compilers. Configured
    with per-gate return codes; writes plausible ``--out`` payloads."""

    def __init__(
        self,
        *,
        profile_rc=0,
        fixed_rc=0,
        digit_rc=0,
        leak_rc=0,
        selftest_rc=0,
        candidates=None,
        selftest_report=None,
        leak_stderr="REFUSED: 2 finding(s) in spec.md\n  [containment] line 3 vs letter-01.md: 8-token run",
    ):
        self.profile_rc = profile_rc
        self.fixed_rc = fixed_rc
        self.digit_rc = digit_rc
        self.leak_rc = leak_rc
        self.selftest_rc = selftest_rc
        self.candidates = (
            candidates
            if candidates is not None
            else [
                {
                    "text": "Very truly yours,",
                    "category": "closing",
                    "tokens": 3,
                    "doc_count": 3,
                    "docs": [],
                }
            ]
        )
        self.selftest_report = selftest_report
        self.leak_stderr = leak_stderr
        self.calls: list[list[str]] = []

    def _out_path(self, cmd) -> Path | None:
        return Path(cmd[cmd.index("--out") + 1]) if "--out" in cmd else None

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        script = Path(cmd[1]).name
        rc, stderr = 0, ""
        if script == "voice_profile.py" and "--card" in cmd:
            rc = self.digit_rc
            if rc:
                stderr = "REFUSED: 2 digit(s) in spec.md outside a profile token"
        elif script == "voice_profile.py":
            rc = self.profile_rc
            out = self._out_path(cmd)
            if rc == 0 and out:
                out.write_text(json.dumps({"corpus_docs": 2, "schema_version": 1}))
            stderr = "REFUSED: empty corpus" if rc else ""
        elif script == "spec_fixed_strings.py":
            rc = self.fixed_rc
            out = self._out_path(cmd)
            if rc == 0 and out:
                out.write_text(json.dumps({"candidates": self.candidates, "approved": []}))
        elif script == "spec_leak_check.py":
            rc = self.leak_rc
            stderr = self.leak_stderr if rc else ""
        elif script == "spec_selftest.py":
            rc = self.selftest_rc
            out = self._out_path(cmd)
            report = self.selftest_report or {
                "rules_checked": 1,
                "rules_demoted": 0,
                "results": [],
            }
            if rc in (0, 2) and out:
                out.write_text(json.dumps(report))
        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout="", stderr=stderr)


class _Cfg:
    def __init__(self, declared):
        self.output_classes = declared


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


DOCS = [
    {"doc_id": "doc-1", "name": "letter-01.md", "text": "Dear Ms. Reyes,\n\nWe write plainly.\n"},
    {"doc_id": "doc-2", "name": "letter-02.md", "text": "Dear Mr. Cho,\n\nShort sentences.\n"},
]


def build_run(spool: Path, *, run_id="run-1", phase="install", docs=DOCS, **overrides):
    run_dir = spool / "runs" / run_id
    (run_dir / "docs").mkdir(parents=True)
    for doc in docs:
        payload = {
            **doc,
            "sha256": _sha(doc["text"]),
            "source": {"connector": "smokeball", "document_id": doc["doc_id"]},
        }
        (run_dir / "docs" / f"{doc['doc_id']}.json").write_text(json.dumps(payload))
    submission = {
        "run_id": run_id,
        "staging_id": "set-1",
        "phase": phase,
        "created_at": "2026-08-02T00:00:00Z",
    }
    if phase == "install":
        body = overrides.pop("spec_body", "Write in plain declarative sentences.\n")
        submission.update(
            {
                "output_class": "work_product",
                "property": "voice",
                "spec_body": body,
                "spec_sha256": _sha(body),
                "assertions": {},
                "corpus_manifest": [
                    {"doc_id": d["doc_id"], "sha256": _sha(d["text"])} for d in docs
                ],
                "instructed_by": "chris@firm.com",
                "source_ref": "msg-1",
            }
        )
    submission.update(overrides)
    (run_dir / "submission.json").write_text(json.dumps(submission))
    return run_dir


@pytest.fixture(autouse=True)
def _compilers_present(monkeypatch):
    """The daemon degrades when the compilers are absent; these tests exercise
    the gates through the injected runner, so presence is stipulated (the
    degraded path has its own test that un-stipulates it)."""
    monkeypatch.setattr(gates, "missing_compilers", lambda *a, **k: [])


def make_intake(tmp_path, *, s3=None, runner=None, declared=None, broker_uid=None, auto_apply=True):
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    spec_dir = tmp_path / "specs"
    if s3 is None:
        s3 = FakeS3(spec_dir=spec_dir if auto_apply else None)
    return (
        EstablishIntake(
            spool_dir=spool,
            s3_client=s3,
            bucket=BUCKET,
            slug=SLUG,
            spec_dir=spec_dir,
            broker_uid=broker_uid,
            broker_gid=None,
            gate_runner=runner or FakeRunner(),
            sleep_fn=lambda _s: None,
            converge_timeout=0.0,
            customer_config_fn=(lambda: _Cfg(declared)) if declared is not None else None,
        ),
        s3,
        spool,
    )


def _read_result(spool: Path, run_id="run-1") -> dict:
    return json.loads((spool / "results" / f"{run_id}.json").read_text())


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------


def test_analyze_returns_profile_and_approved_strings(tmp_path):
    intake, _s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool, phase="analyze")
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_ANALYZED
    assert result["profile"]["corpus_docs"] == 2
    assert result["approved_strings"] == ["Very truly yours,"]
    # The raw artifacts persist ROOT-side for the later install's leak check…
    approved = spool / "staging" / "set-1" / "analysis" / "approved_strings.json"
    assert json.loads(approved.read_text())["approved"] == ["Very truly yours,"]
    # …and the run dir (the corpus) is gone.
    assert not run_dir.exists()


def test_analyze_empty_corpus_rejects(tmp_path):
    intake, _s3, spool = make_intake(tmp_path, runner=FakeRunner(profile_rc=1))
    intake.process_run(build_run(spool, phase="analyze"))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("empty corpus" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# Install — the happy path and the merge invariant
# ---------------------------------------------------------------------------


def _seed_existing() -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "customer": SLUG,
            "classes": {"staff": {"voice": {"body": "Warm.", "sha256": _sha("Warm.")}}},
        }
    ).encode()


def test_install_merges_and_never_clobbers_sibling_classes(tmp_path):
    existing = _seed_existing()
    spec_dir = tmp_path / "specs"
    s3 = FakeS3({MAIN_KEY: existing}, spec_dir=spec_dir)
    intake, s3, spool = make_intake(tmp_path, s3=s3)
    intake.process_run(build_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_INSTALLED
    merged = json.loads(s3.objects[MAIN_KEY])
    assert merged["classes"]["staff"]["voice"]["body"] == "Warm."  # sibling preserved
    entry = merged["classes"]["work_product"]["voice"]
    assert entry["sha256"] == _sha(entry["body"])
    assert result["gates"] == {
        "digit_invariant": "pass",
        "leak_check": "pass",
        "selftest": "not_run",
    }


def test_previous_key_holds_the_pre_update_body_after_an_update(tmp_path):
    """The falsifiable form of 'the prior spec is recoverable' (design
    amendment point 2): after an UPDATE, the single fixed previous key holds
    exactly the bytes the vault held before the put."""
    existing = _seed_existing()
    intake, s3, spool = make_intake(
        tmp_path, s3=FakeS3({MAIN_KEY: existing}, spec_dir=tmp_path / "specs")
    )
    intake.process_run(build_run(spool))
    assert s3.objects[PREV_KEY] == existing
    assert _read_result(spool)["previous_key"] == PREV_KEY


def test_first_install_writes_no_previous_key(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool))
    assert PREV_KEY not in s3.objects
    assert _read_result(spool)["previous_key"] is None


def test_applier_lag_reports_accepted_pending_install(tmp_path):
    intake, _s3, spool = make_intake(tmp_path, auto_apply=False)
    intake.process_run(build_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_ACCEPTED_PENDING
    assert any("applier" in w for w in result["warnings"])


def test_undeclared_class_warns_but_installs(tmp_path):
    intake, _s3, spool = make_intake(tmp_path, declared={})
    intake.process_run(build_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_INSTALLED
    assert any("not declared" in w for w in result["warnings"])


def test_declared_class_carries_no_warning(tmp_path):
    intake, _s3, spool = make_intake(
        tmp_path, declared={"work_product": {"voice_spec": "expected"}}
    )
    intake.process_run(build_run(spool))
    assert _read_result(spool)["warnings"] == []


# ---------------------------------------------------------------------------
# Install — gate refusals
# ---------------------------------------------------------------------------


def test_leak_reject_leaves_the_prior_spec_standing(tmp_path):
    """The hard gate. A rejected spec must change NOTHING in the vault — no
    put, no previous-key copy — while the corpus is still purged."""
    existing = _seed_existing()
    s3 = FakeS3({MAIN_KEY: existing})
    intake, s3, spool = make_intake(tmp_path, s3=s3, runner=FakeRunner(leak_rc=2))
    run_dir = build_run(spool)
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert result["gates"]["leak_check"] == "reject"
    assert any("letter-01.md" in r for r in result["reasons"])  # names the doc, offsets only
    assert s3.objects[MAIN_KEY] == existing
    assert s3.puts == []
    assert not run_dir.exists()  # purge happens on fail too


def test_digit_invariant_rejects_a_voice_spec_with_digits(tmp_path):
    intake, s3, spool = make_intake(tmp_path, runner=FakeRunner(digit_rc=2))
    intake.process_run(build_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert result["gates"]["digit_invariant"] == "reject"
    assert s3.puts == []


def test_digit_invariant_is_not_run_for_format_specs(tmp_path):
    runner = FakeRunner()
    intake, _s3, spool = make_intake(tmp_path, runner=runner)
    body = "Sections in this order: summary, findings, next steps.\n"
    intake.process_run(build_run(spool, property="format", spec_body=body))
    assert _read_result(spool)["status"] == STATUS_INSTALLED
    assert not any("--card" in call for call in runner.calls)


def test_selftest_malformed_rules_reject(tmp_path):
    intake, s3, spool = make_intake(tmp_path, runner=FakeRunner(selftest_rc=1))
    intake.process_run(build_run(spool, assertions={"rules": [{"id": "r1", "kind": "absence"}]}))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert result["gates"]["selftest"] == "reject"
    assert s3.puts == []


def test_selftest_demotions_ride_the_result(tmp_path):
    """Demotion is not failure: the install PROCEEDS and the result names each
    demoted rule and the firm documents that broke it, so the Operator's reply
    can be honest about what was demoted (ADR 0085 §8/§4)."""
    report = {
        "rules_checked": 2,
        "rules_demoted": 1,
        "results": [
            {
                "rule_id": "no-hedging",
                "demoted": True,
                "failed_exemplary_docs": ["letter-02.md"],
                "detail": "threshold <= 20 mean words",
            },
            {"rule_id": "short-sentences", "demoted": False, "failed_exemplary_docs": []},
        ],
    }
    intake, _s3, spool = make_intake(tmp_path, runner=FakeRunner(selftest_report=report))
    intake.process_run(
        build_run(
            spool, assertions={"rules": [{"id": "no-hedging", "kind": "absence", "tier": "block"}]}
        )
    )
    result = _read_result(spool)
    assert result["status"] == STATUS_INSTALLED
    assert result["demotions"] == [
        {
            "rule_id": "no-hedging",
            "documents": ["letter-02.md"],
            "detail": "threshold <= 20 mean words",
        }
    ]


def test_zero_rules_record_selftest_not_run_never_passed(tmp_path):
    """Law 12: a check that cannot fail has measured nothing. With no rules the
    selftest is RECORDED not_run — asserting 'pass' here must fail."""
    runner = FakeRunner()
    intake, _s3, spool = make_intake(tmp_path, runner=runner)
    intake.process_run(build_run(spool, assertions={}))
    result = _read_result(spool)
    assert result["gates"]["selftest"] == "not_run"
    assert result["gates"]["selftest"] != "pass"
    assert not any("spec_selftest.py" in call[1] for call in runner.calls)


# ---------------------------------------------------------------------------
# Integrity refusals (design §4)
# ---------------------------------------------------------------------------


def test_spool_files_not_owned_by_the_broker_uid_are_refused(tmp_path):
    """Root's input surface is broker-authored files ONLY. On-box, a spool file
    with any other uid did not pass broker validation and is refused."""
    intake, s3, spool = make_intake(tmp_path, broker_uid=999_999)
    intake.process_run(build_run(spool))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("workspace-broker uid" in r for r in result["reasons"])
    assert s3.puts == []


def test_doc_hash_mismatch_rejects(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool)
    doc_path = run_dir / "docs" / "doc-1.json"
    doc = json.loads(doc_path.read_text())
    doc["text"] = doc["text"] + "tampered"
    doc_path.write_text(json.dumps(doc))
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("rehash" in r for r in result["reasons"])
    assert s3.puts == []


def test_manifest_doc_mismatch_rejects(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_run(
        spool,
        corpus_manifest=[{"doc_id": "doc-1", "sha256": _sha(DOCS[0]["text"])}],  # doc-2 missing
    )
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("doc-2" in r and "corpus_manifest" in r for r in result["reasons"])


def test_spec_body_hash_mismatch_rejects(tmp_path):
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(build_run(spool, spec_sha256="0" * 64))
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("spec_sha256" in r for r in result["reasons"])


def test_per_doc_ceiling_rejects(tmp_path):
    big = [{"doc_id": "doc-big", "name": "big.md", "text": "x" * (1024 * 1024 + 1)}]
    intake, s3, spool = make_intake(tmp_path)
    intake.process_run(
        build_run(
            spool, docs=big, corpus_manifest=[{"doc_id": "doc-big", "sha256": _sha(big[0]["text"])}]
        )
    )
    result = _read_result(spool)
    assert result["status"] == STATUS_REJECTED
    assert any("ceiling" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# Degraded mode + loop mechanics
# ---------------------------------------------------------------------------


def test_missing_compilers_refuse_runs_loudly(tmp_path, monkeypatch):
    """An absent compiler must not mean a skipped gate. The run is answered
    with a terminal error NAMING the missing path, and nothing installs."""
    monkeypatch.setattr(
        gates, "missing_compilers", lambda *a, **k: ["/opt/smd/operator/bin/spec_leak_check.py"]
    )
    intake, s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool)
    intake.process_run(run_dir)
    result = _read_result(spool)
    assert result["status"] == STATUS_ERROR
    assert any("spec_leak_check.py" in r for r in result["reasons"])
    assert s3.puts == []
    assert not run_dir.exists()


def test_poll_once_skips_runs_without_a_submission(tmp_path):
    """submission.json lands LAST (broker-side atomic ordering); a docs-only
    run dir is still being written and must not be consumed."""
    intake, _s3, spool = make_intake(tmp_path)
    half = spool / "runs" / "run-half"
    (half / "docs").mkdir(parents=True)
    assert intake.poll_once() == []
    assert half.exists()


def test_poll_once_never_reads_a_dot_prefixed_assembly_dir(tmp_path):
    """The broker assembles the COMPLETE run — submission.json included —
    inside runs/.tmp-<run_id>/ and atomically renames it into place (ss-console
    workspace_broker/establishment.py contract). A dot-dir therefore LOOKS
    ready by the submission.json test alone; consuming it would gate a partial
    corpus under the wrong run id and purge the path out from under the
    broker's rename. Caught in cross-half reconciliation before first deploy;
    this test fails on the pre-fix scan (iterdir + submission.json only)."""
    intake, _s3, spool = make_intake(tmp_path)
    assembling = spool / "runs" / ".tmp-run-race"
    (assembling / "docs").mkdir(parents=True)
    (assembling / "submission.json").write_text("{}", encoding="utf-8")
    assert intake.poll_once() == []
    assert assembling.exists()
    assert (assembling / "submission.json").is_file()


def test_install_run_purges_the_staging_set_too(tmp_path):
    """Corpus discard: after an install run — pass or fail — the staging set,
    including the root-side analysis artifacts, is gone."""
    intake, _s3, spool = make_intake(tmp_path)
    staging = spool / "staging" / "set-1" / "analysis"
    staging.mkdir(parents=True)
    (staging / "approved_strings.json").write_text('{"approved": []}')
    intake.process_run(build_run(spool))
    assert not (spool / "staging" / "set-1").exists()


def test_results_dir_is_group_writable_so_the_broker_can_unlink(tmp_path):
    """One-shot delivery: the broker deletes a result after first read, and
    unlink requires WRITE on the containing directory. At 0750 the one-shot
    contract is dead letter (broker reads, can never delete; every result
    survives to the TTL sweep). Found in cross-half reconciliation (ss-console
    PR #2181). Falsifier: this test fails at dir mode 0750. Result FILES stay
    0640 — the broker must not be able to rewrite a root-authored verdict."""
    intake, _s3, spool = make_intake(tmp_path)
    run_dir = build_run(spool)
    intake.process_run(run_dir)
    results = spool / "results"
    assert (results.stat().st_mode & 0o777) == 0o770
    result_files = list(results.glob("*.json"))
    assert result_files, "a result file must exist after process_run"
    assert (result_files[0].stat().st_mode & 0o777) == 0o640


def test_default_spool_dir_is_outside_the_agent_home():
    """The Hermes gateway chmods /opt/data to 0700 mid-boot, so a spool under
    that tree is unreachable by the workspace-broker uid — the principal that
    creates staging sets and run dirs — while its own dirs read a correct 0770.
    Live-caught on hermes-pilot-smokeball 2026-08-02 (first establishment call
    returned PermissionError on a 0770 staging dir). Falsifier: this test fails
    on the pre-fix default."""
    from establish_intake.__main__ import _DEFAULT_SPOOL_DIR

    assert not _DEFAULT_SPOOL_DIR.startswith("/opt/data")
    assert _DEFAULT_SPOOL_DIR == "/var/lib/smd-establish-spool"


# ---------------------------------------------------------------------------
# Corpus provenance on install (ss-console#2339)
#
# The run already synthesizes this at install time for the leak check's
# proper-noun scan, and it dies with the run directory. Card 4 asked the seat
# what it reviewed and it could not say. This carries the answer onto the
# installed spec.
# ---------------------------------------------------------------------------


def test_install_records_which_documents_the_spec_was_learned_from(tmp_path):
    spec_dir = tmp_path / "specs"
    intake, s3, spool = make_intake(tmp_path, s3=FakeS3({}, spec_dir=spec_dir))
    intake.process_run(build_run(spool))
    assert _read_result(spool)["status"] == STATUS_INSTALLED

    prov = json.loads(s3.objects[MAIN_KEY])["classes"]["work_product"]["voice"]["provenance"]
    assert prov["run_id"] == "run-1"
    assert prov["document_count"] == 2
    assert [d["name"] for d in prov["documents"]] == ["letter-01.md", "letter-02.md"]
    # The digest of each source document, so the record is checkable later.
    assert [d["sha256"] for d in prov["documents"]] == [_sha(d["text"]) for d in DOCS]


def test_provenance_carries_no_document_text(tmp_path):
    """Letters 07 and 10 promise the firm we keep no copy of their matter
    files. The record of WHAT WE READ must not become a second copy of it.

    FALSIFIER: include the staged text and this finds the client names the
    fixture documents open with.
    """
    spec_dir = tmp_path / "specs"
    intake, s3, spool = make_intake(tmp_path, s3=FakeS3({}, spec_dir=spec_dir))
    intake.process_run(build_run(spool))

    serialized = json.dumps(
        json.loads(s3.objects[MAIN_KEY])["classes"]["work_product"]["voice"]["provenance"]
    )
    assert "Reyes" not in serialized
    assert "Cho" not in serialized
    assert "Short sentences" not in serialized
