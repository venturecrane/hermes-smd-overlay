"""Authored record stores: the one place an inbound turn may write.

WHY THIS EXISTS (ss-console#2793, proven live 2026-09-25). The open-house
capture skill asks a real estate agent to email a dictated conversation and
have it kept as a visitor record on the seat's volume. Inbound-email turns get
no ``write_file``: the webhook platform is offered ``web``, ``vision``,
``clarify`` and the one-tool read surface, and NEVER the ``file`` toolset,
because an email body is untrusted content and a write on such a turn is what
the safe default exists to deny. Hermes auto-repaired the skill's
``write_file`` calls to ``read_file``, the records directory stayed empty after
two dictations, and the seat's own draft said "write_file is not available".

The fix is not to hand inbound turns the file toolset. It is a purpose-built
tool pair (``plugins/hermes-smd-record-store``) whose WRITE can only land
inside a directory the engagement AUTHORED for it, by name, in
``customer.yaml``::

    record_stores:
      - name: open-house-visitors
        path: /opt/data/open-house/visitors

The model never passes a path. It names a store and a record; this module
resolves the two to a file and refuses anything that would leave the store:
a name with a separator, a ``..``, a symlink that points out, a store nobody
authored. A seat that authors no store gets no tools at all (the plugin's
``register`` gate), so every other seat's inbound surface is byte-identical to
what it was.

WHAT IS AND IS NOT FENCED HERE. The directory is the fence; the CONTENT is the
skill's business (the open-house skill keeps the agent's words verbatim and
authors nothing about a visitor the agent did not say). The write refuses on a
tainted session (``shared.inbound``): a turn that read untrusted content may
not put that content where an unfenced read will later find it, or the store
would launder an injection into a trusted source.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from shared.customer_config import CustomerConfig, CustomerConfigError

#: The customer.yaml key. A list of ``{name, path}`` mappings.
CONFIG_KEY = "record_stores"

#: Store names are identifiers the skill text carries: short, kebab-case.
STORE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

#: Record names are single path segments. No separators, no leading dot, no
#: control characters, one of a few text extensions. The model composes these
#: from a date, a property slug and a visitor slug, so the shape is generous
#: about what a slug can hold and strict about what a path can be.
RECORD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,158}\.(md|txt|json|yaml|yml)$")

#: Every authored path must live under the volume root, and never in the parts
#: of it the runtime owns. A store on the profile home would let a record
#: overwrite a cron store or a skill body; a store at the volume root would
#: make ``customer.yaml`` a record name away.
VOLUME_ROOT = Path("/opt/data")
RESERVED_PREFIXES: tuple[str, ...] = (
    "/opt/data/profiles",
    "/opt/data/attachment-spool",
    "/opt/data/smokeball-extract-cache",
    "/opt/data/.smd",
    "/opt/data/medchron",
)

#: A record is prose the size of an email. Anything larger is not a record.
MAX_RECORD_BYTES = 256 * 1024


class RecordStoreError(RuntimeError):
    """A store or record could not be resolved, read, listed, or written.

    The message is written for the model: it says which rule refused and never
    echoes a resolved filesystem path outside the store.
    """


def _clean_stores(raw: Any) -> list[dict[str, str]]:
    """The authored list, shape-checked. Bad entries raise; validate.py reports
    the same faults at provisioning so a seat never boots into them."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RecordStoreError(f"{CONFIG_KEY} must be a list of {{name, path}} entries")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RecordStoreError(f"{CONFIG_KEY}[{i}] must be a mapping with name and path")
        name = entry.get("name")
        path = entry.get("path")
        if not isinstance(name, str) or not STORE_NAME_RE.match(name):
            raise RecordStoreError(f"{CONFIG_KEY}[{i}].name must be kebab-case (a-z, 0-9, -)")
        if name in seen:
            raise RecordStoreError(f"{CONFIG_KEY}: store {name!r} is authored twice")
        seen.add(name)
        if not isinstance(path, str) or not path.startswith("/"):
            raise RecordStoreError(f"{CONFIG_KEY}[{i}].path must be an absolute path")
        problem = path_problem(path)
        if problem:
            raise RecordStoreError(f"{CONFIG_KEY}[{i}].path: {problem}")
        out.append({"name": name, "path": path})
    return out


def path_problem(path: str) -> str | None:
    """Why an authored store path is refused, or ``None`` when it is fine.

    Shared with ``bootstrap.validate`` so the provisioning-time rule and the
    runtime rule cannot drift apart. Purely lexical: it judges the authored
    string, not the disk, because the disk is not there at validation time.
    """
    normalized = os.path.normpath(path)
    if normalized != path.rstrip("/") and normalized != path:
        return "must be a normalized absolute path (no '..', '.', or doubled separators)"
    if normalized == str(VOLUME_ROOT):
        return "must be a directory under /opt/data, not /opt/data itself"
    if not normalized.startswith(str(VOLUME_ROOT) + "/"):
        return "must live under /opt/data (the seat's persistent volume)"
    for reserved in RESERVED_PREFIXES:
        if normalized == reserved or normalized.startswith(reserved + "/"):
            return f"must not live under {reserved} (owned by the runtime)"
    return None


def authored_stores(config: CustomerConfig | None = None) -> dict[str, Path]:
    """``{store name: root path}`` for the seat, from its authored config.

    Raises :class:`RecordStoreError` when the config cannot be read or the
    block is malformed, so a caller cannot mistake "could not tell" for "none
    authored". The plugin's register gate catches the error, registers
    nothing, and logs why.
    """
    if config is None:
        try:
            config = CustomerConfig.from_volume()
        except CustomerConfigError as exc:
            raise RecordStoreError(f"customer.yaml unreadable: {exc}") from exc
    raw = config.raw.get(CONFIG_KEY)
    return {entry["name"]: Path(entry["path"]) for entry in _clean_stores(raw)}


def _store_root(stores: dict[str, Path], store: Any) -> Path:
    if not isinstance(store, str) or store not in stores:
        known = ", ".join(sorted(stores)) or "none"
        raise RecordStoreError(
            f"no record store named {store!r} is authored on this seat (authored: {known})"
        )
    return stores[store]


def _record_path(root: Path, name: Any) -> Path:
    if not isinstance(name, str) or not RECORD_NAME_RE.match(name):
        raise RecordStoreError(
            "record name must be a single file name ending in .md, .txt, .json or .yaml: "
            "letters, digits, dots, dashes, underscores or spaces, no separators, no leading dot"
        )
    candidate = root / name
    # The store root may not yet exist on a fresh volume; resolve what does.
    real_root = root.resolve()
    real_candidate = candidate.resolve()
    if real_candidate.parent != real_root:
        raise RecordStoreError("record name resolves outside its store; refused")
    return candidate


def list_records(store: str, *, stores: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    """Every record in the store, newest first: name, size, modified (UTC ISO)."""
    from datetime import datetime, timezone

    root = _store_root(stores if stores is not None else authored_stores(), store)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if not RECORD_NAME_RE.match(entry.name):
            continue
        stat = entry.stat()
        out.append(
            {
                "name": entry.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    out.sort(key=lambda r: (r["modified"], r["name"]), reverse=True)
    return out


def read_record(store: str, name: str, *, stores: dict[str, Path] | None = None) -> str:
    """The record's text. A missing record is an error the model can say."""
    root = _store_root(stores if stores is not None else authored_stores(), store)
    path = _record_path(root, name)
    if not path.is_file():
        raise RecordStoreError(f"no record named {name!r} in store {store!r}")
    if path.stat().st_size > MAX_RECORD_BYTES:
        raise RecordStoreError(f"record {name!r} is larger than a record can be; not read")
    return path.read_text(encoding="utf-8")


def write_record(
    store: str,
    name: str,
    content: str,
    *,
    overwrite: bool = False,
    stores: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Write one record atomically. Returns ``{store, name, bytes, created}``.

    ``overwrite`` is opt-in: a capture that would silently replace yesterday's
    record with today's is a lost record, so the default refuses and the model
    must say it means to replace. The directory is created on first write,
    owned by the process uid (the agent), which is what lets the scheduled turn
    read it back tomorrow.
    """
    root = _store_root(stores if stores is not None else authored_stores(), store)
    path = _record_path(root, name)
    if not isinstance(content, str):
        raise RecordStoreError("content must be text")
    data = content.encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise RecordStoreError("content is larger than a record can be; not written")
    if not data.strip():
        raise RecordStoreError("content is empty; nothing to keep")
    existed = path.exists()
    if existed and not overwrite:
        raise RecordStoreError(
            f"record {name!r} already exists in store {store!r}; pass overwrite=true to replace it"
        )
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"store": store, "name": name, "bytes": len(data), "created": not existed}


__all__ = [
    "CONFIG_KEY",
    "MAX_RECORD_BYTES",
    "RECORD_NAME_RE",
    "RESERVED_PREFIXES",
    "STORE_NAME_RE",
    "RecordStoreError",
    "authored_stores",
    "list_records",
    "path_problem",
    "read_record",
    "write_record",
]
