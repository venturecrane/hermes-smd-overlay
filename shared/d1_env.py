"""Env-bound :class:`D1Client` construction helpers.

At Machine boot every plugin that wants per-customer D1 access should
call :func:`d1_client_from_env`; it reads ``CUSTOMER_SLUG`` and the
binding env var, validates both, and returns a :class:`D1Client`
already wired to the correct namespace.

Ported from ``ss-console/operator/adapter/d1_env.py``. The source
returned a ``NamespacedD1Executor`` wrapping a raw HTTP executor with
the audit writer injected. The overlay's :class:`D1Client` folds the
namespace assertion directly into the client (no wrapper), so this
helper is just an env-binding factory.
"""

import logging
import os

from shared.d1_client import D1Client

logger = logging.getLogger(__name__)


DEFAULT_BINDING_NAME = "CUSTOMER_DB"


def d1_client_from_env(
    customer_slug: str | None = None,
    *,
    binding_name: str = DEFAULT_BINDING_NAME,
) -> D1Client:
    """Construct a :class:`D1Client` from the per-customer Machine env.

    Args:
        customer_slug: Optional explicit slug. When omitted, the value
            of the ``CUSTOMER_SLUG`` env var is used (the Machine boot
            sequence is required to populate this).
        binding_name: Name of the env var carrying the D1 file path.
            Defaults to ``CUSTOMER_DB``.

    Returns:
        A :class:`D1Client` bound to the resolved customer slug. The
        underlying connection is opened lazily on first call.

    Raises:
        RuntimeError: If no slug can be resolved (neither argument nor
            env var is set). This is a bootstrap-time invariant
            failure and should abort container start.
        ValueError: If the resolved slug does not match the slug
            regex.
    """
    slug = customer_slug if customer_slug is not None else os.environ.get("CUSTOMER_SLUG", "")
    if not slug:
        raise RuntimeError(
            "d1_client_from_env: CUSTOMER_SLUG env var unset (and no explicit "
            "slug passed); Machine bootstrap must set this from the per-customer "
            "binding"
        )
    return D1Client(binding_name=binding_name, customer_slug=slug)


__all__ = ["DEFAULT_BINDING_NAME", "d1_client_from_env"]
