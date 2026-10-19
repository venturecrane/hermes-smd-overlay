"""Root skills-applier entry point (ss seat fleet scaling).

Launched by the Machine entrypoint as ROOT, the only principal that may write
``/var/lib/smd-skills``. Two modes:

* ``python -m skills_applier --once`` — the BOOT fetch, before the privilege
  drop. Installs and commits the pointed-at release with no volume copy;
  bootstrap.sh then runs ``python -m skills_applier.sync`` from ``current`` as
  hermes. Always exits 0: an un-adoptable release must not brick a boot, and
  the seat comes up on the skills it already had.
* ``python -m skills_applier`` — the poll loop (every ``SMD_SKILLS_POLL_SECONDS``,
  default 15). Per tick: HEAD the seat's own pointer; on a changed ETag stage
  the release, copy it onto the volume as hermes, and only then commit
  ``current``. Committing after the copy keeps ``current/VERSION`` (what the
  heartbeat reports as ``skills_version``) honest about what the agent can
  actually read: a copy that fails leaves ``current`` where it was, so a
  rollout's read-back sees the old version and rolls back. The ETag is cached
  only once a tick has finished with it, so a fault retries next tick.

SIGTERM / SIGINT exit 130, never 0, so the entrypoint's respawn loop can tell
"the Machine is stopping" from "the applier crashed".
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skills_applier.applier import (
    DEFAULT_CAPABILITIES,
    DEFAULT_IMAGE_SKILLS,
    DEFAULT_ROOT,
    RETIRED_FILE,
    SKILLS_SUBDIR,
    Outcome,
    PointerMissing,
    SkillsApplyError,
    SkillsApplyResult,
    commit,
    emit_skills_write,
    prune,
    stage,
)
from skills_applier.manifest import pointer_key

logger = logging.getLogger(__name__)

EXIT_STOPPED = 130

_DEFAULT_POLL_SECONDS = 15.0
_DEFAULT_HERMES_HOME = "/opt/data"
_DEFAULT_CUSTOMER_YAML = "/var/lib/smd-config/customer.yaml"
_IMAGE_RETIRED = "/app/retired-skills.txt"
_SYNC_TIMEOUT_SECONDS = 600
#: The only env the hermes-uid sync child sees. Never the R2 credentials.
_SYNC_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ", "HERMES_HOME", "SMD_SPEC_DIR")

SyncRunner = Callable[[list[str]], int]


def sync_args(*, version_dir: Path, hermes_home: str, customer_yaml: str) -> list[str]:
    """The ``skills_applier.sync`` argv for one staged release."""
    args = [
        "--source",
        str(version_dir / SKILLS_SUBDIR),
        "--dest",
        str(Path(hermes_home) / "skills"),
        "--retired-file",
        str(version_dir / RETIRED_FILE),
    ]
    if os.path.exists(_IMAGE_RETIRED):
        args += ["--retired-file", _IMAGE_RETIRED]
    args += ["--enabled-skills-from", customer_yaml, "--refresh-profiles", hermes_home]
    return args


def run_sync_as_hermes(args: list[str]) -> int:
    """Run the sync as the hermes uid with a scrubbed env; returns its exit code."""
    user = os.environ.get("SMD_SKILLS_SYNC_USER") or "hermes"
    setpriv = shutil.which("setpriv") or "/usr/bin/setpriv"
    cmd = [
        setpriv,
        f"--reuid={user}",
        f"--regid={user}",
        "--init-groups",
        "--",
        sys.executable,
        "-m",
        "skills_applier.sync",
        *args,
    ]
    env = {k: os.environ[k] for k in _SYNC_ENV_KEYS if os.environ.get(k)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, env=env, cwd="/", timeout=_SYNC_TIMEOUT_SECONDS, check=False
        ).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("skills_applier: sync child failed to run: %s", exc)
        return 1


@dataclass
class SkillsPollLoop:
    s3_client: Any
    bucket: str
    slug: str
    root: Path
    hermes_home: str
    customer_yaml: str
    image_skills: Path
    capabilities_path: Path
    sync_runner: SyncRunner
    audit_client: Any = None
    poll_seconds: float = _DEFAULT_POLL_SECONDS
    _last_etag: str | None = None
    _logged_missing: bool = False

    def _head_etag(self) -> str | None:
        try:
            head = self.s3_client.head_object(Bucket=self.bucket, Key=pointer_key(self.slug))
        except Exception as exc:  # noqa: BLE001 — absent or unreachable: skip this tick
            if not self._logged_missing:
                self._logged_missing = True
                logger.info("skills_applier: no readable skills pointer (%s)", exc)
            return None
        self._logged_missing = False
        etag = head.get("ETag") if isinstance(head, dict) else getattr(head, "ETag", None)
        return etag if isinstance(etag, str) else None

    def _stage(self) -> SkillsApplyResult:
        return stage(
            s3_client=self.s3_client,
            bucket=self.bucket,
            slug=self.slug,
            root=self.root,
            image_skills=self.image_skills,
            capabilities_path=self.capabilities_path,
        )

    def run_once(self) -> Outcome | None:
        etag = self._head_etag()
        if etag is None or etag == self._last_etag:
            return None
        try:
            result = self._stage()
        except PointerMissing:
            return None
        except SkillsApplyError as exc:
            logger.error("skills_applier: stage failed (%s); retrying next tick", exc)
            return None
        if result.outcome is Outcome.REJECTED:
            logger.warning(
                "skills_applier: release %s REJECTED (%s); keeping %s",
                result.version,
                "; ".join(result.reasons),
                result.previous,
            )
        elif result.outcome is Outcome.APPLIED:
            if not self._finish(result):
                return None
        self._last_etag = etag
        return result.outcome

    def _finish(self, result: SkillsApplyResult) -> bool:
        """Copy onto the volume as hermes, then commit. False = retry next tick."""
        assert result.path is not None and result.version is not None
        args = sync_args(
            version_dir=result.path,
            hermes_home=self.hermes_home,
            customer_yaml=self.customer_yaml,
        )
        code = self.sync_runner(args)
        if code != 0:
            logger.error(
                "skills_applier: volume sync for %s exited %s; current stays %s",
                result.version,
                code,
                result.previous,
            )
            return False
        try:
            commit(self.root, result.version)
        except (OSError, SkillsApplyError) as exc:
            logger.error("skills_applier: commit of %s failed: %s", result.version, exc)
            return False
        prune(self.root, {result.version, result.previous or result.version})
        logger.info(
            "skills_applier: APPLIED %s (previous %s, refused %s, retired %s)",
            result.version,
            result.previous,
            sorted(result.refused),
            list(result.retired),
        )
        if self.audit_client is not None:
            emit_skills_write(audit_client=self.audit_client, slug=self.slug, result=result)
        return True

    def run_forever(self, stop: Callable[[], bool]) -> None:
        logger.info(
            "skills_applier: poll loop started (slug=%s root=%s interval=%ss)",
            self.slug,
            self.root,
            self.poll_seconds,
        )
        while not stop():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the loop outlives any one tick
                logger.exception("skills_applier: unexpected error in poll tick; continuing")
            slept = 0.0
            while slept < self.poll_seconds and not stop():
                time.sleep(min(1.0, self.poll_seconds - slept))
                slept += 1.0


def run_boot_once(loop: SkillsPollLoop) -> int:
    """Stage and commit with no volume copy (bootstrap copies from current)."""
    try:
        result = loop._stage()
    except PointerMissing:
        logger.info("skills_applier: no skills pointer for %s; image skills stand", loop.slug)
        return 0
    except SkillsApplyError as exc:
        logger.error("skills_applier: boot fetch failed (%s); keeping current", exc)
        return 0
    if result.outcome is Outcome.APPLIED and result.version:
        try:
            commit(loop.root, result.version)
            prune(loop.root, {result.version, result.previous or result.version})
        except (OSError, SkillsApplyError) as exc:
            logger.error("skills_applier: boot commit failed (%s); keeping current", exc)
            return 0
    logger.info(
        "skills_applier: boot fetch %s (version %s%s)",
        result.outcome.value,
        result.version,
        f"; {'; '.join(result.reasons)}" if result.reasons else "",
    )
    return 0


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"skills_applier: required env var {name} is unset")
    return value


def _poll_seconds() -> float:
    raw = os.environ.get("SMD_SKILLS_POLL_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_POLL_SECONDS
    except ValueError:
        return _DEFAULT_POLL_SECONDS
    return value if value >= 1.0 else _DEFAULT_POLL_SECONDS


def build_loop(*, with_audit: bool) -> SkillsPollLoop:
    import boto3

    slug = _require_env("CUSTOMER_SLUG")
    bucket = _require_env("R2_BUCKET_CONFIG")
    s3 = boto3.client(
        "s3",
        endpoint_url=_require_env("R2_ENDPOINT_URL"),
        aws_access_key_id=_require_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_require_env("R2_SECRET_ACCESS_KEY"),
    )
    audit = None
    if with_audit:
        from shared.audit_client import audit_client_from_env

        audit = audit_client_from_env(customer_slug=slug)
    return SkillsPollLoop(
        s3_client=s3,
        bucket=bucket,
        slug=slug,
        root=Path(os.environ.get("SMD_SKILLS_ROOT") or DEFAULT_ROOT),
        hermes_home=os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME,
        customer_yaml=os.environ.get("SMD_CUSTOMER_YAML_PATH") or _DEFAULT_CUSTOMER_YAML,
        image_skills=Path(DEFAULT_IMAGE_SKILLS),
        capabilities_path=Path(DEFAULT_CAPABILITIES),
        sync_runner=run_sync_as_hermes,
        audit_client=audit,
        poll_seconds=_poll_seconds(),
    )


def main(argv: list[str] | None = None, *, loop_factory=build_loop) -> int:
    args = sys.argv[1:] if argv is None else argv
    once = "--once" in args
    logging.basicConfig(
        level=os.environ.get("SMD_SKILLS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stopped = {"flag": False}

    def _request_stop(signum, _frame) -> None:
        logger.info("skills_applier: received signal %s; stopping", signum)
        stopped["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    try:
        loop = loop_factory(with_audit=not once)
    except SystemExit as exc:
        logger.error("skills_applier: startup failed: %s", exc)
        return 0 if once else 2
    if once:
        return run_boot_once(loop)
    loop.run_forever(stop=lambda: stopped["flag"])
    return EXIT_STOPPED if stopped["flag"] else 0


if __name__ == "__main__":
    sys.exit(main())
