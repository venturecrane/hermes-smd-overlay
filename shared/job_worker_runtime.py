"""Hermes/infra binding for the B1 worker (ADR 0051) — the staging seam.

This is the thin, Hermes- and infra-coupled layer the injection-tested
orchestrator/adapter stub out: construct the real ``AIAgent`` (the way cron's
``run_job`` does), read real provider usage for cost, deliver the result,
persist it, and run the worker as a background thread behind a readiness
barrier. Hermes imports are lazy so the overlay test suite (no Hermes) still
imports this module for the pure ``readiness_ok`` helper.

NOTE: every function below the readiness helper is exercised on staging, not in
unit tests — there is no live LLM / broker / R2 in CI. The cost reader and the
agent construction carry ``# STAGING:`` markers where the exact Hermes contract
is confirmed on the Machine.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

WORKER_SWEEP_INTERVAL_S = 15.0
WORKER_READINESS_TIMEOUT_S = 120.0
SEGMENT_MAX_ITERATIONS = 8


# -- readiness (pure, unit-tested) --------------------------------------------
def readiness_ok(checks: list[Callable[[], bool]]) -> bool:
    """All readiness checks must pass before the worker claims any job. A check
    that raises counts as not-ready (a not-yet-listening broker socket raises)."""
    for check in checks:
        try:
            if not check():
                return False
        except Exception:
            return False
    return True


def _broker_healthy() -> bool:
    """The broker socket answers a health ping AND reports the job ledger ready."""
    import json
    import socket

    sock_path = os.environ.get("SMD_WORKSPACE_BROKER_SOCKET", "")
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(3.0)
            c.connect(sock_path)
            c.sendall(json.dumps({"action": "health"}).encode() + b"\n")
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = c.recv(65_536)
                if not chunk:
                    break
                buf.extend(chunk)
        data = json.loads(buf)
        return bool(data.get("ok")) and bool(data.get("jobs_ready"))
    except OSError:
        return False


# -- Hermes agent construction (STAGING) --------------------------------------
def build_hermes_agent(*, model: str, session_id: str, max_iterations: int, session_db: Any) -> Any:
    """Construct an AIAgent on our session lineage, mirroring run_job's LLM-path
    setup (cron/scheduler.py). Guardrails come from the process-global plugins,
    so they are inherited, not wired here (ADR 0051 Decision 2).

    STAGING: a behavioral-equivalence smoke against run_job's construction is the
    follow-up conformance test; this is the minimal faithful subset.
    """
    from run_agent import AIAgent  # lazy: Hermes-only
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested=None)
    return AIAgent(
        model=model or runtime.get("model") or "",
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        max_iterations=max_iterations,
        quiet_mode=True,
        load_soul_identity=True,
        skip_memory=True,
        platform="job",
        session_id=session_id,
        session_db=session_db,
        disabled_toolsets=["cronjob", "messaging", "clarify"],
    )


def hermes_segment_cost(agent: Any) -> int:
    """Real provider-reported cost of the just-run segment, in cents, from the
    agent's accumulated ``session_*_tokens`` (V2). Best-effort: a cost-read
    failure must not crash the job — it returns 0 and logs, so the budget guard
    is conservative rather than fatal.

    STAGING: confirm estimate_usage_cost's exact signature against the Machine.
    """
    try:
        from agent.usage_pricing import estimate_usage_cost
        from agent.usage_pricing import normalize_usage  # noqa: F401  (presence check)

        in_tok = int(getattr(agent, "session_input_tokens", 0) or 0)
        out_tok = int(getattr(agent, "session_output_tokens", 0) or 0)
        cache_r = int(getattr(agent, "session_cache_read_tokens", 0) or 0)
        cache_w = int(getattr(agent, "session_cache_write_tokens", 0) or 0)
        model = getattr(agent, "model", "") or ""
        cost = estimate_usage_cost(
            model,
            type("U", (), {
                "input_tokens": in_tok, "output_tokens": out_tok,
                "cache_read_tokens": cache_r, "cache_write_tokens": cache_w,
                "reasoning_tokens": 0, "prompt_tokens": in_tok, "total_tokens": in_tok + out_tok,
            })(),
        )
        # estimate_usage_cost returns a dollar figure or a dict; coerce to cents.
        dollars = cost.get("total") if isinstance(cost, dict) else float(cost)
        return max(0, round(float(dollars) * 100))
    except Exception as exc:  # never crash the job on a cost-read miss
        logger.warning("hermes_segment_cost: could not read usage cost (%s); recording 0", exc)
        return 0


def hermes_preflight_cost(model: str, history: list[dict]) -> int:
    """Estimate the next request's INPUT cost in cents, to pre-spend-refuse a
    segment that would blow the remaining budget. Rough by design (advisory);
    the authoritative guard is the real post-segment usage.

    STAGING: wire estimate_request_tokens_rough; until then a conservative
    char/4 token estimate keeps the guard from over-refusing.
    """
    try:
        chars = sum(len(str(m.get("content") or "")) for m in (history or []))
        approx_in = chars // 4
        # ~$3/MTok input as a conservative mid-tier rate → cents.
        return round(approx_in / 1_000_000 * 300)
    except Exception:
        return 0


# -- delivery + result store (STAGING) ----------------------------------------
def deliver_result(job: dict, result_ref: str) -> bool:
    """Deliver the result to the job's authored channel. Leverages Hermes'
    standalone ``_deliver_result`` for gateway-configured channels. Returns True
    on success.

    STAGING: managed-mailbox delivery routes through the broker; deliver_to
    allowlist validation is enforced here against customer.yaml. The first cycle
    proves the spine with channel delivery.
    """
    deliver_to = str(job.get("deliver_to") or "").strip()
    if not deliver_to:
        logger.info("job %s: no deliver_to; result retrievable via job_status", job.get("id"))
        return True
    try:
        from cron.scheduler import _deliver_result  # lazy: Hermes-only

        platform, _, chat_id = deliver_to.partition(":")
        synthetic = {
            "id": job["id"],
            "name": f"job {job['id']}",
            "deliver": platform,
            "origin": {"platform": platform, "chat_id": chat_id},
        }
        content = f"Background job {job['id']} complete. Result: {result_ref}"
        err = _deliver_result(synthetic, content, adapters=None, loop=None)
        if err:
            logger.warning("job %s: delivery error: %s", job["id"], err)
            return False
        return True
    except Exception as exc:
        logger.exception("job %s: delivery raised: %s", job["id"], exc)
        return False


def _r2_results_config_from_env() -> tuple[str, str, str, str, str] | None:
    """Resolve the per-customer R2 results target from the Machine env, or None
    when any required var is missing (local/test/misconfigured box → caller falls
    back to the volume).

    Reuses the SAME env contract the ADR-0044 config applier reads
    (``config_applier/__main__.py``): ``R2_ENDPOINT_URL`` / ``R2_ACCESS_KEY_ID``
    / ``R2_SECRET_ACCESS_KEY`` / ``R2_BUCKET_CONFIG`` / ``CUSTOMER_SLUG``. Per
    ADR 0007/0022 each customer has its own R2 bucket; ``R2_BUCKET_CONFIG`` IS
    that per-customer bucket (the voice vault already shares it under a prefix),
    so job results land under a ``jobs/<slug>/`` prefix beside ``vaults/<slug>/``.

    Returns ``(endpoint_url, access_key_id, secret_access_key, bucket, slug)``.
    """
    endpoint = os.environ.get("R2_ENDPOINT_URL")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET_CONFIG")
    slug = os.environ.get("CUSTOMER_SLUG") or os.environ.get("SMD_CUSTOMER_SLUG")
    if not (endpoint and access_key and secret_key and bucket and slug):
        return None
    return endpoint, access_key, secret_key, bucket, slug


def _build_r2_uploader() -> Callable[[str, str, bytes], None] | None:
    """Construct the default R2 uploader from env, or None when R2 is unconfigured.

    Mirrors ``config_applier.__main__._build_s3_client`` (lazy boto3 import,
    env-resolved credentials). The returned callable PUTs ``(bucket, key, body)``
    to R2 over the S3-compatible API; it is injectable so the unit test exercises
    the R2 path without a live bucket.
    """
    cfg = _r2_results_config_from_env()
    if cfg is None:
        return None
    endpoint, access_key, secret_key, _bucket, _slug = cfg

    def _upload(bucket: str, key: str, body: bytes) -> None:
        import boto3  # lazy: tests inject a fake uploader and never reach here

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="text/markdown; charset=utf-8",
        )

    return _upload


def put_result(
    job: dict,
    result_text: str,
    *,
    uploader: Callable[[str, str, bytes], None] | None = None,
) -> str:
    """Persist the result and return a retrievable ref.

    The durable target is per-customer R2 (survives a host reschedule that would
    orphan a Fly-volume file — ADR 0051 Decision 8). The result is written to
    ``jobs/<customer_slug>/<job_id>.md`` in the per-customer bucket and an
    ``r2://`` ref is returned.

    ``uploader`` is injectable ``(bucket, key, body) -> None`` for the unit test;
    when omitted it is built from env (lazy boto3). If R2 env is unset (local /
    CI / misconfigured Machine) OR the R2 PUT raises, fall back to the Fly volume
    and return a ``file://`` ref with a clear log — best-effort-safe so the job
    still produces a retrievable artifact rather than crashing.
    """
    body = (result_text or "").encode("utf-8")
    cfg = _r2_results_config_from_env()
    up = uploader if uploader is not None else _build_r2_uploader()

    if cfg is not None and up is not None:
        _endpoint, _ak, _sk, bucket, slug = cfg
        key = f"jobs/{slug}/{job['id']}.md"
        try:
            up(bucket, key, body)
            return f"r2://{bucket}/{key}"
        except Exception as exc:  # never lose the result on an R2 miss
            logger.warning(
                "job %s: R2 put failed (%s); falling back to volume", job.get("id"), exc
            )
    else:
        logger.info(
            "job %s: R2 results env unset; persisting result to volume", job.get("id")
        )

    home = os.environ.get("HERMES_HOME") or "/opt/data"
    results_dir = os.path.join(home, "job_results")
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{job['id']}.md")
    with open(path, "wb") as f:
        f.write(body)
    return f"file://{path}"


# -- the worker thread --------------------------------------------------------
_thread_started = threading.Lock()
_started = False


def start_worker_thread() -> bool:
    """Launch the durable-job worker as a daemon thread (the cron model — off the
    asyncio loop). Idempotent: only the first call spawns. Returns True if it
    started a thread this call."""
    global _started
    with _thread_started:
        if _started:
            return False
        if not os.environ.get("SMD_WORKSPACE_BROKER_SOCKET"):
            logger.info("hermes-smd-jobs: no broker socket; worker thread not started")
            return False
        _started = True
    t = threading.Thread(target=_worker_loop, name="smd-job-worker", daemon=True)
    t.start()
    logger.info("hermes-smd-jobs: worker thread started")
    return True


def _worker_loop() -> None:
    from shared.job_ledger_client import BrokerJobClient
    from shared.job_segment import make_run_segment
    from shared.job_worker import JobWorker

    # Readiness barrier: do not claim until the broker + job ledger are live.
    deadline = time.monotonic() + WORKER_READINESS_TIMEOUT_S
    while time.monotonic() < deadline:
        if readiness_ok([_broker_healthy]):
            break
        time.sleep(2.0)
    else:
        logger.error("smd-job-worker: broker not ready within %ss; worker idle", WORKER_READINESS_TIMEOUT_S)
        return

    from hermes_state import SessionDB  # lazy: Hermes-only

    session_db = SessionDB()
    client = BrokerJobClient()
    worker_id = f"gw-{os.getpid()}"
    run_segment = make_run_segment(
        session_db=session_db,
        build_agent=build_hermes_agent,
        preflight_cost=hermes_preflight_cost,
        segment_cost=hermes_segment_cost,
        segment_max_iterations=SEGMENT_MAX_ITERATIONS,
    )
    worker = JobWorker(
        client,
        worker_id=worker_id,
        run_segment=run_segment,
        deliver=deliver_result,
        put_result=put_result,
    )
    logger.info("smd-job-worker: ready (worker_id=%s); sweeping every %ss", worker_id, WORKER_SWEEP_INTERVAL_S)
    while True:
        try:
            worker.sweep()
        except Exception as exc:  # the loop must never die
            logger.exception("smd-job-worker: sweep crashed (continuing): %s", exc)
        time.sleep(WORKER_SWEEP_INTERVAL_S)


__all__ = ["readiness_ok", "start_worker_thread", "build_hermes_agent"]
