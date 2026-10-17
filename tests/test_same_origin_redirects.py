"""A credential stays on the host it was minted for, across redirects.

``urlopen``'s redirect handler follows a 30x to any host and copies the
original request's headers, ``Authorization`` included, onto the new request.
Every authenticated urllib caller (AgentMail's API GET, the Graph client, the
Honcho sidecar client) routes through ``shared.same_origin_redirects``, which
refuses any hop off the request's origin. These tests drive the REAL redirect
machinery with a scripted network handler; nothing reaches the network.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response
from typing import Any

import pytest

from shared import msgraph_client
from shared import same_origin_redirects as same_origin
from tests.conftest import load_plugin


class _ScriptedNet(urllib.request.BaseHandler):
    """Answers http and https from a script: a str is a redirect target, bytes
    are a 200 body. Records each hop with the Authorization it carried."""

    handler_order = 100

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.hops: list[tuple[str, str | None]] = []

    def _answer(self, req: urllib.request.Request) -> Any:
        url = req.full_url
        self.hops.append((url, req.get_header("Authorization")))
        answer = self.script[url]
        headers = email.message.Message()
        if isinstance(answer, str):
            headers["Location"] = answer
            resp = urllib.response.addinfourl(io.BytesIO(b""), headers, url, 302)
            resp.msg = "Found"
        else:
            resp = urllib.response.addinfourl(io.BytesIO(answer), headers, url, 200)
            resp.msg = "OK"
        return resp

    http_open = _answer
    https_open = _answer


def _install(script: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> _ScriptedNet:
    net = _ScriptedNet(script)
    opener = urllib.request.build_opener(same_origin.SameOriginRedirects, net)
    monkeypatch.setattr(same_origin, "SAME_ORIGIN_OPENER", opener)
    return net


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("https://api.x.example/v0/a", "https://api.x.example/v0/b?q=1", True),
        ("https://api.x.example/a", "https://API.X.EXAMPLE:443/b", True),
        ("http://localhost:8000/a", "http://localhost:8000/b", True),
        ("https://api.x.example/a", "https://evil.example/a", False),
        ("https://api.x.example/a", "http://api.x.example/a", False),  # downgrade
        ("https://api.x.example/a", "https://api.x.example:8443/a", False),
        ("https://api.x.example/a", "https://api.x.example.evil.example/a", False),
        ("https://api.x.example/a", "https://user@evil.example/a", False),
        ("https://api.x.example/a", "https://api.x.example:bad/a", False),
        ("https:///a", "https:///b", False),  # no host is never an origin
    ],
)
def test_same_origin(a: str, b: str, same: bool) -> None:
    assert same_origin.same_origin(a, b) is same


def test_an_off_origin_hop_is_refused_before_any_byte_goes_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "https://api.x.example/v0/thing"
    net = _install({start: "https://evil.example/steal"}, monkeypatch)
    req = urllib.request.Request(start, headers={"Authorization": "Bearer k"})
    with pytest.raises(urllib.error.HTTPError, match="origin"):
        same_origin.open_same_origin(req, timeout=5)
    assert net.hops == [(start, "Bearer k")]


def test_a_chain_cannot_walk_off_the_origin_one_hop_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = "https://api.x.example/a"
    mid = "https://api.x.example/b"
    net = _install({start: mid, mid: "https://evil.example/c"}, monkeypatch)
    req = urllib.request.Request(start, headers={"Authorization": "Bearer k"})
    with pytest.raises(urllib.error.HTTPError):
        same_origin.open_same_origin(req, timeout=5)
    assert [url for url, _ in net.hops] == [start, mid]


def test_the_stock_handler_is_what_this_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the premise: without the guard, urllib re-sends Authorization to the
    redirect target. If urllib ever stops doing that, this fails and the module
    can be revisited, not silently trusted."""
    start = "https://api.x.example/a"
    net = _ScriptedNet({start: "https://evil.example/steal", "https://evil.example/steal": b"{}"})
    opener = urllib.request.build_opener(net)
    opener.open(urllib.request.Request(start, headers={"Authorization": "Bearer k"}), timeout=5)
    assert net.hops[-1] == ("https://evil.example/steal", "Bearer k")


# ---- the Graph client ------------------------------------------------------


def test_the_graph_client_defaults_to_the_same_origin_opener() -> None:
    client = msgraph_client.MsGraphClient(
        tenant_id="t", client_id="c", client_secret="s", mailbox="op@client.example"
    )
    assert client._open is same_origin.open_same_origin


def test_a_graph_redirect_off_graph_never_carries_the_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = msgraph_client.MsGraphClient(
        tenant_id="t", client_id="c", client_secret="s", mailbox="op@client.example"
    )
    token_url = "https://login.microsoftonline.com/t/oauth2/v2.0/token"
    url = client.mail_url("/messages/m1")
    net = _install(
        {
            token_url: json.dumps({"access_token": "graph-tok", "expires_in": 3600}).encode(),
            url: "https://evil.example/steal",
        },
        monkeypatch,
    )
    with pytest.raises(msgraph_client.MsGraphApiError):
        client.request("GET", url)
    assert all(not u.startswith("https://evil.example") for u, _ in net.hops)
    assert (url, "Bearer graph-tok") in net.hops


# ---- the Honcho sidecar client ---------------------------------------------


def test_a_honcho_redirect_off_the_sidecar_never_carries_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = load_plugin("hermes-smd-memory-mirror")
    client = mod.honcho_client.HonchoClient(base_url="http://localhost:8000", api_key="hk")
    start = "http://localhost:8000/v2/anything"
    # The off-origin URL is deliberately unscripted: reaching it is a KeyError.
    net = _install({start: "http://evil.example/steal"}, monkeypatch)
    with pytest.raises(mod.honcho_client.HonchoUnreachable, match="HTTP 302"):
        client._get("/v2/anything")
    assert net.hops == [(start, "Bearer hk")]
