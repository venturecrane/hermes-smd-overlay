"""No real counterparty's email domain may appear in shipped source (ss#2258).

WHY. A real client contact's address was used as sample data in a unit test
(`test_person_prefs.py`). This repository is PUBLIC, and every file in it ships
to every customer seat -- tests included. So one convenient test value put a
named individual at a client firm into a public repo and onto every Machine.

It surfaced during the ss#2258 investigation, where that address was the ONLY
occurrence anywhere the seat could read: not the seat's config, memories, SOUL,
skills or prompt snapshot, and not one of the ~90 Smokeball records across
staff, contacts, matters, roles, tasks, events or memos. Whether the agent read
it could not be settled -- the run transcripts were destroyed by a reprovision --
and that ambiguity is itself the argument for this gate. A test fixture should
never be a candidate explanation for who a live system emailed.

WHY HASHES, NOT A WORD LIST. The thing to forbid is a real counterparty's
domain, so a readable deny-list would republish exactly what the gate exists to
remove. The domains are stored as SHA-256 of the lowercased domain: the gate can
recognise one without anyone being able to read it here.

Deliberately NOT flagged: obvious placeholders on generic domains
(`partner@firm.com`, `a@b.com`, `attacker@evil.com`). They carry no identity, and
a gate that fires on them gets silenced -- which is how a gate stops working.

TO ADD A DOMAIN: `python3 -c "import hashlib,sys;
print(hashlib.sha256(sys.argv[1].lower().encode()).hexdigest())" thedomain.com`
and paste the digest below with a non-identifying comment.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_SCAN_DIRS = ("shared", "plugins", "bootstrap", "tests")
_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")

#: SHA-256 of each forbidden domain, lowercased. One entry per counterparty.
_FORBIDDEN_DOMAIN_HASHES: dict[str, str] = {
    "b70916503fe3fe57a5612267dec0f753277d1ca8b93a63e228c7dd9bf2ca1c9e": (
        "first law-firm client (ss#2258 — the incident that created this gate)"
    ),
}


def _digest(domain: str) -> str:
    return hashlib.sha256(domain.lower().rstrip(".").encode()).hexdigest()


def _forbidden(domain: str) -> str | None:
    """The label for a forbidden domain, or None. Also matches a subdomain of one."""
    d = domain.lower().rstrip(".")
    parts = d.split(".")
    # Check the domain and every parent (mail.firm.com -> firm.com -> com).
    for i in range(len(parts) - 1):
        label = _FORBIDDEN_DOMAIN_HASHES.get(_digest(".".join(parts[i:])))
        if label:
            return label
    return None


def _offenders() -> list[str]:
    found: list[str] = []
    for rel in _SCAN_DIRS:
        base = _ROOT / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in _SUFFIXES:
                continue
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                for match in _EMAIL.finditer(line):
                    label = _forbidden(match.group(1))
                    if label:
                        # Report the location and WHY, never the address itself --
                        # a failing CI log is public too.
                        found.append(
                            f"{path.relative_to(_ROOT)}:{line_no}  "
                            f"address at a forbidden domain ({label})"
                        )
    return found


def test_no_counterparty_addresses_in_shipped_source() -> None:
    offenders = _offenders()
    assert not offenders, (
        "Email address(es) at a real counterparty's domain found in shipped "
        "source. This repository is public and every file ships to every customer "
        "seat, so an address here is both an exposure and a candidate explanation "
        "for who a live system emailed (ss#2258). Replace with a reserved domain "
        "(example.com, *.example, *.invalid):\n  " + "\n  ".join(offenders)
    )


def test_the_gate_can_actually_fail() -> None:
    """Law 12: a guard that cannot fire has measured nothing.

    Uses the digest directly rather than a readable domain, so proving the gate
    works does not reintroduce the string it forbids.
    """
    known = next(iter(_FORBIDDEN_DOMAIN_HASHES))
    assert _FORBIDDEN_DOMAIN_HASHES[known]  # the entry carries a reason
    assert _forbidden("example.com") is None
    assert _forbidden("firm.com") is None  # generic placeholder stays allowed
    assert _digest("EXAMPLE.COM") == _digest("example.com")  # case-insensitive


def test_subdomains_of_a_forbidden_domain_are_also_caught(monkeypatch) -> None:
    """`mail.<client>.com` must not slip past an exact-match check.

    Injects its OWN forbidden entry so the parent-walk is genuinely exercised
    without this file naming a real counterparty.
    """
    victim = "notarealfirm.test"
    monkeypatch.setitem(_FORBIDDEN_DOMAIN_HASHES, _digest(victim), "injected by this test")

    assert _forbidden(victim) == "injected by this test"
    assert _forbidden("mail." + victim) == "injected by this test"
    assert _forbidden("a.b." + victim) == "injected by this test"
    # A domain that merely ENDS with the same letters is not a subdomain.
    assert _forbidden("x" + victim) is None
    # And an unrelated domain still passes.
    assert _forbidden("example.com") is None
