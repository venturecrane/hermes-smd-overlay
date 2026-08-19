"""Replay a dead-lettered msgraph item through the gate→router path (overlay#275).

The poller preserves a permanently-failing inbound message as
``<state-dir>/dead-letter/<sha16>.json`` holding ``{"reason", "message_id",
"raw"}`` where ``raw`` is the UN-normalized Graph item. This tool is the
documented recovery path the dead-letter log line points at:

    python3 -m shared.msgraph_replay /opt/data/msgraph/dead-letter/<file>.json

It re-normalizes ``raw`` exactly as the poller would, rebuilds the byte-exact
stamped envelope (compact separators — the HMAC is over these bytes), signs
with the route secret, and POSTs to the same loopback adapter the poller uses.
On a 2xx the file is renamed ``<file>.replayed`` so the recovery is visible and
idempotent; on anything else the file is left untouched. A replay enters the
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

from shared import msgraph_client
from shared.msgraph_poller import (
    EVENT_TYPE,
    SOURCE,
    _default_forward,
    _hex_hmac_sha256,
)
from shared.secrets import get_secret

logger = logging.getLogger("hermes-smd-msgraph-replay")

_SIGNING_SECRET_ENV = "WEBHOOK_SECRET_MSGRAPH"


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
    forward_fn: Callable[..., int] | None = None,
) -> int:
    """Replay one dead-letter file. Returns the process exit code."""
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
    signature = _hex_hmac_sha256(body, signing_secret)
    request_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    forward = forward_fn or _default_forward
    try:
        status = forward(body=body, signature=signature, request_id=request_id)
    except Exception as exc:  # noqa: BLE001 — report, don't traceback, on a seat console
        logger.error("replay: forward failed (%s)", exc)
        return 1
    if not 200 <= status < 300:
        logger.error("replay: adapter rejected the item (HTTP %d); file left in place", status)
        return 1
    replayed = path + ".replayed"
    try:
        os.replace(path, replayed)
    except OSError as exc:
        logger.warning("replay: accepted (HTTP %d) but rename failed (%s)", status, exc)
        return 0
    logger.info("replay: accepted (HTTP %d); %s -> %s", status, path, replayed)
    return 0


def main(argv: list[str] | None = None, *, forward_fn: Callable[..., int] | None = None) -> int:
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
