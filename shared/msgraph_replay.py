"""Replay a dead-lettered msgraph item through the gate→router path (overlay#275).

The poller preserves a permanently-failing inbound message as
``<state-dir>/dead-letter/<sha16>.json`` holding ``{"reason", "message_id",
"raw"}`` where ``raw`` is the UN-normalized Graph item. This tool is the
documented recovery path the dead-letter log line points at:

    python3 -m shared.msgraph_replay /opt/data/msgraph/dead-letter/<file>.json

It re-normalizes ``raw`` exactly as the poller would, rebuilds the byte-exact
stamped envelope (compact separators — the HMAC is over these bytes), signs
with the route secret, and POSTs to the same loopback adapter the poller uses.
On a 2xx that started a turn the file is renamed ``<file>.replayed`` so the
recovery is visible and idempotent; on anything else the file is left untouched.
A 2xx is not by itself a delivery: the adapter answers HTTP 200
``{"status":"duplicate"}`` when the request id is in its 1h in-memory
idempotency cache, and HTTP 200 ``{"status":"ignored"}`` for a filtered event.
Both dropped the item, so both exit 1 with the file left in place. A replay enters the
model ONLY through the same fenced webhook door as live mail — this tool grants
nothing that the poller does not.

Exit codes follow bootstrap/cli.py: 0 accepted, 1 rejected/transport failure,
2 bad input (missing file, id-less item, missing secret/mailbox).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable

from shared import forward_signature, msgraph_client
from shared.msgraph_poller import (
    EVENT_TYPE,
    SOURCE,
    _post_forward,
)
from shared.secrets import get_secret

logger = logging.getLogger("hermes-smd-msgraph-replay")

_SIGNING_SECRET_ENV = "WEBHOOK_SECRET_MSGRAPH"

#: The Hermes webhook adapter's idempotency TTL (``_idempotency_ttl = 3600`` in
#: hermes ``gateway/platforms/webhook.py``). In-memory: a gateway restart clears it.
_ADAPTER_IDEMPOTENCY_TTL_S = 3600

#: 2xx answers from the adapter that mean it did NOT start a turn. ``duplicate``
#: is the idempotency cache hit (HTTP 200); ``ignored`` is an event/filter/script
#: drop (HTTP 200). Neither is a delivery.
_DROPPED_STATUSES = frozenset({"duplicate", "ignored"})


def _adapter_status(payload: bytes) -> str:
    """The adapter's ``{"status": ...}`` field, or "" when absent/unparseable."""
    try:
        parsed = json.loads(payload.decode("utf-8")) if payload else None
    except (UnicodeDecodeError, ValueError):
        return ""
    if isinstance(parsed, dict) and isinstance(parsed.get("status"), str):
        return parsed["status"]
    return ""


def build_envelope(raw: dict, *, mailbox: str) -> tuple[bytes, str] | None:
    """The poller's exact forward body + message id, or None for an id-less item.

    Byte-parity with ``MsGraphPoller._forward`` is load-bearing: same key order,
    same compact separators, because the signature is over these exact bytes."""
    dto = msgraph_client.normalize_message(raw, mailbox=mailbox)
    message_id = dto.get("message_id") or ""
    if not message_id:
        return None
    body = json.dumps(
        {
            "source": SOURCE,
            "event_type": EVENT_TYPE,
            "inbound_message": dto,
            "event_id": message_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return body, message_id


def replay(
    path: str,
    *,
    mailbox: str,
    signing_secret: str,
    forward_fn: Callable[..., tuple[int, bytes]] | None = None,
) -> int:
    """Replay one dead-letter file. Returns the process exit code.

    ``forward_fn`` returns ``(status, response_body)``; the body is read because
    the adapter answers HTTP 200 ``{"status":"duplicate"}`` for an idempotency
    cache hit, which dropped the replay rather than delivering it."""
    try:
        with open(path, encoding="utf-8") as fh:
            letter = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error("replay: cannot read %s (%s)", path, exc)
        return 2
    raw = letter.get("raw") if isinstance(letter, dict) else None
    if not isinstance(raw, dict):
        logger.error("replay: %s carries no raw item", path)
        return 2
    built = build_envelope(raw, mailbox=mailbox)
    if built is None:
        logger.error(
            "replay: item in %s has no message id; it cannot be replayed through the "
            "idempotent door — handle manually",
            path,
        )
        return 2
    body, message_id = built
    timestamp, signature = forward_signature.sign_forward(body, signing_secret)
    request_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    forward = forward_fn or _post_forward
    try:
        status, payload = forward(
            body=body, signature=signature, timestamp=timestamp, request_id=request_id
        )
    except Exception as exc:  # noqa: BLE001 — report, don't traceback, on a seat console
        logger.error("replay: forward failed (%s)", exc)
        return 1
    if not 200 <= status < 300:
        logger.error("replay: adapter rejected the item (HTTP %d); file left in place", status)
        return 1
    adapter_status = _adapter_status(payload)
    if adapter_status == "duplicate":
        logger.error(
            "replay: NOT delivered. The adapter answered HTTP %d status=duplicate: it "
            "dropped the item because X-Request-ID %s was already seen within its %ds "
            "in-memory idempotency cache (the poller or an earlier replay sent this "
            "message less than an hour ago). File left in place. The cache entry "
            "expires %ds after that earlier send, or on a gateway restart; confirm no "
            "new webhook:msgraph session ran, then replay again after that.",
            status,
            request_id,
            _ADAPTER_IDEMPOTENCY_TTL_S,
            _ADAPTER_IDEMPOTENCY_TTL_S,
        )
        return 1
    if adapter_status in _DROPPED_STATUSES:
        logger.error(
            "replay: NOT delivered. The adapter answered HTTP %d status=%s (no turn "
            "started); file left in place. Body: %s",
            status,
            adapter_status,
            payload[:300].decode("utf-8", "replace"),
        )
        return 1
    replayed = path + ".replayed"
    try:
        os.replace(path, replayed)
    except OSError as exc:
        logger.warning("replay: accepted (HTTP %d) but rename failed (%s)", status, exc)
        return 0
    logger.info(
        "replay: accepted (HTTP %d status=%s); %s -> %s. Confirm a new webhook:msgraph "
        "session row for this delivery before calling it recovered.",
        status,
        adapter_status or "?",
        path,
        replayed,
    )
    return 0


def main(
    argv: list[str] | None = None, *, forward_fn: Callable[..., tuple[int, bytes]] | None = None
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="python3 -m shared.msgraph_replay",
        description="Replay a dead-lettered msgraph item through the fenced webhook door.",
    )
    parser.add_argument("path", help="dead-letter JSON file to replay")
    parser.add_argument(
        "--mailbox",
        default=None,
        help="operator mailbox for DTO normalization (default: MSGRAPH_MAILBOX)",
    )
    args = parser.parse_args(argv)

    mailbox = args.mailbox
    if not mailbox:
        try:
            mailbox = get_secret("MSGRAPH_MAILBOX")
        except KeyError:
            logger.error("replay: MSGRAPH_MAILBOX unset and no --mailbox given")
            return 2
    signing_secret = os.environ.get(_SIGNING_SECRET_ENV)
    if not signing_secret:
        logger.error("replay: %s unset — cannot sign the envelope", _SIGNING_SECRET_ENV)
        return 2
    return replay(args.path, mailbox=mailbox, signing_secret=signing_secret, forward_fn=forward_fn)


if __name__ == "__main__":
    sys.exit(main())
