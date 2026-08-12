"""The content-sensitivity floor reads ``html_body`` (ss-console#2297).

``enforce._SEND_BODY_ARG_KEYS`` is the ADR 0031 floor's notion of "the visible
surface of this send". It reads ``subject`` alongside ``text`` and ``html``,
CONCATENATES every match rather than taking the first, and fails toward draft
when it finds nothing — three properties that only make sense if the intent is
to assemble everything a recipient will see. It omitted ``html_body``, which
``outbound._SEND_SCAN_KEYS`` and ``matter_gate._BODY_KEYS`` both read.

The consequence is not "an html-only send is uninspectable" — a send carrying
NOTHING the floor recognizes already fails closed. It is narrower and worse: a
send with a benign ``subject`` and its money / contract language in
``html_body`` produced a non-empty body (the subject), classified clean, and was
certified non-sensitive. The floor did not decline to judge it; it judged the
envelope and passed.

So the falsifier below is deliberately NOT a bare ``html_body`` send. That shape
blocks either way — before the fix as "indeterminate", after it as "money" —
and a test written on it would have gone green against the defect. Every
assertion here therefore checks that the block names a real category, because
"blocked" and "blocked for the right reason" are different findings.
"""

from __future__ import annotations

import pytest

from tests.conftest import load_plugin

# A benign envelope. Nothing here trips a floor pattern, so a block on these
# args can only have come from reading the html half.
_BENIGN_SUBJECT = "Following up on your note"

# Money + contract language, present ONLY in the html half.
_SENSITIVE_HTML = (
    "<p>Please remit payment of $12,500 under the retainer agreement before Friday.</p>"
)

# The control body: an html-only send with nothing a human needs to sign off on.
_CLEAN_HTML = "<p>Got it, that works on my end. Talk soon.</p>"


@pytest.fixture
def enforce():
    return load_plugin("hermes-smd-trust").enforce


def _assert_blocked_on_content(block: dict | None, category: str) -> None:
    """A block that names ``category`` — not the fail-closed path wearing a block.

    ``classify(None)`` also returns a block, labelled ``indeterminate``. Asserting
    only ``block is not None`` would pass on a send the floor never read, which is
    exactly the defect this file exists to catch.
    """
    assert block is not None, "the floor certified this send non-sensitive"
    assert block["action"] == "block"
    assert "indeterminate" not in block["message"], (
        "blocked by the fail-closed path, not by reading the body — the floor "
        f"still cannot see the content. Message: {block['message']}"
    )
    assert category in block["message"], (
        f"expected the floor to name {category!r}; got: {block['message']}"
    )


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_floor_reads_money_language_that_lives_only_in_html_body(enforce) -> None:
    """A send whose commercial language is in ``html_body`` must route to draft."""
    args = {"subject": _BENIGN_SUBJECT, "html_body": _SENSITIVE_HTML}
    _assert_blocked_on_content(
        enforce._apply_content_floor("agentmail:send_message", args), "money"
    )


def test_extracted_body_contains_the_html_half(enforce) -> None:
    """The extractor's own output, asserted directly.

    The gate-level test above can only report "blocked"; this one reports WHY,
    and pins the concatenate-everything contract the floor depends on — the
    subject must survive alongside the html half, not be replaced by it.
    """
    body = enforce._extract_send_body({"subject": _BENIGN_SUBJECT, "html_body": _SENSITIVE_HTML})
    assert body is not None
    assert "retainer agreement" in body
    assert _BENIGN_SUBJECT in body


def test_bare_html_body_send_blocks_for_the_right_reason(enforce) -> None:
    """No subject, sensitive html half: blocked before AND after the fix — but
    only after it does the floor say what it found. Kept as a regression pin on
    the distinction, since a fail-closed block is the one outcome that looks
    identical to working."""
    block = enforce._apply_content_floor("agentmail:send_message", {"html_body": _SENSITIVE_HTML})
    _assert_blocked_on_content(block, "money")


# ---------------------------------------------------------------------------
# The control — widening a blocking gate must not become blanket-holding
# ---------------------------------------------------------------------------


def test_clean_html_body_send_still_ships(enforce) -> None:
    """A genuinely non-sensitive html-only send is still certified clean.

    Without this, the fix above is indistinguishable from "hold every send that
    carries an html half", which would be a far larger behavior change than the
    one ss-console#2297 asked for.
    """
    args = {"subject": "Quick reply", "html_body": _CLEAN_HTML}
    assert enforce._apply_content_floor("agentmail:send_message", args) is None


def test_clean_html_body_alongside_plaintext_still_ships(enforce) -> None:
    """The established two-part shape (text + html half) is unchanged."""
    args = {
        "subject": "Quick reply",
        "text": "Got it, that works on my end. Talk soon.",
        "html_body": _CLEAN_HTML,
    }
    assert enforce._apply_content_floor("agentmail:send_message", args) is None


# ---------------------------------------------------------------------------
# End to end, on the real send path
# ---------------------------------------------------------------------------


def test_autonomous_send_with_sensitive_html_body_is_downgraded(enforce, monkeypatch) -> None:
    """Through ``evaluate_tool_call``: an AUTONOMOUS outside send whose money
    language is html-only is downgraded to draft.

    The voice gate is forced silent so the resulting block can only be the
    floor's — two gates on this path both emit a draft directive, and a test
    that accepted either would not be measuring this one.
    """
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: False)
    result = enforce.evaluate_tool_call(
        "agentmail:send_message",
        {"subject": _BENIGN_SUBJECT, "html_body": _SENSITIVE_HTML},
        "smd",
        session_id="sess-2297",
    )
    assert isinstance(result, dict)
    _assert_blocked_on_content(result, "money")


def test_autonomous_send_with_clean_html_body_still_sends(enforce, monkeypatch) -> None:
    """The same path, benign content: still allowed."""
    monkeypatch.setattr(
        enforce,
        "_resolve_persona_exposure",
        lambda slug="": {enforce.ActionClass.EXTERNAL_SEND: enforce.Ceiling.AUTONOMOUS},
    )
    monkeypatch.setenv("HERMES_ACTIVE_PROFILE", "marcus")
    monkeypatch.setattr(enforce.voice_gate, "_voice_authored", lambda: False)
    result = enforce.evaluate_tool_call(
        "agentmail:send_message",
        {"subject": "Quick reply", "html_body": _CLEAN_HTML},
        "smd",
        session_id="sess-2297",
    )
    assert result is None
