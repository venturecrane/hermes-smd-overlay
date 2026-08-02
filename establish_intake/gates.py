"""Compiler write gates — subprocess wrappers + per-gate dispositions.

THE COMPILERS ARE THE CONTROL. ADR 0085 §4 moves the witness-never-author line
for admin-instructed establishment, and the security argument that replaces the
blanket prohibition is provenance + mediation + THESE gates. They are the
mechanism that makes an agent-derived spec trustworthy enough for §3's
immediacy: the leak check refuses client prose beyond the approved fixed
strings, the digit invariant refuses asserted numbers, and the self-test demotes
any block rule the firm's own writing violates — naming the documents, so the
Operator's reply can be honest about what was demoted and why.

WHERE THE COMPILERS LIVE. They are NOT vendored here: the console PR COPYs them
into the customer image at repo-mirrored paths (``/opt/smd/operator/bin/*.py``,
with ``drafting_gate_check.py`` at its shipped ``operator/templates/drafting/``
path — repo-mirrored because ``spec_leak_check.py`` resolves the gate-check
module RELATIVE to its own location, ``parents[2]``, and a flattened copy would
break that import). The paths are constants below; :func:`missing_compilers`
is the daemon's degrade-loudly probe — an absent compiler refuses runs rather
than skipping gates, because a gate that silently did not run reads exactly
like a gate that passed (Law 12).

EXIT-CODE CONTRACT (probed against the shipped compilers, 2026-08-02):

* ``spec_leak_check``: 0 clean; 1 refused (empty corpus); 2 findings.
  Disposition: 1 and 2 both REJECT, hard — a leak is never installable.
* ``voice_profile``: 0 ok; 1 empty corpus; 2 digit(s) on the ``--card`` outside
  a profile token. Disposition: nonzero REJECTs.
* ``spec_fixed_strings``: 0 ok; 1 empty corpus. Nonzero REJECTs.
* ``spec_selftest``: 0 ok (INCLUDING demotions — the shipped compiler reports
  demotions in its ``--out`` JSON and exits 0); 1 refused. The intake design's
  §5 table assigned demotions exit code 2, which the shipped compiler does not
  emit; this wrapper therefore keys demotion detection off the REPORT, not the
  exit code, and tolerates a future exit-2-on-demotions compiler by treating 2
  as proceed-with-demotions when the report is readable. Malformed rules or an
  unreadable report REJECT — a self-test whose verdict cannot be read did not
  certify anything.

Every wrapper takes an injectable ``runner`` (``subprocess.run``-shaped) so the
dispositions are unit-testable without the compilers present.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404 — root daemon runs pinned, image-shipped compilers
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Repo-mirrored compiler install prefix on the customer image (console PR C1
#: COPYs these; see the module docstring for why the layout mirrors the repo).
COMPILER_BIN_DIR = Path("/opt/smd/operator/bin")
VOICE_PROFILE = COMPILER_BIN_DIR / "voice_profile.py"
SPEC_FIXED_STRINGS = COMPILER_BIN_DIR / "spec_fixed_strings.py"
SPEC_LEAK_CHECK = COMPILER_BIN_DIR / "spec_leak_check.py"
SPEC_SELFTEST = COMPILER_BIN_DIR / "spec_selftest.py"
#: Imported by spec_leak_check relative to its own path — its absence breaks
#: the leak check at import time, so it is part of the presence probe.
DRAFTING_GATE_CHECK = Path("/opt/smd/operator/templates/drafting/drafting_gate_check.py")

REQUIRED_COMPILERS: tuple[Path, ...] = (
    VOICE_PROFILE,
    SPEC_FIXED_STRINGS,
    SPEC_LEAK_CHECK,
    SPEC_SELFTEST,
    DRAFTING_GATE_CHECK,
)

#: Per-gate subprocess ceiling. The compilers are stdlib text passes over at
#: most 16 MiB of corpus; minutes of runtime means something is wrong, and a
#: hung gate must not wedge the (serial) intake daemon forever.
GATE_TIMEOUT_SECONDS = 180

#: Closed disposition vocabulary. NOT_RUN is deliberately distinct from PASS:
#: a self-test with zero rules is RECORDED as not run, never as passed (Law 12
#: — a check that cannot fail has measured nothing).
PASS = "pass"
REJECT = "reject"
NOT_RUN = "not_run"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class GateOutcome:
    """One gate's verdict. ``reasons`` is non-empty exactly when rejected."""

    gate: str
    disposition: str
    reasons: tuple[str, ...] = ()
    #: Self-test only: ``[{rule_id, documents, detail}]`` for every demoted rule.
    demotions: tuple[dict[str, Any], ...] = ()
    #: Gate-specific payload (profile JSON, fixed-string candidates, ...).
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return self.disposition == REJECT


def missing_compilers(required: Sequence[Path] = REQUIRED_COMPILERS) -> list[str]:
    """The compiler paths absent from this box, as strings (empty = healthy)."""
    return [str(p) for p in required if not p.is_file()]


def _run(cmd: list[str], runner: Runner) -> subprocess.CompletedProcess[str] | None:
    """Run one compiler; ``None`` means the invocation itself failed (spawn
    fault or timeout), which every caller treats as REJECT — a gate that could
    not run must not read as a gate that passed."""
    try:
        return runner(
            cmd,
            capture_output=True,
            text=True,
            timeout=GATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "establish_intake: gate timed out after %ss: %s", GATE_TIMEOUT_SECONDS, cmd[:2]
        )
        return None
    except OSError as exc:
        logger.error("establish_intake: gate could not run (%s): %s", exc, cmd[:2])
        return None


def _stderr_reasons(proc: subprocess.CompletedProcess[str], limit: int = 20) -> tuple[str, ...]:
    """The compiler's own refusal lines (offsets only by the compilers' design —
    they never print matched corpus text, so these are safe to carry into the
    one-shot transient result)."""
    lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
    return tuple(lines[:limit])


def run_profile(
    corpus: Sequence[Path], out_path: Path, *, runner: Runner = subprocess.run
) -> GateOutcome:
    """``voice_profile --corpus ... --out`` — the analyze-phase profiler."""
    cmd = [
        sys.executable,
        str(VOICE_PROFILE),
        "--corpus",
        *map(str, corpus),
        "--out",
        str(out_path),
    ]
    proc = _run(cmd, runner)
    if proc is None:
        return GateOutcome("voice_profile", REJECT, ("voice_profile could not run",))
    if proc.returncode != 0:
        return GateOutcome(
            "voice_profile", REJECT, _stderr_reasons(proc) or ("voice_profile refused",)
        )
    try:
        data = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return GateOutcome("voice_profile", REJECT, (f"profile output unreadable: {exc}",))
    return GateOutcome("voice_profile", PASS, data=data if isinstance(data, dict) else {})


def run_fixed_strings(
    corpus: Sequence[Path], out_path: Path, *, runner: Runner = subprocess.run
) -> GateOutcome:
    """``spec_fixed_strings --corpus ... --out`` — candidate verbatim strings."""
    cmd = [
        sys.executable,
        str(SPEC_FIXED_STRINGS),
        "--corpus",
        *map(str, corpus),
        "--out",
        str(out_path),
    ]
    proc = _run(cmd, runner)
    if proc is None:
        return GateOutcome("spec_fixed_strings", REJECT, ("spec_fixed_strings could not run",))
    if proc.returncode != 0:
        return GateOutcome(
            "spec_fixed_strings", REJECT, _stderr_reasons(proc) or ("spec_fixed_strings refused",)
        )
    try:
        data = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return GateOutcome(
            "spec_fixed_strings", REJECT, (f"fixed-strings output unreadable: {exc}",)
        )
    return GateOutcome("spec_fixed_strings", PASS, data=data if isinstance(data, dict) else {})


def run_digit_invariant(
    corpus: Sequence[Path], card_path: Path, *, runner: Runner = subprocess.run
) -> GateOutcome:
    """``voice_profile --corpus ... --card <spec>`` — no asserted numbers.

    ADR 0085 §4 names the digit invariant as one of the three write gates. It is
    run on VOICE submissions only (the intake decides that): the invariant was
    built for voice cards, where a digit outside a ``{{profile.*}}`` token is an
    asserted measurement nobody computed; a format spec's thresholds live in its
    machine-checked ``assertions``, not in body prose.
    """
    cmd = [
        sys.executable,
        str(VOICE_PROFILE),
        "--corpus",
        *map(str, corpus),
        "--card",
        str(card_path),
    ]
    proc = _run(cmd, runner)
    if proc is None:
        return GateOutcome("digit_invariant", REJECT, ("digit-invariant check could not run",))
    if proc.returncode != 0:
        return GateOutcome(
            "digit_invariant",
            REJECT,
            _stderr_reasons(proc) or ("digit invariant refused the spec",),
        )
    return GateOutcome("digit_invariant", PASS)


def run_leak_check(
    *,
    spec_path: Path,
    corpus: Sequence[Path],
    attestation_path: Path,
    approved_strings_path: Path | None = None,
    provenance_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> GateOutcome:
    """``spec_leak_check`` — the hard gate. Exit 1/2 both REJECT (ADR 0085 §4:
    no client prose is retained beyond the approved fixed strings, ever)."""
    cmd = [
        sys.executable,
        str(SPEC_LEAK_CHECK),
        "--spec",
        str(spec_path),
        "--corpus",
        *map(str, corpus),
        "--attestation",
        str(attestation_path),
    ]
    if approved_strings_path is not None and approved_strings_path.is_file():
        cmd += ["--approved-strings", str(approved_strings_path)]
    if provenance_path is not None and provenance_path.is_file():
        cmd += ["--provenance", str(provenance_path)]
    proc = _run(cmd, runner)
    if proc is None:
        return GateOutcome("leak_check", REJECT, ("leak check could not run",))
    if proc.returncode != 0:
        return GateOutcome(
            "leak_check", REJECT, _stderr_reasons(proc) or ("leak check refused the spec",)
        )
    return GateOutcome("leak_check", PASS)


def run_selftest(
    *,
    rules: list[dict[str, Any]],
    corpus: Sequence[Path],
    labels_path: Path,
    out_path: Path,
    rules_path: Path,
    runner: Runner = subprocess.run,
) -> GateOutcome:
    """``spec_selftest`` — demote, don't refuse, what the firm's own writing breaks.

    Zero rules ⇒ NOT_RUN, recorded as such and NEVER as a pass (Law 12): a spec
    with no checkable assertions had nothing measured, and the result must say
    so rather than wear a green check. Demotions ride the outcome so the
    Operator's reply can name each demoted rule and the documents that broke it
    (the honesty ADR 0085 moves from a presented report into the reply itself).
    """
    if not rules:
        return GateOutcome("selftest", NOT_RUN, data={"rules_checked": 0})
    rules_path.write_text(json.dumps({"rules": rules}, indent=2) + "\n")
    cmd = [
        sys.executable,
        str(SPEC_SELFTEST),
        "--rules",
        str(rules_path),
        "--corpus",
        *map(str, corpus),
        "--labels",
        str(labels_path),
        "--out",
        str(out_path),
    ]
    proc = _run(cmd, runner)
    if proc is None:
        return GateOutcome("selftest", REJECT, ("selftest could not run",))
    if proc.returncode not in (0, 2):
        return GateOutcome(
            "selftest", REJECT, _stderr_reasons(proc) or ("selftest refused the rules",)
        )
    try:
        report = json.loads(out_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        # An unreadable verdict certifies nothing — reject rather than guess,
        # even on exit 0.
        return GateOutcome("selftest", REJECT, (f"selftest report unreadable: {exc}",))
    demotions = tuple(
        {
            "rule_id": r.get("rule_id"),
            "documents": list(r.get("failed_exemplary_docs") or []),
            "detail": r.get("detail") or "",
        }
        for r in (report.get("results") or [])
        if isinstance(r, dict) and r.get("demoted")
    )
    return GateOutcome(
        "selftest",
        PASS,
        demotions=demotions,
        data={
            "rules_checked": report.get("rules_checked"),
            "rules_demoted": report.get("rules_demoted"),
        },
    )


__all__ = [
    "COMPILER_BIN_DIR",
    "DRAFTING_GATE_CHECK",
    "GATE_TIMEOUT_SECONDS",
    "NOT_RUN",
    "PASS",
    "REJECT",
    "REQUIRED_COMPILERS",
    "GateOutcome",
    "missing_compilers",
    "run_digit_invariant",
    "run_fixed_strings",
    "run_leak_check",
    "run_profile",
    "run_selftest",
]
