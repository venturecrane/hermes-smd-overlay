"""Voice-sample retrieval from per-customer R2 vault.

Samples live at R2 path: ``vaults/<customer-slug>/voice/samples/`` (and
the ingestion pipeline writes them at
``<customer-slug>/voice/cohort/<cohort-id>/<sample-id>.json``). Each
sample is a JSON file with: ``schema_version``, ``word_count``,
``sentence_count``, ``paragraph_count``, ``subject_word_count``,
``avg_sentence_length``, ``sentence_length_distribution``,
``greeting_style``, ``signoff_style``, ``opener_template``,
``closer_template``, ``punctuation_rhythm``, ``recipient_cohort``.

This module is the read-only retrieval surface used by the
``pre_llm_call`` hook to inject relevant samples into the user message,
and by :func:`evaluate_draft_voice_fidelity` for observational
post-call scoring.

The runtime in production is a Cloudflare R2 binding wired into the
Fly Machine env. Locally the binding doesn't exist; the helpers below
degrade to returning an empty list rather than raising so the hook
stays exception-safe per AGENTS.md hard rule #3.

TODO(shared): When ss-console and this overlay share a runtime R2
client implementation, lift it into shared/ rather than duplicating
here. For §7 the implementation is in-overlay.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("aie.voice.samples")


# Maximum number of samples to inject into a single pre_llm_call. The
# context budget is the constraint — a typical structural-diff JSON is
# 400-800 bytes, so 8 samples is roughly 3-6 KB of prompt overhead.
MAX_SAMPLES_PER_TURN = 8


class R2SampleReader(Protocol):
    """Per-customer R2 reader for sample retrieval.

    The concrete implementation is :class:`R2VaultSampleReader` (built from the
    Machine's R2 env by :func:`reader_from_env`), an S3-compatible boto3 client
    over the per-customer config bucket. Tests supply a fake implementing the
    two synchronous methods below. Hermes invokes plugin callbacks
    synchronously, so the reader must complete before ``pre_llm_call`` returns
    its context contribution.
    """

    def list_keys(self, prefix: str) -> list[str]: ...

    def get(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class _SampleHit:
    """One ranked sample, parsed from R2."""

    key: str
    cohort: str
    diff: dict


# ---------------------------------------------------------------------------
# Concrete R2 reader — S3-compatible client over the per-customer R2 config
# bucket, which holds the voice vault at vaults/<slug>/voice/.
#
# This mirrors the proven boto3/R2 pattern in hermes-smd-audit/skill_capture.py
# (lazy boto3 import; env-resolved credentials; graceful None on misconfigured
# Machine). The Machine env sets R2_ENDPOINT_URL / R2_ACCESS_KEY_ID /
# R2_SECRET_ACCESS_KEY / R2_BUCKET_CONFIG (bootstrap.sh §"R2 secrets"); the
# voice vault lives in the config bucket, not a separate one. Hermes' plugin
# dispatcher is synchronous, which matches boto3's API.
# ---------------------------------------------------------------------------


class R2VaultSampleReader:
    """An :class:`R2SampleReader` backed by the customer's R2 config bucket.

    Constructed from env via :func:`reader_from_env`. Keys are relative to the
    bucket root; the caller passes ``<slug>/voice/cohort/`` prefixes, and this
    reader scopes them under the vault root ``vaults/`` automatically so the
    on-disk layout (``vaults/<slug>/voice/...``) matches customer.yaml's
    ``memory.r2_vault_path``.
    """

    _VAULT_ROOT = "vaults/"

    def __init__(
        self, *, endpoint_url: str, access_key_id: str, secret_access_key: str, bucket: str
    ):
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket = bucket

    def _client(self):
        import boto3  # type: ignore[import-not-found]  # lazy: see skill_capture.py

        return boto3.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
        )

    def _full_prefix(self, prefix: str) -> str:
        return f"{self._VAULT_ROOT}{prefix}"

    def list_keys(self, prefix: str) -> list[str]:
        s3 = self._client()
        full = self._full_prefix(prefix)
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": full}
            if token:
                kwargs["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                key = obj.get("Key")
                if key:
                    # Strip the vault root so callers (and _cohort_from_key) see
                    # the slug-relative key shape they expect.
                    keys.append(
                        key[len(self._VAULT_ROOT) :] if key.startswith(self._VAULT_ROOT) else key
                    )
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
        return keys

    def get(self, key: str) -> bytes:
        s3 = self._client()
        full = self._full_prefix(key)
        resp = s3.get_object(Bucket=self._bucket, Key=full)
        return resp["Body"].read()


def reader_from_env() -> R2VaultSampleReader | None:
    """Build an R2 voice-sample reader from the Machine env.

    Returns ``None`` when any required R2 var is missing — the binder logs a
    warning and the plugin stays inactive (it must never *silently* register as
    a no-op; see register() in __init__.py). Mirrors skill_capture's
    no-reader-on-missing-env posture, using the config-bucket credentials the
    bootstrap sets (R2_ENDPOINT_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
    R2_BUCKET_CONFIG).
    """
    endpoint = os.getenv("R2_ENDPOINT_URL")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    bucket = os.getenv("R2_BUCKET_CONFIG")
    if not (endpoint and access_key and secret_key and bucket):
        return None
    return R2VaultSampleReader(
        endpoint_url=endpoint,
        access_key_id=access_key,
        secret_access_key=secret_key,
        bucket=bucket,
    )


def retrieve_relevant_samples(
    *,
    customer_slug: str,
    r2_reader: R2SampleReader | None,
    query_context: dict | None = None,
    limit: int = MAX_SAMPLES_PER_TURN,
) -> list[dict]:
    """Return up to ``limit`` ranked voice samples for the current turn.

    Ranking heuristic: when ``query_context`` provides a
    ``recipient_cohort`` hint, samples tagged with that cohort sort
    first; remaining samples follow in arbitrary order. If no cohort
    hint is provided, samples are returned in the order R2 lists them.

    Returns a list of structural-diff dicts (NOT raw bytes) so callers
    can feed them straight into :func:`evaluate_draft_voice_fidelity`
    or render them into a prompt context block.

    Exception-safe: any R2 error or parse failure logs a warning and
    yields an empty result. The pre_llm_call hook MUST stay quiet
    rather than crash the turn — degraded voice matching is fine,
    crashing the agent is not.
    """
    if r2_reader is None:
        return []

    prefix = f"{customer_slug}/voice/cohort/"
    try:
        keys = r2_reader.list_keys(prefix)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice.samples.list_failed customer=%s prefix=%s err=%s",
            customer_slug,
            prefix,
            exc,
        )
        return []

    if not keys:
        return []

    requested_cohort = None
    if query_context:
        requested_cohort = query_context.get("recipient_cohort") or query_context.get("cohort")

    hits: list[_SampleHit] = []
    for key in keys:
        cohort = _cohort_from_key(key)
        try:
            raw = r2_reader.get(key)
            diff = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "voice.samples.get_failed customer=%s key=%s err=%s",
                customer_slug,
                key,
                exc,
            )
            continue
        if not isinstance(diff, dict):
            continue
        hits.append(_SampleHit(key=key, cohort=cohort, diff=diff))

    if requested_cohort:
        hits.sort(key=lambda h: 0 if h.cohort == requested_cohort else 1)

    return [h.diff for h in hits[:limit]]


def render_sample_block(samples: list[dict]) -> str:
    """Render samples into a model-actionable author-voice profile block.

    Injected via pre_llm_call's ``{"context": "<text>"}`` so the model
    drafts in the author's register. Aggregates across the supplied
    structural-diff fingerprints (style only — never raw content) into a
    compact directive surfacing the signal that actually distinguishes a
    writer: sentence brevity + length distribution, punctuation rhythm,
    and greeting/signoff habits.

    Surfaces the rhythm signal — ``sentence_length_distribution`` and
    ``punctuation_rhythm`` — that the differ computes and stores. The
    prior version dropped both, injecting only greeting/signoff/avg, a
    near-empty label that could not shape a draft (the differ's signal
    was computed, stored, and then silently discarded at render time).

    Empty samples produce an empty string; the hook returns ``None`` in
    that case to keep the user message unchanged.
    """
    if not samples:
        return ""

    n = len(samples)
    greeting = _dominant(samples, "greeting_style")
    signoff = _dominant(samples, "signoff_style")
    avg_len = _mean_float(samples, "avg_sentence_length")
    length_mix = _aggregate_distribution(samples)
    punctuation = _mean_punctuation(samples)

    lines: list[str] = [
        "[author voice profile — draft in this writer's style; match the rhythm and brevity below]",
        f"derived from {n} of the author's own message{'s' if n != 1 else ''} "
        "(style only, no content)",
        f"- greeting: {greeting}; sign-off: {signoff} "
        "(do not add salutations or closers the author omits)",
        f"- typical sentence length: ~{avg_len:.1f} words",
    ]
    if length_mix:
        lines.append(f"- sentence-length mix: {length_mix}")
    if punctuation:
        lines.append(f"- punctuation per 100 words: {punctuation}")
    return "\n".join(lines)


# Bucket keys emitted by diff._distribute_sentence_lengths, in ascending order,
# with human-readable word-count ranges for the prompt.
_LENGTH_BUCKET_LABELS = [
    ("lt_5", "<5w"),
    ("lt_10", "5-9w"),
    ("lt_20", "10-19w"),
    ("lt_35", "20-34w"),
    ("gte_35", "35w+"),
]

# Keys emitted by diff._punctuation_rhythm (normalized per 100 words).
_PUNCT_LABELS = [
    ("period_per_100", "period"),
    ("comma_per_100", "comma"),
    ("semicolon_per_100", "semicolon"),
    ("dash_per_100", "dash"),
    ("question_per_100", "question"),
    ("exclamation_per_100", "exclamation"),
]


def _dominant(samples: list[dict], field: str) -> str:
    """Most common non-empty value of ``field`` across samples."""
    vals = [s.get(field) for s in samples if isinstance(s, dict) and s.get(field)]
    if not vals:
        return "unknown"
    return Counter(vals).most_common(1)[0][0]


def _mean_float(samples: list[dict], field: str) -> float:
    """Mean of a numeric ``field`` across samples; 0.0 when none present."""
    vals: list[float] = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        try:
            vals.append(float(s.get(field, 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def _aggregate_distribution(samples: list[dict]) -> str:
    """Sum the per-sample sentence-length histograms into percentages.

    Defensive: the diffs come from R2 JSON, so any bucket may be absent
    or non-numeric. Returns "" when there's no usable distribution.
    """
    totals: dict[str, int] = {}
    grand = 0
    for s in samples:
        if not isinstance(s, dict):
            continue
        dist = s.get("sentence_length_distribution")
        if not isinstance(dist, dict):
            continue
        for key, val in dist.items():
            try:
                count = int(val)
            except (TypeError, ValueError):
                continue
            totals[key] = totals.get(key, 0) + count
            grand += count
    if grand == 0:
        return ""
    parts: list[str] = []
    for key, label in _LENGTH_BUCKET_LABELS:
        count = totals.get(key, 0)
        if count:
            parts.append(f"{label} {round(100 * count / grand)}%")
    return " / ".join(parts)


def _mean_punctuation(samples: list[dict]) -> str:
    """Mean per-100-word punctuation rates across samples.

    Skips marks averaging below 0.5/100w to keep the directive tight.
    Returns "" when no rhythm data is present.
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for s in samples:
        if not isinstance(s, dict):
            continue
        rhythm = s.get("punctuation_rhythm")
        if not isinstance(rhythm, dict):
            continue
        for key, val in rhythm.items():
            try:
                rate = float(val)
            except (TypeError, ValueError):
                continue
            sums[key] = sums.get(key, 0.0) + rate
            counts[key] = counts.get(key, 0) + 1
    if not sums:
        return ""
    parts: list[str] = []
    for key, label in _PUNCT_LABELS:
        if counts.get(key):
            mean = sums[key] / counts[key]
            if mean >= 0.5:
                parts.append(f"{label} {round(mean)}")
    return ", ".join(parts)


def _cohort_from_key(key: str) -> str:
    """Extract the cohort segment from an R2 key shaped as
    ``{slug}/voice/cohort/{cohort}/{sample-id}.json``. Returns
    ``"unassigned"`` when the key shape doesn't match.
    """
    parts = key.split("/")
    try:
        cohort_idx = parts.index("cohort")
    except ValueError:
        return "unassigned"
    if cohort_idx + 1 >= len(parts):
        return "unassigned"
    return parts[cohort_idx + 1] or "unassigned"


__all__ = [
    "MAX_SAMPLES_PER_TURN",
    "R2SampleReader",
    "R2VaultSampleReader",
    "reader_from_env",
    "render_sample_block",
    "retrieve_relevant_samples",
]
