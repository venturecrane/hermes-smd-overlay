"""Root-owned config-applier poll loop (ADR 0044 WS3 — entry point).

Launched by the Machine entrypoint as ``python -m config_applier``, running as
ROOT (the only principal that can write the hermes-owned ``customer.yaml`` and,
later, signal the gateway — see the apply-privilege analysis). It polls the
authoritative R2 config object and applies a change to the running Operator via
:func:`config_applier.applier.apply`, with no reboot.

All I/O is constructed from the environment in :func:`main` — nothing is
hardcoded — and the loop logic lives in :class:`PollLoop`, which takes the S3
and audit clients injected so it is unit-tested with fakes (no R2, no broker, no
real volume).

Change detection is a cheap ``HEAD`` (ETag compare) BEFORE any apply, because
``apply()`` is not a no-op on unchanged input — it would rewrite and re-audit
every tick. Only a changed ETag drives a full pull → validate → safety →
atomic-write → ``CONFIG_WRITE`` cycle.

v1 has NO SIGUSR1 / gateway signaling: every live-writable field (ceilings via
``enforce.py``, demo/webhook via the WS2 plugins) is read fresh per action, so a
write takes effect on the next action with no reload. (``escalation`` is in the
allow-list but currently profile-baked — a known v1 latency nit, not a safety
issue; the safety-critical ceiling fields ARE live. The reload tier is Phase 2.)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_applier.applier import (
    ApplyOutcome,
    ConfigApplyError,
    apply,
    atomic_write,
    config_key,
)
from shared.audit_client import audit_client_from_env

logger = logging.getLogger(__name__)

# Env var names + defaults. The entrypoint sets the R2 vars and CUSTOMER_SLUG;
# the SMD_APPLIER_* tuning knobs default sensibly so the box needs only the
# required four.
_ENV_R2_ENDPOINT = "R2_ENDPOINT_URL"
_ENV_R2_KEY_ID = "R2_ACCESS_KEY_ID"
_ENV_R2_SECRET = "R2_SECRET_ACCESS_KEY"
_ENV_R2_BUCKET = "R2_BUCKET_CONFIG"
_ENV_SLUG = "CUSTOMER_SLUG"
_ENV_VOLUME = "SMD_APPLIER_VOLUME_PATH"
_ENV_EPOCH_FILE = "SMD_APPLIER_EPOCH_FILE"
_ENV_POLL_SECONDS = "SMD_APPLIER_POLL_SECONDS"

_DEFAULT_VOLUME = "/opt/data/customer.yaml"
_DEFAULT_EPOCH_FILE = "/opt/data/.config-epoch"
_DEFAULT_POLL_SECONDS = 15


# ---------------------------------------------------------------------------
# Epoch file (small monotonic counter persisted beside the config)
# ---------------------------------------------------------------------------


def read_epoch(epoch_file: Path) -> int | None:
    """Read the last-applied epoch from ``epoch_file``, or ``None`` if absent /
    unparseable. A garbled epoch file must not crash the loop — it reads as
    ``None`` (``next_epoch`` then resets the floor), and the next APPLIED write
    rewrites a clean value.
    """
    try:
        text = epoch_file.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "applier: could not read epoch file %s (%s); treating as absent", epoch_file, exc
        )
        return None
    try:
        return int(text)
    except ValueError:
        logger.warning(
            "applier: epoch file %s holds non-int %r; treating as absent", epoch_file, text
        )
        return None


def write_epoch(epoch_file: Path, epoch: int) -> None:
    """Atomically persist ``epoch`` to ``epoch_file``.

    Reuses :func:`atomic_write` so a crash can never leave a torn epoch value.
    A write failure here is logged but NOT fatal — the config write already
    succeeded; a stale epoch only costs one redundant stamp next cycle.
    """
    try:
        atomic_write(epoch_file, str(epoch).encode("utf-8"))
    except ConfigApplyError as exc:
        logger.warning("applier: could not persist epoch %d to %s: %s", epoch, epoch_file, exc)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


@dataclass
class PollLoop:
    """The applier's per-tick logic, with I/O injected for unit testing.

    ``s3_client`` and ``audit_client`` are constructed in :func:`main` from env;
    tests pass fakes. The loop caches the ETag of the last R2 object it acted on
    (applied OR rejected/deferred) so it does not re-evaluate the same object
    every tick. On first iteration the cache is seeded from the CURRENT R2 ETag
    (when a config is already on the volume) so a Machine that booted with the
    current config does not immediately re-apply it.
    """

    s3_client: Any
    bucket: str
    slug: str
    volume_path: Path
    epoch_file: Path
    audit_client: Any
    poll_seconds: float = _DEFAULT_POLL_SECONDS
    _last_etag: str | None = None
    _seeded: bool = False

    def _head_etag(self) -> str | None:
        """HEAD the R2 config object and return its ETag, or ``None`` on any
        fault (object missing / R2 unreachable). A ``None`` ETag is treated as
        'no actionable change this tick' — the loop retries next tick rather than
        applying against an object it could not even HEAD."""
        key = config_key(self.slug)
        try:
            head = self.s3_client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — any HEAD fault is a skip-this-tick
            logger.warning(
                "applier: HEAD s3://%s/%s failed (%s); will retry", self.bucket, key, exc
            )
            return None
        etag = head.get("ETag") if isinstance(head, dict) else getattr(head, "ETag", None)
        return etag if isinstance(etag, str) else None

    def _seed_cache(self) -> None:
        """Seed ``_last_etag`` on the first tick so a boot-time-current config is
        not re-applied. The on-volume file's content digest is not the R2 ETag,
        so we cannot compare them directly; instead we record the CURRENT R2 ETag
        as already-seen IFF the on-volume config already equals what R2 serves.
        Cheap heuristic: if the volume file exists, seed with the current R2 ETag
        (assume the Machine booted from it); the first genuine change then differs
        and applies. If the volume file is absent (fresh Machine), leave the cache
        ``None`` so the first tick pulls + seeds the initial config."""
        self._seeded = True
        if not self.volume_path.exists():
            # Fresh Machine: no config yet — let the first tick apply the initial
            # seed. Leave _last_etag None so any HEAD differs.
            return
        # Volume already has a config (the image baked one, or a prior apply).
        # Record the current R2 ETag as seen so we don't re-apply an unchanged
        # object on boot. A real edit to R2 changes the ETag and applies.
        self._last_etag = self._head_etag()
        logger.info(
            "applier: seeded change-cache from current R2 ETag=%s (volume config present)",
            self._last_etag,
        )

    def run_once(self) -> ApplyOutcome | None:
        """One poll tick. Returns the apply outcome when an apply ran, or
        ``None`` when nothing changed (or the object could not be HEADed).

        Never raises on a config-level rejection/deferral or an R2/write fault —
        all are caught, logged, and the loop continues. The only effect on the
        running Machine is a successful APPLIED write; everything else leaves the
        current config in place.
        """
        if not self._seeded:
            self._seed_cache()

        etag = self._head_etag()
        if etag is None:
            return None  # could not HEAD — skip this tick, retry next
        if etag == self._last_etag:
            return None  # unchanged — the guard that keeps apply() from churning

        logger.info("applier: R2 config ETag changed (%s -> %s); applying", self._last_etag, etag)
        prev_epoch = read_epoch(self.epoch_file)
        try:
            result = apply(
                s3_client=self.s3_client,
                bucket=self.bucket,
                slug=self.slug,
                volume_path=self.volume_path,
                audit_client=self.audit_client,
                prev_epoch=prev_epoch,
                # Don't crash the loop on a rebuild-class edit pushed to R2 —
                # return DEFERRED (logged, left for reprovision) instead of a
                # hard reject. The live path still rejects nothing it would write.
                allow_deferred_paths=True,
            )
        except ConfigApplyError as exc:
            # Unrecoverable R2 read / volume write fault. Do NOT cache the ETag —
            # we want to retry this same object next tick (the fault may be
            # transient). Keep the running config.
            logger.error("applier: apply failed for ETag=%s (%s); retrying next tick", etag, exc)
            return None

        if result.outcome is ApplyOutcome.APPLIED:
            logger.info(
                "applier: APPLIED epoch=%s changed=%s audited=%s",
                result.epoch,
                list(result.changed),
                result.audited,
            )
            if result.epoch is not None:
                write_epoch(self.epoch_file, result.epoch)
        else:
            # REJECTED / DEFERRED — log loudly. Cache the ETag so we don't
            # re-evaluate the same bad/rebuild-class object every tick; it will
            # be re-evaluated only when someone pushes a NEW object (new ETag).
            logger.warning(
                "applier: %s — %s; keeping running config (ETag cached)",
                result.outcome.value.upper(),
                "; ".join(result.reasons) or "(no reason given)",
            )

        # Mark this object handled (applied or refused) so it is not re-processed.
        self._last_etag = etag
        return result.outcome

    def run_forever(self, stop) -> None:
        """Poll until ``stop()`` returns True (set by the signal handler).

        Each tick is wrapped so a wholly-unexpected error (outside the
        config-level handling in ``run_once``) logs and the loop survives — the
        applier must not die on the box and silently stop tracking config.
        """
        logger.info(
            "applier: poll loop started (slug=%s bucket=%s volume=%s interval=%ss)",
            self.slug,
            self.bucket,
            self.volume_path,
            self.poll_seconds,
        )
        while not stop():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any single tick
                logger.exception("applier: unexpected error in poll tick; continuing")
            # Sleep in short slices so a stop signal is honored promptly.
            slept = 0.0
            while slept < self.poll_seconds and not stop():
                time.sleep(min(1.0, self.poll_seconds - slept))
                slept += 1.0
        logger.info("applier: stop requested; poll loop exiting cleanly")


# ---------------------------------------------------------------------------
# Env wiring + entry point
# ---------------------------------------------------------------------------


def _build_s3_client() -> Any:
    """Construct the boto3 R2 client from env. Imported lazily so the module is
    importable (and unit-testable with fakes) on a box without boto3 configured.
    """
    import boto3  # lazy: tests inject a fake client and never reach here

    return boto3.client(
        "s3",
        endpoint_url=os.environ[_ENV_R2_ENDPOINT],
        aws_access_key_id=os.environ[_ENV_R2_KEY_ID],
        aws_secret_access_key=os.environ[_ENV_R2_SECRET],
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"config_applier: required env var {name} is unset")
    return value


def _poll_seconds_from_env() -> float:
    raw = os.environ.get(_ENV_POLL_SECONDS)
    if not raw:
        return float(_DEFAULT_POLL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "applier: %s=%r not a number; using default %ss",
            _ENV_POLL_SECONDS,
            raw,
            _DEFAULT_POLL_SECONDS,
        )
        return float(_DEFAULT_POLL_SECONDS)
    # Guard against a pathological 0 / negative interval (busy-loop / no sleep).
    return value if value >= 1.0 else float(_DEFAULT_POLL_SECONDS)


def build_loop() -> PollLoop:
    """Construct a :class:`PollLoop` wired to real R2 + broker audit from env."""
    slug = _require_env(_ENV_SLUG)
    bucket = _require_env(_ENV_R2_BUCKET)
    volume = Path(os.environ.get(_ENV_VOLUME) or _DEFAULT_VOLUME)
    epoch_file = Path(os.environ.get(_ENV_EPOCH_FILE) or _DEFAULT_EPOCH_FILE)
    return PollLoop(
        s3_client=_build_s3_client(),
        bucket=bucket,
        slug=slug,
        volume_path=volume,
        epoch_file=epoch_file,
        audit_client=audit_client_from_env(customer_slug=slug),
        poll_seconds=_poll_seconds_from_env(),
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m config_applier``.

    Builds the loop from env, installs SIGTERM/SIGINT handlers for a clean exit,
    and polls until signalled. Returns a process exit code.
    """
    logging.basicConfig(
        level=os.environ.get("SMD_APPLIER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stopped = {"flag": False}

    def _request_stop(signum, _frame) -> None:
        logger.info("applier: received signal %s; requesting clean stop", signum)
        stopped["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        loop = build_loop()
    except SystemExit as exc:
        logger.error("applier: startup failed: %s", exc)
        return 2

    loop.run_forever(stop=lambda: stopped["flag"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
