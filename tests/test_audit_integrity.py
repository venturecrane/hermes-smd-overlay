"""Tests for plugins/hermes-smd-audit/integrity.py.

Ported from ss-console/ai-employee/adapter/tests/test_audit_log_integrity.py.

Exercises the D1 vs Logpush mirror integrity comparison against fake
in-memory loaders.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path


def load_plugin(plugin_name: str):
    """Load the plugin package so submodule imports inside ``__init__.py`` resolve.

    See test_audit_emit.py for the explanation; the shared conftest loader
    doesn't pre-register the parent module in ``sys.modules``.
    """
    root = Path(__file__).parent.parent
    init_path = root / "plugins" / plugin_name / "__init__.py"
    sanitized = plugin_name.replace("-", "_")
    mod_name = f"plugin_{sanitized}"
    spec = importlib.util.spec_from_file_location(mod_name, init_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin spec for {plugin_name!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _row_factory(mod):
    """Build an AuditRow with sensible defaults."""

    def _row(
        id: str,
        ts: str,
        *,
        action_type: str = "DRAFT_CREATED",
        actor: str = "agent",
        actor_role: str = "agent",
        skill_name: str = "inbox-triage",
        matter_ref: str = "matter-123",
        input_digest: str = "abc",
        output_digest: str = "def",
        diff_digest=None,
        trust_ceiling: str = "draft_for_review",
        metadata: str = '{"k":1}',
    ):
        return mod.integrity.AuditRow(
            id=id,
            ts=ts,
            action_type=action_type,
            actor=actor,
            actor_role=actor_role,
            skill_name=skill_name,
            matter_ref=matter_ref,
            input_digest=input_digest,
            output_digest=output_digest,
            diff_digest=diff_digest,
            trust_ceiling=trust_ceiling,
            metadata=metadata,
        )

    return _row


class _FakeLoader:
    """Test loader yielding a fixed list of rows."""

    def __init__(self, rows) -> None:
        self._rows = rows

    def load(self, start_ts: str, end_ts: str) -> Iterator:  # noqa: ARG002
        return iter(self._rows)


class _BrokenLoader:
    """Loader whose load() raises when iterated."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def load(self, start_ts: str, end_ts: str):  # noqa: ARG002
        exc = self._exc

        def _gen():
            raise exc
            yield  # pragma: no cover

        return _gen()


# Fixed reference timestamp used across tests (well outside the 5-min
# mirror-lag grace window so "old" rows don't accidentally get the grace).
_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_NOW_TS = "2026-05-21T12:00:00.000Z"
_OLD_TS = "2026-05-20T12:00:00.000Z"  # 24h before _NOW


# ---------------------------------------------------------------------------
# Clean path
# ---------------------------------------------------------------------------


def test_clean_when_d1_and_mirror_match() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    rows = [row("01A", _OLD_TS), row("01B", _OLD_TS)]
    d1 = _FakeLoader(list(rows))
    mirror = _FakeLoader(list(rows))
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.clean is True
    assert report.findings == []
    assert report.d1_rows_checked == 2
    assert report.mirror_rows_checked == 2


def test_clean_when_both_empty() -> None:
    mod = load_plugin("hermes-smd-audit")
    report = mod.integrity.check_audit_integrity(
        _FakeLoader([]),
        _FakeLoader([]),
        start_ts=_OLD_TS,
        end_ts=_NOW_TS,
        now=lambda: _NOW,
    )
    assert report.clean is True
    assert report.d1_rows_checked == 0
    assert report.mirror_rows_checked == 0


# ---------------------------------------------------------------------------
# IN_D1_NOT_IN_MIRROR
# ---------------------------------------------------------------------------


def test_in_d1_not_in_mirror_old_row_is_a_finding() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1 = _FakeLoader([row("01A", _OLD_TS), row("01B", _OLD_TS)])
    mirror = _FakeLoader([row("01A", _OLD_TS)])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.clean is False
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.kind == mod.integrity.FindingKind.IN_D1_NOT_IN_MIRROR
    assert finding.row_id == "01B"


def test_in_d1_not_in_mirror_recent_row_within_grace_is_skipped() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    # Row's ts is "now" exactly — well within the 5-minute lag grace.
    d1 = _FakeLoader([row("01A", _NOW_TS)])
    mirror = _FakeLoader([])  # mirror hasn't caught up yet
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.clean is True
    assert report.findings == []


def test_in_d1_not_in_mirror_just_outside_grace_is_a_finding() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    six_min_ago = _NOW - timedelta(minutes=6)
    six_min_ago_ts = (
        six_min_ago.strftime("%Y-%m-%dT%H:%M:%S.") + f"{six_min_ago.microsecond // 1000:03d}Z"
    )
    d1 = _FakeLoader([row("01A", six_min_ago_ts)])
    mirror = _FakeLoader([])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert len(report.findings) == 1
    assert report.findings[0].kind == mod.integrity.FindingKind.IN_D1_NOT_IN_MIRROR


def test_in_d1_not_in_mirror_unparseable_ts_does_not_grant_grace() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1 = _FakeLoader([row("01A", "garbage timestamp")])
    mirror = _FakeLoader([])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert len(report.findings) == 1
    assert report.findings[0].kind == mod.integrity.FindingKind.IN_D1_NOT_IN_MIRROR


# ---------------------------------------------------------------------------
# IN_MIRROR_NOT_IN_D1
# ---------------------------------------------------------------------------


def test_in_mirror_not_in_d1_always_a_finding() -> None:
    """The substrate's load-bearing case: D1 row disappeared but mirror has it."""
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1 = _FakeLoader([])
    mirror = _FakeLoader([row("01A", _OLD_TS)])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert len(report.findings) == 1
    assert report.findings[0].kind == mod.integrity.FindingKind.IN_MIRROR_NOT_IN_D1
    assert report.findings[0].row_id == "01A"


# ---------------------------------------------------------------------------
# DIGEST_MISMATCH
# ---------------------------------------------------------------------------


def test_digest_mismatch_when_load_bearing_column_differs() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1_row = row("01A", _OLD_TS, input_digest="aaa")
    mirror_row = row("01A", _OLD_TS, input_digest="bbb")
    d1 = _FakeLoader([d1_row])
    mirror = _FakeLoader([mirror_row])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert len(report.findings) == 1
    assert report.findings[0].kind == mod.integrity.FindingKind.DIGEST_MISMATCH
    assert report.findings[0].row_id == "01A"


def test_metadata_alone_does_not_drive_a_mismatch() -> None:
    """Metadata is excluded from compare_key on purpose."""
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1_row = row("01A", _OLD_TS, metadata='{"k":1}')
    mirror_row = row("01A", _OLD_TS, metadata='{"k":2}')
    d1 = _FakeLoader([d1_row])
    mirror = _FakeLoader([mirror_row])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.findings == []
    assert report.clean is True


# ---------------------------------------------------------------------------
# Multi-finding
# ---------------------------------------------------------------------------


def test_report_collects_multiple_finding_kinds() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1 = _FakeLoader(
        [
            row("01A", _OLD_TS),  # match
            row("01B", _OLD_TS),  # only-in-d1
            row("01C", _OLD_TS, input_digest="aaa"),  # mismatch
        ]
    )
    mirror = _FakeLoader(
        [
            row("01A", _OLD_TS),
            row("01C", _OLD_TS, input_digest="bbb"),
            row("01D", _OLD_TS),  # only-in-mirror
        ]
    )
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    kinds = {f.kind for f in report.findings}
    assert kinds == {
        mod.integrity.FindingKind.IN_D1_NOT_IN_MIRROR,
        mod.integrity.FindingKind.IN_MIRROR_NOT_IN_D1,
        mod.integrity.FindingKind.DIGEST_MISMATCH,
    }
    assert len(report.findings) == 3


# ---------------------------------------------------------------------------
# Loader failures surface, no exception escapes
# ---------------------------------------------------------------------------


def test_loader_failure_surfaces_via_report() -> None:
    mod = load_plugin("hermes-smd-audit")
    d1 = _BrokenLoader(RuntimeError("d1 down"))
    mirror = _FakeLoader([])
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.clean is False
    assert report.findings == []
    assert "d1 down" in (report.loader_error or "")


def test_mirror_loader_failure_also_surfaces() -> None:
    mod = load_plugin("hermes-smd-audit")
    row = _row_factory(mod)
    d1 = _FakeLoader([row("01A", _OLD_TS)])
    mirror = _BrokenLoader(OSError("r2 timeout"))
    report = mod.integrity.check_audit_integrity(
        d1, mirror, start_ts=_OLD_TS, end_ts=_NOW_TS, now=lambda: _NOW
    )
    assert report.clean is False
    assert "r2 timeout" in (report.loader_error or "")


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


def test_report_initial_state_is_clean_and_empty() -> None:
    mod = load_plugin("hermes-smd-audit")
    report = mod.integrity.IntegrityReport()
    assert report.clean is True
    assert report.findings == []
    assert report.d1_rows_checked == 0
    assert report.mirror_rows_checked == 0
    assert report.loader_error is None
