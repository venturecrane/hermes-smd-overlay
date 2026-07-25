"""Tests for the shared Microsoft Graph client (shared/msgraph_client.py).

The overlay's poller / reply / confirm paths speak Graph directly (outside the
model's tool path) through this ONE client. These pin the auth + request
semantics that the sandbox proved on the connector and that the seam depends on:
client-credentials token mint, a single re-mint on a 401, a 410 cursor reset on
poll_delta, the DTO normalization parity with the connector, and the fail-closed
env builder.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from shared import msgraph_client
from shared.msgraph_client import MsGraphApiError, MsGraphAuthError, MsGraphClient


class _Resp:
    """Minimal urlopen response context manager (status + read())."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://graph", code, "err", {}, io.BytesIO(body))


class _Opener:
    """Injectable urlopen: routes token vs graph calls, plays a queued script."""

    def __init__(self, *, token_responses=None, graph_script=None) -> None:
        # token_responses: list of (_Resp | HTTPError); each token POST pops one.
        self._token = list(
            token_responses or [_Resp(200, b'{"access_token":"T","expires_in":3600}')]
        )
        # graph_script: list of (_Resp | HTTPError); each non-token call pops one.
        self._graph = list(graph_script or [])
        self.calls: list[tuple[str, str]] = []  # (method, url)
        self.bodies: list[bytes | None] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append((req.get_method(), url))
        self.bodies.append(req.data)
        if "oauth2/v2.0/token" in url:
            item = self._token.pop(0)
        else:
            item = self._graph.pop(0)
        if isinstance(item, urllib.error.HTTPError):
            raise item
        return item


def _client(opener: _Opener, mailbox: str = "op@client.example") -> MsGraphClient:
    return MsGraphClient(
        tenant_id="t", client_id="c", client_secret="s", mailbox=mailbox, opener=opener
    )


def test_missing_config_fails_closed_at_construction():
    with pytest.raises(ValueError):
        MsGraphClient(tenant_id="", client_id="c", client_secret="s", mailbox="m@x.example")


def test_token_mint_then_authenticated_get():
    opener = _Opener(graph_script=[_Resp(200, b'{"id":"m1"}')])
    client = _client(opener)
    result = client.get_message("m1")
    assert result == {"id": "m1"}
    # First call mints the token, second is the Graph GET with a Bearer header.
    assert "oauth2/v2.0/token" in opener.calls[0][1]
    assert opener.calls[1][0] == "GET"
    assert "/users/op@client.example/messages/m1" in opener.calls[1][1]


def test_token_mint_rejection_raises_auth_error_without_body():
    opener = _Opener(token_responses=[_http_error(401, b"secret-echo")])
    client = _client(opener)
    with pytest.raises(MsGraphAuthError) as exc:
        client.get_message("m1")
    # The rejection carries status + host, never the response body (no secret echo).
    assert "secret-echo" not in str(exc.value)


def test_401_triggers_single_remint_then_succeeds():
    # token(mint) -> GET 401 -> token(re-mint) -> GET 200
    opener = _Opener(
        token_responses=[
            _Resp(200, b'{"access_token":"T1","expires_in":3600}'),
            _Resp(200, b'{"access_token":"T2","expires_in":3600}'),
        ],
        graph_script=[_http_error(401), _Resp(200, b'{"id":"m1"}')],
    )
    client = _client(opener)
    assert client.get_message("m1") == {"id": "m1"}
    # Exactly two token mints (initial + one refresh) and two graph attempts.
    token_calls = [c for c in opener.calls if "oauth2/v2.0/token" in c[1]]
    assert len(token_calls) == 2


def test_persistent_4xx_raises_api_error_with_status():
    opener = _Opener(graph_script=[_http_error(403, b"forbidden")])
    client = _client(opener)
    with pytest.raises(MsGraphApiError) as exc:
        client.get_message("m1")
    assert exc.value.status == 403


def test_poll_delta_returns_messages_and_delta_link():
    page = json.dumps(
        {
            "value": [{"id": "m1"}, {"id": "m2", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": "https://graph/delta?$deltatoken=abc",
        }
    ).encode()
    opener = _Opener(graph_script=[_Resp(200, page)])
    client = _client(opener)
    items, delta_link, reset = client.poll_delta(None)
    assert [m["id"] for m in items] == ["m1"]  # @removed tombstone dropped
    assert delta_link == "https://graph/delta?$deltatoken=abc"
    assert reset is False


def test_poll_delta_410_on_stored_cursor_resyncs_and_flags_reset():
    # Stored cursor GET -> 410; base delta GET -> 200 with a fresh delta link.
    fresh = json.dumps(
        {"value": [{"id": "m9"}], "@odata.deltaLink": "https://graph/delta2"}
    ).encode()
    opener = _Opener(graph_script=[_http_error(410, b"gone"), _Resp(200, fresh)])
    client = _client(opener)
    items, delta_link, reset = client.poll_delta("https://graph/old-cursor")
    assert [m["id"] for m in items] == ["m9"]
    assert delta_link == "https://graph/delta2"
    assert reset is True


def test_send_mail_posts_sendmail_with_flat_args_nested():
    opener = _Opener(graph_script=[_Resp(202, b"")])
    client = _client(opener)
    client.send_mail(to="a@x.example", subject="Hi", body_text="Body", cc=["b@x.example"])
    method, url = opener.calls[1]
    assert method == "POST" and url.endswith("/users/op@client.example/sendMail")
    body = json.loads(opener.bodies[1])
    msg = body["message"]
    assert msg["toRecipients"] == [{"emailAddress": {"address": "a@x.example"}}]
    assert msg["ccRecipients"] == [{"emailAddress": {"address": "b@x.example"}}]
    assert msg["body"] == {"contentType": "Text", "content": "Body"}


def test_reply_posts_to_message_reply_endpoint():
    opener = _Opener(graph_script=[_Resp(202, b"")])
    client = _client(opener)
    client.reply("graph-mid-1", "thanks")
    method, url = opener.calls[1]
    assert method == "POST"
    assert url.endswith("/users/op@client.example/messages/graph-mid-1/reply")
    assert json.loads(opener.bodies[1]) == {"comment": "thanks"}


def test_normalize_message_matches_connector_dto_shape():
    raw = {
        "id": "AAMk-1",
        "conversationId": "conv-1",
        "from": {"emailAddress": {"address": "Greg@Whitfield.example"}},
        "toRecipients": [{"emailAddress": {"address": "op@client.example"}}],
        "ccRecipients": [],
        "subject": "Re: matter",
        "body": {"contentType": "html", "content": "<p>Hello <b>there</b></p>"},
        "receivedDateTime": "2026-07-24T10:00:00Z",
    }
    dto = msgraph_client.normalize_message(raw, mailbox="op@client.example")
    assert dto["provider"] == "msgraph"
    assert dto["from_addr"] == "greg@whitfield.example"  # bare + lowercased
    assert dto["message_id"] == "AAMk-1"
    assert dto["thread_ref"] == "conv-1"
    assert dto["body_text"] == "Hello there"  # html stripped
    assert dto["provider_refs"] == {"graph_message_id": "AAMk-1", "conversation_id": "conv-1"}


def test_build_client_from_env_fail_closed_when_unset(monkeypatch):
    for name in msgraph_client.MSGRAPH_ENV:
        monkeypatch.delenv(name, raising=False)
    assert msgraph_client.build_client_from_env() is None


def test_build_client_from_env_constructs_when_present(monkeypatch):
    monkeypatch.setenv("MSGRAPH_TENANT_ID", "t")
    monkeypatch.setenv("MSGRAPH_CLIENT_ID", "c")
    monkeypatch.setenv("MSGRAPH_CLIENT_SECRET", "s")
    monkeypatch.setenv("MSGRAPH_MAILBOX", "op@client.example")
    client = msgraph_client.build_client_from_env()
    assert isinstance(client, MsGraphClient)
    assert client.mailbox == "op@client.example"


# ---------------------------------------------------------------------------
# Connector-health instrumentation (ADR 0080 / ss#1990)
#
# The Graph mail channel bypasses the MCP tool path, so its health is
# observed at the request() chokepoint: every outcome lands in the
# connector ledger under the msgraph_mail key, with conn-class computed
# from the REAL status code.
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", str(path))
    return path


def _ledger_entry(path):
    import json as _json

    return _json.loads(path.read_text(encoding="utf-8"))["servers"]["msgraph_mail"]


def test_success_records_ok_in_connector_ledger(ledger_path):
    opener = _Opener(graph_script=[_Resp(200, b'{"id":"m1"}')])
    _client(opener).get_message("m1")
    entry = _ledger_entry(ledger_path)
    assert entry["consecutive_failures"] == 0
    assert "last_ok_ts" in entry


def test_5xx_records_conn_class_failure(ledger_path):
    opener = _Opener(graph_script=[_http_error(503, b"upstream"), _http_error(503, b"upstream")])
    client = _client(opener)
    with pytest.raises(MsGraphApiError):
        client.get_message("m1")
    entry = _ledger_entry(ledger_path)
    assert entry["consecutive_failures"] == 1
    assert "last_conn_error_ts" in entry
    assert "-> HTTP 503" in entry["last_error_message"]


def test_404_records_failure_without_conn_evidence(ledger_path):
    opener = _Opener(graph_script=[_http_error(404, b"gone")])
    client = _client(opener)
    with pytest.raises(MsGraphApiError):
        client.get_message("m1")
    entry = _ledger_entry(ledger_path)
    assert entry["consecutive_failures"] == 1
    assert "last_conn_error_ts" not in entry


def test_auth_failure_records_conn_class(ledger_path):
    # Dead app credential — the canonical ADR 0078 outage shape.
    opener = _Opener(
        token_responses=[_http_error(401, b"invalid_client")],
        graph_script=[],
    )
    client = _client(opener)
    with pytest.raises(MsGraphAuthError):
        client.get_message("m1")
    entry = _ledger_entry(ledger_path)
    assert entry["consecutive_failures"] == 1
    assert "last_conn_error_ts" in entry


def test_ledger_failure_never_breaks_the_mail_path(ledger_path, monkeypatch):
    # Point the ledger at an unwritable location: the Graph call must still
    # succeed — health capture is fail-soft by contract.
    monkeypatch.setenv("SMD_CONNECTOR_LEDGER_PATH", "/dev/null/nope/ledger.json")
    opener = _Opener(graph_script=[_Resp(200, b'{"id":"m1"}')])
    assert _client(opener).get_message("m1") == {"id": "m1"}
