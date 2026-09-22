"""The page an approve button opens (ss ADR 0089 amendment 5a).

WHY A PAGE AND NOT A LINK THAT ACTS. Mail security scanners follow every link in
a message before the person ever sees it (Outlook Safe Links, and every gateway
like it). A GET that sent the letter would be sent by the scanner, from an empty
office, seconds after the approval email arrived. So GET only ever RENDERS, and
the decision is a POST the person makes by pressing the button on this page.

WHAT THIS MODULE IS TRUSTED FOR: nothing. It parses a token out of a query
string and hands it to the broker, which holds the signing key and re-reads the
row. A wrong, expired, replayed or invented token is the broker's refusal to
make, and this page just says what came back.
"""

from __future__ import annotations

import html
import logging
from typing import Any
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

#: Cap on a token we will even hand to the broker. A real one is ~60 characters.
_MAX_TOKEN = 400

_STYLE = (
    "font-family:system-ui,-apple-system,sans-serif;max-width:34rem;margin:12vh auto;"
    "padding:0 1.5rem;color:#111;line-height:1.5"
)
_BUTTON = (
    "display:inline-block;padding:12px 22px;border:0;border-radius:6px;"
    "font-size:16px;background:#1a7f37;color:#fff;cursor:pointer"
)


def token_from_query(query: str) -> str:
    """The ``t`` parameter, bounded; ``""`` when absent or oversized."""
    values = parse_qs(query or "").get("t") or []
    token = values[0].strip() if values else ""
    return token if 0 < len(token) <= _MAX_TOKEN else ""


def confirm_page(token: str) -> tuple[int, str]:
    """The page a button opens: one more press, which POSTs back here.

    It deliberately shows no draft text. This page is reachable by anyone holding
    the link, and the letter's contents are already in the approver's mailbox.
    """
    if not token:
        return 400, _page("That link is incomplete", "Open the approval email again.")
    safe = html.escape(token, quote=True)
    body = (
        "<h1>Confirm</h1>"
        "<p>Press the button to apply your answer to the draft the Operator "
        "emailed you. Nothing has happened yet.</p>"
        '<form method="POST" action="/approve">'
        f'<input type="hidden" name="t" value="{safe}">'
        f'<button type="submit" style="{_BUTTON}">Confirm</button>'
        "</form>"
    )
    return 200, _wrap(body)


def apply_click(token: str, decide: Any) -> tuple[int, str]:
    """Hand the token to the broker and render what it decided."""
    if not token:
        return 400, _page("That link is incomplete", "Open the approval email again.")
    try:
        verdict = decide(token=token)
    except Exception:  # noqa: BLE001 — an unreachable broker decided nothing
        logger.warning("approve: broker unreachable", exc_info=True)
        return 503, _page(
            "Nothing was done",
            "This seat could not record your answer just now, so nothing was sent. "
            "Try again in a minute, or reply to the approval email instead.",
        )
    status = str((verdict or {}).get("status") or "").upper()
    reason = str((verdict or {}).get("reason") or "").strip()
    said = {
        "DISPATCHED": ("Sent", "The email has gone out from your address."),
        "CANCELLED": ("Cancelled", "Nothing was sent, and the draft is closed."),
        "EXPIRED": ("Too late", "That draft expired, so nothing was sent."),
        "SUPERSEDED": ("Replaced", "A newer draft replaced this one. Answer that one instead."),
        "FAILED": ("Not sent", reason or "The send failed and nothing went out."),
    }.get(status)
    if said is None:
        return 400, _page("Nothing was done", reason or "That link is not valid for this draft.")
    return 200, _page(*said)


def _page(title: str, detail: str) -> str:
    return _wrap(f"<h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p>")


def _wrap(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        "<title>Operator</title></head>"
        f'<body style="{_STYLE}">{body}</body></html>'
    )
