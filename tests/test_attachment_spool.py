"""The seat-local attachment spool: what it refuses, and that it can refuse.

The spool is the only path an emailed attachment's BYTES take between two
processes on a seat, and the only thing the agent carries across is a token it
did not choose the shape of. Every test here is a way that arrangement could be
turned into a file read the caller should not get, or into a stale byte being
filed as a fresh one. Each carries the direction that would be a defect.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from shared import attachment_spool as spool


@pytest.fixture
def spooled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(spool.SPOOL_DIR_ENV, str(tmp_path / "spool"))
    return tmp_path / "spool"


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


def test_write_then_read_returns_the_same_bytes(spooled: Path) -> None:
    blob = b"%PDF-1.7 an invoice"
    receipt = spool.write(blob, filename="invoice-1001.pdf", content_type="application/pdf")
    assert spool.TOKEN_RE.match(receipt["spool_token"])
    assert receipt["sha256"] == hashlib.sha256(blob).hexdigest()
    assert receipt["size"] == len(blob)
    assert receipt["filename"] == "invoice-1001.pdf"
    assert receipt["content_type"] == "application/pdf"
    assert spool.read(receipt["spool_token"], expected_sha256=receipt["sha256"]) == blob


def test_the_receipt_never_carries_the_bytes(spooled: Path) -> None:
    """The point of a token: the model sees a receipt, not the document."""
    receipt = spool.write(b"secret vendor totals", filename="x.pdf", content_type="application/pdf")
    assert "secret vendor totals" not in repr(receipt)
    assert set(receipt) == {"spool_token", "filename", "content_type", "size", "sha256"}


def test_two_writes_get_different_tokens(spooled: Path) -> None:
    a = spool.write(b"one", filename="a.pdf", content_type="application/pdf")
    b = spool.write(b"two", filename="a.pdf", content_type="application/pdf")
    assert a["spool_token"] != b["spool_token"]
    assert spool.read(a["spool_token"]) == b"one"


# ---------------------------------------------------------------------------
# The vendor's filename never becomes a path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "/etc/shadow",
        "..\\..\\windows\\system32\\config",
        "inv\x00oice.pdf",
        "." * 40,
        "",
    ],
)
def test_a_hostile_filename_does_not_reach_the_disk(spooled: Path, hostile: str) -> None:
    """The on-disk name comes from the TOKEN. A sender who names a file
    ``../../etc/passwd`` gets a spool entry called ``<token>.bin`` and a
    reportable filename with the path stripped out."""
    receipt = spool.write(b"bytes", filename=hostile, content_type="application/pdf")
    written = sorted(p.name for p in spooled.iterdir())
    assert written == [f"{receipt['spool_token']}.bin", f"{receipt['spool_token']}.json"]
    assert "/" not in receipt["filename"] and "\\" not in receipt["filename"]
    assert "\x00" not in receipt["filename"]
    assert receipt["filename"]


def test_safe_filename_keeps_an_ordinary_name(spooled: Path) -> None:
    """The falsifier for the test above: if sanitizing mangled every name, the
    hostile cases would pass while the tool became useless."""
    assert spool.safe_filename("Invoice 2026-09 (copy).pdf") == "Invoice 2026-09 (copy).pdf"


# ---------------------------------------------------------------------------
# Token validation — the only accepted shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "..",
        "",
        "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "ABCDEF0123456789abcdef0123456789",  # uppercase hex is not the issued shape
        "abcdef0123456789abcdef012345678",  # one short
        "abcdef0123456789abcdef0123456789a",  # one long
        None,
        12345,
    ],
)
def test_a_token_that_is_not_the_issued_shape_is_refused(spooled: Path, token: object) -> None:
    """Refused ON THE SHAPE, and the message says so.

    Matching the message is deliberate. Without it this test passes for the
    wrong reason — a traversal token names a file that does not exist, so
    ``resolve(strict=True)`` raises and the assertion is satisfied whether or
    not the shape check is there at all. Pinning the reason is what makes
    ``TOKEN_RE`` load-bearing: delete it and every case here goes red.
    """
    with pytest.raises(spool.SpoolError, match="32 lowercase hex"):
        spool.resolve(token)


def test_a_traversal_token_pointing_at_a_real_file_is_still_refused(spooled: Path) -> None:
    """The case that would actually leak. ``../outside`` names a file that DOES
    exist, so nothing about a missing path saves us here; the shape check is the
    only thing between the caller and someone else's bytes."""
    spooled.mkdir(parents=True)
    (spooled.parent / "outside.bin").write_bytes(b"not yours")
    with pytest.raises(spool.SpoolError):
        spool.read("../outside")


def test_an_issued_token_with_no_entry_is_refused(spooled: Path) -> None:
    spooled.mkdir(parents=True)
    with pytest.raises(spool.SpoolError):
        spool.read(spool.new_token())


def test_a_symlinked_entry_is_refused(spooled: Path) -> None:
    """Shape alone is not enough: a token that resolves to a link out of the
    spool must not be followed, however the link got there."""
    spooled.mkdir(parents=True)
    outside = spooled.parent / "outside.txt"
    outside.write_bytes(b"not yours")
    token = spool.new_token()
    os.symlink(outside, spooled / f"{token}.bin")
    with pytest.raises(spool.SpoolError, match="symlink"):
        spool.read(token)


def test_a_directory_named_like_an_entry_is_refused(spooled: Path) -> None:
    spooled.mkdir(parents=True)
    token = spool.new_token()
    (spooled / f"{token}.bin").mkdir()
    with pytest.raises(spool.SpoolError, match="regular file"):
        spool.read(token)


# ---------------------------------------------------------------------------
# Integrity and size
# ---------------------------------------------------------------------------


def test_a_wrong_digest_refuses_rather_than_returning_the_bytes(spooled: Path) -> None:
    receipt = spool.write(b"the invoice read", filename="a.pdf", content_type="application/pdf")
    with pytest.raises(spool.SpoolError, match="sha256"):
        spool.read(receipt["spool_token"], expected_sha256="0" * 64)


def test_the_right_digest_passes(spooled: Path) -> None:
    """The falsifier for the test above: a digest check that refused everything
    would pass it and make the spool unusable."""
    receipt = spool.write(b"the invoice read", filename="a.pdf", content_type="application/pdf")
    assert (
        spool.read(receipt["spool_token"], expected_sha256=receipt["sha256"]) == b"the invoice read"
    )


def test_an_oversized_attachment_is_never_written(spooled: Path) -> None:
    with pytest.raises(spool.SpoolError, match="spool limit"):
        spool.write(
            b"x" * (spool.MAX_SPOOL_BYTES + 1), filename="big.pdf", content_type="application/pdf"
        )
    assert not spooled.exists() or list(spooled.iterdir()) == []


def test_an_empty_attachment_is_never_written(spooled: Path) -> None:
    with pytest.raises(spool.SpoolError):
        spool.write(b"", filename="empty.pdf", content_type="application/pdf")


def test_entries_are_owner_only(spooled: Path) -> None:
    receipt = spool.write(b"client material", filename="a.pdf", content_type="application/pdf")
    mode = (spooled / f"{receipt['spool_token']}.bin").stat().st_mode & 0o777
    assert mode == 0o600, (
        f"spool entry is {oct(mode)}; client material must not be group/world readable"
    )
    assert spooled.stat().st_mode & 0o777 == 0o700


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_a_stale_entry_is_pruned_on_the_next_write(spooled: Path) -> None:
    old = spool.write(b"yesterday", filename="a.pdf", content_type="application/pdf")
    stale = time.time() - spool.SPOOL_TTL_SECONDS - 60
    for suffix in (".bin", ".json"):
        os.utime(spooled / f"{old['spool_token']}{suffix}", (stale, stale))
    fresh = spool.write(b"today", filename="b.pdf", content_type="application/pdf")
    with pytest.raises(spool.SpoolError):
        spool.read(old["spool_token"])
    assert spool.read(fresh["spool_token"]) == b"today"


def test_a_fresh_entry_survives_a_prune(spooled: Path) -> None:
    """The falsifier: a prune that removed everything would satisfy the test
    above while destroying the handoff it exists for."""
    first = spool.write(b"one", filename="a.pdf", content_type="application/pdf")
    spool.write(b"two", filename="b.pdf", content_type="application/pdf")
    assert spool.read(first["spool_token"]) == b"one"


def test_prune_on_a_missing_directory_is_quiet(spooled: Path) -> None:
    assert spool.prune() == 0
