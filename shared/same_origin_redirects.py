"""Keep a credential on the host it was minted for, across HTTP redirects.

``urllib.request.urlopen`` follows a 30x to ANY host, and its redirect handler
copies the original request's headers onto the new request, ``Authorization``
included. So an authenticated GET answered with ``302 Location:
https://elsewhere/`` re-sends the bearer key to ``elsewhere``. One compromised
or misbehaving hop is enough to hand a seat's API key to a host nobody chose.

Every authenticated urllib call goes through :func:`open_same_origin` (or an
opener built with :class:`SameOriginRedirects`). A redirect is followed only
when it stays on the same origin (scheme, host and port) as the request that
received it; anything else is refused as an ``HTTPError`` before a byte goes to
the new host. Same-origin redirects keep working, so a vendor that moves a path
under its own host is unaffected. Refusing is the fail-closed choice: the call
fails and is reported; it never succeeds against a host it was not pointed at.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1  # an unparseable port never matches a real origin
    return scheme, (parsed.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def same_origin(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` share scheme, host and port (a downgrade to
    plain http is a different scheme, so it is never the same origin)."""
    origin_a = _origin(a)
    return bool(origin_a[1]) and origin_a == _origin(b)


class SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the origin of the request that received it.

    Checked per hop against the hop's own request, so a chain cannot walk off
    the origin one step at a time.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not same_origin(req.full_url, newurl):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "redirect off the request's origin refused (credential stays on its host)",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAME_ORIGIN_OPENER = urllib.request.build_opener(SameOriginRedirects)


def open_same_origin(req: urllib.request.Request, *, timeout: float) -> Any:
    """``urlopen`` with every redirect hop held to the request's own origin."""
    return SAME_ORIGIN_OPENER.open(req, timeout=timeout)  # noqa: S310 - origin checked per hop


__all__ = ["SAME_ORIGIN_OPENER", "SameOriginRedirects", "open_same_origin", "same_origin"]
