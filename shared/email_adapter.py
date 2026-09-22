"""Which mail transport a seat authored, read from its own customer.yaml.

Which transport a tool takes is a property of the ENGAGEMENT, not of the tool's
name. A seat authors ``connectors.Email.adapter`` once and every path that
speaks mail has to agree with it, because two paths that disagree about one
seat's transport is a worse failure than either of them picking wrong.

THIS IS THE THIRD PLACE THIS READ LIVES. ``hermes-smd-reply`` and
``hermes-smd-trust`` each carry their own copy, with slightly different
handling of the failure case. Rather than add a fourth copy later, the read
lands here as a module neither of those plugins imports yet, so converting them
is a one-line change whenever someone is in those files for another reason and
nothing has to move while another session holds one of them open.

THE DEFAULT AND THE FAILURE ARE NOT THE SAME THING, and this is the one place
the existing copies differ from each other. A seat that authors no adapter gets
``agentmail``, matching both of them: that default predates msgraph and every
seat but one still authors it. But a config that could not be READ gets an
exception, not the default. Those two states are indistinguishable from inside
a ``try`` that returns a string, and they call for opposite behaviour: a Graph
seat silently dispatched to the AgentMail branch reports that it cannot read its
own mail, which is exactly the dead end a caller cannot diagnose from the turn.
"""

from __future__ import annotations

from shared.customer_config import CustomerConfig, CustomerConfigError

#: The adapter a seat gets when its Email connector names none. Shared with
#: ``hermes-smd-reply`` and ``hermes-smd-trust`` on purpose.
DEFAULT_ADAPTER = "agentmail"

ADAPTER_AGENTMAIL = "agentmail"
ADAPTER_MSGRAPH = "msgraph"


class EmailAdapterUnreadable(RuntimeError):
    """The seat's config could not be read, so its transport is UNKNOWN.

    Never conflated with "the seat authored no adapter". Unknown is not a
    default; it is a refusal the caller must surface.
    """


def email_adapter(config: CustomerConfig | None = None) -> str:
    """The seat's authored Email adapter, lowercased.

    Re-read live rather than cached: a seat whose transport changes must be
    dispatched correctly on the next turn, without a redeploy.
    """
    try:
        cfg = config if config is not None else CustomerConfig.from_volume()
        record = cfg.connectors.get("Email")
    except CustomerConfigError as exc:
        raise EmailAdapterUnreadable(f"the seat's customer.yaml could not be read: {exc}") from exc
    except OSError as exc:
        raise EmailAdapterUnreadable(f"the seat's customer.yaml could not be read: {exc}") from exc
    if isinstance(record, dict):
        adapter = record.get("adapter")
        if isinstance(adapter, str) and adapter.strip():
            return adapter.strip().lower()
    return DEFAULT_ADAPTER


def email_connector_enabled(config: CustomerConfig | None = None) -> bool:
    """Whether this seat authors an Email connector that is switched on.

    This is the CAPABILITY question, and it is what the attachment plugin gates
    its load on. Gating on a vendor's key instead is how the tools came to be
    absent on a Microsoft 365 seat: the gate was asking about AgentMail when the
    question was whether the seat has mail at all.

    Unreadable config answers False rather than raising. A plugin's load
    decision runs before any turn, so there is no one to tell; the tools then do
    not register, which is the same outcome as a seat with no mail. A seat that
    HAS mail and a broken config surfaces that through
    :func:`email_adapter` inside the turn, where someone can read it.
    """
    try:
        cfg = config if config is not None else CustomerConfig.from_volume()
        record = cfg.connectors.get("Email")
    except (CustomerConfigError, OSError):
        return False
    if not isinstance(record, dict):
        return False
    enabled = record.get("enabled")
    return enabled is not False


__all__ = [
    "ADAPTER_AGENTMAIL",
    "ADAPTER_MSGRAPH",
    "DEFAULT_ADAPTER",
    "EmailAdapterUnreadable",
    "email_adapter",
    "email_connector_enabled",
]
