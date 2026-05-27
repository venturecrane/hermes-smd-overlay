"""Agent-authored skill body persistence — ADR 0022 Stream 2 writer.

When the Hermes ``skill_manage`` tool creates a new skill via its
``post_tool_call`` event, this module:

1. Reads the SKILL.md body from the per-profile skills directory on the
   Fly volume (the file Hermes wrote during the tool call).
2. Computes SHA-256 of the body bytes; this is the canonical identifier.
3. INSERTs a row into ``agent_skills_inventory`` with ``r2_status='pending'``.
4. PUTs the body to the per-customer R2 bucket via S3-compatible API.
5. UPDATEs the row to ``r2_status='persisted'`` on success or ``'failed'``
   with ``r2_write_error`` populated on failure.

This is the **write-ahead** discipline from the approved plan: the D1
row commits before the R2 PUT, so the row is always visible to the
admin portal even when R2 is unreachable. A subsequent boot picks up
``pending``/``failed`` rows via :func:`reconcile_pending_bodies` and
re-attempts the PUT.

Contract: ``contracts/skill_capture_v1.json`` (root of this repo, mirrored
from ss-console).

Constraints (per AGENTS.md):
  * Never modify Hermes core. This module attaches to ``post_tool_call``
    via the existing audit plugin hook; no Hermes patches.
  * Callbacks must be exception-safe. The hook wrapper in
    ``__init__.py`` catches and logs.
  * No secrets in code. R2 credentials read from env via
    ``shared.secrets.require``.
  * Per-customer isolation: the bucket itself is the trust boundary.
    Per Captain decision (2026-05-27, ADR 0022 §"R2 bucket model"),
    each customer gets their own R2 bucket; the credentials in this
    Machine are scoped to that bucket only.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from shared.d1_client import D1Client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key shape
#
# Per the shared contract (skill_capture_v1.json), the R2 key for a body
# is: ``skills/<persona_slug>/<skill_name>/<content_hash>.md``. The bucket
# itself encodes the customer (one bucket per customer per ADR 0007), so
# the key does not include a customer-slug component.
# ---------------------------------------------------------------------------


def compute_content_hash(body_bytes: bytes) -> str:
    """SHA-256 hex digest, lowercase. The canonical content identifier."""
    return hashlib.sha256(body_bytes).hexdigest()


def make_r2_key(*, persona_slug: str, skill_name: str, content_hash: str) -> str:
    """Build the R2 object key per the shared contract."""
    return f"skills/{persona_slug}/{skill_name}/{content_hash}.md"


# ---------------------------------------------------------------------------
# Profile-directory body reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillBody:
    """Resolved body bytes + provenance."""

    body_bytes: bytes
    content_hash: str
    path: Path


def read_skill_body(
    *,
    hermes_home: str,
    persona_slug: str,
    skill_name: str,
) -> SkillBody | None:
    """Read the SKILL.md body from the per-profile skills directory.

    Returns None when the file does not exist (skill was deleted from the
    volume before reconciliation, for instance). The caller treats this
    as a write-ahead row that cannot be recovered and marks
    ``r2_status='failed'`` with ``r2_write_error='BodyMissingOnVolume'``.
    """
    skill_path = Path(hermes_home) / "profiles" / persona_slug / "skills" / skill_name / "SKILL.md"
    if not skill_path.exists():
        return None
    try:
        body_bytes = skill_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "skill_capture: failed reading SKILL.md (persona=%s skill=%s err=%s)",
            persona_slug,
            skill_name,
            exc,
        )
        return None
    return SkillBody(
        body_bytes=body_bytes,
        content_hash=compute_content_hash(body_bytes),
        path=skill_path,
    )


# ---------------------------------------------------------------------------
# R2 client adapter
#
# Lazy import of boto3 — the dependency is declared as optional in
# pyproject.toml so test environments without the wheel can still import
# the module. The customer Machine always has boto3 (it's already in the
# Dockerfile alongside awscli, which uses boto3 under the hood).
# ---------------------------------------------------------------------------


class R2WriteError(RuntimeError):
    """Raised when the R2 PUT fails for a recoverable reason.

    Carries a short ``reason`` token stable enough to alert on
    (AccessDenied / BucketNotFound / ThrottledByR2 / BodyMissingOnVolume /
    BodyHashMismatch / Other).
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class R2Config:
    """Per-Machine R2 credentials for the skill-bodies bucket."""

    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str


def load_r2_config_from_env() -> R2Config | None:
    """Resolve R2 config from env vars. Returns None when any required
    var is missing so the caller can no-op gracefully on a misconfigured
    Machine (matching the audit plugin's no-op-on-missing-env posture).
    """
    endpoint = os.getenv("R2_ENDPOINT_URL")
    access_key = os.getenv("R2_SKILL_BODIES_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SKILL_BODIES_SECRET_ACCESS_KEY")
    bucket = os.getenv("R2_SKILL_BODIES_BUCKET")
    if not (endpoint and access_key and secret_key and bucket):
        return None
    return R2Config(
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        bucket=bucket,
    )


def put_skill_body(config: R2Config, key: str, body_bytes: bytes) -> None:
    """PUT body bytes to R2 at the given key.

    Raises :class:`R2WriteError` with a short reason token on failure.
    The hash-addressed key makes re-PUTs idempotent (R2's PutObject is
    last-writer-wins on identical content).
    """
    try:
        import boto3  # type: ignore[import-not-found]
        from botocore.exceptions import ClientError  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — exercised only on misconfigured deploys
        raise R2WriteError("BotoMissing", str(exc)) from exc

    s3 = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
    )
    try:
        s3.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=body_bytes,
            ContentType="text/markdown; charset=utf-8",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Other")
        # Normalize the common Cloudflare R2 error codes to the contract
        # vocabulary (AccessDenied / BucketNotFound / ThrottledByR2 / Other).
        if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
            reason = "AccessDenied"
        elif code in ("NoSuchBucket", "BucketNotFound"):
            reason = "BucketNotFound"
        elif code in ("SlowDown", "RequestLimitExceeded", "TooManyRequests"):
            reason = "ThrottledByR2"
        else:
            reason = "Other"
        raise R2WriteError(reason, f"R2 PUT failed: code={code}") from exc


# ---------------------------------------------------------------------------
# D1 write helpers
# ---------------------------------------------------------------------------


_INSERT_PENDING_SQL = (
    "INSERT INTO agent_skills_inventory "
    "(customer_slug, persona_slug, skill_name, skill_content_hash, "
    " source_turn_id, r2_key, r2_status, r2_write_error) "
    "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)"
)

_UPDATE_PERSISTED_SQL = (
    "UPDATE agent_skills_inventory "
    "SET r2_status = 'persisted', r2_write_error = NULL "
    "WHERE customer_slug = ? AND persona_slug = ? "
    "  AND skill_name = ? AND skill_content_hash = ?"
)

_UPDATE_FAILED_SQL = (
    "UPDATE agent_skills_inventory "
    "SET r2_status = 'failed', r2_write_error = ? "
    "WHERE customer_slug = ? AND persona_slug = ? "
    "  AND skill_name = ? AND skill_content_hash = ?"
)

_SELECT_PENDING_SQL = (
    "SELECT customer_slug, persona_slug, skill_name, skill_content_hash, r2_key "
    "FROM agent_skills_inventory "
    "WHERE r2_status IN ('pending', 'failed') "
    "ORDER BY created_at ASC"
)


def _insert_pending(
    client: D1Client,
    *,
    customer_slug: str,
    persona_slug: str,
    skill_name: str,
    content_hash: str,
    source_turn_id: str,
    r2_key: str,
) -> None:
    client.execute(
        _INSERT_PENDING_SQL,
        customer_slug,
        persona_slug,
        skill_name,
        content_hash,
        source_turn_id,
        r2_key,
    )


def _update_persisted(
    client: D1Client,
    *,
    customer_slug: str,
    persona_slug: str,
    skill_name: str,
    content_hash: str,
) -> None:
    client.execute(
        _UPDATE_PERSISTED_SQL,
        customer_slug,
        persona_slug,
        skill_name,
        content_hash,
    )


def _update_failed(
    client: D1Client,
    *,
    customer_slug: str,
    persona_slug: str,
    skill_name: str,
    content_hash: str,
    reason: str,
) -> None:
    client.execute(
        _UPDATE_FAILED_SQL,
        reason,
        customer_slug,
        persona_slug,
        skill_name,
        content_hash,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of one capture attempt. Used by tests + diagnostics."""

    recorded: bool
    r2_status: str  # 'persisted' | 'failed' | 'skipped'
    r2_key: str | None
    reason: str | None


def capture_skill_body(
    client: D1Client,
    r2: R2Config | None,
    *,
    customer_slug: str,
    persona_slug: str,
    skill_name: str,
    source_turn_id: str,
    hermes_home: str,
) -> CaptureResult:
    """Write-ahead capture of one agent-authored skill body.

    Called from the post_tool_call hook in __init__.py once
    detect_skill_manage_creation returns a slug. Exception-safe: callers
    in the hook wrap this in try/except per the AGENTS.md hard rule.

    Returns a CaptureResult describing the outcome — useful for tests
    and structured log lines.
    """
    body = read_skill_body(
        hermes_home=hermes_home, persona_slug=persona_slug, skill_name=skill_name
    )
    if body is None:
        # No body file on the volume — we cannot capture. The audit row
        # for AGENT_SKILL_CREATED still emits separately; this skill
        # body is unrecoverable.
        return CaptureResult(
            recorded=False,
            r2_status="skipped",
            r2_key=None,
            reason="BodyMissingOnVolume",
        )

    r2_key = make_r2_key(
        persona_slug=persona_slug,
        skill_name=skill_name,
        content_hash=body.content_hash,
    )

    # Step 1: write-ahead INSERT (pending). The row is durable even if R2
    # is unreachable; the boot reconciler can retry from this row.
    try:
        _insert_pending(
            client,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=skill_name,
            content_hash=body.content_hash,
            source_turn_id=source_turn_id,
            r2_key=r2_key,
        )
    except Exception as exc:  # noqa: BLE001 — D1 errors are recoverable; log and skip
        logger.warning(
            "skill_capture: D1 INSERT pending failed (persona=%s skill=%s err=%s)",
            persona_slug,
            skill_name,
            exc,
        )
        return CaptureResult(
            recorded=False,
            r2_status="skipped",
            r2_key=r2_key,
            reason="D1InsertFailed",
        )

    # Step 2: R2 PUT (skipped when env is missing — reconciler retries).
    if r2 is None:
        return CaptureResult(
            recorded=True, r2_status="pending", r2_key=r2_key, reason="R2EnvMissing"
        )
    try:
        put_skill_body(r2, r2_key, body.body_bytes)
    except R2WriteError as exc:
        _safe_update_failed(
            client,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=skill_name,
            content_hash=body.content_hash,
            reason=exc.reason,
        )
        return CaptureResult(recorded=True, r2_status="failed", r2_key=r2_key, reason=exc.reason)
    except Exception as exc:  # noqa: BLE001 — unknown PUT failure shape
        _safe_update_failed(
            client,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=skill_name,
            content_hash=body.content_hash,
            reason="Other",
        )
        logger.warning(
            "skill_capture: R2 PUT raised unexpected exception (persona=%s skill=%s err=%s)",
            persona_slug,
            skill_name,
            exc,
        )
        return CaptureResult(recorded=True, r2_status="failed", r2_key=r2_key, reason="Other")

    # Step 3: mark persisted.
    try:
        _update_persisted(
            client,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=skill_name,
            content_hash=body.content_hash,
        )
    except Exception as exc:  # noqa: BLE001 — D1 errors are recoverable
        logger.warning(
            "skill_capture: D1 UPDATE persisted failed (persona=%s skill=%s err=%s)",
            persona_slug,
            skill_name,
            exc,
        )
        # The R2 object is already in place; the row is in 'pending'. The
        # reconciler will see it on next boot and re-attempt the (now
        # idempotent) R2 PUT + UPDATE pair.
        return CaptureResult(
            recorded=True, r2_status="pending", r2_key=r2_key, reason="D1UpdateFailed"
        )

    return CaptureResult(recorded=True, r2_status="persisted", r2_key=r2_key, reason=None)


def _safe_update_failed(
    client: D1Client,
    *,
    customer_slug: str,
    persona_slug: str,
    skill_name: str,
    content_hash: str,
    reason: str,
) -> None:
    """UPDATE the row to failed; swallow any secondary D1 errors so the
    caller's primary failure (R2) gets surfaced cleanly."""
    try:
        _update_failed(
            client,
            customer_slug=customer_slug,
            persona_slug=persona_slug,
            skill_name=skill_name,
            content_hash=content_hash,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 — D1 secondary failure is logged-only
        logger.warning(
            "skill_capture: D1 UPDATE failed (persona=%s skill=%s reason=%s err=%s)",
            persona_slug,
            skill_name,
            reason,
            exc,
        )


# ---------------------------------------------------------------------------
# Boot-time reconciler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileSummary:
    """Aggregate result of one reconciler pass."""

    scanned: int
    persisted: int
    failed: int
    skipped_missing_body: int


def reconcile_pending_bodies(
    client: D1Client,
    r2: R2Config | None,
    *,
    hermes_home: str,
    customer_slug: str,
    max_iterations: int = 500,
) -> ReconcileSummary:
    """Boot-time pass: retry any row in r2_status IN ('pending','failed').

    For each row, re-read the body from the per-profile skills directory,
    verify the hash, and re-attempt the R2 PUT. The hash check is the
    livelock guard — if the file's hash doesn't match the row's, the
    row's body is unrecoverable and the row is marked failed with
    ``BodyHashMismatch``.

    Bounded by ``max_iterations`` to prevent runaway loops on a
    pathological D1 state.
    """
    scanned = persisted = failed = skipped_missing_body = 0
    if r2 is None:
        logger.warning(
            "skill_capture: reconciler skipped (R2 env missing, customer=%s)", customer_slug
        )
        return ReconcileSummary(0, 0, 0, 0)

    rows = client.query(_SELECT_PENDING_SQL)
    for row in rows[:max_iterations]:
        scanned += 1
        # D1Client.query returns sqlite3.Row-shaped tuples; access by index.
        c_slug, p_slug, s_name, c_hash, r2_key = row[0], row[1], row[2], row[3], row[4]
        body = read_skill_body(hermes_home=hermes_home, persona_slug=p_slug, skill_name=s_name)
        if body is None:
            _safe_update_failed(
                client,
                customer_slug=c_slug,
                persona_slug=p_slug,
                skill_name=s_name,
                content_hash=c_hash,
                reason="BodyMissingOnVolume",
            )
            skipped_missing_body += 1
            continue
        if body.content_hash != c_hash:
            _safe_update_failed(
                client,
                customer_slug=c_slug,
                persona_slug=p_slug,
                skill_name=s_name,
                content_hash=c_hash,
                reason="BodyHashMismatch",
            )
            failed += 1
            continue
        try:
            put_skill_body(r2, r2_key, body.body_bytes)
        except R2WriteError as exc:
            _safe_update_failed(
                client,
                customer_slug=c_slug,
                persona_slug=p_slug,
                skill_name=s_name,
                content_hash=c_hash,
                reason=exc.reason,
            )
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001
            _safe_update_failed(
                client,
                customer_slug=c_slug,
                persona_slug=p_slug,
                skill_name=s_name,
                content_hash=c_hash,
                reason="Other",
            )
            logger.warning(
                "skill_capture reconcile: R2 PUT raised unexpected exception "
                "(persona=%s skill=%s err=%s)",
                p_slug,
                s_name,
                exc,
            )
            failed += 1
            continue
        try:
            _update_persisted(
                client,
                customer_slug=c_slug,
                persona_slug=p_slug,
                skill_name=s_name,
                content_hash=c_hash,
            )
            persisted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "skill_capture reconcile: D1 UPDATE persisted failed (skill=%s err=%s)",
                s_name,
                exc,
            )
            # Row stays in pending; next boot retries.
    return ReconcileSummary(
        scanned=scanned,
        persisted=persisted,
        failed=failed,
        skipped_missing_body=skipped_missing_body,
    )


__all__ = [
    "AGENT_SKILLS_INVENTORY_DDL",  # re-export for tests + bootstrap
    "CaptureResult",
    "R2Config",
    "R2WriteError",
    "ReconcileSummary",
    "SkillBody",
    "capture_skill_body",
    "compute_content_hash",
    "load_r2_config_from_env",
    "make_r2_key",
    "put_skill_body",
    "read_skill_body",
    "reconcile_pending_bodies",
]


# Late re-export to satisfy __all__ without circular imports.
from .schemas import AGENT_SKILLS_INVENTORY_DDL  # noqa: E402, F401
