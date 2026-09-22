"""The send-as approve page (ss ADR 0089 amendment 5a).

The property that matters most is the one a mail gateway breaks: a link in an
email is FOLLOWED by scanners before any person reads the message, so rendering
must decide nothing and only a POST may act.
"""

from __future__ import annotations

from shared import approve_page

TOKEN = "7f3a2c1d.send.1790000000.c2lnbmF0dXJl"


class _Decider:
    def __init__(self, verdict: dict | Exception) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, *, token: str) -> dict:
        self.calls.append(token)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def test_rendering_the_page_decides_nothing():
    # A scanner's GET reaches confirm_page. Nothing here can reach the broker:
    # the function takes no decider at all, which is the guarantee.
    status, page = approve_page.confirm_page(TOKEN)
    assert status == 200
    assert 'method="POST"' in page and f'value="{TOKEN}"' in page
    assert "Nothing has happened yet" in page


def test_the_page_shows_no_draft_text():
    # Anyone holding the link can render this; the letter is already in the
    # approver's mailbox and does not belong on a page with no sign-in.
    _status, page = approve_page.confirm_page(TOKEN)
    assert "draft" in page.lower()
    assert "Records request" not in page


def test_a_press_carries_the_token_and_reports_what_the_broker_did():
    decide = _Decider({"status": "DISPATCHED"})
    status, page = approve_page.apply_click(TOKEN, decide)
    assert (status, decide.calls) == (200, [TOKEN])
    assert "gone out from your address" in page


def test_a_refusal_is_reported_without_inventing_an_outcome():
    decide = _Decider({"status": "REFUSED", "reason": "that approval link is expired"})
    status, page = approve_page.apply_click(TOKEN, decide)
    assert status == 400
    assert "expired" in page and "Sent" not in page


def test_an_unreachable_broker_says_nothing_was_sent():
    decide = _Decider(OSError("no socket"))
    status, page = approve_page.apply_click(TOKEN, decide)
    assert status == 503
    assert "nothing was sent" in page


def test_a_missing_or_oversized_token_never_reaches_the_broker():
    decide = _Decider({"status": "DISPATCHED"})
    for query in ("", "t=", "t=" + "x" * 401):
        status, _page = approve_page.apply_click(approve_page.token_from_query(query), decide)
        assert status == 400
    assert decide.calls == []


def test_the_token_is_read_from_the_query_or_form_body():
    assert approve_page.token_from_query(f"t={TOKEN}") == TOKEN
    assert approve_page.token_from_query(f"t={TOKEN}&other=1") == TOKEN
    assert approve_page.token_from_query("nothing=here") == ""
