"""Every action type any writer emits is declared in ACCEPTED_ACTION_TYPES.

The failure class this guard closes (found 2026-08-02, ss-console #2122): the
repo has TWO writer layers and only one validates. ``AuditLogWriter.write``
checks membership; ``shared.audit_contract.agent_event_params`` + a raw
``execute(INSERT_SQL, ...)`` accepts any string. Eight action types were
written to live ledgers for weeks while absent from the vocabulary — parity
and membership tests all passed, because every one was hand-added and none
enumerated the source.

Mechanics: AST-walk every non-test ``.py`` in the writer surfaces and collect

  * every string literal passed as an ``action_type=`` keyword,
  * every string literal assigned to a bare ``action_type`` variable or
    passed positionally to ``_emit_confirm_event`` (the one indirect writer
    whose literals travel as positional args).

Assert the collected set is a subset of ``ACCEPTED_ACTION_TYPES``. The guard
deliberately over-collects (a literal in a docstring-adjacent helper that
never reaches a ledger still must be declared) — an over-declared vocabulary
is harmless; an under-declared one hides rows from every consumer that
filters by the declared set.

Law 12 control: the scanner is proven able to fail — a synthetic module with
an undeclared type must be flagged by the same collector the real scan uses.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent

# The writer surfaces: everywhere a row can originate in this repo.
SCAN_DIRS = ["plugins", "shared", "hooks", "bootstrap", "config_applier"]
SCAN_FILES = ["webhook_gate.py"]

# Functions whose positional string args carry an action type.
_POSITIONAL_CARRIERS = {"_emit_confirm_event"}


def _accepted() -> frozenset[str]:
    import importlib.util
    import sys

    init_path = ROOT / "plugins" / "hermes-smd-audit" / "schemas.py"
    spec = importlib.util.spec_from_file_location("audit_schemas_for_guard", init_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_schemas_for_guard"] = module
    spec.loader.exec_module(module)
    return module.ACCEPTED_ACTION_TYPES


def _collect_from_source(source: str) -> set[str]:
    """Collect action-type string literals from one module's source."""
    found: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "action_type" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found.add(kw.value.value)
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _POSITIONAL_CARRIERS:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "action_type" in targets and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    found.add(node.value.value)
    return found


def _scan_paths() -> list[Path]:
    paths: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if base.is_dir():
            paths.extend(p for p in base.rglob("*.py") if "test" not in p.name)
    for f in SCAN_FILES:
        p = ROOT / f
        if p.is_file():
            paths.append(p)
    return paths


def test_every_written_action_type_is_declared():
    accepted = _accepted()
    undeclared: dict[str, set[str]] = {}
    scanned = 0
    for path in _scan_paths():
        scanned += 1
        try:
            found = _collect_from_source(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a broken source file is its own failure
            raise AssertionError(f"unparseable writer surface {path}: {exc}") from exc
        extra = {t for t in found if t not in accepted and t.isupper()}
        if extra:
            undeclared[str(path.relative_to(ROOT))] = extra
    assert scanned > 20, f"scan surface collapsed ({scanned} files) — guard is not guarding"
    assert not undeclared, (
        "action types written but absent from ACCEPTED_ACTION_TYPES "
        f"(declare them in plugins/hermes-smd-audit/schemas.py): {undeclared}"
    )


def test_scanner_catches_an_undeclared_type():
    """FALSE CONTROL (Law 12): the collector must flag a type we know is not
    declared — otherwise the green above measures nothing."""
    src = (
        "from shared.audit_contract import agent_event_params\n"
        "params = agent_event_params(action_type='TOTALLY_UNDECLARED_TYPE', metadata={})\n"
    )
    found = _collect_from_source(src)
    assert "TOTALLY_UNDECLARED_TYPE" in found
    assert "TOTALLY_UNDECLARED_TYPE" not in _accepted()


def test_scanner_sees_all_three_carrier_shapes():
    src = (
        "action_type = 'ASSIGNED_SHAPE'\n"
        "f(action_type='KEYWORD_SHAPE')\n"
        "_emit_confirm_event('POSITIONAL_SHAPE', {})\n"
    )
    assert _collect_from_source(src) == {
        "ASSIGNED_SHAPE",
        "KEYWORD_SHAPE",
        "POSITIONAL_SHAPE",
    }


def test_known_previously_undeclared_types_are_now_declared():
    """The eight types found live on both seats 2026-08-02 + the broker-side
    correction type: pinned so a future vocabulary refactor cannot silently
    drop them while their producers keep writing."""
    accepted = _accepted()
    for t in (
        "REPLY_SENT",
        "REPLY_HELD",
        "REPLY_FAILED",
        "CONFIRM_SEND_DISPATCHED",
        "CONFIRM_SEND_FAILED",
        "SPEC_GATE_TRIGGERED",
        "VOICE_GATE_TRIGGERED",
        "WEBHOOK_SUPPRESSED",
        "CORRECTION_PROPOSED",
    ):
        assert t in accepted, t


# ---------------------------------------------------------------------------
# The JOIN vocabulary (ss-console#2497)
#
# Same failure class as the action-type gap above, one layer down. An action
# type absent from the vocabulary hides a whole row class from every consumer
# that filters by it; a metadata KEY spelled two ways hides half the rows from
# a join. #2312 is the precedent: the per-tool builder wrote ``trace_id`` where
# six other emitters wrote ``tool_call_id``, and one query silently missed one
# side or the other for weeks. These names are the joins the ledger is now sold
# on, and they are written by six emitters across two repos.
# ---------------------------------------------------------------------------


def test_the_join_keys_are_declared_in_the_contract():
    from shared.audit_contract import JOIN_KEYS

    assert set(JOIN_KEYS) == {
        "sender_key",
        "vendor_message_id",
        "session_id",
        "matter_ref",
        "document_id",
        "memo_id",
        "draft_id",
        "written_body_sha256",
    }


def test_build_per_tool_metadata_documents_every_key_it_can_emit():
    """The docstring is the contract a consumer reads (the AC names it), so a key
    the builder can write and the docstring does not list is a field nobody
    downstream knows exists.

    FALSIFIER: delete one of the object-identity paragraphs from that docstring
    and this fails; delete the key from the extractor instead and the
    object-identity tests fail. Neither half can drift alone.
    """
    emit_path = ROOT / "plugins" / "hermes-smd-audit" / "emit.py"
    tree = ast.parse(emit_path.read_text(encoding="utf-8"))
    doc = next(
        (
            ast.get_docstring(node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "build_per_tool_metadata"
        ),
        None,
    )
    assert doc is not None, "build_per_tool_metadata not found in emit.py"
    # The DOCUMENTED key list is the bullet names, not any substring of the
    # prose: "seam" appears in that docstring as ordinary English ("the
    # pre-to-post seam"), so a substring check would pass on a docstring that
    # never names the field. Match the bullet form the docstring already uses.
    documented = set(re.findall(r"^\s*- (\w+):", doc, re.MULTILINE))
    required = {
        "document_id",
        "document_ids",
        "memo_id",
        "draft_id",
        "written_body_sha256",
        "written_body_field",
        "seam",
    }
    assert required <= documented, f"emitted but undocumented: {sorted(required - documented)}"
