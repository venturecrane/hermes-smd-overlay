"""Every root poll loop exits 130 on SIGTERM, never 0.

ss-console's entrypoint respawns an applier that exits; a stopping Machine
must not have its appliers restarted under it. 130 is how a signalled stop
tells the respawn loop apart from a crash.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

import config_applier.__main__ as config_main
import establish_intake.__main__ as intake_main
import spec_applier.__main__ as spec_main


class _SignalLoop:
    def run_forever(self, stop) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        while not stop():
            pass


def test_config_applier_exits_130_on_sigterm(monkeypatch):
    monkeypatch.setattr(config_main, "build_loop", lambda: _SignalLoop())
    assert config_main.main() == 130


def test_spec_applier_exits_130_on_sigterm(monkeypatch):
    monkeypatch.setattr(spec_main, "build_loop", lambda: _SignalLoop())
    assert spec_main.main([]) == 130


def test_establish_intake_exits_130_on_sigterm(monkeypatch, tmp_path):
    class _Intake:
        slug = "acme"
        spool_dir = Path(tmp_path)
        spec_dir = Path(tmp_path)

        def poll_once(self):
            os.kill(os.getpid(), signal.SIGTERM)
            return []

    monkeypatch.setattr(intake_main, "build_intake", lambda: _Intake())
    monkeypatch.setattr(intake_main.gates, "missing_compilers", lambda: [])
    monkeypatch.setattr(intake_main, "write_heartbeat", lambda *a, **k: None)
    assert intake_main.main([]) == 130
