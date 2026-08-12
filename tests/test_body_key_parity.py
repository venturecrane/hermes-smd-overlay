"""The matter gate and the fabrication scanner must read the SAME body (ss#2290).

``matter_gate._BODY_KEYS`` is a hand-copy of ``outbound._SEND_SCAN_KEYS``,
duplicated rather than imported because ``outbound`` imports ``enforce`` and
``enforce`` calls ``matter_gate`` — importing would close that cycle. The
duplication was held together by a comment ("a key added there must be added
here") and nothing else. A comment is not a control: a key added to the scanner
and not to the gate means a matter cited only in the new field goes unseen by
the identity gate while the fabrication gate reads it, and both suites stay
green.

Scope note: this pins the two lists that are DECLARED to be copies of each
other. ``enforce._SEND_BODY_ARG_KEYS`` and ``outbound._BODY_ARG_KEYS`` are
deliberately NOT pinned to these — see the module note at the bottom.
"""

from __future__ import annotations

from shared import matter_gate
from tests.conftest import load_plugin


def _parity_failure(gate_keys: tuple[str, ...], scan_keys: tuple[str, ...]) -> str:
    """Empty when the two lists agree; otherwise the human-readable difference.

    Factored out so the falsifier test below can drive the same comparison with
    a deliberately desynced pair — a parity assertion that cannot be made to
    fail has measured nothing.
    """
    missing = [k for k in scan_keys if k not in gate_keys]
    extra = [k for k in gate_keys if k not in scan_keys]
    if not missing and not extra:
        return ""
    parts = []
    if missing:
        parts.append(f"scanned but not gated: {missing}")
    if extra:
        parts.append(f"gated but not scanned: {extra}")
    return "; ".join(parts)


def test_matter_gate_body_keys_match_the_send_scanner() -> None:
    outbound = load_plugin("hermes-smd-trust").outbound
    failure = _parity_failure(matter_gate._BODY_KEYS, outbound._SEND_SCAN_KEYS)
    assert not failure, (
        "matter_gate._BODY_KEYS has drifted from outbound._SEND_SCAN_KEYS "
        f"({failure}). Update both, or a matter cited only in the new field "
        "is invisible to the identity gate."
    )


def test_the_parity_check_catches_a_desynced_list() -> None:
    # Law 12: prove the instrument can fire before trusting its silence.
    outbound = load_plugin("hermes-smd-trust").outbound
    desynced = tuple(k for k in matter_gate._BODY_KEYS if k != "html_body")
    failure = _parity_failure(desynced, outbound._SEND_SCAN_KEYS)
    assert "html_body" in failure
    # And in the other direction — a key the gate reads that nothing scans.
    failure_extra = _parity_failure(
        matter_gate._BODY_KEYS + ("invented_field",), outbound._SEND_SCAN_KEYS
    )
    assert "invented_field" in failure_extra


# ---------------------------------------------------------------------------
# The two lists this file deliberately does NOT pin (ss#2290 → ss#2297)
#
# Four body-key lists exist across the send path and only two of them claim to
# be copies. The other two disagree with these and with each other. ss#2290
# reported both disagreements without acting on either; ss#2297 resolved half of
# it, and the halves went opposite ways for a stated reason:
#
#   enforce._SEND_BODY_ARG_KEYS   CONVERGED on html_body (ss#2297) — it assembles
#                                 the whole visible surface, so an html half it
#                                 could not see was a hole, not a definition.
#                                 Still omits `note`, which is an annotation on a
#                                 record rather than a surface a recipient reads.
#   outbound._BODY_ARG_KEYS       STAYS narrow — omits subject, first-match-wins,
#                                 because it resolves ONE authored draft body.
#                                 `_DRAFT_SCAN_KEYS` is the explicit superset for
#                                 the jobs that need the whole surface.
#
# So this is not a list-alignment exercise. These assertions pin what each list
# means, so a future change to either is a deliberate edit to this file.
# ---------------------------------------------------------------------------


def test_the_neighbouring_lists_disagree_as_documented() -> None:
    trust = load_plugin("hermes-smd-trust")
    # ss#2297: the floor reads the html half. Removing this key returns the gap
    # where a benign subject beside sensitive html shipped autonomously.
    assert "html_body" in trust.enforce._SEND_BODY_ARG_KEYS
    assert "note" not in trust.enforce._SEND_BODY_ARG_KEYS
    assert "subject" not in trust.outbound._BODY_ARG_KEYS
