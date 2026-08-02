"""Establishment-intake entry point — ``python -m establish_intake``.

Launched by the Machine entrypoint as ROOT (console PR C0 adds the launch,
gated on ``import establish_intake`` so a lagging overlay degrades to a loud
"not launched" rather than a broken boot). One mode: the spool poll loop.
There is no ``--once`` boot fetch here — unlike ``spec_applier``, nothing at
boot depends on this daemon having run; an establishment run submitted while
the daemon was down simply waits in the spool.

A SILENT NOT-LAUNCHED MUST BE LOUD (design amendment point 6). Two surfaces:

* the BOOT LINE — one unmistakable log line on launch, asserted by the
  rehearsal pre-flight (``establish_intake: LAUNCHED ...``), including the
  compiler-presence verdict so a degraded daemon announces itself in the same
  breath it starts;
* the HEARTBEAT FILE — ``<spool>/intake-heartbeat.json``, atomically rewritten
  every tick with pid, started_at, last_poll_at, runs_processed, and the
  degraded state. World-readable by design: it carries liveness only, never
  content, and a probe (seat-probe.sh, the rehearsal pre-flight) must be able
  to read it without root. Projection into the control-plane heartbeat payload
  is a console-side follow-on; the file is the on-box source of truth either way.

DEGRADED, NOT DEAD, when the compilers are absent: the daemon stays up, logs
ERROR, marks the heartbeat file, and answers every submitted run with a
terminal error result (``intake.process_run`` refuses gate-less processing) —
the agent polling ``establish_status`` gets a truthful answer instead of a
forever-pending run, and no gate is ever skipped (Law 12: a run the gates could
not examine must not read as one they passed).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from config_applier.applier import atomic_write
from establish_intake import gates
from establish_intake.intake import EstablishIntake
from shared.ids import iso_utc

logger = logging.getLogger(__name__)

_ENV_R2_ENDPOINT = "R2_ENDPOINT_URL"
_ENV_R2_KEY_ID = "R2_ACCESS_KEY_ID"
_ENV_R2_SECRET = "R2_SECRET_ACCESS_KEY"
_ENV_R2_BUCKET = "R2_BUCKET_CONFIG"
_ENV_SLUG = "CUSTOMER_SLUG"
_ENV_SPEC_DIR = "SMD_SPEC_DIR"
_ENV_SPOOL_DIR = "SMD_ESTABLISH_SPOOL_DIR"
_ENV_POLL_SECONDS = "SMD_ESTABLISH_POLL_SECONDS"

_DEFAULT_SPEC_DIR = "/var/lib/smd-config/specs"
#: NOT under ``/opt/data``. The Hermes gateway chmods its home (``/opt/data``)
#: to 0700 MID-BOOT, which strips every group-traverse the entrypoint granted
#: before it — so a spool under that tree is reachable by root (which ignores
#: modes) and unreachable by the workspace-broker uid, which is the principal
#: that must create staging sets and run dirs. The failure is invisible from
#: the spool's own permissions: the dirs read 0770 root:workspace-broker and
#: are correct; the ANCESTOR is what severs them. Live-caught on
#: hermes-pilot-smokeball 2026-08-02, first establishment call:
#: ``PermissionError: '/opt/data/establish-spool/staging'`` with the leaf at
#: 0770 and ``/opt/data`` at 0700 hermes. The audit ledger solved the same
#: problem with a bind mount (entrypoint.sh); the spool is transient (30-min
#: TTL, runs are short-lived) so it simply lives outside the agent's home, the
#: same place the broker's other state lives (``/var/lib/smd-*``).
_DEFAULT_SPOOL_DIR = "/var/lib/smd-establish-spool"
_DEFAULT_POLL_SECONDS = 5.0

HEARTBEAT_BASENAME = "intake-heartbeat.json"


def _build_s3_client() -> Any:
    """boto3 R2 client from env — byte-for-byte the spec_applier wiring."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ[_ENV_R2_ENDPOINT],
        aws_access_key_id=os.environ[_ENV_R2_KEY_ID],
        aws_secret_access_key=os.environ[_ENV_R2_SECRET],
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"establish_intake: required env var {name} is unset")
    return value


def _poll_seconds() -> float:
    raw = os.environ.get(_ENV_POLL_SECONDS)
    if not raw:
        return _DEFAULT_POLL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "establish_intake: %s=%r not a number; using default %ss",
            _ENV_POLL_SECONDS,
            raw,
            _DEFAULT_POLL_SECONDS,
        )
        return _DEFAULT_POLL_SECONDS
    return value if value >= 1.0 else _DEFAULT_POLL_SECONDS


def write_heartbeat(
    spool_dir: Path,
    *,
    started_at: str,
    runs_processed: int,
    missing: list[str],
) -> None:
    """Atomically rewrite the liveness file. Never raises — a heartbeat fault
    must not take the daemon down with it (the daemon IS the thing the
    heartbeat reports on)."""
    try:
        payload = {
            "pid": os.getpid(),
            "started_at": started_at,
            "last_poll_at": iso_utc(),
            "runs_processed": runs_processed,
            "degraded": "compilers_missing" if missing else None,
            "missing_compilers": missing,
        }
        path = spool_dir / HEARTBEAT_BASENAME
        atomic_write(path, json.dumps(payload, sort_keys=True).encode() + b"\n")
        try:
            os.chmod(path, 0o644)  # liveness only, world-readable by design
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        logger.warning("establish_intake: heartbeat write failed", exc_info=True)


def build_intake() -> EstablishIntake:
    """Construct the intake wired to real R2 + volume paths from env."""
    from shared.customer_config import CustomerConfig

    return EstablishIntake(
        spool_dir=Path(os.environ.get(_ENV_SPOOL_DIR) or _DEFAULT_SPOOL_DIR),
        s3_client=_build_s3_client(),
        bucket=_require_env(_ENV_R2_BUCKET),
        slug=_require_env(_ENV_SLUG),
        spec_dir=Path(os.environ.get(_ENV_SPEC_DIR) or _DEFAULT_SPEC_DIR),
        customer_config_fn=CustomerConfig.from_volume,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("SMD_ESTABLISH_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        intake = build_intake()
    except SystemExit as exc:
        logger.error("establish_intake: startup failed: %s", exc)
        return 2

    started_at = iso_utc()
    missing = gates.missing_compilers()
    # THE BOOT LINE. The rehearsal pre-flight greps for exactly this prefix; a
    # seat where it never printed is a seat where establishment silently does
    # not exist, which is the failure mode this line makes impossible to miss.
    logger.info(
        "establish_intake: LAUNCHED (slug=%s spool=%s spec_dir=%s poll=%ss compilers=%s)",
        intake.slug,
        intake.spool_dir,
        intake.spec_dir,
        _poll_seconds(),
        "ok" if not missing else f"MISSING:{','.join(missing)}",
    )
    if missing:
        logger.error(
            "establish_intake: DEGRADED at launch — compiler(s) absent, every run "
            "will be refused until the image ships them: %s",
            missing,
        )
    write_heartbeat(intake.spool_dir, started_at=started_at, runs_processed=0, missing=missing)

    stopped = {"flag": False}

    def _request_stop(signum: int, _frame: Any) -> None:
        logger.info("establish_intake: received signal %s; requesting clean stop", signum)
        stopped["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    poll_seconds = _poll_seconds()
    runs_processed = 0
    while not stopped["flag"]:
        try:
            runs_processed += len(intake.poll_once())
        except Exception:  # noqa: BLE001 — the loop must outlive any single tick
            logger.exception("establish_intake: unexpected error in poll tick; continuing")
        missing = gates.missing_compilers()
        write_heartbeat(
            intake.spool_dir,
            started_at=started_at,
            runs_processed=runs_processed,
            missing=missing,
        )
        slept = 0.0
        while slept < poll_seconds and not stopped["flag"]:
            time.sleep(min(1.0, poll_seconds - slept))
            slept += 1.0
    logger.info("establish_intake: stop requested; poll loop exiting cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
