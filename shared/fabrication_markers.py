"""HIGH_RISK_MARKERS registry + matcher — Tier-1 universal fabrication scan.

ADR 0028 outbound provenance gate. This module is the data-driven half of the
gate's most-universal tier: a set of banned fabrication-marker strings that
must NEVER appear in a draft body the agent produces, regardless of vertical.

These are the CLAUDE.md Pattern-A / Pattern-B + tone-rule banned strings
(committed template sentences that imply uncontracted commitments, runtime
fabrication from non-authoritative fields, and banned style markers). Matching
is case-insensitive; markers are either literal phrases or regexes.

Single source of truth
----------------------
The canonical marker registry lives in **ss-console** at
``ai-employee/safety-substrate/fabrication_markers.json`` (authored by PR-B).
This overlay vendors a BYTE-EXACT copy at ``shared/fabrication_markers.json``.
The two must not drift:
``tests/test_outbound_gate.py::test_vendored_markers_match_canonical_sha256_and_version``
pins the vendored bytes (sha256) AND the version string to the canonical
artifact, so any edit on either side fails CI until both are updated together.

The vendored copy is the final canonical artifact (ss-console PR #1151,
version ``2026-05-29.2``, 14 markers). Schema is ``{id, kind, value, note}``
with ``kind`` ∈ literal (exact, case-sensitive) | literal_ci | regex (both
case-insensitive); ``pattern``/``reason`` remain accepted as legacy aliases.
TODO(post-merge): replace the hand-copied file + sha pin with a build-time
vendoring step (pinned raw-URL fetch from ss-console main) once the cadence of
artifact changes warrants the automation.

Fail-closed posture
-------------------
A gate that cannot evaluate must BLOCK, never pass. Concretely:

* If the JSON file is missing / unreadable / malformed → ``load_markers``
  raises. Callers (the outbound gate) treat a raised marker-load as a block.
* A marker with a malformed regex is dropped from the compiled matcher with a
  warning, BUT the registry as a whole must still contain at least one usable
  marker; an all-malformed registry raises so the gate cannot silently degrade
  to "no markers, everything clean."
"""

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


# Path to the vendored canonical registry, co-located with this module.
_MARKERS_PATH = Path(__file__).with_name("fabrication_markers.json")


class FabricationMarkersError(RuntimeError):
    """Raised when the marker registry cannot be loaded or is unusable.

    The outbound gate treats this as a block: an indeterminate marker scan
    must never let a draft body through (fail-closed, ADR 0028).
    """


@dataclass(frozen=True)
class MarkerHit:
    """One matched banned marker inside a scanned body.

    ``marker_id`` and ``reason`` come from the registry; ``match`` is the
    literal substring that hit (used in the audit row — NOT the full body).
    """

    marker_id: str
    reason: str
    match: str


@dataclass(frozen=True)
class _CompiledMarker:
    marker_id: str
    reason: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class MarkerRegistry:
    """Loaded + compiled marker registry.

    ``version`` is carried so the structural test (and the future hash-check
    against ss-console) can assert provenance.
    """

    version: str
    source: str
    markers: tuple[_CompiledMarker, ...]

    def scan(self, body: str) -> list[MarkerHit]:
        """Return every banned-marker hit in ``body``. Empty list = clean."""
        if not isinstance(body, str) or not body:
            return []
        hits: list[MarkerHit] = []
        seen: set[tuple[str, str]] = set()
        for marker in self.markers:
            for m in marker.pattern.finditer(body):
                matched = m.group(0)
                key = (marker.marker_id, matched.lower())
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    MarkerHit(
                        marker_id=marker.marker_id,
                        reason=marker.reason,
                        match=matched,
                    )
                )
        return hits

    def contains_marker(self, body: str) -> bool:
        """Fast yes/no: True if any banned marker matches ``body``."""
        if not isinstance(body, str) or not body:
            return False
        return any(marker.pattern.search(body) for marker in self.markers)


def _compile_marker(entry: dict) -> _CompiledMarker | None:
    """Compile one registry entry into a matcher.

    Canonical ss-console schema (source of truth): ``{id, kind, value, note}``
    where ``kind`` is one of:

      * ``literal``    — exact, CASE-SENSITIVE substring match.
      * ``literal_ci`` — case-INSENSITIVE substring match.
      * ``regex``      — case-INSENSITIVE regex, compiled as-authored.

    ``pattern``/``reason`` are accepted as legacy aliases for ``value``/``note``
    so the loader is tolerant during the cross-repo transition. A malformed
    entry returns ``None`` and is logged; the registry-level loader decides
    whether the survivors are sufficient (fail-closed if none remain).
    """
    marker_id = entry.get("id")
    # Canonical key is ``value``; fall back to the legacy ``pattern`` alias.
    pattern_text = entry.get("value")
    if pattern_text is None:
        pattern_text = entry.get("pattern")
    kind = (entry.get("kind") or "literal_ci").lower()
    # Canonical key is ``note``; fall back to legacy ``reason`` then id.
    reason = entry.get("note") or entry.get("reason") or marker_id or "fabrication marker"
    if not isinstance(marker_id, str) or not marker_id:
        logger.warning("fabrication_markers: entry missing 'id'; dropping entry")
        return None
    if not isinstance(pattern_text, str) or not pattern_text:
        logger.warning("fabrication_markers: marker %r missing 'value'; dropping", marker_id)
        return None
    try:
        if kind == "regex":
            compiled = re.compile(pattern_text, re.IGNORECASE)
        elif kind == "literal":
            # Exact, case-SENSITIVE literal substring.
            compiled = re.compile(re.escape(pattern_text))
        else:
            # literal_ci (and any unknown kind, fail-closed to the broadest
            # case-insensitive literal so an unrecognized kind never silently
            # disables a marker).
            compiled = re.compile(re.escape(pattern_text), re.IGNORECASE)
    except re.error as exc:
        logger.warning(
            "fabrication_markers: marker %r has an invalid regex (%s); dropping",
            marker_id,
            exc,
        )
        return None
    return _CompiledMarker(marker_id=marker_id, reason=str(reason), pattern=compiled)


def _load_registry(path: Path) -> MarkerRegistry:
    """Load + compile the registry from ``path``. Fail-closed on any error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FabricationMarkersError(
            f"fabrication marker registry unreadable at {path}: {exc}"
        ) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise FabricationMarkersError(
            f"fabrication marker registry at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise FabricationMarkersError(
            f"fabrication marker registry at {path} must be a JSON object"
        )

    version = doc.get("version")
    if not isinstance(version, str) or not version:
        raise FabricationMarkersError(
            f"fabrication marker registry at {path} is missing a 'version' string"
        )

    entries = doc.get("markers")
    if not isinstance(entries, list) or not entries:
        raise FabricationMarkersError(
            f"fabrication marker registry at {path} has no 'markers' — "
            "refusing to load an empty registry (fail-closed)"
        )

    compiled: list[_CompiledMarker] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("fabrication_markers: non-object marker entry; dropping")
            continue
        marker = _compile_marker(entry)
        if marker is not None:
            compiled.append(marker)

    if not compiled:
        # Every marker was malformed. Failing closed here is mandatory: a
        # silently-empty matcher would mark every fabricated body "clean."
        raise FabricationMarkersError(
            f"fabrication marker registry at {path} produced zero usable markers "
            "after compilation — refusing to operate with an empty matcher (fail-closed)"
        )

    return MarkerRegistry(
        version=version,
        source=str(doc.get("source", "")),
        markers=tuple(compiled),
    )


@lru_cache(maxsize=1)
def load_markers() -> MarkerRegistry:
    """Return the process-wide compiled marker registry.

    Cached: the registry is immutable for the life of the Machine. Raises
    :class:`FabricationMarkersError` if the vendored JSON is missing, empty,
    or yields no usable markers — the outbound gate converts that into a
    block (fail-closed).
    """
    return _load_registry(_MARKERS_PATH)


__all__ = [
    "FabricationMarkersError",
    "MarkerHit",
    "MarkerRegistry",
    "load_markers",
]
