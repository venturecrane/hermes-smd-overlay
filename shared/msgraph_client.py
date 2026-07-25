"""Shared Microsoft Graph HTTP client for the msgraph email seam (ADR 0078).

ONE Graph client, shared by every overlay path that speaks Graph directly: the
delta poller (inbound, ``shared.msgraph_poller``), the reply relay
(``hermes-smd-reply``), and the confirm dispatch (``hermes-smd-trust``). The
author-built ``operator/connectors/msgraph-mail`` MCP server is the AGENT'S tool
surface; these overlay paths run OUTSIDE the model's tool path (a scheduled poll,
an out-of-band governed reply/send) and must not shell into the MCP server, so
they mint their own token and call Graph over stdlib ``urllib`` — the same
posture the AgentMail transports (``hermes-smd-reply/relay.py`` /
``hermes-smd-trust/outbound_send.py``) already use.

Auth is app-only client credentials (Microsoft identity platform): mint a bearer
at ``.../oauth2/v2.0/token`` with ``grant_type=client_credentials`` +
``scope=https://graph.microsoft.com/.default``; there is NO refresh token — the
token is re-minted from the same creds when it nears expiry. The mailbox is
PINNED at construction (every path is ``/users/{mailbox}/...``), so no method
takes a mailbox argument and a caller can never redirect a read or a send to
another mailbox — the code-layer belt to the tenant-side ApplicationAccessPolicy.

Request semantics mirror the connector's ``MsGraphClient`` (the sandbox-proven
contract): retry a 429 with bounded backoff, re-mint once on a 401, surface a
``MsGraphApiError`` carrying the status so the delta path can detect a 410 expired
cursor and resync. Secrets come only from ``shared.secrets`` (AGENTS.md #4); a
missing credential fails the construction as a named error (fail-closed), never a
live path reporting success.
"""

from __future__ import annotations

import html
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from shared.secrets import get_secret

logger = logging.getLogger(__name__)

# Connector-health ledger key for the Graph mail channel (ADR 0080). Matches
# the sanitized name Hermes would register for an mcp:msgraph-mail server, so
# the alert identity stays stable if/when the connector also lands as agent
# MCP tools.
_CHANNEL_SERVER = "msgraph_mail"

# Conn-class statuses, computed structurally at this chokepoint (no message
# matching needed): unreachable (our status 0), auth (401/403/407),
# timeout-ish (408), throttle (429), and any 5xx. 4xx business errors
# (400/404/410-expired-cursor...) count as failures but carry no conn-class
# evidence — the signature-free backstop still pages a sustained run of them.
_CONN_CLASS_FIXED = frozenset({0, 401, 403, 407, 408, 429})


def _record_channel_outcome(
    *, ok: bool, status: int | None = None, message: str | None = None
) -> None:
    """Fail-soft ledger write for one Graph call. Health capture must never
    break the mail channel; any ledger error is logged and swallowed."""
    try:
        from shared.connector_ledger import record_call

        if ok:
            record_call(_CHANNEL_SERVER, ok=True)
        else:
            conn = status is not None and (status in _CONN_CLASS_FIXED or 500 <= status <= 599)
            record_call(_CHANNEL_SERVER, ok=False, error_message=message, conn_class=conn)
    except Exception as exc:  # noqa: BLE001 — never raise into the mail path
        logger.debug("msgraph connector-health record failed: %s", exc)


PROVIDER = "msgraph"

_TOKEN_HOST = "https://login.microsoftonline.com"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"

_TOKEN_SKEW_SECONDS = 60
_MAX_ATTEMPTS = 4
_MAX_ERROR_BODY = 600
_DEFAULT_TIMEOUT_S = 30.0

# The bounded field set the delta poll selects — metadata + body, so an inbound
# message normalizes from the delta payload without a separate full-body fetch
# (mirrors the connector's _DELTA_SELECT so behavior matches the sandbox proof).
_DELTA_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,conversationId,body"
)

# The MSGRAPH_* env the client reads via shared.secrets (never os.environ direct).
MSGRAPH_ENV = ("MSGRAPH_TENANT_ID", "MSGRAPH_CLIENT_ID", "MSGRAPH_CLIENT_SECRET", "MSGRAPH_MAILBOX")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MsGraphAuthError(RuntimeError):
    """Token mint failed — bad client creds, wrong tenant, or the identity platform
    rejected the grant. Surfaced (without the secret) so the caller logs a clear
    refusal rather than a raw 4xx."""


class MsGraphApiError(RuntimeError):
    """A Graph request returned a 4xx/5xx after retries. Carries the HTTP status so
    the delta path can detect a 410 expired-cursor and resync; the (truncated)
    body is included so logs see WHY a call failed, never just a status code."""

    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"MSGraph {method} {url} -> HTTP {status}: {body or '(empty body)'}")


# ---------------------------------------------------------------------------
# HTML → text (mirrors operator/connectors/msgraph-mail normalize.py)
# ---------------------------------------------------------------------------

_DROP_CONTENT_TAGS = {"script", "style", "head", "title"}
_BREAK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML fragment, dropping script/style content
    and inserting newlines at block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _DROP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BREAK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw_html: str) -> str:
    """Strip an HTML mail body to plain text (stdlib only): block boundaries become
    line breaks, empty lines dropped, entities decoded. Content, not layout."""
    parser = _TextExtractor()
    parser.feed(raw_html)
    parser.close()
    text = html.unescape(parser.get_text())
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------------------
# Graph message → InboundMessage DTO (spec D2; mirrors the connector normalize)
# ---------------------------------------------------------------------------


def _bare_address(recipient: Any) -> str | None:
    """A Graph recipient (``{"emailAddress": {"address": ...}}``) → bare lowercased
    address, or None when absent/malformed (never invented)."""
    if not isinstance(recipient, dict):
        return None
    email = recipient.get("emailAddress")
    if not isinstance(email, dict):
        return None
    addr = (email.get("address") or "").strip().lower()
    return addr or None


def _address_list(recipients: Any) -> list[str]:
    """A Graph recipient array → bare lowercased addresses (blanks dropped)."""
    if not isinstance(recipients, list):
        return []
    return [a for a in (_bare_address(r) for r in recipients) if a]


def _body_text(raw: dict[str, Any]) -> str:
    """Plain-text body: strip HTML when ``body.contentType == 'html'``; otherwise
    the text content verbatim. ``""`` when the message carries no body content."""
    body = raw.get("body")
    if not isinstance(body, dict):
        return ""
    content = body.get("content") or ""
    if not content:
        return ""
    if (body.get("contentType") or "").lower() == "html":
        return html_to_text(content)
    return content.strip()


def has_body_content(raw: dict[str, Any]) -> bool:
    """Whether a raw Graph message carries body content — the poller's signal to
    fall back to the read path for a delta item that omitted the body."""
    body = raw.get("body")
    return isinstance(body, dict) and bool(body.get("content"))


def normalize_message(raw: dict[str, Any], *, mailbox: str) -> dict[str, Any]:
    """A raw Graph message → the ``InboundMessage`` DTO dict (spec D2).

    ``provider_refs`` is opaque and carries the Graph ids the reply transport
    needs (``graph_message_id`` + ``conversation_id``). Missing fields degrade to
    empty/None, never a guess. Byte-compatible with the connector's
    ``normalize_message`` so the seam DTO is identical whichever path produced it.
    """
    message_id = raw.get("id") or ""
    conversation_id = raw.get("conversationId")
    from_addr = _bare_address(raw.get("from")) or ""
    return {
        "provider": PROVIDER,
        "mailbox": mailbox,
        "message_id": message_id,
        "thread_ref": conversation_id,
        "from_addr": from_addr,
        "to": _address_list(raw.get("toRecipients")),
        "cc": _address_list(raw.get("ccRecipients")),
        "subject": raw.get("subject") or "",
        "body_text": _body_text(raw),
        "received_at": raw.get("receivedDateTime"),
        "provider_refs": {
            "graph_message_id": message_id,
            "conversation_id": conversation_id,
        },
    }


# ---------------------------------------------------------------------------
# Recipient nesting (flat → Graph shape; mirrors the connector)
# ---------------------------------------------------------------------------


def _recipients(addrs: str | list[str] | None) -> list[dict[str, Any]] | None:
    """Build Graph ``toRecipients``/``ccRecipients`` nesting from FLAT addresses.
    Accepts a single address string or a list; drops blanks. The overlay's send
    args are flat (governance saw them flat); the nesting happens here."""
    if addrs is None:
        return None
    items = [addrs] if isinstance(addrs, str) else list(addrs)
    out = [{"emailAddress": {"address": str(a).strip()}} for a in items if str(a).strip()]
    return out or None


def _message_payload(
    *, to: str | list[str], subject: str, body_text: str, cc: str | list[str] | None
) -> dict[str, Any]:
    """A Graph ``message`` resource (Text body) from flat args."""
    msg: dict[str, Any] = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body_text},
        "toRecipients": _recipients(to) or [],
    }
    cc_nested = _recipients(cc)
    if cc_nested:
        msg["ccRecipients"] = cc_nested
    return msg


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class MsGraphClient:
    """App-only Graph client over stdlib urllib. Mailbox pinned at construction."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        timeout: float = _DEFAULT_TIMEOUT_S,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        # Fail closed at construction: a missing credential must never reach a
        # live path reporting success. Name the offender without echoing a value.
        missing = [
            name
            for name, val in (
                ("tenant_id", tenant_id),
                ("client_id", client_id),
                ("client_secret", client_secret),
                ("mailbox", mailbox),
            )
            if not (val or "").strip()
        ]
        if missing:
            raise ValueError(
                f"MsGraphClient: missing required config {missing} "
                f"(MSGRAPH_{'/MSGRAPH_'.join(m.upper() for m in missing)})"
            )
        self._tenant_id = tenant_id.strip()
        self._client_id = client_id.strip()
        self._client_secret = client_secret
        self.mailbox = mailbox.strip()
        self._token_url = f"{_TOKEN_HOST}/{self._tenant_id}/oauth2/v2.0/token"
        self._token: str | None = None
        self._token_deadline = 0.0
        self._timeout = timeout
        # Injectable for tests (defaults to urllib.request.urlopen). Same shape as
        # the reply/confirm transports so the whole seam mocks identically.
        self._open = opener or urllib.request.urlopen

    # ---- url building -----------------------------------------------------
    def _mail_url(self, suffix: str) -> str:
        """A Graph URL under the PINNED mailbox: ``.../users/{mailbox}/{suffix}``.

        The mailbox (trusted config) and Graph-issued message ids are passed raw,
        matching the sandbox-proven connector — Graph wants the bare address in the
        path, and quoting the ``@`` breaks the route."""
        return f"{_GRAPH_BASE}/users/{self.mailbox}/{suffix}"

    def _token_host(self) -> str:
        """The token endpoint (log-safe: no secret, tenant id only)."""
        return f"{_TOKEN_HOST}/{self._tenant_id}/oauth2/v2.0/token"

    # ---- auth -------------------------------------------------------------
    def _mint_token(self) -> None:
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": _GRAPH_SCOPE,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._token_url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Never include the response body verbatim — it can echo the request.
            raise MsGraphAuthError(
                f"token mint (client_credentials) rejected with HTTP {exc.code} at {self._token_host()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MsGraphAuthError(
                f"token request to {self._token_host()} failed: {exc.reason}"
            ) from exc
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MsGraphAuthError("token response was not decodable JSON") from exc
        token = body.get("access_token")
        if not token:
            raise MsGraphAuthError("token response had no access_token")
        expires_in = int(body.get("expires_in", 3600))
        self._token = token
        self._token_deadline = time.monotonic() + max(expires_in - _TOKEN_SKEW_SECONDS, 0)

    def _bearer(self) -> str:
        if self._token is None or time.monotonic() >= self._token_deadline:
            self._mint_token()
        assert self._token is not None
        return self._token

    # ---- requests ---------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        """Authenticated Graph request to an ABSOLUTE url (built by ``_mail_url`` or
        a Graph-supplied ``@odata.nextLink``/``deltaLink``). Returns parsed JSON (or
        None for a 202/204/empty body). Retries a 429 with backoff; re-mints once on
        a 401. Raises :class:`MsGraphApiError` on a 4xx/5xx (its ``status`` lets the
        delta path detect a 410).

        Every outcome is also recorded in the connector-health ledger
        (ADR 0080 / ss#1990): the Graph mail channel bypasses the MCP tool
        path (poller in the gate, transports in plugins), so the
        ``post_tool_call`` seam never sees it — this chokepoint is where the
        whole channel's health is observed. Conn-class is computed from the
        REAL status code here, not from message text."""
        try:
            result = self._request_inner(method, url, params=params, json_body=json_body)
        except MsGraphAuthError as exc:
            # A dead app credential / tenant grant is the canonical ADR 0078
            # outage: auth failures are conn-class by definition.
            _record_channel_outcome(ok=False, status=401, message=str(exc))
            raise
        except MsGraphApiError as exc:
            _record_channel_outcome(ok=False, status=exc.status, message=str(exc))
            raise
        _record_channel_outcome(ok=True)
        return result

    def _request_inner(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if urllib.parse.urlsplit(url).query else "?"
                url = url + sep + urllib.parse.urlencode(clean)
        data = (
            json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            if json_body is not None
            else None
        )
        refreshed = False
        last_status = 0
        last_body = ""
        for attempt in range(_MAX_ATTEMPTS):
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self._bearer()}",
                    "Accept": "application/json",
                    **({"Content-Type": "application/json"} if data is not None else {}),
                },
                method=method,
            )
            try:
                with self._open(req, timeout=self._timeout) as resp:
                    status = resp.status
                    payload = resp.read()
                if status in (202, 204) or not payload:
                    return None
                return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                try:
                    last_body = exc.read().decode("utf-8", "replace")
                except Exception:  # noqa: BLE001 — body read best-effort for the log
                    last_body = ""
                if exc.code == 429 and attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(min(2**attempt, 8))
                    continue
                if exc.code == 401 and not refreshed:
                    self._token = None  # force a fresh mint, then retry once
                    refreshed = True
                    continue
                raise MsGraphApiError(method, url, exc.code, _truncate_body(last_body)) from exc
            except urllib.error.URLError as exc:
                raise MsGraphApiError(method, url, 0, f"unreachable: {exc.reason}") from exc
        raise MsGraphApiError(method, url, last_status, _truncate_body(last_body))

    # ---- reads ------------------------------------------------------------
    def get_message(self, message_id: str) -> Any:
        """Get one message by id, including its full ``body`` (the read path the
        poller falls back to when a delta item omits the body)."""
        return self.request("GET", self._mail_url(f"messages/{message_id}"))

    def poll_delta(self, delta_link: str | None = None) -> tuple[list[Any], str | None, bool]:
        """Drain the inbox delta query, following ``@odata.nextLink`` pages, and
        return ``(raw_messages, delta_link, cursor_reset)``.

        First call (no ``delta_link``) issues the base delta URL with a bounded
        ``$select``; a subsequent call passes the stored ``deltaLink`` verbatim. A
        410 Gone on a provided cursor (expired sync state) restarts the delta from
        scratch and flags ``cursor_reset``. ``@removed`` tombstones are dropped."""
        try:
            items, out = self._drain_delta(delta_link)
            return items, out, False
        except MsGraphApiError as exc:
            if exc.status == 410 and delta_link is not None:
                items, out = self._drain_delta(None)
                return items, out, True
            raise

    def _drain_delta(self, delta_link: str | None) -> tuple[list[Any], str | None]:
        if delta_link:
            next_url: str | None = delta_link
            params: dict[str, Any] | None = None
        else:
            next_url = self._mail_url("mailFolders/inbox/messages/delta")
            params = {"$select": _DELTA_SELECT}
        items: list[Any] = []
        delta_out: str | None = None
        while next_url:
            resp = self.request("GET", next_url, params=params) or {}
            params = None  # only the first constructed call carries $select
            items.extend(
                v for v in resp.get("value", []) if isinstance(v, dict) and "@removed" not in v
            )
            delta_out = resp.get("@odata.deltaLink") or delta_out
            next_url = resp.get("@odata.nextLink")
        return items, delta_out

    # ---- writes -----------------------------------------------------------
    def send_mail(
        self,
        *,
        to: str | list[str],
        subject: str,
        body_text: str,
        cc: str | list[str] | None = None,
        save_to_sent_items: bool = True,
    ) -> Any:
        """Send a new message (POST /sendMail), saving a copy to Sent Items. Graph
        returns 202 with no body. The out-of-band confirm dispatch transport."""
        self.request(
            "POST",
            self._mail_url("sendMail"),
            json_body={
                "message": _message_payload(to=to, subject=subject, body_text=body_text, cc=cc),
                "saveToSentItems": save_to_sent_items,
            },
        )
        return {"status": "sent", "saveToSentItems": save_to_sent_items}

    def reply(self, message_id: str, comment: str, *, reply_all: bool = False) -> Any:
        """Reply on an existing message thread (POST /messages/{id}/reply or
        /replyAll) — the recipient-locked reply path: Graph derives the recipients
        from the original message, so the reply cannot be redirected. 202, no body."""
        action = "replyAll" if reply_all else "reply"
        self.request(
            "POST",
            self._mail_url(f"messages/{message_id}/{action}"),
            json_body={"comment": comment},
        )
        return {"status": "replied", "reply_all": reply_all, "message_id": message_id}


def _truncate_body(text: str | None) -> str:
    """Trim an API error body to a log-safe length (no credential is ever in a
    Graph error body; request headers are never included regardless)."""
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= _MAX_ERROR_BODY else text[:_MAX_ERROR_BODY] + "...(truncated)"


# ---- env-driven construction (single source of truth) ---------------------


def build_client_from_env(*, opener: Callable[..., Any] | None = None) -> MsGraphClient | None:
    """Construct an :class:`MsGraphClient` from ``MSGRAPH_*`` via ``shared.secrets``.

    Returns ``None`` (fail-closed, with a clear WARNING) when any required var is
    unset — a msgraph transport with no credentials REFUSES rather than falling
    back to another provider. Secrets are read only through ``shared.secrets``
    (AGENTS.md #4); no value is ever logged."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in MSGRAPH_ENV:
        try:
            values[name] = get_secret(name)
        except KeyError:
            missing.append(name)
    if missing:
        logger.warning(
            "msgraph client: not constructed — required env unset: %s (fail-closed)",
            ", ".join(missing),
        )
        return None
    try:
        return MsGraphClient(
            tenant_id=values["MSGRAPH_TENANT_ID"],
            client_id=values["MSGRAPH_CLIENT_ID"],
            client_secret=values["MSGRAPH_CLIENT_SECRET"],
            mailbox=values["MSGRAPH_MAILBOX"],
            opener=opener,
        )
    except ValueError as exc:
        logger.warning("msgraph client: construction failed (%s)", exc)
        return None


__all__ = [
    "MSGRAPH_ENV",
    "PROVIDER",
    "MsGraphApiError",
    "MsGraphAuthError",
    "MsGraphClient",
    "build_client_from_env",
    "has_body_content",
    "html_to_text",
    "normalize_message",
]
