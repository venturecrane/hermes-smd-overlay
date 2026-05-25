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
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("aie.voice.samples")


# Maximum number of samples to inject into a single pre_llm_call. The
# context budget is the constraint — a typical structural-diff JSON is
# 400-800 bytes, so 8 samples is roughly 3-6 KB of prompt overhead.
MAX_SAMPLES_PER_TURN = 8


class R2SampleReader(Protocol):
    """Per-customer R2 binding for sample retrieval.

    The Fly Machine env binds an R2 client tagged with
    ``SMD_R2_VOICE_BINDING`` to the customer's namespace; in the
    overlay process this binding is exposed via a small adapter that
    implements the two methods below.
    """

    async def list_keys(self, prefix: str) -> list[str]: ...

    async def get(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class _SampleHit:
    """One ranked sample, parsed from R2."""

    key: str
    cohort: str
    diff: dict


async def retrieve_relevant_samples_async(
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
        keys = await r2_reader.list_keys(prefix)
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
            raw = await r2_reader.get(key)
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


def retrieve_relevant_samples(customer_slug: str, query_context: dict) -> list[dict]:
    """Synchronous facade for legacy callers.

    Not used by the hook (which is async) — kept for parity with the
    original stub signature so tests that call this directly continue
    to compile. In the runtime path callers should prefer
    :func:`retrieve_relevant_samples_async`.
    """
    if not customer_slug:
        return []
    # Synchronous path has no R2 reader wired; an actual sync caller
    # would need to bind a sync R2 client, which is not part of the
    # overlay's runtime surface. Returning empty here matches the
    # graceful-degradation contract.
    log.debug(
        "voice.samples.sync_facade_called customer=%s; returning empty",
        customer_slug,
    )
    return []


def render_sample_block(samples: list[dict]) -> str:
    """Render a sample list into a compact text block for prompt injection.

    The block is intended to ride on the user message via pre_llm_call's
    ``{"context": "<text>"}`` return shape. Hermes joins each plugin's
    context contribution with ``\\n\\n`` (per docs/hook-surface.md §3),
    so the block produced here is self-contained.

    Empty samples produce an empty string; the hook returns ``None`` in
    that case to keep the user message unchanged.
    """
    if not samples:
        return ""

    lines: list[str] = [
        "[voice samples: <greeting>, <signoff>, avg sentence length]",
    ]
    for i, diff in enumerate(samples, start=1):
        greeting = diff.get("greeting_style", "unknown")
        signoff = diff.get("signoff_style", "unknown")
        avg_len = diff.get("avg_sentence_length", 0.0)
        cohort = diff.get("recipient_cohort", "unassigned")
        lines.append(
            f"  {i}. cohort={cohort} greeting={greeting} signoff={signoff} "
            f"avg_sentence_len={avg_len}"
        )
    return "\n".join(lines)


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
    "render_sample_block",
    "retrieve_relevant_samples",
    "retrieve_relevant_samples_async",
]
