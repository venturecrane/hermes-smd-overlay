"""Verify → gate-run → install → result → purge, for one establishment run.

THE SPOOL CONTRACT (with the broker's establishment verbs — ss-console PR C0,
built from the same design §3; the two halves meet on these exact names):

    <spool>/                          root:workspace-broker 0750; hermes: none
      staging/<staging_id>/docs/<doc_id>.json     broker-written
      staging/<staging_id>/analysis/              ROOT-written (this daemon),
                                                  0700 — analyze artifacts
      runs/<run_id>/docs/<doc_id>.json            broker-written (copied for
                                                  analyze, moved for install)
      runs/<run_id>/submission.json               broker-written LAST — its
                                                  presence marks the run ready
      results/<run_id>.json                       ROOT-written, 0640
                                                  root:workspace-broker (the
                                                  broker reads + deletes after
                                                  first read; TTL sweep here)

``submission.json`` fields (broker-rebuilt from a bounded set, never forwarded
from the wire): ``run_id``, ``staging_id``, ``phase`` (``analyze``/``install``),
``created_at``; install adds ``output_class``, ``property``, ``spec_body``
(LF-normalized broker-side), ``spec_sha256`` (broker-computed), ``assertions``
(object, optional), ``corpus_manifest`` (``[{doc_id, sha256}]``),
``instructed_by``, ``source_ref``. Doc files carry ``doc_id``, ``name``,
``sha256`` (broker-computed), ``text``, ``source``.

WHAT THIS DAEMON VERIFIES ITSELF (design §4 — defense in depth behind the
broker, because root's input surface must not TRUST the spool merely because
perms say only the broker can write it):

1. every submission + doc file's uid == the workspace-broker uid (stat);
2. internal hash consistency — every doc rehashes to its declared sha256, the
   manifest maps 1:1 onto the staged docs, and the spec body rehashes;
3. ceilings re-checked (docs ≤64, total ≤16 MiB, doc ≤1 MiB, spec ≤256 KiB);
4. vocabulary re-checked (property, class-slug charset);
5. the compiler gates (``gates.py``) — the control that bounds a hostile
   submission's damage;
6. the prior spec is copied to the SINGLE fixed previous key before every put.

Claimed ``instructed_by`` is provenance for the audit trail, never
authorization (same posture as corrections ``stated_by``); authorization is the
hook-side admin gate, which this process structurally cannot see.

PRIOR-SPEC RECOVERY (design amendment point 2 — the versioned-key scheme is
VOID): before every put, the current vault object is copied verbatim to
``vaults/<slug>/output-classes.previous.json``. Recovery is one generation deep
BY DESIGN; versioned history is a filed follow-on. The rehearsal asserts this
falsifiably: after an update, the previous-key body equals the pre-update body.

CORPUS-DISCARD GUARANTEE. The run directory — corpus, approved strings,
attestation, gate outputs — is purged on BOTH pass and fail, and the staging
set is purged after any install run. After the purge, nothing on the seat or in
R2 can re-run the leak check. The result file carries doc NAMES, rule ids and
counts — never corpus text (the compilers themselves print offsets only).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config_applier.applier import atomic_write
from establish_intake import gates
from shared.ids import iso_utc, sha256
from shared.person_prefs import (
    MAX_PREF_BODY_BYTES,
    PREFS_MANIFEST_NAME,
    normalize_person_address,
    person_slug,
)
from spec_applier.preferences import person_pref_key, previous_person_pref_key

logger = logging.getLogger(__name__)

#: The uid every spool input must carry. Resolved by name so the check follows
#: the image's user database rather than a hardcoded number.
BROKER_USER = "workspace-broker"

#: Ceilings, re-checked here even though the broker enforced them (design §4).
MAX_DOCS = 64
MAX_DOC_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SPEC_BYTES = 256 * 1024  # applier parity (spec_applier.applier.MAX_SPEC_BYTES)

SPEC_PROPERTIES = ("voice", "format")
_SAFE_SEGMENT = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")

#: Result files the broker never collected are swept after this long.
RESULT_TTL_SECONDS = 30 * 60
#: Backstop sweep for staging sets (the broker's own 30-min TTL sweep cannot
#: remove the root-owned ``analysis/`` subdir, so root is the finisher).
STAGING_TTL_SECONDS = 60 * 60

#: How long to wait for the installed manifest to converge on the new object.
CONVERGE_TIMEOUT_SECONDS = 90.0
CONVERGE_INTERVAL_SECONDS = 3.0

#: Terminal result statuses.
STATUS_ANALYZED = "analyzed"
STATUS_INSTALLED = "installed"
STATUS_ACCEPTED_PENDING = "accepted_pending_install"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"


def previous_object_key(slug: str) -> str:
    """The single fixed recovery key — one generation deep by design."""
    return f"vaults/{slug}/output-classes.previous.json"


def spec_object_key(slug: str) -> str:
    """The vault object the fail-static applier polls (spec_applier parity)."""
    return f"vaults/{slug}/output-classes.json"


def _corpus_provenance(run_id: str, docs: list[dict[str, Any]]) -> dict[str, Any]:
    """What this spec was learned from, in a form the agent can read later.

    WHY (ss-console#2339, rehearsal card 4, 2026-08-12). Asked "show me what you
    learned about how we write — and what you reviewed to learn it", the seat
    answered the first half in depth and the second not at all: "I cannot read
    the establishment tool's audit log from this turn, so I cannot name the
    individual corpus documents by title here." Letter 23 commits us in writing
    to self-initialization that "includes reading matters in Smokeball to
    synthesize the firm's voice", and this firm's diligence thread is about
    retention and confidentiality (letters 07 and 10). "I read your documents
    but cannot tell you which" is the worst available answer to that reader.

    The run already synthesizes exactly this at install time — the
    ``provenance.json`` written for the leak check's proper-noun scan — but it
    dies with the run directory. This carries it onto the installed spec, so the
    question is answerable from an ordinary turn with no run id.

    NAMES AND DIGESTS ONLY, never text. Same rule as the leak-check file it
    mirrors, and the reason letters 07 and 10 can stay true: the firm's answer to
    "what did you read" must not itself become a copy of what was read.

    NO COHORT FIELD. The staged docs carry ``doc_id``/``name``/``sha256`` and
    nothing else — inferring which audience each document belonged to is
    precisely the invention the seat correctly refused to commit. A caller that
    needs cohorts must stage them.
    """
    return {
        "run_id": run_id,
        "document_count": len(docs),
        "documents": [
            {"name": str(d.get("name") or ""), "sha256": str(d.get("sha256") or "")} for d in docs
        ],
    }


def _resolve_broker_uid() -> int | None:
    """The workspace-broker uid, or ``None`` off-box (dev/test machines have no
    such user; the uid check is then skipped WITH a warning — same posture as
    ``spec_applier._harden``, whose chown is a no-op off-root)."""
    try:
        import pwd

        return pwd.getpwnam(BROKER_USER).pw_uid
    except (ImportError, KeyError):
        return None


def _resolve_broker_gid() -> int | None:
    try:
        import grp

        return grp.getgrnam(BROKER_USER).gr_gid
    except (ImportError, KeyError):
        return None


@dataclass
class EstablishIntake:
    """One customer's establishment intake, every side effect injectable.

    ``s3_client`` is a boto3-shaped client (get_object/put_object); ``spec_dir``
    is the spec_applier install target whose ``manifest.json`` the converge-wait
    reads. ``broker_uid`` defaults to the on-box resolution and may be injected
    for tests; ``None`` skips the uid checks (off-box only).
    """

    spool_dir: Path
    s3_client: Any
    bucket: str
    slug: str
    spec_dir: Path
    broker_uid: int | None = field(default_factory=_resolve_broker_uid)
    broker_gid: int | None = field(default_factory=_resolve_broker_gid)
    gate_runner: Any = None  # forwarded to gates.run_* as `runner` when set
    sleep_fn: Callable[[float], None] = time.sleep
    now_fn: Callable[[], float] = time.time
    converge_timeout: float = CONVERGE_TIMEOUT_SECONDS
    converge_interval: float = CONVERGE_INTERVAL_SECONDS
    #: Live customer-config reader for the undeclared-class warning; injected
    #: in tests. ``None`` values fail soft (warning suppressed, install still
    #: correct — declaration is sequencing hygiene (#2094), not a gate).
    customer_config_fn: Callable[[], Any] | None = None
    _uid_skip_logged: bool = False

    # ------------------------------------------------------------------
    # Spool layout
    # ------------------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self.spool_dir / "runs"

    @property
    def results_dir(self) -> Path:
        return self.spool_dir / "results"

    @property
    def staging_dir(self) -> Path:
        return self.spool_dir / "staging"

    def analysis_dir(self, staging_id: str) -> Path:
        return self.staging_dir / staging_id / "analysis"

    # ------------------------------------------------------------------
    # Poll loop body
    # ------------------------------------------------------------------

    def poll_once(self) -> list[str]:
        """One tick: sweep TTLs, then process every ready run. Returns the
        run ids processed this tick. Never raises for a single bad run — each
        run's fault becomes its result file, and the loop continues."""
        self._sweep_results()
        self._sweep_staging()
        processed: list[str] = []
        if not self.runs_dir.is_dir():
            return processed
        for run_dir in sorted(p for p in self.runs_dir.iterdir() if p.is_dir()):
            if run_dir.name.startswith("."):
                # Broker assembly area: runs are built complete (INCLUDING
                # submission.json) inside a dot-prefixed temp dir and atomically
                # renamed into place. Processing one mid-assembly would run the
                # gates against a partial corpus under the wrong run id, and
                # the broker's rename would then hit a purged path. The rename
                # is the publish; a dot-dir is never ours to read.
                continue
            if not (run_dir / "submission.json").is_file():
                continue  # broker still writing — submission.json lands last
            self.process_run(run_dir)
            processed.append(run_dir.name)
        return processed

    def process_run(self, run_dir: Path) -> dict[str, Any]:
        """Process one run end to end. The result file is written and the run
        dir purged on EVERY path out of here, including a crash — a run must
        never be silently dropped (the agent is polling) nor survive as corpus
        on disk (the discard guarantee)."""
        run_id = run_dir.name
        degraded = gates.missing_compilers()
        try:
            if not _SAFE_SEGMENT.match(run_id):
                result = self._result(
                    run_id, "?", STATUS_REJECTED, reasons=[f"unsafe run id {run_id!r}"]
                )
            elif degraded and self._submission_scope(run_dir) != "person":
                # Person-scoped runs deliberately survive a degraded daemon:
                # they run NO compiler gates (see _install_person), so an
                # absent compiler cannot mean a skipped gate for them — while
                # for firm runs the refusal stands (Law 12: a run the gates
                # could not examine must not read as one they passed).
                logger.error(
                    "establish_intake: DEGRADED — compiler(s) missing, refusing run %s: %s",
                    run_id,
                    degraded,
                )
                result = self._result(
                    run_id,
                    "?",
                    STATUS_ERROR,
                    reasons=[
                        f"establish_intake degraded: compiler missing at {p}" for p in degraded
                    ],
                )
            else:
                result = self._process(run_dir)
        except Exception as exc:  # noqa: BLE001 — a crashed run answers, never wedges
            logger.exception("establish_intake: run %s crashed", run_id)
            result = self._result(run_id, "?", STATUS_ERROR, reasons=[f"intake fault: {exc}"])
        self._write_result(result)
        self._purge(run_dir)
        if result.get("phase") == "install" and result.get("staging_id"):
            # Corpus discard: after an install run (pass OR fail) the staging
            # set — including the root-owned analysis artifacts — goes too.
            self._purge(self.staging_dir / str(result["staging_id"]))
        return result

    def _runner_kw(self) -> dict[str, Any]:
        """Forward the injected gate runner to ``gates.run_*``, when set."""
        return {"runner": self.gate_runner} if self.gate_runner is not None else {}

    # ------------------------------------------------------------------
    # Verification (design §4)
    # ------------------------------------------------------------------

    def _check_uid(self, path: Path, problems: list[str]) -> None:
        if self.broker_uid is None:
            if not self._uid_skip_logged:
                self._uid_skip_logged = True
                logger.warning(
                    "establish_intake: no '%s' user on this box; spool uid checks SKIPPED "
                    "(acceptable off-box only — a customer image always has the user)",
                    BROKER_USER,
                )
            return
        try:
            st_uid = path.stat().st_uid
        except OSError as exc:
            problems.append(f"{path.name}: unreadable ({exc})")
            return
        if st_uid != self.broker_uid:
            problems.append(
                f"{path.name}: uid {st_uid} is not the workspace-broker uid — "
                "the file did not pass through broker validation"
            )

    def _load_docs(self, run_dir: Path, problems: list[str]) -> list[dict[str, Any]]:
        """Load + verify every staged doc in the run. Hash is recomputed here
        over the text bytes — the manifest's word is never taken for it."""
        docs_dir = run_dir / "docs"
        docs: list[dict[str, Any]] = []
        paths = sorted(docs_dir.glob("*.json")) if docs_dir.is_dir() else []
        if not paths:
            problems.append("no staged documents in the run")
            return docs
        if len(paths) > MAX_DOCS:
            problems.append(f"{len(paths)} documents exceeds the {MAX_DOCS}-doc ceiling")
            return docs
        total = 0
        for path in paths:
            self._check_uid(path, problems)
            try:
                doc = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"{path.name}: unreadable ({exc})")
                continue
            text = doc.get("text")
            declared = doc.get("sha256")
            name = doc.get("name")
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{path.name}: empty or missing text")
                continue
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_DOC_BYTES:
                problems.append(f"{path.name}: {len(encoded)} bytes exceeds the per-doc ceiling")
                continue
            total += len(encoded)
            actual = sha256(encoded)
            if not isinstance(declared, str) or actual != declared.strip().lower():
                problems.append(f"{path.name}: text does not rehash to its declared sha256")
                continue
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{path.name}: missing document name")
                continue
            docs.append(
                {
                    "doc_id": str(doc.get("doc_id") or path.stem),
                    "name": name,
                    "sha256": actual,
                    "text": text,
                }
            )
        if total > MAX_TOTAL_BYTES:
            problems.append(f"corpus totals {total} bytes, over the {MAX_TOTAL_BYTES}-byte ceiling")
        return docs

    @staticmethod
    def _verify_manifest(manifest: Any, docs: list[dict[str, Any]], problems: list[str]) -> None:
        """The submitted corpus manifest must map 1:1 onto the staged docs with
        matching hashes — this is what binds the spec to exactly the corpus the
        agent read (and the gates checked)."""
        if not isinstance(manifest, list) or not manifest:
            problems.append("corpus_manifest missing or empty")
            return
        by_id = {d["doc_id"]: d["sha256"] for d in docs}
        seen: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict):
                problems.append("corpus_manifest entry is not an object")
                continue
            doc_id = str(entry.get("doc_id") or "")
            declared = str(entry.get("sha256") or "").strip().lower()
            if doc_id not in by_id:
                problems.append(f"corpus_manifest names doc {doc_id!r} not present in the run")
                continue
            if by_id[doc_id] != declared:
                problems.append(
                    f"corpus_manifest hash for doc {doc_id!r} does not match the staged text"
                )
                continue
            seen.add(doc_id)
        for doc_id in by_id:
            if doc_id not in seen:
                problems.append(f"staged doc {doc_id!r} is not in the corpus_manifest")

    @staticmethod
    def _materialize_corpus(run_dir: Path, docs: list[dict[str, Any]]) -> list[Path]:
        """Write each doc's text as a corpus file for the compilers, named by
        the firm's own document name (that name is what demotion reports and
        leak findings cite). Names are sanitized to a single path segment and
        de-duplicated — a collision must not silently merge two documents."""
        corpus_dir = run_dir / "corpus"
        corpus_dir.mkdir(exist_ok=True)
        paths: list[Path] = []
        used: set[str] = set()
        for doc in docs:
            base = re.sub(r"[^A-Za-z0-9._-]+", "-", doc["name"]).strip("-.") or doc["doc_id"]
            candidate = base
            n = 1
            while candidate in used:
                n += 1
                candidate = f"{base}-{n}"
            used.add(candidate)
            path = corpus_dir / candidate
            path.write_text(doc["text"])
            doc["corpus_name"] = candidate
            paths.append(path)
        return paths

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    @staticmethod
    def _submission_scope(run_dir: Path) -> str:
        """Best-effort read of the submission's scope, for the degraded gate.

        Any fault reads as ``"firm"`` — the fail-closed direction, because a
        firm-scoped run on a degraded daemon is refused while a person-scoped
        one proceeds. ``_process`` re-reads and fully validates the submission;
        this peek decides only which degraded posture applies.
        """
        try:
            sub = json.loads((run_dir / "submission.json").read_text())
            scope = sub.get("scope") if isinstance(sub, dict) else None
            return scope if scope == "person" else "firm"
        except (OSError, json.JSONDecodeError):
            return "firm"

    def _process(self, run_dir: Path) -> dict[str, Any]:
        run_id = run_dir.name
        submission_path = run_dir / "submission.json"
        problems: list[str] = []
        self._check_uid(submission_path, problems)
        try:
            sub = json.loads(submission_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return self._result(
                run_id, "?", STATUS_REJECTED, reasons=[f"submission unreadable: {exc}"]
            )
        if not isinstance(sub, dict):
            return self._result(
                run_id, "?", STATUS_REJECTED, reasons=["submission is not an object"]
            )
        phase = str(sub.get("phase") or "")
        if phase not in ("analyze", "install"):
            return self._result(
                run_id, phase, STATUS_REJECTED, reasons=[f"unknown phase {phase!r}"]
            )
        scope = str(sub.get("scope") or "firm")
        if scope == "person":
            # Person-scoped establishment: no staging set, no corpus, no
            # compiler gates. The uid problems list still applies — a
            # submission that did not come from the broker uid is refused.
            if phase != "install":
                return self._result(
                    run_id,
                    phase,
                    STATUS_REJECTED,
                    reasons=["person-scoped establishment supports only phase 'install'"],
                )
            if problems:
                return self._result(run_id, phase, STATUS_REJECTED, reasons=problems)
            return self._install_person(run_id, sub)
        if scope != "firm":
            return self._result(
                run_id, phase, STATUS_REJECTED, reasons=[f"unknown scope {scope!r}"]
            )
        staging_id = str(sub.get("staging_id") or "")
        if not _SAFE_SEGMENT.match(staging_id):
            return self._result(
                run_id, phase, STATUS_REJECTED, reasons=["unsafe or missing staging_id"]
            )

        docs = self._load_docs(run_dir, problems)
        if problems:
            return self._result(
                run_id, phase, STATUS_REJECTED, staging_id=staging_id, reasons=problems
            )
        corpus = self._materialize_corpus(run_dir, docs)
        if phase == "analyze":
            return self._analyze(run_dir, run_id, staging_id, corpus)
        return self._install(run_dir, run_id, staging_id, sub, docs, corpus)

    def _analyze(
        self, run_dir: Path, run_id: str, staging_id: str, corpus: list[Path]
    ) -> dict[str, Any]:
        """Profile the corpus and compute the fixed strings the agent MAY use
        verbatim. The raw candidates file stays root-side (the analysis dir the
        agent and broker cannot read); the RESULT carries the profile and the
        approved string texts — those are precisely what the agent is allowed
        to reproduce, so surfacing them is the point, not a leak."""
        profile = gates.run_profile(corpus, run_dir / "profile.json", **self._runner_kw())
        if profile.rejected:
            return self._result(
                run_id,
                "analyze",
                STATUS_REJECTED,
                staging_id=staging_id,
                reasons=list(profile.reasons),
                gates={"voice_profile": profile.disposition},
            )
        fixed = gates.run_fixed_strings(corpus, run_dir / "fixed_strings.json", **self._runner_kw())
        if fixed.rejected:
            return self._result(
                run_id,
                "analyze",
                STATUS_REJECTED,
                staging_id=staging_id,
                reasons=list(fixed.reasons),
                gates={
                    "voice_profile": profile.disposition,
                    "spec_fixed_strings": fixed.disposition,
                },
            )
        approved = [
            str(c.get("text"))
            for c in (fixed.data.get("candidates") or [])
            if isinstance(c, dict) and c.get("text")
        ]
        analysis = self.analysis_dir(staging_id)
        analysis.mkdir(parents=True, exist_ok=True)
        self._harden_down(self.staging_dir / staging_id, analysis, dir_mode=0o700, gid=0)
        atomic_write(analysis / "profile.json", json.dumps(profile.data, sort_keys=True).encode())
        atomic_write(
            analysis / "approved_strings.json",
            json.dumps({"schema_version": 1, "approved": approved}, sort_keys=True).encode(),
        )
        for name in ("profile.json", "approved_strings.json"):
            self._harden_path(analysis / name, 0o600, gid=0)
        return self._result(
            run_id,
            "analyze",
            STATUS_ANALYZED,
            staging_id=staging_id,
            gates={"voice_profile": "pass", "spec_fixed_strings": "pass"},
            extra={"profile": profile.data, "approved_strings": approved},
        )

    def _install(
        self,
        run_dir: Path,
        run_id: str,
        staging_id: str,
        sub: dict[str, Any],
        docs: list[dict[str, Any]],
        corpus: list[Path],
    ) -> dict[str, Any]:
        problems: list[str] = []
        output_class = str(sub.get("output_class") or "")
        prop = str(sub.get("property") or "")
        spec_body = sub.get("spec_body")
        declared_hash = sub.get("spec_sha256")
        assertions = sub.get("assertions") if isinstance(sub.get("assertions"), dict) else {}
        if not _SAFE_SEGMENT.match(output_class):
            problems.append(f"output_class {output_class!r} outside the permitted charset")
        if prop not in SPEC_PROPERTIES:
            problems.append(f"property must be one of {SPEC_PROPERTIES}; got {prop!r}")
        if not isinstance(spec_body, str) or not spec_body.strip():
            problems.append("spec_body missing or empty")
            spec_body = ""
        encoded = spec_body.encode("utf-8")
        if len(encoded) > MAX_SPEC_BYTES:
            problems.append(
                f"spec_body {len(encoded)} bytes exceeds the {MAX_SPEC_BYTES}-byte ceiling"
            )
        digest = sha256(encoded)
        if isinstance(declared_hash, str) and declared_hash.strip().lower() != digest:
            problems.append("spec_body does not rehash to the broker-computed spec_sha256")
        self._verify_manifest(sub.get("corpus_manifest"), docs, problems)
        if problems:
            return self._result(
                run_id, "install", STATUS_REJECTED, staging_id=staging_id, reasons=problems
            )

        spec_path = run_dir / "spec.md"
        spec_path.write_text(spec_body)
        # Synthesized provenance: doc names feed the leak check's proper-noun
        # scan. Names only — never text.
        provenance_path = run_dir / "provenance.json"
        provenance_path.write_text(
            json.dumps({"documents": [{"file_name": d["name"]} for d in docs]}) + "\n"
        )
        gate_states: dict[str, str] = {}

        if prop == "voice":
            digit = gates.run_digit_invariant(corpus, spec_path, **self._runner_kw())
            gate_states["digit_invariant"] = digit.disposition
            if digit.rejected:
                return self._result(
                    run_id,
                    "install",
                    STATUS_REJECTED,
                    staging_id=staging_id,
                    reasons=list(digit.reasons),
                    gates=gate_states,
                )

        approved_path = self.analysis_dir(staging_id) / "approved_strings.json"
        leak = gates.run_leak_check(
            spec_path=spec_path,
            corpus=corpus,
            attestation_path=run_dir / "attestation.json",
            approved_strings_path=approved_path if approved_path.is_file() else None,
            provenance_path=provenance_path,
            **self._runner_kw(),
        )
        gate_states["leak_check"] = leak.disposition
        if leak.rejected:
            # The hard gate. Prior spec stands (nothing was written yet).
            return self._result(
                run_id,
                "install",
                STATUS_REJECTED,
                staging_id=staging_id,
                reasons=list(leak.reasons),
                gates=gate_states,
            )

        rules = assertions.get("rules") if isinstance(assertions.get("rules"), list) else []
        labels_path = run_dir / "labels.json"
        labels_path.write_text(json.dumps({"exemplary": [d["corpus_name"] for d in docs]}) + "\n")
        selftest = gates.run_selftest(
            rules=[r for r in rules if isinstance(r, dict)],
            corpus=corpus,
            labels_path=labels_path,
            out_path=run_dir / "selftest.json",
            rules_path=run_dir / "rules.json",
            **self._runner_kw(),
        )
        gate_states["selftest"] = selftest.disposition
        if selftest.rejected:
            return self._result(
                run_id,
                "install",
                STATUS_REJECTED,
                staging_id=staging_id,
                reasons=list(selftest.reasons),
                gates=gate_states,
            )
        demotions = [dict(d) for d in selftest.demotions]

        warnings: list[str] = []
        if not self._class_declared(output_class):
            warnings.append(
                f"output class {output_class!r} is not declared in customer.yaml "
                "output_classes — the spec installs (spec-before-declare, #2094), but "
                "declare the class to bind the gate"
            )

        return self._put_and_converge(
            run_id=run_id,
            staging_id=staging_id,
            output_class=output_class,
            prop=prop,
            spec_body=spec_body,
            digest=digest,
            assertions=assertions,
            provenance=_corpus_provenance(run_id, docs),
            demotions=demotions,
            gate_states=gate_states,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # R2 install (merge, never clobber) + converge-wait
    # ------------------------------------------------------------------

    def _put_and_converge(
        self,
        *,
        run_id: str,
        staging_id: str,
        output_class: str,
        prop: str,
        spec_body: str,
        digest: str,
        assertions: dict[str, Any],
        provenance: dict[str, Any],
        demotions: list[dict[str, Any]],
        gate_states: dict[str, str],
        warnings: list[str],
    ) -> dict[str, Any]:
        key = spec_object_key(self.slug)
        try:
            current = self._get_object(key)
        except Exception as exc:  # noqa: BLE001 — an unreadable vault refuses the write
            return self._result(
                run_id,
                "install",
                STATUS_ERROR,
                staging_id=staging_id,
                reasons=[f"could not read the current vault object: {exc}"],
                gates=gate_states,
            )
        previous_key: str | None = None
        merged_classes: dict[str, Any] = {}
        if current is not None:
            try:
                existing = json.loads(current.decode("utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("classes"), dict):
                    merged_classes = existing["classes"]
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A corrupt current object is still copied to the previous key
                # (it is the recoverable state, whatever it is) but contributes
                # no classes to the merge.
                logger.warning("establish_intake: current vault object unparseable; merging none")
            previous_key = previous_object_key(self.slug)
            try:
                self.s3_client.put_object(
                    Bucket=self.bucket,
                    Key=previous_key,
                    Body=current,
                    ContentType="application/json",
                )
            except Exception as exc:  # noqa: BLE001
                # No recovery copy ⇒ no install. Fail-static: prior spec stands.
                return self._result(
                    run_id,
                    "install",
                    STATUS_ERROR,
                    staging_id=staging_id,
                    reasons=[f"could not write the previous-spec recovery copy: {exc}"],
                    gates=gate_states,
                )

        entry: dict[str, Any] = {"body": spec_body, "sha256": digest}
        if assertions:
            entry["assertions"] = assertions
        if provenance:
            entry["provenance"] = provenance
        merged_classes.setdefault(output_class, {})
        if not isinstance(merged_classes[output_class], dict):
            merged_classes[output_class] = {}
        merged_classes[output_class][prop] = entry
        doc = {"schema_version": 1, "customer": self.slug, "classes": merged_classes}
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        try:
            self.s3_client.put_object(
                Bucket=self.bucket, Key=key, Body=raw, ContentType="application/json"
            )
            back = self._get_object(key)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                run_id,
                "install",
                STATUS_ERROR,
                staging_id=staging_id,
                reasons=[f"vault write failed: {exc}"],
                gates=gate_states,
                extra={"previous_key": previous_key},
            )
        if back != raw:
            return self._result(
                run_id,
                "install",
                STATUS_ERROR,
                staging_id=staging_id,
                reasons=["read-back after the vault write does not match what was written"],
                gates=gate_states,
                extra={"previous_key": previous_key},
            )

        source_digest = sha256(raw)
        converged = self._wait_for_converge(source_digest)
        status = STATUS_INSTALLED if converged else STATUS_ACCEPTED_PENDING
        if not converged:
            warnings.append(
                "the spec applier has not yet installed this object; it applies on "
                "the applier's next successful poll (fail-static — nothing is lost)"
            )
        return self._result(
            run_id,
            "install",
            status,
            staging_id=staging_id,
            gates=gate_states,
            warnings=warnings,
            extra={
                "output_class": output_class,
                "property": prop,
                "demotions": demotions,
                "previous_key": previous_key,
                "source_digest": source_digest,
            },
        )

    def _get_object(self, key: str) -> bytes | None:
        """GET one vault object; ``None`` when absent, raises on real faults."""
        try:
            response = self.s3_client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — classify absent vs fault
            code = ""
            resp = getattr(exc, "response", None)
            if isinstance(resp, dict):
                code = str((resp.get("Error") or {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"} or type(exc).__name__ == "NoSuchKey":
                return None
            raise
        body = (
            response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
        )
        data = body.read() if body is not None else b""
        return data if isinstance(data, bytes) else bytes(data)

    def _wait_for_converge(self, source_digest: str) -> bool:
        """Poll the installed manifest until its ``source_digest`` matches the
        object just written, up to the converge timeout. Purely observational —
        the applier owns the install; this only decides which honest status the
        result carries (``installed`` vs ``accepted_pending_install``)."""
        deadline = self.now_fn() + self.converge_timeout
        while True:
            try:
                manifest = json.loads((self.spec_dir / "manifest.json").read_text())
                if isinstance(manifest, dict) and manifest.get("source_digest") == source_digest:
                    return True
            except (OSError, json.JSONDecodeError):
                pass
            if self.now_fn() >= deadline:
                return False
            self.sleep_fn(self.converge_interval)

    # ------------------------------------------------------------------
    # Person-scoped install (ADR 0085 §6, ss#2067) — no gates, by design
    # ------------------------------------------------------------------

    def _install_person(self, run_id: str, sub: dict[str, Any]) -> dict[str, Any]:
        """Install one person's preference artifact into the vault.

        WHY NO COMPILER GATES RUN HERE. The gates exist to bound what a firm
        CORPUS can smuggle into a firm-wide spec: the leak check stops client
        prose retention, the digit invariant stops asserted numbers, the
        selftest checks rules against exemplars. A person-scoped establishment
        has no corpus — the artifact is the subject's OWN authored instruction
        about their OWN work (the ss#2067 "project instructions" shape), its
        verbatim retention is the feature, and a number in it ("keep my emails
        under 150 words") is a preference, not an asserted fact. What holds
        instead: the seat-side sender==subject predicate (authorization), the
        broker's provenance path (mediation), the roster check below
        (defense in depth), and the byte ceiling.

        AUTHORITY RECAP: the hook-side predicate already refused any submit
        whose subject is not the attributed sender. This function re-validates
        shape and ROSTER only — it structurally cannot see the sender, same as
        the firm path cannot see the admin (module header).
        """
        phase = "install"
        problems: list[str] = []
        person = normalize_person_address(sub.get("person"))
        if person is None:
            problems.append("person is not a valid person address")
        body = sub.get("spec_body")
        if not isinstance(body, str) or not body.strip():
            problems.append("spec_body missing or empty")
            body = ""
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_PREF_BODY_BYTES:
            problems.append(
                f"spec_body {len(encoded)} bytes exceeds the {MAX_PREF_BODY_BYTES}-byte ceiling"
            )
        digest = sha256(encoded)
        declared = sub.get("spec_sha256")
        if isinstance(declared, str) and declared.strip().lower() != digest:
            problems.append("spec_body does not rehash to the broker-computed spec_sha256")
        assertions = sub.get("assertions")
        if assertions is not None and not isinstance(assertions, (dict, list)):
            problems.append("assertions must be an object or list when present")
        if person is not None:
            self._person_on_roster(person, problems)
        if problems:
            return self._result(run_id, phase, STATUS_REJECTED, reasons=problems)

        pslug = person_slug(person)
        key = person_pref_key(self.slug, pslug)
        try:
            current = self._get_object(key)
        except Exception as exc:  # noqa: BLE001 — an unreadable vault refuses the write
            return self._result(
                run_id,
                phase,
                STATUS_ERROR,
                reasons=[f"could not read the current preference object: {exc}"],
            )
        previous_key: str | None = None
        if current is not None:
            previous_key = previous_person_pref_key(self.slug, pslug)
            try:
                self.s3_client.put_object(
                    Bucket=self.bucket,
                    Key=previous_key,
                    Body=current,
                    ContentType="application/json",
                )
            except Exception as exc:  # noqa: BLE001
                # No recovery copy ⇒ no install (firm-path parity).
                return self._result(
                    run_id,
                    phase,
                    STATUS_ERROR,
                    reasons=[f"could not write the previous-preference recovery copy: {exc}"],
                )

        doc: dict[str, Any] = {
            "schema_version": 1,
            "customer": self.slug,
            "person": person,
            "person_slug": pslug,
            "body": body,
            "sha256": digest,
            "updated_at": iso_utc(),
        }
        if assertions:
            doc["assertions"] = assertions
        instructed_by = sub.get("instructed_by")
        if isinstance(instructed_by, str) and instructed_by:
            doc["instructed_by"] = instructed_by
        source_ref = sub.get("source_ref")
        if isinstance(source_ref, str) and source_ref:
            doc["source_ref"] = source_ref
        raw = json.dumps(doc, sort_keys=True).encode("utf-8")
        try:
            self.s3_client.put_object(
                Bucket=self.bucket, Key=key, Body=raw, ContentType="application/json"
            )
            back = self._get_object(key)
        except Exception as exc:  # noqa: BLE001
            return self._result(
                run_id,
                phase,
                STATUS_ERROR,
                reasons=[f"preference vault write failed: {exc}"],
                extra={"previous_key": previous_key},
            )
        if back != raw:
            return self._result(
                run_id,
                phase,
                STATUS_ERROR,
                reasons=["read-back after the preference write does not match what was written"],
                extra={"previous_key": previous_key},
            )

        object_digest = sha256(raw) or ""
        converged = self._wait_for_prefs_converge(pslug, object_digest)
        status = STATUS_INSTALLED if converged else STATUS_ACCEPTED_PENDING
        warnings: list[str] = []
        if not converged:
            warnings.append(
                "the spec applier has not yet installed this preference object; it "
                "applies on the applier's next successful poll (fail-static — "
                "nothing is lost)"
            )
        return self._result(
            run_id,
            phase,
            status,
            warnings=warnings,
            extra={
                "scope": "person",
                "person": person,
                "person_slug": pslug,
                "previous_key": previous_key,
                "source_digest": object_digest,
            },
        )

    def _person_on_roster(self, person: str, problems: list[str]) -> bool:
        """Roster check, FAIL-CLOSED on every fault.

        Unlike ``_class_declared`` (a soft warning about sequencing hygiene),
        this is a trust-boundary backstop: a subject who is not on the
        organization roster has no standing to hold a preference artifact on
        this seat, and a config that cannot be read must refuse rather than
        wave through — "cannot evaluate" must not read as "permitted".
        """
        if self.customer_config_fn is None:
            problems.append(
                "no customer-config reader wired; refusing a person-scoped install "
                "(roster cannot be checked)"
            )
            return False
        try:
            if self.customer_config_fn().sender_on_roster(person):
                return True
            problems.append(
                f"{person} is not on the organization roster (scope.inbound_allow_from)"
            )
            return False
        except Exception as exc:  # noqa: BLE001 — an unreadable roster refuses
            problems.append(f"roster unreadable ({exc}); refusing a person-scoped install")
            return False

    def _wait_for_prefs_converge(self, pslug: str, object_digest: str) -> bool:
        """Poll the preferences manifest until it records this object's digest.

        The manifest hash is root-computed over the installed file bytes, and
        the applier installs the object VERBATIM — so equality with the digest
        of the bytes just written is exactly "the applier adopted this write".
        Purely observational, same as the firm converge-wait.
        """
        deadline = self.now_fn() + self.converge_timeout
        while True:
            try:
                manifest = json.loads((self.spec_dir / PREFS_MANIFEST_NAME).read_text())
                prefs = manifest.get("preferences") if isinstance(manifest, dict) else None
                entry = prefs.get(pslug) if isinstance(prefs, dict) else None
                if isinstance(entry, dict) and entry.get("sha256") == object_digest:
                    return True
            except (OSError, json.JSONDecodeError):
                pass
            if self.now_fn() >= deadline:
                return False
            self.sleep_fn(self.converge_interval)

    def _class_declared(self, output_class: str) -> bool:
        """Whether ``output_classes.<class>`` is declared in customer.yaml.

        Soft check feeding a WARNING only: undeclared installs are allowed by
        sequencing rule #2094 (spec-before-declare). Any read fault suppresses
        the warning rather than blocking the install."""
        if self.customer_config_fn is None:
            return True
        try:
            declared = self.customer_config_fn().output_classes
            return output_class in declared
        except Exception:  # noqa: BLE001 — a warning is never worth a crash
            return True

    # ------------------------------------------------------------------
    # Results, purge, sweeps, hardening
    # ------------------------------------------------------------------

    def _result(
        self,
        run_id: str,
        phase: str,
        status: str,
        *,
        staging_id: str | None = None,
        reasons: list[str] | None = None,
        gates: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "phase": phase,
            "status": status,
            "reasons": reasons or [],
            "warnings": warnings or [],
            "gates": gates or {},
            "completed_at": iso_utc(),
        }
        if staging_id:
            result["staging_id"] = staging_id
        if extra:
            result.update(extra)
        return result

    def _write_result(self, result: dict[str, Any]) -> None:
        """Write the run's result 0640 root:workspace-broker — readable by the
        broker (which serves it to the agent once, then deletes it), by nobody
        else. The write is atomic so the broker can never read a torn result."""
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._harden_down(self.spool_dir, self.results_dir, dir_mode=0o750, gid=self.broker_gid)
        # The results DIRECTORY itself is 0770, not 0750: unlink requires write
        # on the containing directory, and the one-shot contract ("broker
        # deletes after first read") is dead letter at 0750 — the broker could
        # read but never delete, so every result would survive to the TTL sweep.
        # Found in cross-half reconciliation (ss-console PR #2181). Result FILES
        # stay 0640 so the broker cannot rewrite root-authored verdicts.
        self._harden_path(self.results_dir, 0o770, gid=self.broker_gid)
        path = self.results_dir / f"{result['run_id']}.json"
        atomic_write(path, json.dumps(result, sort_keys=True).encode("utf-8") + b"\n")
        self._harden_path(path, 0o640, gid=self.broker_gid)

    def _purge(self, path: Path) -> None:
        """Remove a run or staging tree entirely. Failure is loud but not fatal
        to the loop; a leftover is retried by the next tick's sweep."""
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
        except OSError as exc:
            logger.error("establish_intake: could not purge %s: %s", path, exc)

    def _sweep_results(self) -> None:
        """TTL sweep for results the broker never collected."""
        if not self.results_dir.is_dir():
            return
        cutoff = self.now_fn() - RESULT_TTL_SECONDS
        for path in self.results_dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def _sweep_staging(self) -> None:
        """Backstop sweep for expired staging sets. The broker sweeps its own
        30-minute TTL but cannot remove the root-owned ``analysis/`` subdir, so
        root finishes the job here — otherwise every analyzed-but-never-
        installed set would leak its approved-strings file forever."""
        if not self.staging_dir.is_dir():
            return
        cutoff = self.now_fn() - STAGING_TTL_SECONDS
        for path in self.staging_dir.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    self._purge(path)
            except OSError:
                pass

    def _harden_path(self, path: Path, mode: int, gid: int | None) -> None:
        """chown root:<gid> + chmod, logging rather than raising off-box —
        the same posture as ``spec_applier._harden``, and for the same reason:
        tests and dev boxes are not root, and the boot-side invariants, not
        this function, refuse to serve a tree with the wrong owner."""
        import os

        try:
            os.chown(path, 0, gid if gid is not None else 0)
        except OSError as exc:
            logger.debug("establish_intake: could not chown %s (%s)", path, exc)
        try:
            os.chmod(path, mode)
        except OSError as exc:
            logger.warning("establish_intake: could not chmod %s to %o: %s", path, mode, exc)

    def _harden_down(self, base: Path, leaf: Path, *, dir_mode: int, gid: int | None) -> None:
        """Harden every directory from ``base`` down to ``leaf`` inclusive —
        the ``spec_applier._harden_ancestors`` walk, applied to spool trees.
        ``mkdir(parents=True)`` leaves intermediates at umask-derived modes,
        and one wrong intermediate silently severs the broker's read path to
        the results (the exact live-seat failure shape of 2026-07-31,
        vfy_01KYWVR8PBBEP85W3F5SSNC9FD). The spool ROOT is deliberately not
        touched: its 0750 root:workspace-broker is authored by the entrypoint."""
        try:
            rel = leaf.relative_to(base)
        except ValueError:
            logger.warning("establish_intake: refusing to harden %s; not under %s", leaf, base)
            return
        current = base
        for part in rel.parts:
            current = current / part
            self._harden_path(current, dir_mode, gid)


__all__ = [
    "BROKER_USER",
    "CONVERGE_TIMEOUT_SECONDS",
    "MAX_DOCS",
    "MAX_DOC_BYTES",
    "MAX_SPEC_BYTES",
    "MAX_TOTAL_BYTES",
    "RESULT_TTL_SECONDS",
    "SPEC_PROPERTIES",
    "STAGING_TTL_SECONDS",
    "STATUS_ACCEPTED_PENDING",
    "STATUS_ANALYZED",
    "STATUS_ERROR",
    "STATUS_INSTALLED",
    "STATUS_REJECTED",
    "EstablishIntake",
    "previous_object_key",
    "spec_object_key",
]
