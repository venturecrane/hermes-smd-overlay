"""Root-owned config applier orchestration (ADR 0044 WS3).

Ties together the five steps of a live config apply, with every side effect
injected so the module is fully unit-testable:

    pull (R2) → validate (parity validator) → safety (live-apply checks)
        → atomic write (volume) → audit (CONFIG_WRITE row)

The boot script constructs the real S3 client (boto3) and the broker-aware
audit client (:func:`shared.audit_client.audit_client_from_env`) and calls
:func:`apply`. Tests pass fakes. :func:`apply` never raises on a rejected or
deferred change — it returns a structured :class:`ApplyResult`; it raises only
on a genuine I/O fault the caller cannot recover from (and even then the volume
is never left half-written, because the write is atomic).

Fail-closed posture: an unparseable pulled config, a validation error, a
floor violation, or a rebuild-class path in the diff all REJECT (no write). The
running Machine keeps its current config.
"""

from __future__ import annotations

import enum
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bootstrap.validate import validate_customer_yaml
from config_applier import safety
from shared.audit_contract import INSERT_SQL, agent_event_params
from shared.ids import sha256

logger = logging.getLogger(__name__)


class ConfigApplyError(RuntimeError):
    """An unrecoverable fault during apply (R2 read failure, write failure).

    A *rejected* or *deferred* change is NOT an error — those are returned in
    :class:`ApplyResult`. This is raised only when the caller cannot proceed at
    all (the source object could not be read, or the atomic write failed). The
    volume is never left half-written: the write is atomic, so on a write fault
    the previous config remains intact.
    """


class ApplyOutcome(str, enum.Enum):
    """Terminal outcome of an apply attempt."""

    APPLIED = "applied"  # written to the volume + audited
    REJECTED = "rejected"  # validation / floor / non-writable path — no write
    DEFERRED = "deferred"  # valid but rebuild-class / widening — needs re-provision


@dataclass(frozen=True)
class ApplyResult:
    """Structured result of an apply attempt.

    ``outcome`` is the terminal state. ``reasons`` carries the human-readable
    rejection / deferral reasons (empty on success). ``changed`` is the diff of
    touched dotted paths (empty when the pulled config equals the on-volume one).
    ``epoch`` is the config-epoch stamped on a successful apply (``None`` when
    nothing was written). ``audited`` is True iff the CONFIG_WRITE row was
    emitted (a write succeeds even if the audit emission later fails — audit is
    observability, not a gate — but the flag records it).
    """

    outcome: ApplyOutcome
    reasons: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    epoch: int | None = None
    audited: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.outcome is ApplyOutcome.APPLIED


# ---------------------------------------------------------------------------
# Step 1 — pull from R2
# ---------------------------------------------------------------------------


def config_key(slug: str) -> str:
    """The R2 object key for a customer's authored config: ``vaults/<slug>/customer.yaml``.

    The slug is the isolation boundary — every customer's config lives under its
    own ``vaults/<slug>/`` prefix (matches the ``memory.r2_vault_path`` invariant
    enforced by the validator). A blank slug is rejected so a pull can never
    accidentally address another customer's vault root.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise ConfigApplyError("config_key: customer slug is required")
    return f"vaults/{slug.strip()}/customer.yaml"


def pull_config(s3_client: Any, bucket: str, slug: str) -> bytes:
    """Read ``vaults/<slug>/customer.yaml`` from R2 and return its raw bytes.

    ``s3_client`` is an injected boto3-style client exposing ``get_object`` (the
    boot script passes a real R2 client; tests pass a fake). The body is read in
    full and returned verbatim — no parsing here, so the downstream validator
    sees exactly the authored bytes (the secret-scan raw pass depends on this).

    Raises:
        ConfigApplyError: the object is missing, unreadable, or the response has
            no readable ``Body`` — every R2 fault is wrapped so the caller has
            one error type to catch, and the running config is left untouched.
    """
    key = config_key(slug)
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 — every R2 fault is unrecoverable here
        raise ConfigApplyError(f"pull_config: could not read s3://{bucket}/{key}: {exc}") from exc

    body = response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
    if body is None:
        raise ConfigApplyError(f"pull_config: response for {key} has no Body")
    try:
        data = body.read()
    except Exception as exc:  # noqa: BLE001
        raise ConfigApplyError(f"pull_config: reading body of {key} failed: {exc}") from exc

    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)):
        raise ConfigApplyError(
            f"pull_config: body of {key} is not bytes (got {type(data).__name__})"
        )
    return bytes(data)


# ---------------------------------------------------------------------------
# Step 2 — validate (parity-hardened validator, re-used not reimplemented)
# ---------------------------------------------------------------------------


def validate_bytes(data: bytes) -> list[str]:
    """Validate raw ``customer.yaml`` bytes via the parity validator.

    ``bootstrap.validate.validate_customer_yaml`` takes a ``Path`` (it runs the
    raw-text secret scan before parsing), so the bytes are written to a private
    temp file inside a temp dir and validated there. The temp dir is removed
    afterward. Returns the validator's error-string list — empty means valid.

    Non-decodable bytes are reported as a single error rather than raising, so
    the caller treats it as a normal rejection (fail closed, no write).
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"customer.yaml is not valid UTF-8: {exc}"]

    with tempfile.TemporaryDirectory(prefix="cfg-apply-") as tmp:
        tmp_path = Path(tmp) / "customer.yaml"
        tmp_path.write_text(text)
        return validate_customer_yaml(tmp_path)


# ---------------------------------------------------------------------------
# Step 4 — atomic write
# ---------------------------------------------------------------------------


def atomic_write(path: str | Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path``, preserving the target's owner/mode.

    Writes to a temp file in the SAME directory (so ``os.replace`` is a
    same-filesystem atomic rename), fsyncs the file, then renames over the
    target. A reader on the volume sees either the old file or the new one,
    never a partial write — a crash mid-apply cannot corrupt the running config.
    The containing directory is fsynced too so the rename is durable across a
    Machine restart.

    OWNER/MODE PRESERVATION (on-box critical). ``tempfile.mkstemp`` creates the
    staging file as ``root:root 0600`` (the applier runs as root). Left as-is,
    the replaced ``/opt/data/customer.yaml`` would become root-owned 0600 and the
    ``hermes`` agent could no longer read its OWN config — enforcement fails
    closed / boot breaks. So when the target already exists, its uid/gid/mode are
    captured BEFORE the replace and restored onto the staging file BEFORE the
    rename, so the file appears at the target path already wearing the right
    owner+mode (live file is ``hermes:hermes 0644``). For a brand-new target
    (initial seed) there is nothing to preserve — the caller's umask/explicit
    chown governs, matching prior behavior.

    Raises:
        ConfigApplyError: the write or rename failed. On failure the temp file
            is cleaned up and the target is left as it was (the rename is the
            only mutation of the target, and it is atomic).
    """
    target = Path(path)
    if not isinstance(data, (bytes, bytearray)):
        raise ConfigApplyError("atomic_write: data must be bytes")
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigApplyError(f"atomic_write: cannot create {directory}: {exc}") from exc

    # Capture the existing target's ownership/mode BEFORE we touch anything, so a
    # root-run replace restores hermes:hermes 0644 rather than leaving root:root
    # 0600. ``None`` means the target does not exist yet (initial seed).
    preserve = _stat_for_preserve(target)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Restore owner+mode onto the staging file BEFORE the rename so the file
        # is never momentarily unreadable at the target path.
        _apply_preserve(tmp_path, preserve)
        os.replace(tmp_path, target)
        _fsync_dir(directory)
    except OSError as exc:
        # Best-effort cleanup of the temp file; the target is untouched (the
        # replace either happened fully or not at all).
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("atomic_write: could not remove temp file %s", tmp_path)
        raise ConfigApplyError(f"atomic_write: writing {target} failed: {exc}") from exc


def _stat_for_preserve(target: Path) -> tuple[int, int, int] | None:
    """Return ``(uid, gid, mode)`` of an existing target, or ``None`` if absent.

    ``mode`` is the permission bits (``st_mode & 0o7777``). A stat failure on an
    existing file is surfaced as ``None`` (we then fall back to default temp-file
    ownership) and logged — better to apply with default perms than to refuse the
    config write; the boot-time chown in entrypoint is the backstop.
    """
    try:
        st = target.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("atomic_write: could not stat %s for owner/mode preserve: %s", target, exc)
        return None
    return (st.st_uid, st.st_gid, st.st_mode & 0o7777)


def _apply_preserve(tmp_path: Path, preserve: tuple[int, int, int] | None) -> None:
    """Restore captured ``(uid, gid, mode)`` onto the staging file before rename.

    No-op when ``preserve`` is ``None`` (initial seed — no prior file). chown is
    attempted before chmod; a chown failure (e.g. not privileged in a dev/test
    run, or the target was root-owned and we are not root) is logged and the mode
    is still applied — ownership preservation is the root-on-box concern, and a
    non-root test run cannot chown to an arbitrary uid anyway.
    """
    if preserve is None:
        return
    uid, gid, mode = preserve
    try:
        os.chown(tmp_path, uid, gid)
    except OSError as exc:
        logger.warning(
            "atomic_write: could not restore owner %d:%d on %s (%s); mode still applied",
            uid,
            gid,
            tmp_path,
            exc,
        )
    try:
        os.chmod(tmp_path, mode)
    except OSError as exc:
        logger.warning("atomic_write: could not restore mode %o on %s: %s", mode, tmp_path, exc)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename within it is durable. Best-effort: some
    filesystems disallow opening a directory for fsync — that is not fatal to
    the apply (the file rename already happened atomically)."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        logger.debug("atomic_write: directory fsync of %s not supported", directory)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Step 5 — audit
# ---------------------------------------------------------------------------


def _emit_config_write(
    *,
    audit_client: Any,
    slug: str,
    epoch: int,
    changed: tuple[str, ...],
    new_digest: str | None,
    old_digest: str | None,
) -> bool:
    """Emit one CONFIG_WRITE audit row. Returns True on success, False on
    failure (audit is observability, never a gate — a write that already
    happened is not rolled back because its audit row failed to persist).

    The row carries the customer slug, the new epoch, the count + list of
    changed paths, and the input/output digests — never the config CONTENT
    (which can carry token_ref / secret material). The paths are field names,
    safe to record. Built via the shared audit contract so the column order
    can never drift from the other writers.
    """
    metadata = {
        "config_apply": True,
        "customer": slug,
        "epoch": epoch,
        "changed_count": len(changed),
        # Cap the recorded path list so a pathological diff cannot bloat the row;
        # the count above is always exact.
        "changed_paths": list(changed[:64]),
    }
    params = agent_event_params(
        action_type="CONFIG_WRITE",
        metadata=metadata,
    )
    # agent_event_params leaves the digest columns NULL (its signature does not
    # take them). Splice the config digests into the positional row via the
    # contract's column index so output/diff carry the provenance without the
    # content. COLUMNS == (id, ts, action_type, actor, actor_role, skill_name,
    # matter_ref, input_digest, output_digest, diff_digest, trust_ceiling,
    # metadata). input_digest[7] = old config, output_digest[8] = new config.
    params[7] = old_digest
    params[8] = new_digest
    try:
        audit_client.execute(INSERT_SQL, *params)
        return True
    except Exception as exc:  # noqa: BLE001 — audit failure never undoes the write
        logger.warning(
            "config_applier: CONFIG_WRITE audit emission failed (%s); "
            "config was written, audit row was not persisted",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _load_current(path: Path) -> dict[str, Any]:
    """Best-effort parse of the on-volume config for the safety diff.

    A missing or unparseable current file is treated as ``{}`` — the first apply
    onto a fresh Machine has no prior config, and a corrupt current file should
    not block replacing it with a validated good one (every path then reads as
    changed, and the live-writability check governs whether that is allowed)."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "config_applier: current config at %s unreadable (%s); treating as empty", path, exc
        )
        return {}
    return data if isinstance(data, dict) else {}


def _validate_and_parse(new_bytes: bytes) -> tuple[dict[str, Any] | None, ApplyResult | None]:
    """Validate raw bytes and parse them to a mapping for the safety diff.

    Returns ``(cfg, None)`` on success, or ``(None, ApplyResult REJECTED)`` when
    validation fails or the validated bytes do not parse as a mapping. The second
    parse is belt-and-suspenders — validation already proved a mapping — so its
    failure branches are fail-closed guards, not the expected path.
    """
    errors = validate_bytes(new_bytes)
    if errors:
        return None, ApplyResult(outcome=ApplyOutcome.REJECTED, reasons=tuple(errors))
    try:
        cfg = yaml.safe_load(new_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:  # pragma: no cover - validate caught it
        return None, ApplyResult(
            outcome=ApplyOutcome.REJECTED, reasons=(f"post-validate parse failed: {exc}",)
        )
    if not isinstance(cfg, dict):
        return None, ApplyResult(
            outcome=ApplyOutcome.REJECTED, reasons=("validated config is not a mapping",)
        )
    return cfg, None


def _safety_gate(
    old_cfg: dict[str, Any],
    new_cfg: dict[str, Any],
    changed: tuple[str, ...],
    *,
    allow_deferred_paths: bool,
) -> ApplyResult | None:
    """Run the live-apply safety checks. Returns a REJECTED/DEFERRED result to
    stop the apply, or ``None`` to proceed to the write.

    The non-live-writable gate governs the LIVE path (replacing a config the
    Machine already booted). An INITIAL seed (no config on the volume yet —
    ``old_cfg`` empty) legitimately writes the whole document, rebuild-class
    fields included; there is no running image to diverge from, so the gate is
    skipped. The compliance-floor check runs in BOTH cases — a floor can never be
    widened past, even on first seed.
    """
    is_initial = not old_cfg
    non_writable = () if is_initial else tuple(safety.non_live_writable_changes(old_cfg, new_cfg))
    if non_writable:
        reason = "change touches rebuild-class (non-live-writable) paths: " + ", ".join(
            non_writable
        )
        outcome = ApplyOutcome.DEFERRED if allow_deferred_paths else ApplyOutcome.REJECTED
        return ApplyResult(outcome=outcome, reasons=(reason,), changed=changed)

    if not safety.floor_preserving(old_cfg, new_cfg):
        return ApplyResult(
            outcome=ApplyOutcome.REJECTED,
            reasons=("change would raise an action-class ceiling above its vertical floor",),
            changed=changed,
        )
    return None


def apply(
    *,
    s3_client: Any,
    bucket: str,
    slug: str,
    volume_path: str | Path = "/opt/data/customer.yaml",
    audit_client: Any,
    prev_epoch: int | None = None,
    allow_deferred_paths: bool = False,
) -> ApplyResult:
    """Pull, validate, safety-check, atomically write, and audit a config apply.

    Steps (fail-closed — any failure short of a write yields REJECTED/DEFERRED,
    never a partial write):

      1. Pull ``vaults/<slug>/customer.yaml`` from R2 (raises ConfigApplyError on
         an unrecoverable read fault — the running config is left untouched).
      2. Validate the raw bytes with the parity validator (secret scan + enums).
         Any error → REJECTED. (``_validate_and_parse``.)
      3. Safety: compute the diff vs the on-volume config; reject if it touches a
         rebuild-class (non-live-writable) path unless ``allow_deferred_paths``
         routes that to DEFERRED; reject if it would widen past a vertical
         compliance floor. (``_safety_gate``.)
      4. Atomic write to ``volume_path`` (raises ConfigApplyError on write fault;
         the volume is never half-written).
      5. Emit a CONFIG_WRITE audit row (failure is logged, not fatal).

    ``prev_epoch`` is the last applied epoch; the new config is stamped
    ``next_epoch(prev_epoch)``. When ``allow_deferred_paths`` is False (the
    default — the live path), a rebuild-class path REJECTS; when True, it returns
    DEFERRED so a supervisor can route the change to a re-provision instead.

    Returns:
        ApplyResult with outcome APPLIED / REJECTED / DEFERRED.

    Raises:
        ConfigApplyError: an unrecoverable R2 read or volume write fault.
    """
    volume = Path(volume_path)

    # Step 1 — pull (may raise ConfigApplyError; that propagates by design).
    new_bytes = pull_config(s3_client, bucket, slug)

    # Step 2 — validate + parse.
    new_cfg, rejected = _validate_and_parse(new_bytes)
    if rejected is not None:
        return rejected
    assert new_cfg is not None  # narrowed: rejected is None ⇒ cfg parsed

    old_cfg = _load_current(volume)
    changed = tuple(safety.changed_paths(old_cfg, new_cfg))

    # Step 3 — safety gate.
    stop = _safety_gate(old_cfg, new_cfg, changed, allow_deferred_paths=allow_deferred_paths)
    if stop is not None:
        return stop

    epoch = safety.next_epoch(prev_epoch)

    # Step 4 — atomic write (may raise ConfigApplyError; propagates by design).
    atomic_write(volume, new_bytes)

    # Step 5 — audit (best-effort).
    audited = _emit_config_write(
        audit_client=audit_client,
        slug=slug,
        epoch=epoch,
        changed=changed,
        new_digest=sha256(new_bytes),
        old_digest=sha256(yaml.safe_dump(old_cfg, sort_keys=True).encode()) if old_cfg else None,
    )

    return ApplyResult(
        outcome=ApplyOutcome.APPLIED,
        changed=changed,
        epoch=epoch,
        audited=audited,
        metadata={"changed_count": len(changed)},
    )


__all__ = [
    "ApplyOutcome",
    "ApplyResult",
    "ConfigApplyError",
    "apply",
    "atomic_write",
    "config_key",
    "pull_config",
    "validate_bytes",
]
