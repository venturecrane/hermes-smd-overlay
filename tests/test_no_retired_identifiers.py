"""Guard: retired identifiers must never reappear in this repo.

The ss-console retirement (#1869, 2026-07-13) scanned ss-console only; this
repo kept the retired persona name alive in a docstring example and five test
fixtures, and the fixtures kept teaching the name to new code until a
volume-wide runtime scan found them (ss-console#2009 close-out, 2026-07-26).
Every repo that can hold an identifier needs its own guard — CI in one repo
proves nothing about another.

Scope: every tracked text file in the repo. No historical-record exemption —
unlike ss-console, this repo has no dated correspondence or grading logs;
nothing here legitimately needs a retired identifier.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Retired identifiers, one entry per retirement. Assembled from fragments so
# this guard never trips over its own definition.
RETIRED_IDENTIFIERS = [
    "qu" + "inn",  # persona name retired 2026-07-02..13; runtime purge 2026-07-26
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


def test_no_retired_identifiers_anywhere() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except (OSError, IsADirectoryError):
            continue
        for ident in RETIRED_IDENTIFIERS:
            if ident.lower() in text:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: contains retired identifier {ident!r}"
                )
    assert not offenders, (
        "Retired identifiers found — these names are permanently retired and must "
        "not reappear in any form (code, fixtures, docs, comments):\n  " + "\n  ".join(offenders)
    )
