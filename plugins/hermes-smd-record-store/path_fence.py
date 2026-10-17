"""The file-tool path fence: on a seat that keeps records, a file tool reads only
what is listed here.

THE DEFECT (proven offline 2026-10-17 against the real Hermes ``read_file_tool``
at the pinned v2026.9.14). The owner fence in this plugin guards the
``record_store_*`` tools and nothing else. ``read_file`` is READ-class, which
the trust plugin always allows, and it is on the webhook (email) surface
(``shared.webhook_read_surface``: ``smd_webhook_read`` = ``read_file``).
Hermes' own read guard denies credential stores, ``.env`` and devices, never a
directory of records. So an agent's email turn that names
``/opt/data/open-house/visitors/<colleague's record>.md`` got the colleague's
private notes back while ``record_store_read`` of the same record refused. The
same path reads cron output, the session store, memories and logs, which carry
other people's turns.

THE FENCE IS AN ALLOWLIST. A denylist of the places records happen to live
today would miss the next one. On a seat that authors ``record_stores`` the
file tools may read:

* the skill roots (``/app/skills``, ``/opt/data/skills``, ``$HERMES_HOME/skills``),
  where the omitted-skill fence (``hermes-smd-initiation/skill_fence.py``)
  decides which skill;
* the installed spec tree (``$SMD_SPEC_DIR``), which the spec read-mark needs;
* a spillover file Hermes wrote for a tool result of THIS session (an
  oversized result is saved to ``$HERMES_HOME/cache/spillover/<call id>.txt``
  and the model is told to read it). Another session's spillover is another
  person's turn and is refused.

Nothing else: not a record store (denied first, even if one were authored
under an allowed root), not the cron store or its output, ``state.db``,
``sessions/``, ``memories/``, ``logs/``, the attachment spool, ``/tmp``, the
Hermes install. A record is opened only through ``record_store_read``, which
applies the store's owner rules.

``terminal`` and ``execute_code`` are refused outright on such a seat. A shell
command or a code cell cannot be shown to read only listed paths (string
concatenation defeats any parser), so an allowlist over them can only be
honest by refusing. No seat that authors a record store authors autonomous
code execution today, so the trust plugin already refuses both there; this
makes the refusal independent of that posture.

WHO IT APPLIES TO. Every turn on the seat, the seat's own scheduled wakes
included. Records are never read by path on any turn; this does not change
what a scheduled wake may do through the ``record_store_*`` tools (viewer-less
turns stay unfenced there). A seat that authors no store never loads this
fence: ``register`` gates it.

THE WRITE SIDE (2026-10-17, follow-on). ``write_file`` and ``patch`` are not on
the email surface, but a scheduled wake is offered the ``file`` toolset, and a
``write_file`` into a store skips everything ``record_store_write`` enforces:
the owner stamp, the writers list, the no-silent-overwrite rule, the taint
refusal. The same allowlist shape governs them: a write lands only under a
root in :data:`WRITABLE_ROOTS`, store roots are denied first, and anything the
fence cannot resolve refuses. The list is EMPTY on purpose. No skill a
record-keeping seat runs writes a file (the open-house skill says outright that
``write_file`` is not available), so the honest allowlist is none, and a
record changes only through ``record_store_write``. A V4A ``patch`` carries its
paths inside the patch text; it is refused rather than parsed. A seat that
someday needs a scratch directory adds it to :data:`WRITABLE_ROOTS` here, in
review, and the store-first denial still holds inside it.

FAIL-CLOSED. A path that cannot be resolved, a read or write with no path, a
hook fault: all refuse.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

#: Tools whose arguments name a file to read, or run code that can.
CODE_TOOLS = frozenset({"terminal", "execute_code"})
WRITE_TOOLS = frozenset({"write_file", "patch"})
FENCED_TOOLS = frozenset({"read_file", "search_files", "skill_view"}) | CODE_TOOLS | WRITE_TOOLS

#: Where a file tool may write on a seat that keeps records. Empty: see the
#: module docstring, THE WRITE SIDE.
WRITABLE_ROOTS: tuple[str, ...] = ()

#: The image's and the volume's skill roots. Mirrors ``SKILL_ROOTS`` in
#: ``hermes-smd-initiation/skill_fence.py`` (plugins share no import path).
SKILL_ROOTS: tuple[str, ...] = ("/app/skills", "/opt/data/skills")
SPEC_DIR_ENV = "SMD_SPEC_DIR"
_PATH_KEYS = ("path", "file_path", "filename", "directory", "dir")
_SEARCH_GLOB_KEYS = ("pattern", "file_glob")

#: Spillover files this process has seen named for a session's tool calls,
#: bounded so a long-lived gateway cannot grow it without limit.
_MAX_SESSIONS = 256
_MAX_CALLS_PER_SESSION = 512

REFUSAL = (
    "could not read that path: on this seat a file tool reads only the seat's skills "
    "and specs. Records are opened with record_store_list and record_store_read, which "
    "apply each store's owner rules. Say plainly that it could not be read, and never "
    "guess at what it holds."
)
WRITE_REFUSAL = (
    "could not write that file: on this seat a file tool writes nowhere. A record is kept "
    "or changed only with record_store_write, which applies the store's owner rules. Say "
    "plainly that the file was not written."
)
CODE_REFUSAL = (
    "terminal and execute_code are not available on this seat: it keeps private records, "
    "and a command cannot be shown to stay out of them. Use the seat's own tools "
    "(record_store_list, record_store_read, record_store_write)."
)


class _SpilloverLedger:
    """``{session key: {spillover file name}}`` for this session's tool calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: OrderedDict[str, OrderedDict[str, None]] = OrderedDict()

    def note(self, session_key: str, file_name: str) -> None:
        if not session_key or not file_name:
            return
        with self._lock:
            names = self._by_session.pop(session_key, None) or OrderedDict()
            names[file_name] = None
            while len(names) > _MAX_CALLS_PER_SESSION:
                names.popitem(last=False)
            self._by_session[session_key] = names
            while len(self._by_session) > _MAX_SESSIONS:
                self._by_session.popitem(last=False)

    def holds(self, session_key: str, file_name: str) -> bool:
        if not session_key:
            return False
        with self._lock:
            return file_name in self._by_session.get(session_key, ())

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._by_session.clear()


SPILLOVER = _SpilloverLedger()


def _session_key(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _hermes_spillover() -> tuple[str, Callable[[str], str]] | None:
    """Hermes' spillover dir and file-name rule, or ``None`` when unimportable
    (then no spillover file is readable: fail closed)."""
    try:
        from tools.tool_result_storage import _safe_result_filename, get_spillover_dir
    except Exception:  # noqa: BLE001 - outside a Hermes process there is no spillover
        return None
    return os.path.realpath(str(get_spillover_dir())), _safe_result_filename


def _hermes_resolve(path: str, task_id: str) -> str | None:
    """Where Hermes' own file tools would resolve ``path`` for this task."""
    try:
        from tools.file_tools_paths import _resolve_path_for_task
    except Exception:  # noqa: BLE001 - outside a Hermes process
        return None
    try:
        return os.path.realpath(str(_resolve_path_for_task(path, task_id or "default")))
    except Exception:  # noqa: BLE001 - unresolvable reads as unresolvable, never as fine
        return ""


def _candidates(path: str, task_id: str, base: str | None = None) -> list[str]:
    """Every place ``path`` could resolve to. A relative path is resolved both
    the way Hermes resolves it for the task and against the process cwd (or a
    tool ``workdir``); all must be allowed. ``""`` marks an unresolvable one."""
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return [os.path.realpath(expanded)]
    out = [os.path.realpath(os.path.join(base or os.getcwd(), expanded))]
    hermes = _hermes_resolve(path, task_id)
    if hermes is not None:
        out.append(hermes)
    return out


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def readable_roots() -> list[str]:
    """The directories a file tool may read under, resolved."""
    roots = list(SKILL_ROOTS)
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        roots.append(os.path.join(home, "skills"))
    spec = os.environ.get(SPEC_DIR_ENV, "").strip()
    if spec:
        roots.append(spec)
    out: list[str] = []
    for root in roots:
        for form in (root, os.path.realpath(root)):
            if form not in out:
                out.append(form)
    return out


def _path_problem(
    resolved: str, store_roots: Iterable[str], session_key: str, *, is_search: bool
) -> bool:
    """True when ``resolved`` may not be read."""
    if not resolved or not os.path.isabs(resolved):
        return True
    for store in store_roots:
        for form in (store, os.path.realpath(store)):
            # A store, anything in it, and (for a search) anything that holds it.
            if _under(resolved, form) or (is_search and _under(form, resolved)):
                return True
    if any(_under(resolved, root) for root in readable_roots()):
        return False
    spill = _hermes_spillover()
    if spill is not None and not is_search:
        spill_dir, _ = spill
        if os.path.dirname(resolved) == spill_dir:
            return not SPILLOVER.holds(session_key, os.path.basename(resolved))
    return True


def _skill_view_problem(args: dict[str, Any]) -> bool:
    for key in ("name", "skill", "slug"):
        value = args.get(key)
        if isinstance(value, str) and (".." in value or "/" in value or "\\" in value):
            return True
    file_path = args.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        parts = re.split(r"[/\\]+", file_path.strip())
        if os.path.isabs(file_path.strip()) or file_path.strip().startswith("~") or ".." in parts:
            return True
    return False


def _write_problem(
    tool_name: str, args: dict[str, Any], store_roots: list[str], kwargs: dict[str, Any]
) -> bool:
    """True when this write may not land: no path, a V4A patch, or any place
    the path could resolve to that is in a store or outside every writable root."""
    if tool_name == "patch" and (
        str(args.get("mode") or "replace") != "replace"
        or (isinstance(args.get("patch"), str) and args["patch"].strip())
    ):
        return True  # a V4A patch names its files inside the text: refused, not parsed
    paths = [
        args[k].strip() for k in _PATH_KEYS if isinstance(args.get(k), str) and args[k].strip()
    ]
    if not paths:
        return True
    task_id = str(kwargs.get("task_id") or "")
    for path in paths:
        for resolved in _candidates(path, task_id):
            if not resolved or not os.path.isabs(resolved):
                return True
            for store in store_roots:
                if any(_under(resolved, form) for form in (store, os.path.realpath(store))):
                    return True
            writable = [form for root in WRITABLE_ROOTS for form in (root, os.path.realpath(root))]
            if not any(_under(resolved, root) for root in writable):
                return True
    return False


def check(
    tool_name: str, args: Any, store_roots: Iterable[str], kwargs: dict[str, Any]
) -> str | None:
    """The refusal sentence, or ``None`` when the call may proceed."""
    if tool_name not in FENCED_TOOLS:
        return None
    if tool_name in CODE_TOOLS:
        return CODE_REFUSAL
    if not isinstance(args, dict):
        return REFUSAL
    if tool_name == "skill_view":
        return REFUSAL if _skill_view_problem(args) else None
    if tool_name in WRITE_TOOLS:
        return WRITE_REFUSAL if _write_problem(tool_name, args, list(store_roots), kwargs) else None
    roots = list(store_roots)
    task_id = str(kwargs.get("task_id") or "")
    session_key = _session_key(kwargs)
    is_search = tool_name == "search_files"
    paths = [
        args[k].strip() for k in _PATH_KEYS if isinstance(args.get(k), str) and args[k].strip()
    ]
    if is_search:
        for key in _SEARCH_GLOB_KEYS:
            value = args.get(key)
            if isinstance(value, str) and ("/" in value or "\\" in value or value.startswith("~")):
                # A glob that names a directory can reach past ``path``.
                return REFUSAL
        if not paths:
            paths = ["."]  # Hermes searches the working directory
    elif not paths:
        return REFUSAL  # a read that names nothing cannot be shown to be allowed
    for path in paths:
        for resolved in _candidates(path, task_id):
            if _path_problem(resolved, roots, session_key, is_search=is_search):
                return REFUSAL
    return None


def make_hooks(
    store_roots: Callable[[], Iterable[str]],
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """``(pre_tool_call, post_tool_call)`` for a seat whose store roots
    ``store_roots()`` returns. Raising there refuses (fail closed)."""

    def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name not in FENCED_TOOLS:
            return None
        try:
            message = check(tool_name, kwargs.get("args"), [str(r) for r in store_roots()], kwargs)
        except Exception:  # noqa: BLE001 - a fence that cannot decide refuses
            logger.exception(
                "hermes-smd-record-store: path fence could not decide; refusing %s", tool_name
            )
            return {"action": "block", "message": REFUSAL}
        if message is None:
            return None
        logger.info("hermes-smd-record-store: path fence refused %s", tool_name)
        return {"action": "block", "message": message}

    def on_post_tool_call(**kwargs: Any) -> None:
        try:
            call_id = str(kwargs.get("tool_call_id") or "")
            spill = _hermes_spillover()
            if call_id and spill is not None:
                SPILLOVER.note(_session_key(kwargs), spill[1](call_id))
        except Exception:  # noqa: BLE001 - an observer never raises
            logger.debug("hermes-smd-record-store: spillover note failed", exc_info=True)

    return on_pre_tool_call, on_post_tool_call


__all__ = [
    "CODE_REFUSAL",
    "CODE_TOOLS",
    "WRITABLE_ROOTS",
    "WRITE_REFUSAL",
    "WRITE_TOOLS",
    "FENCED_TOOLS",
    "REFUSAL",
    "SKILL_ROOTS",
    "SPILLOVER",
    "check",
    "make_hooks",
    "readable_roots",
]
