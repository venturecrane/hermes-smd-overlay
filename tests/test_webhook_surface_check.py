"""Tests for the gate-side warn-tier surface check (ss-console#2222).

The check reads a sentinel the AGENT process wrote at boot, because the resolved
tool surface only exists in that process while the heartbeat emitter runs in the
GATE. Everything worth testing here is about the three outcomes staying
distinguishable, since the whole reason this tier exists is that its failure is
non-fatal and therefore has to be VISIBLE somewhere else.

Each check names its falsifier:

* HOLD ON ABSENCE. No sentinel → ``None`` → both heartbeat fields omitted →
  the console holds. Falsifier: return ``ok=True`` on absence, which would
  resolve an open alert using the fact that we did not look.
* HOLD ON STALENESS. A sentinel written by a dead pid is a PREVIOUS boot's
  answer. Falsifier: serve it as current — a stale green would resolve an alert
  about a process that never wrote it.
* OUR BLINDNESS PAGES SEPARATELY. ``ok=False, tools=None`` is the check being
  broken, not a tool being missing; the two want opposite responses and identical
  emptiness would hide the second behind the first.
"""

from __future__ import annotations

import json

from shared import webhook_surface_check
from shared.webhook_read_surface import write_webhook_surface_status

_ALIVE = lambda _pid: True  # noqa: E731 — a one-expression injectable stand-in
_DEAD = lambda _pid: False  # noqa: E731


def test_absent_sentinel_holds(tmp_path):
    assert webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE) is None


def test_a_healthy_sentinel_reports_both_sides(tmp_path):
    write_webhook_surface_status(
        ok=True,
        tools={"operator_seat_facts": {"expected": True, "offered": True}},
        hermes_home=str(tmp_path),
    )
    result = webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE)
    assert result.ok is True
    assert result.tools == {"operator_seat_facts": {"expected": True, "offered": True}}


def test_a_missing_tool_is_reported_not_hidden(tmp_path):
    """The alertable state. ``ok`` stays True — the CHECK worked; it is the
    ``offered: False`` entry that is the finding."""
    write_webhook_surface_status(
        ok=True,
        tools={"operator_seat_facts": {"expected": True, "offered": False}},
        hermes_home=str(tmp_path),
    )
    result = webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE)
    assert result.ok is True
    assert result.tools["operator_seat_facts"]["offered"] is False


def test_a_broken_check_pages_rather_than_going_dark(tmp_path):
    write_webhook_surface_status(ok=False, tools=None, hermes_home=str(tmp_path))
    result = webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE)
    assert result.ok is False
    assert result.tools is None


def test_a_previous_boots_sentinel_is_held_not_served(tmp_path):
    """Falsifier for the staleness key: without it, a green written by a process
    that has since died would keep resolving alerts about the live one. A handler
    cannot sentinel its own non-execution, which is why liveness of the WRITER is
    the key rather than the file's age."""
    write_webhook_surface_status(
        ok=True,
        tools={"operator_seat_facts": {"expected": True, "offered": True}},
        hermes_home=str(tmp_path),
    )
    assert webhook_surface_check.check(str(tmp_path), pid_alive=_DEAD) is None


def test_a_sentinel_with_no_usable_pid_is_held(tmp_path):
    path = tmp_path / ".smd" / "webhook_surface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "smd.webhook_surface/1", "ok": True, "tools": {}, "pid": "nope"}),
        encoding="utf-8",
    )
    assert webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE) is None


def test_a_malformed_tools_map_is_our_blindness_not_an_all_clear(tmp_path):
    path = tmp_path / ".smd" / "webhook_surface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "smd.webhook_surface/1", "ok": True, "tools": "not-a-map", "pid": 1}),
        encoding="utf-8",
    )
    result = webhook_surface_check.check(str(tmp_path), pid_alive=_ALIVE)
    assert result.ok is False
    assert result.tools is None


def test_the_check_never_raises(monkeypatch, tmp_path):
    def _boom(*_a, **_kw):
        raise OSError("exploded")

    monkeypatch.setattr(
        webhook_surface_check.webhook_read_surface, "read_webhook_surface_status", _boom
    )
    result = webhook_surface_check.check(str(tmp_path))
    assert result.ok is False
    assert result.tools is None
