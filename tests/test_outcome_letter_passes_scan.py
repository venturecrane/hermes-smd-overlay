"""An outcome letter must be one the seat's own outbound scan lets out.

2026-10-16, A&P: act 7fbf43bb (a records order) lapsed. Its lapse letter quoted
the readback verbatim, the readback carried the vendor's "$100.00" custodian
fee, the out-of-turn send runs with an empty session so no figure is verified,
and the scan refused the letter every thirty seconds for 21 hours. These tests
run the REAL text shape through the REAL outbound checks, so a letter that could
not pass fails here instead of on a client seat.
"""

from __future__ import annotations

import pytest

from shared import rule_dispatch
from tests.conftest import load_plugin

trust = load_plugin("hermes-smd-trust")
outbound = trust.__dict__["outbound"]

#: The A&P row's text with the client's name and identifiers replaced. The
#: shape that matters is kept: a readback ending in a dollar fee.
ACT_TEXT = (
    "Place a medical-records order with YoCierge on matter 100001 for Jane Roe, "
    "ordered by requester@example.com, 1 facility: 1) Example Hospital (Sacramento, "
    "CA 95608): Medical and Billing, 2021-10-14 to 2026-10-14. Authorization by the "
    "vendor's e-signature; pre-approved custodian fee $100.00; certification not "
    'requested. Reply "yes, place it" to proceed.'
)


class _Capture:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

        class _R:
            sent = True
            reason = ""
            message_id = "m"

        return _R()


def _scan_refuses(subject: str, text: str) -> bool:
    """The two checks the out-of-turn send runs, in its order, empty session."""
    payload = {"to": ["requester@example.com"], "subject": subject, "text": text}
    for check in (outbound.check_outbound_draft, outbound.check_outbound_send):
        if check(tool_name="smd_send_message", args=payload, session_id="") is not None:
            return True
    return False


def test_the_quoted_fee_is_what_the_scan_refuses():
    """FALSIFIER: the old letter (the full readback) must still be refused, or
    the passing tests below prove nothing."""
    body = "Nobody confirmed this:\n\n[act 7fbf43bb] " + ACT_TEXT
    assert _scan_refuses("subject", body)


@pytest.mark.parametrize("kind", ["lapsed", "declined"])
def test_an_act_letter_with_a_fee_passes_the_scan(kind):
    send = _Capture()
    rule_dispatch.notify_outcome(
        kind=kind,
        proposal_id="7fbf43bb",
        text=ACT_TEXT,
        requester="requester@example.com",
        send=send,
        row_kind="tool_call",
    )
    call = send.calls[0]
    assert "$" not in call["text"]
    assert "[act 7fbf43bb]" in call["text"]
    assert not _scan_refuses(call["subject"], call["text"])


def test_an_act_is_called_a_request_never_a_rule():
    send = _Capture()
    rule_dispatch.notify_outcome(
        kind="lapsed",
        proposal_id="7fbf43bb",
        text="Send the intake packet to the client",
        requester="requester@example.com",
        send=send,
        row_kind="tool_call",
    )
    call = send.calls[0]
    assert "rule" not in call["subject"].lower()
    assert "rule" not in call["text"].lower()
    assert "Send the intake packet to the client" in call["text"]
    assert "—" not in call["text"]


def test_a_clean_rule_is_still_quoted_in_full():
    """The fallback is for a hit only: a clean request keeps its quotation."""
    send = _Capture()
    rule_dispatch.notify_outcome(
        kind="lapsed",
        proposal_id="ab12cd34",
        text="Always copy the paralegal on demand letters",
        requester="requester@example.com",
        send=send,
    )
    assert "[rule ab12cd34] Always copy the paralegal on demand letters" in send.calls[0]["text"]


def test_an_ops_letter_with_a_figure_passes_the_scan():
    send = _Capture()
    rule_dispatch.notify_ops_outcome(
        kind="lapsed",
        proposal_id="ef56ab78",
        text="Raise the monthly drafting cap to $500",
        requester="requester@example.com",
        send=send,
    )
    call = send.calls[0]
    assert "[ops ef56ab78]" in call["text"]
    assert not _scan_refuses(call["subject"], call["text"])
