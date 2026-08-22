"""One seam through which a NON-model process can send the seat's own mail.

WHY THIS EXISTS, and it is a layout problem before it is a design one. The send
machinery lives in ``plugins/hermes-smd-trust`` — ``enforce.evaluate_tool_call``
(the ceiling, the taint gate, the content floor, the fabrication scan) and
``outbound_send`` (the two broker-backed transports). Plugin directories are
hyphenated, so they are not importable module paths: ``hermes-smd-establishment``
cannot import them, and neither can anything under ``shared/``. Every plugin is
loaded from its file location under a sanitized name (``tests/conftest.py``).

The rule-request loop (ss-console#2546) needs a send from three places that are
not the trust plugin: the moment a paralegal's rule is recorded, the moment an
administrator answers it, and a background sweeper that finds a rule nobody
answered. So the trust plugin publishes ITS sender here at ``register()`` time
and the others call it.

WHAT THIS IS NOT. It is not a way around the gate. The registered callable
re-authorizes every payload through the same ``evaluate_tool_call`` a model's
own send goes through, on the same session, with the same taint state — that is
the whole reason the sender is published from the plugin that owns the gate
rather than reimplemented here against the raw transports (which ARE importable
from ``shared``, and which is precisely the shortcut this module exists to
foreclose).

UNREGISTERED IS A FIRST-CLASS ANSWER, not an error. If the trust plugin has not
registered — a stripped seat, a boot ordering nobody predicted, a test — every
caller gets ``DispatchResult(sent=False, reason=...)`` and must SAY so. The
failure mode this forecloses is the one that matters on this feature: an
Operator telling a paralegal that an administrator has been asked, when nothing
left the building.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    """What one out-of-turn send did.

    ``sent`` is the only field a caller may treat as a promise, and it means the
    transport accepted the message — never that anyone read it. ``reason`` is
    written to be quoted to a person: on a refusal it is what the gate said, so
    the Operator can tell somebody why their request did not go rather than
    inventing a cause.
    """

    sent: bool
    message_id: str = ""
    reason: str = ""
    recipients: tuple[str, ...] = field(default_factory=tuple)


#: ``(payload, session_id) -> DispatchResult``. Published by the trust plugin.
Sender = Callable[..., DispatchResult]

_LOCK = threading.Lock()
_SENDER: Sender | None = None


def set_sender(sender: Sender | None) -> None:
    """Publish the seat's gated internal sender. Called once, at register.

    Idempotent and last-writer-wins. A second registration is a re-register of
    the same plugin (tests do this constantly) rather than a second seat, so it
    replaces rather than raising: raising inside ``register`` would take the
    whole plugin down over a duplicate wiring.
    """
    global _SENDER
    with _LOCK:
        _SENDER = sender


def available() -> bool:
    """Whether anything can send right now. Cheap, and safe to ask per turn."""
    with _LOCK:
        return _SENDER is not None


def dispatch(
    *,
    to: list[str],
    subject: str,
    text: str,
    session_id: str = "",
    cc: list[str] | None = None,
    templated: bool = True,
    **extra: Any,
) -> DispatchResult:
    """Send one message through the seat's own gate. Never raises.

    ``templated`` says the body is a FIXED template this repo authored, not
    prose a model composed. It reaches ``shared.spec_gate`` and skips exactly
    one branch there (see ``check_spec_gate``); every other gate runs unchanged.

    Exception-safety is load-bearing rather than tidy: two of the three callers
    are hooks and the third is a daemon thread, and a raise from any of them
    would cost a turn or kill the sweeper. A failure here costs one
    notification, and the caller is required to say the notification did not go.
    """
    with _LOCK:
        sender = _SENDER
    if sender is None:
        return DispatchResult(
            sent=False,
            reason="this seat has no send path wired",
            recipients=tuple(to or ()),
        )
    if not to:
        return DispatchResult(sent=False, reason="no recipient")
    try:
        return sender(
            to=list(to),
            subject=subject,
            text=text,
            session_id=session_id,
            cc=list(cc or []),
            templated=templated,
            **extra,
        )
    except Exception as exc:  # noqa: BLE001 — a hook and a daemon call this
        logger.warning("send_dispatch: out-of-turn send raised (%s)", exc, exc_info=True)
        return DispatchResult(
            sent=False,
            reason=f"the send could not be attempted ({exc})",
            recipients=tuple(to),
        )


__all__ = ["DispatchResult", "Sender", "available", "dispatch", "set_sender"]
