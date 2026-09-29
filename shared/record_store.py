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
from dataclasses import dataclass
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
        for fault in policy_problems(entry):
            raise RecordStoreError(f"{CONFIG_KEY}[{i}].{fault}")
        cleaned: dict[str, Any] = {"name": name, "path": path}
        for key in POLICY_KEYS:
            if key in entry:
                cleaned[key] = entry[key]
        out.append(cleaned)
    return out


#: The keys a store may carry beyond name and path, and what each authors.
#:
#: ``owner_field``  the frontmatter key that names the person a record belongs
#:                  to (the open-house store uses ``agent``). Present = the
#:                  store is PRIVATE PER OWNER: on an inbound turn a person reads
#:                  and rewrites only records stamped with their own address.
#:                  Absent = the store is shared by everyone on the roster.
#: ``readers``      addresses that may READ every owner's records (a broker).
#:                  Read only: a reader never rewrites another owner's record.
#:                  Needs ``owner_field``; a shared store has nothing to grant.
#: ``index``        frontmatter keys the LISTING exposes for every record, so a
#:                  capture can notice a duplicate visitor across owners without
#:                  opening a colleague's notes. ``status`` is always exposed.
POLICY_KEYS: tuple[str, ...] = ("owner_field", "readers", "index")
FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALWAYS_INDEXED: tuple[str, ...] = ("status",)


def policy_problems(entry: dict[str, Any]) -> list[str]:
    """Every fault in an entry's policy keys, as ``key: reason`` strings.

    Shared with ``bootstrap.validate`` so the provisioning-time rule and the
    runtime rule cannot drift. Name and path are judged by the caller.
    """
    faults: list[str] = []
    owner = entry.get("owner_field")
    if "owner_field" in entry and (not isinstance(owner, str) or not FIELD_RE.match(owner)):
        faults.append("owner_field: must be a frontmatter key (a-z, 0-9, _)")
    for key in ("readers", "index"):
        if key not in entry:
            continue
        value = entry[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            faults.append(f"{key}: must be a list of non-empty strings")
            continue
        if key == "readers":
            if "owner_field" not in entry:
                faults.append("readers: needs owner_field (a shared store has nothing to grant)")
            for v in value:
                if "@" not in v or v.strip() != v:
                    faults.append(f"readers: {v!r} is not an email address")
        else:
            for v in value:
                if not FIELD_RE.match(v):
                    faults.append(f"index: {v!r} is not a frontmatter key (a-z, 0-9, _)")
    return faults


@dataclass(frozen=True)
class StorePolicy:
    """One store's root and its authored access posture."""

    root: Path
    owner_field: str | None = None
    readers: frozenset[str] = frozenset()
    index: tuple[str, ...] = ()

    @property
    def private(self) -> bool:
        return self.owner_field is not None


def authored_policies(config: CustomerConfig | None = None) -> dict[str, StorePolicy]:
    """``{store name: StorePolicy}`` for the seat. Same faults as
    :func:`authored_stores`; readers are compared case-insensitively."""
    if config is None:
        try:
            config = CustomerConfig.from_volume()
        except CustomerConfigError as exc:
            raise RecordStoreError(f"customer.yaml unreadable: {exc}") from exc
    out: dict[str, StorePolicy] = {}
    for entry in _clean_stores(config.raw.get(CONFIG_KEY)):
        out[entry["name"]] = StorePolicy(
            root=Path(entry["path"]),
            owner_field=entry.get("owner_field"),
            readers=frozenset(r.strip().lower() for r in entry.get("readers", [])),
            index=tuple(entry.get("index", [])),
        )
    return out


#: A record's frontmatter is read from at most this many bytes: the index is
#: the head of the file, and the listing must stay cheap over a whole store.
_FRONTMATTER_BYTES = 4096


def read_frontmatter(text: str) -> dict[str, str]:
    """The scalar ``key: value`` lines between the first ``---`` pair.

    Deliberately not a YAML parser: a record's frontmatter is written by the
    seat in the shape the skill documents (one scalar per line), and a listing
    that ran a full parser over every record on every turn would be paying for
    shapes no record carries. Quotes around a value are stripped; a list item
    or a nested key is skipped, never guessed.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0] in " \t-#":
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not FIELD_RE.match(key):
            continue
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def record_owner(text: str, policy: StorePolicy) -> str | None:
    """The address a record is stamped with, lower-cased, or ``None``."""
    if policy.owner_field is None:
        return None
    value = read_frontmatter(text).get(policy.owner_field)
    if not isinstance(value, str):
        return None
    return value.strip().lower() or None


def access_problem(
    policy: StorePolicy,
    *,
    action: str,
    viewer: str | None,
    existing: str | None,
    content: str | None = None,
) -> str | None:
    """Why ``viewer`` may not ``action`` this record, or ``None``.

    ``viewer`` is the verified inbound sender of the turn (lower-cased), or
    ``None`` on a turn that has none: a scheduled wake or the principal on a
    channel of their own. Those are the seat's own turns and are never fenced;
    the fence is between the PEOPLE who write in. ``existing`` is the record's
    current text when it exists.

    * A shared store (no owner field) fences nothing.
    * read: another owner's record is refused unless the viewer is a reader.
    * write: the content must be stamped with the viewer's own address, and an
      existing record that belongs to someone else is never rewritten, reader
      or not. A reader reads; the owner corrects.

    An unstamped record in a private store is nobody's: readable by all, and
    rewritable only by stamping it with the writer's own address.
    """
    if not policy.private or viewer is None:
        return None
    viewer = viewer.strip().lower()
    owner = record_owner(existing, policy) if existing is not None else None
    if action == "read":
        if owner and owner != viewer and viewer not in policy.readers:
            return (
                f"that record belongs to another person ({policy.owner_field}: {owner}); "
                "only they, or a reader the store authors, may open it"
            )
        return None
    if action == "write":
        if owner and owner != viewer:
            return (
                f"that record belongs to another person ({policy.owner_field}: {owner}) "
                "and only they may change it; keep your own account as a record of your own"
            )
        stamped = record_owner(content or "", policy)
        if stamped != viewer:
            return (
                f"a record you write must carry {policy.owner_field}: {viewer} in its "
                "frontmatter (yours, exactly), so it is yours to find again"
            )
        return None
    return None


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


def list_records(
    store: str,
    *,
    stores: dict[str, Path] | None = None,
    policy: StorePolicy | None = None,
) -> list[dict[str, Any]]:
    """Every record in the store, newest first: name, size, modified (UTC ISO).

    With a policy, each entry also carries ``index`` (the authored index keys
    plus ``status``, read from the frontmatter) and, on a private store,
    ``owner`` (the address the record is stamped with). The listing is what
    every rostered person may see: enough to notice that a colleague already
    has this visitor, never the notes. The notes are behind ``read_record``
    and its fence.
    """
    from datetime import datetime, timezone

    if policy is not None:
        root = policy.root
    else:
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
        row: dict[str, Any] = {
            "name": entry.name,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }
        if policy is not None and (policy.private or policy.index):
            row.update(_index_entry(entry, policy))
        out.append(row)
    out.sort(key=lambda r: (r["modified"], r["name"]), reverse=True)
    return out


def _index_entry(path: Path, policy: StorePolicy) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            head = handle.read(_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {"index": {}}
    fm = read_frontmatter(head)
    keys = tuple(dict.fromkeys((*policy.index, *ALWAYS_INDEXED)))
    row: dict[str, Any] = {"index": {k: fm[k] for k in keys if k in fm}}
    if policy.private:
        row["owner"] = record_owner(head, policy)
    return row


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
    "POLICY_KEYS",
    "RecordStoreError",
    "StorePolicy",
    "access_problem",
    "authored_policies",
    "authored_stores",
    "list_records",
    "path_problem",
    "policy_problems",
    "read_frontmatter",
    "read_record",
    "record_owner",
    "write_record",
]
