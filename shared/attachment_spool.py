"""A seat-local handoff for email attachment BYTES, because no URL exists.

THE DEFECT THIS CLOSES (proven live 2026-09-18). A seat received a vendor's
invoice by email and answered "Your message arrived without any attachments."
Two facts, each true on their own, combine into that:

1. The ``message.received`` webhook payload a seat captures carries no
   ``attachments`` key at all. Its message keys are created_at, extracted_text,
   from, from_, headers, inbox_id, labels, message_id, organization_id, pod_id,
   preview, size, smtp_id, subject, text, thread_id, timestamp, to, updated_at.
   The turn therefore never learns from the event that an attachment exists,
   even when the message plainly carries one.
2. The connector tools that consume an attachment were written against a
   DOWNLOAD-URL contract (a time-limited vendor URL, host-allowlisted). The
   mail vendor does not mint one: its attachment endpoint returns RAW BYTES to
   an authenticated caller. There was no URL to pass, so the URL-shaped
   argument could never be filled.

The agent cannot carry bytes between two MCP servers through its own context,
and it must never be handed a credential. So the bytes move through the seat's
own filesystem and the agent carries only a TOKEN: the mail side fetches with
the seat's inbox-scoped credential and writes one spool entry; the records side
resolves the token to a path on the same machine and reads it.

WHAT THE TOKEN IS, AND WHAT IT IS NOT. It is 32 lowercase hex characters
generated here, and nothing else is accepted. The on-disk name is derived from
the token ALONE — never from the vendor's filename, which is attacker-chosen
text that may contain ``../``, a NUL, an absolute path, or a name that collides
with something else on the volume. The vendor's filename is carried as DATA in
the entry's metadata, for the reply to quote and for extraction to key off.

THIS MODULE IS MIRRORED. The reader half lives in ss-console
``operator/connectors/smokeball/smokeball_connector/attachment_source.py`` and
re-implements ``resolve``/``read`` against the same layout, because the two
run in different processes from different repos and share no import path. The
layout (``<token>.bin`` + ``<token>.json``, the token alphabet, the env var,
the size cap) is the contract between them; change it on one side and the other
stops finding entries. Both sides validate the token shape independently — a
guard that only one side performs is a guard the other side's caller can skip.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

#: Where spool entries live. Overridable for tests and for a seat that mounts
#: its volume elsewhere; the default is the hermes-owned volume root, which the
#: seat's entrypoint already sweeps to the agent uid, so no ownership change is
#: needed anywhere for this to work.
SPOOL_DIR_ENV = "SMD_ATTACHMENT_SPOOL_DIR"
DEFAULT_SPOOL_DIR = "/opt/data/attachment-spool"

#: The same 25 MB ceiling the records connector applies to a URL fetch
#: (``fetch_attachment_url``). Named here rather than inherited, because the two
#: processes cannot import each other; the connector pins the same number.
MAX_SPOOL_BYTES = 25 * 1024 * 1024

#: How long an entry stays readable. An intake turn is minutes; six hours is
#: generous for a retry and short enough that a volume does not accumulate
#: client documents indefinitely. Pruning happens on every write, so a seat
#: that stops receiving attachments stops accumulating them too.
SPOOL_TTL_SECONDS = 6 * 60 * 60

#: The ONLY accepted token shape. ``secrets.token_hex(16)`` produces exactly
#: this, so a token that fails here did not come from this module.
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

_BYTES_SUFFIX = ".bin"
_META_SUFFIX = ".json"


class SpoolError(RuntimeError):
    """A spool entry could not be written, resolved, or read."""


def spool_dir() -> Path:
    """The spool directory for this process."""
    return Path(os.environ.get(SPOOL_DIR_ENV, "").strip() or DEFAULT_SPOOL_DIR)


def new_token() -> str:
    """A fresh opaque token. 128 bits, so a token cannot be guessed by a caller
    that never received one."""
    return secrets.token_hex(16)


def safe_filename(name: Any) -> str:
    """The vendor's filename, reduced to something safe to REPORT.

    It is never used to build a path. This strips directory separators, NULs
    and control characters, and caps the length, so a filename that reaches a
    log line, a reply, or a Smokeball file name cannot smuggle a path segment.
    An empty or unusable name becomes ``attachment``.
    """
    text = name if isinstance(name, str) else ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable() and ch not in {"\x00"}).strip()
    text = text.strip(". ")
    return text[:200] or "attachment"


def prune(now: float | None = None, *, directory: Path | None = None) -> int:
    """Delete entries older than the TTL. Returns how many entry PAIRS went.

    Best effort by construction: a file that vanishes under us, or one we may
    not remove, must not fail the write this prune is running on behalf of.
    """
    base = directory or spool_dir()
    cutoff = (now if now is not None else time.time()) - SPOOL_TTL_SECONDS
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for path in entries:
        if path.suffix not in (_BYTES_SUFFIX, _META_SUFFIX):
            continue
        try:
            if path.is_symlink() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        if path.suffix == _BYTES_SUFFIX:
            removed += 1
    return removed


def write(blob: bytes, *, filename: Any, content_type: Any) -> dict[str, Any]:
    """Spool one attachment's bytes and return its receipt.

    The receipt is what the agent sees: a token, the vendor's filename reduced
    to a reportable form, the content type, the size, and the sha256 of exactly
    the bytes on disk. Never the bytes, and never a credential.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise SpoolError("spool write expects bytes")
    blob = bytes(blob)
    if not blob:
        raise SpoolError("refusing to spool an empty attachment")
    if len(blob) > MAX_SPOOL_BYTES:
        raise SpoolError(
            f"attachment is {len(blob)} bytes, over the {MAX_SPOOL_BYTES}-byte spool limit"
        )
    base = spool_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
        # Owner-only ON PURPOSE. This directory holds a client's documents on a
        # shared volume; the reader is the same uid (the records connector runs
        # as a child of the same gateway), so nothing needs group or other.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(base, 0o700)
    except OSError as exc:
        raise SpoolError(f"spool directory {base} is not usable: {exc}") from exc
    prune(directory=base)
    token = new_token()
    name = safe_filename(filename)
    ctype = (
        content_type
        if isinstance(content_type, str) and content_type.strip()
        else "application/octet-stream"
    )
    digest = hashlib.sha256(blob).hexdigest()
    receipt: dict[str, Any] = {
        "spool_token": token,
        "filename": name,
        "content_type": ctype.strip()[:120],
        "size": len(blob),
        "sha256": digest,
    }
    bytes_path = base / f"{token}{_BYTES_SUFFIX}"
    meta_path = base / f"{token}{_META_SUFFIX}"
    try:
        # 0o600 from creation, not chmod-after: an attachment is client
        # material and must never exist group- or world-readable, not even for
        # the width of one syscall.
        fd = os.open(bytes_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(
                fd, json.dumps({**receipt, "spooled_at": time.time()}, sort_keys=True).encode()
            )
        finally:
            os.close(fd)
    except OSError as exc:
        raise SpoolError(f"could not write the spool entry: {exc}") from exc
    return receipt


def resolve(token: Any, *, directory: Path | None = None) -> Path:
    """The path holding this token's bytes, or raise.

    Four refusals, in order, each closing a different way a caller-supplied
    string becomes a path it should not reach: a token that is not the exact
    generated shape (which alone forecloses ``..``, an absolute path, and a NUL);
    an entry that is not inside the spool directory after resolution; a symlink;
    anything that is not a regular file.
    """
    base = (directory or spool_dir()).resolve()
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise SpoolError("spool token must be 32 lowercase hex characters issued by the mail side")
    path = base / f"{token}{_BYTES_SUFFIX}"
    if path.is_symlink():
        raise SpoolError("refusing to read a spool entry that is a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SpoolError(f"no spool entry for token {token}") from exc
    if resolved.parent != base:
        raise SpoolError("refusing a spool entry outside the spool directory")
    try:
        mode = resolved.lstat().st_mode
    except OSError as exc:
        raise SpoolError(f"no spool entry for token {token}") from exc
    if not stat.S_ISREG(mode):
        raise SpoolError("refusing a spool entry that is not a regular file")
    return resolved


def read(token: Any, *, expected_sha256: str = "", directory: Path | None = None) -> bytes:
    """The spooled bytes for this token, size-capped and optionally checked.

    ``expected_sha256`` is how a caller states which bytes it means. A mismatch
    raises rather than returning the other bytes, because the whole point of
    carrying a digest through the turn is that the thing filed is the thing read.
    """
    path = resolve(token, directory=directory)
    size = path.stat().st_size
    if size > MAX_SPOOL_BYTES:
        raise SpoolError(f"spool entry is {size} bytes, over the {MAX_SPOOL_BYTES}-byte limit")
    blob = path.read_bytes()
    want = expected_sha256.strip().lower() if isinstance(expected_sha256, str) else ""
    if want and hashlib.sha256(blob).hexdigest() != want:
        raise SpoolError(
            "the spooled bytes do not match the sha256 given; read the attachment again"
        )
    return blob


__all__ = [
    "DEFAULT_SPOOL_DIR",
    "MAX_SPOOL_BYTES",
    "SPOOL_DIR_ENV",
    "SPOOL_TTL_SECONDS",
    "TOKEN_RE",
    "SpoolError",
    "new_token",
    "prune",
    "read",
    "resolve",
    "safe_filename",
    "spool_dir",
    "write",
]
