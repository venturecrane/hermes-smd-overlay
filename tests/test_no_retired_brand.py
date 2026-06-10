"""Regression guard — a retired Operator brand must never reappear.

The external-send identity framing once treated as a product-defining hallmark
was retired venture-wide (ss-console ADR 0035; this overlay mirrors it). External
send is one configurable entitlement among many, named descriptively, with no
special status. Past removals did not stick because nothing failed CI when the
token regrew. This test scans the overlay tree and fails if it reappears. The
banned token is assembled from fragments so THIS guard file stays free of it.
"""

import os
import re

_FRAGMENTS = ("reviewer", "as", "sender")
_BANNED = re.compile("[-_ ]".join(_FRAGMENTS), re.IGNORECASE)
_EXTS = (".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".json", ".sh")
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_retired_brand_absent() -> None:
    offenders: list[str] = []
    root = _repo_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP]
        for fn in filenames:
            if not fn.endswith(_EXTS):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if _BANNED.search(line):
                            offenders.append(
                                f"{os.path.relpath(path, root)}:{i}: {line.strip()[:120]}"
                            )
            except (UnicodeDecodeError, OSError):
                continue
    assert not offenders, "Retired brand reappeared:\n" + "\n".join(offenders)
