"""Root-owned spec-applier entry point (ss ADR 0083, ss-console #2084).

Launched by the Machine entrypoint as ROOT — the only principal that may write
``SMD_SPEC_DIR``. Two modes, both wired from env in :func:`main`:

* ``python -m spec_applier --once`` — the BOOT fetch. Runs synchronously before
  the privilege drop, so the spec tree and its manifest exist before
  ``bootstrap/translate.py`` renders the per-profile skill stamps that point at
  them. Without this, a fresh Machine would race: the poller would install the
  specs a few seconds after the profiles were already stamped with nothing.
* ``python -m spec_applier`` — the poll loop. Picks up a portal edit on the
  running Machine with no reboot. The runtime read of a spec is a plain
  ``read_file`` against the installed tree, so a replaced body takes effect on
  the next read; nothing is baked into a running process.

Change detection is a cheap ``HEAD`` (ETag compare) BEFORE any pull, plus a
source-digest compare inside :func:`spec_applier.applier.apply`. Two layers
because they answer different questions: the ETag avoids the GET, and the digest
avoids a rewrite when a re-uploaded object has identical bytes.

A missing vault object is the ORDINARY state of a seat whose customer has
authored nothing. It logs once at info and the loop keeps running — an absent
spec is an authored outcome (``output_classes`` says ``none``) or a declared-but-
missing one (``expected``), and it is the runtime GATE, never this poller, that
decides which of those is a refusal.
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

from spec_applier.applier import (
    SpecApplyError,
    SpecApplyOutcome,
    SpecObjectMissing,
    apply,
    spec_object_key,
)

logger = logging.getLogger(__name__)

_ENV_R2_ENDPOINT = "R2_ENDPOINT_URL"
_ENV_R2_KEY_ID = "R2_ACCESS_KEY_ID"
_ENV_R2_SECRET = "R2_SECRET_ACCESS_KEY"
_ENV_R2_BUCKET = "R2_BUCKET_CONFIG"
_ENV_SLUG = "CUSTOMER_SLUG"
_ENV_SPEC_DIR = "SMD_SPEC_DIR"
_ENV_POLL_SECONDS = "SMD_SPEC_POLL_SECONDS"

_DEFAULT_SPEC_DIR = "/var/lib/smd-config/specs"
_DEFAULT_POLL_SECONDS = 30


@dataclass
class SpecPollLoop:
    """Per-tick logic with I/O injected, mirroring ``config_applier.PollLoop``."""

    s3_client: Any
    bucket: str
    slug: str
    spec_dir: Path
    poll_seconds: float = _DEFAULT_POLL_SECONDS
    _last_etag: str | None = None
    _logged_missing: bool = False

    def _head_etag(self) -> str | None:
        """HEAD the spec object; ``None`` on any fault (including 'absent').

        A ``None`` ETag means 'no actionable change this tick'. The distinction
        between "R2 is down" and "the customer authored nothing" is not worth
        making here: neither is a reason to touch the installed tree.
        """
        key = spec_object_key(self.slug)
        try:
            head = self.s3_client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — any HEAD fault is a skip-this-tick
            if not self._logged_missing:
                self._logged_missing = True
                logger.info(
                    "spec_applier: no readable spec object at s3://%s/%s (%s); "
                    "the seat serves whatever is already installed",
                    self.bucket,
                    key,
                    exc,
                )
            return None
        self._logged_missing = False
        etag = head.get("ETag") if isinstance(head, dict) else getattr(head, "ETag", None)
        return etag if isinstance(etag, str) else None

    def run_once(self) -> SpecApplyOutcome | None:
        """One tick. Returns the outcome when an apply ran, else ``None``.

        Never raises on a rejection or an R2/write fault — all are caught,
        logged, and the installed tree is left as it stands.
        """
        etag = self._head_etag()
        if etag is None:
            return None
        if etag == self._last_etag:
            return None

        logger.info(
            "spec_applier: spec object ETag changed (%s -> %s); applying",
            self._last_etag,
            etag,
        )
        try:
            result = apply(
                s3_client=self.s3_client,
                bucket=self.bucket,
                slug=self.slug,
                spec_dir=self.spec_dir,
            )
        except SpecObjectMissing:
            return None
        except SpecApplyError as exc:
            # Do NOT cache the ETag — retry this same object next tick; the
            # fault may be transient. The installed tree is untouched.
            logger.error(
                "spec_applier: apply failed for ETag=%s (%s); retrying next tick", etag, exc
            )
            return None

        if result.outcome is SpecApplyOutcome.APPLIED:
            logger.info(
                "spec_applier: APPLIED %d spec(s) %s (pruned %s, source=%s)",
                len(result.installed),
                list(result.installed),
                list(result.pruned),
                result.source_digest,
            )
        elif result.outcome is SpecApplyOutcome.REJECTED:
            logger.warning(
                "spec_applier: REJECTED — %s; keeping the installed spec tree (fail-static)",
                "; ".join(result.reasons) or "(no reason given)",
            )
        # Mark handled (applied, unchanged, or refused) so the same object is
        # not re-evaluated every tick. A new publish changes the ETag.
        self._last_etag = etag
        return result.outcome

    def run_forever(self, stop) -> None:
        """Poll until ``stop()`` returns True."""
        logger.info(
            "spec_applier: poll loop started (slug=%s bucket=%s spec_dir=%s interval=%ss)",
            self.slug,
            self.bucket,
            self.spec_dir,
            self.poll_seconds,
        )
        while not stop():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the loop must outlive any single tick
                logger.exception("spec_applier: unexpected error in poll tick; continuing")
            slept = 0.0
            while slept < self.poll_seconds and not stop():
                time.sleep(min(1.0, self.poll_seconds - slept))
                slept += 1.0
        logger.info("spec_applier: stop requested; poll loop exiting cleanly")


# ---------------------------------------------------------------------------
# Env wiring + entry point
# ---------------------------------------------------------------------------


def _build_s3_client() -> Any:
    """Construct the boto3 R2 client from env (lazy import; tests inject fakes)."""
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
        raise SystemExit(f"spec_applier: required env var {name} is unset")
    return value


def _poll_seconds_from_env() -> float:
    raw = os.environ.get(_ENV_POLL_SECONDS)
    if not raw:
        return float(_DEFAULT_POLL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "spec_applier: %s=%r not a number; using default %ss",
            _ENV_POLL_SECONDS,
            raw,
            _DEFAULT_POLL_SECONDS,
        )
        return float(_DEFAULT_POLL_SECONDS)
    return value if value >= 1.0 else float(_DEFAULT_POLL_SECONDS)


def build_loop() -> SpecPollLoop:
    """Construct a :class:`SpecPollLoop` wired to real R2 from env."""
    slug = _require_env(_ENV_SLUG)
    bucket = _require_env(_ENV_R2_BUCKET)
    spec_dir = Path(os.environ.get(_ENV_SPEC_DIR) or _DEFAULT_SPEC_DIR)
    return SpecPollLoop(
        s3_client=_build_s3_client(),
        bucket=bucket,
        slug=slug,
        spec_dir=spec_dir,
        poll_seconds=_poll_seconds_from_env(),
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m spec_applier [--once]``.

    ``--once`` runs a single apply and exits — the boot fetch. Its exit code is
    deliberately 0 on a rejection or a missing object: an un-adoptable spec must
    not brick a boot. Fail-static means the Machine comes up on the spec tree it
    already had, and the runtime gate (not the boot) is what refuses to produce
    an output whose declared spec never arrived.
    """
    args = sys.argv[1:] if argv is None else argv
    once = "--once" in args

    logging.basicConfig(
        level=os.environ.get("SMD_SPEC_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        loop = build_loop()
    except SystemExit as exc:
        logger.error("spec_applier: startup failed: %s", exc)
        return 2

    if once:
        try:
            result = apply(
                s3_client=loop.s3_client,
                bucket=loop.bucket,
                slug=loop.slug,
                spec_dir=loop.spec_dir,
            )
        except SpecObjectMissing:
            logger.info(
                "spec_applier: no authored spec object for %s; nothing installed this boot",
                loop.slug,
            )
            return 0
        except SpecApplyError as exc:
            logger.error(
                "spec_applier: boot fetch failed (%s); keeping the installed spec tree", exc
            )
            return 0
        if result.outcome is SpecApplyOutcome.REJECTED:
            logger.warning(
                "spec_applier: boot fetch REJECTED — %s; keeping the installed spec tree",
                "; ".join(result.reasons),
            )
        else:
            logger.info(
                "spec_applier: boot fetch %s (%d spec(s) installed)",
                result.outcome.value,
                len(result.installed),
            )
        return 0

    stopped = {"flag": False}

    def _request_stop(signum, _frame) -> None:
        logger.info("spec_applier: received signal %s; requesting clean stop", signum)
        stopped["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    loop.run_forever(stop=lambda: stopped["flag"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
