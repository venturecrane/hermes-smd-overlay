"""The staff mailboxes a seat's firm has authored for the Operator to READ.

The overlay's half of a list ss-console's msgraph-mail connector also reads
(``msgraph_mail_connector/staff_mailboxes.py``). Both read the SAME authored
block off the seat's own customer.yaml, at call time:

    staff_mailbox_reads:
      mailboxes:
        - someone@firm.example

It is duplicated rather than shared because the two run in different processes
from different repos, and the connector cannot import the overlay. What keeps
them agreeing is that neither has a default: an address not on the authored
list is refused by both, and a missing or unreadable block refuses everything.

Tenant-side the firm must ALSO scope the READ app's ApplicationAccessPolicy to
the mailbox; this list is the code-layer belt to that policy's braces.
"""

from __future__ import annotations

import re
from typing import Any

from shared.customer_config import CustomerConfig, CustomerConfigError

CONFIG_BLOCK = "staff_mailbox_reads"

#: The address goes into a Graph URL path, so anything that could change the
#: path (/ ? # % \ or whitespace) never passes, even from the authored list.
_ADDRESS_RE = re.compile(r"^[a-z0-9._+'-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


class StaffMailboxRefused(ValueError):
    """The mailbox is not one the firm authored, or the list cannot be read."""


def normalize_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    addr = value.strip().lower()
    return addr if _ADDRESS_RE.fullmatch(addr) else None


def authored(config: CustomerConfig | None = None) -> tuple[str, ...]:
    """The authored addresses, normalized. Raises when the config cannot be
    read or the block is malformed, because "unreadable" must not become
    "authored nothing" silently in a reply."""
    try:
        cfg = config if config is not None else CustomerConfig.from_volume()
        block = cfg.raw.get(CONFIG_BLOCK)
    except (CustomerConfigError, OSError) as exc:
        raise StaffMailboxRefused(f"the seat's customer.yaml could not be read: {exc}") from exc
    if block is None:
        return ()
    if not isinstance(block, dict):
        raise StaffMailboxRefused(f"{CONFIG_BLOCK} must be a mapping")
    raw = block.get("mailboxes")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise StaffMailboxRefused(f"{CONFIG_BLOCK}.mailboxes must be a list of addresses")
    out: list[str] = []
    for entry in raw:
        addr = normalize_address(entry)
        if addr is not None and addr not in out:
            out.append(addr)
    return tuple(out)


def authorize(mailbox: Any, *, own_mailbox: str, config: CustomerConfig | None = None) -> str:
    """The normalized address when the firm authored it, else raise with a
    reason that names what IS authored."""
    addr = normalize_address(mailbox)
    if addr is None:
        raise StaffMailboxRefused("the mailbox is not a plain email address")
    if addr == (own_mailbox or "").strip().lower():
        raise StaffMailboxRefused("that is the Operator's own mailbox; leave mailbox empty for it")
    allowed = authored(config)
    if addr not in allowed:
        raise StaffMailboxRefused(
            f"{addr} is not a staff mailbox the firm has authored for reading "
            f"(authored: {', '.join(allowed) or 'none'}); only the firm can add one"
        )
    return addr
