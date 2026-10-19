"""What a seat is running and how busy it is, for the fleet table (ss seat fleet scaling).

Six heartbeat fields, read fresh on every beat by :func:`read_fleet_facts`:

* ``image_ref`` — the Fly image the Machine booted (``FLY_IMAGE_REF``, which Fly
  sets on every Machine). A rollout's read-back compares this to the release it
  deployed; nothing else on the seat can say which image is actually running.
* ``config_digest`` — sha256 of the root-owned ``customer.yaml`` the seat obeys,
  so the console can tell which published config a seat has adopted.
* ``skills_version`` — the skills release the root ``skills_applier`` committed
  (``/var/lib/smd-skills/current/VERSION``).
* ``jobs_running`` / ``jobs_queued`` / ``wakes_pending`` — sums over every lane's
  heartbeat file. The rollout's busy gate reads these before it restarts a seat.

NULL IS UNKNOWN, ZERO IS NONE. Every reader returns ``None`` when it cannot
look, and the heartbeat omits a ``None`` field (the receiver stores absence as
NULL). For the job counts this is the whole design: a lane whose heartbeat file
is present but stale (its daemon is wedged, or a long job has stopped its
beats) makes all three sums unknown, never 0, because a 0 is what tells a
rollout it may restart the seat. A lane file that is ABSENT contributes 0: the
lane is not installed on this seat, and a lane that is not installed has no
jobs. A file that is present but unreadable or malformed is unknown, the same
as stale.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("hermes-smd-fleet-facts")

DEFAULT_CONFIG_PATH = "/var/lib/smd-config/customer.yaml"
DEFAULT_SKILLS_VERSION_PATH = "/var/lib/smd-skills/current/VERSION"
DEFAULT_LANE_RUN_DIR = "/run/smd-medchron"
#: Same env var the lanes read, so a seat that moves its run dir moves both.
LANE_RUN_DIR_ENV = "SMD_MEDCHRON_RUN_DIR"

#: Every lane heartbeat file the medchron runners write (ss-console
#: operator/runners/medchron/medchron/*_lane.py ``HEARTBEAT``), without ``.json``.
LANE_HEARTBEATS = (
    "heartbeat",
    "demand-heartbeat",
    "drafting-heartbeat",
    "litigation-heartbeat",
    "negotiation-heartbeat",
    "note-heartbeat",
)

#: A lane file older than this is stale. Lanes beat every poll (seconds) and the
#: negotiation/litigation lanes every 30 s while a job runs.
LANE_STALE_SECONDS = 180.0

#: Cap on any single string field, matching the receiver's own cap.
MAX_TEXT = 300


class FleetFacts(NamedTuple):
    image_ref: str | None
    config_digest: str | None
    skills_version: str | None
    jobs_running: int | None
    jobs_queued: int | None
    wakes_pending: int | None


class LaneCounts(NamedTuple):
    running: int | None
    queued: int | None
    wakes_pending: int | None


_UNKNOWN = LaneCounts(None, None, None)


def read_image_ref(env: dict[str, str] | None = None) -> str | None:
    value = (os.environ if env is None else env).get("FLY_IMAGE_REF", "").strip()
    return value[:MAX_TEXT] or None


def read_config_digest(path: str | Path = DEFAULT_CONFIG_PATH) -> str | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def read_skills_version(path: str | Path = DEFAULT_SKILLS_VERSION_PATH) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text[:MAX_TEXT] or None


def _count(value: object) -> int | None:
    """A non-negative count from a lane field; ``None`` when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def _running(value: object) -> int | None:
    """``running`` is a job id (str), ``None`` when idle, or a list of ids."""
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        return 1
    if isinstance(value, list):
        return len(value)
    return _count(value)


def _lane_counts(path: Path, now: float) -> LaneCounts:
    """One lane's counts; absent file = (0, 0, 0), stale or malformed = unknown."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return LaneCounts(0, 0, 0)
    except OSError:
        return _UNKNOWN
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _UNKNOWN
    if not isinstance(doc, dict):
        return _UNKNOWN
    polled = doc.get("last_poll_at")
    if isinstance(polled, bool) or not isinstance(polled, (int, float)):
        polled = st.st_mtime
    if now - float(polled) > LANE_STALE_SECONDS:
        return _UNKNOWN
    return LaneCounts(
        _running(doc.get("running")),
        _count(doc.get("queued")),
        _count(doc.get("wakes_pending")),
    )


def _sum(values: Iterable[int | None]) -> int | None:
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def read_lane_counts(
    run_dir: str | Path | None = None,
    *,
    names: Iterable[str] = LANE_HEARTBEATS,
    clock: Callable[[], float] = time.time,
) -> LaneCounts:
    """Sum running/queued/wakes_pending across every lane heartbeat file."""
    root = Path(run_dir or os.environ.get(LANE_RUN_DIR_ENV) or DEFAULT_LANE_RUN_DIR)
    now = clock()
    per_lane = [_lane_counts(root / f"{name}.json", now) for name in names]
    return LaneCounts(
        _sum(c.running for c in per_lane),
        _sum(c.queued for c in per_lane),
        _sum(c.wakes_pending for c in per_lane),
    )


def read_fleet_facts(
    *,
    env: dict[str, str] | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    skills_version_path: str | Path = DEFAULT_SKILLS_VERSION_PATH,
    run_dir: str | Path | None = None,
    clock: Callable[[], float] = time.time,
) -> FleetFacts:
    lanes = read_lane_counts(run_dir, clock=clock)
    return FleetFacts(
        image_ref=read_image_ref(env),
        config_digest=read_config_digest(config_path),
        skills_version=read_skills_version(skills_version_path),
        jobs_running=lanes.running,
        jobs_queued=lanes.queued,
        wakes_pending=lanes.wakes_pending,
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_LANE_RUN_DIR",
    "DEFAULT_SKILLS_VERSION_PATH",
    "LANE_HEARTBEATS",
    "LANE_STALE_SECONDS",
    "FleetFacts",
    "LaneCounts",
    "read_config_digest",
    "read_fleet_facts",
    "read_image_ref",
    "read_lane_counts",
    "read_skills_version",
]
