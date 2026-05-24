"""Env-var-only credential access for plugins.

Credentials live in process environment variables, populated from Fly secrets
at Machine boot. This module is the single gate: plugins call ``get_secret``
or ``require``; nothing reads ``os.environ`` directly. No clipboard reads, no
file reads, no echoing of values into logs.

Per AGENTS.md hard rule #4: never read credentials from anywhere except
environment variables exposed through this module.
"""

import logging
import os

logger = logging.getLogger(__name__)


def get_secret(name: str) -> str:
    """Return the value of an environment variable.

    Args:
        name: Environment variable name (e.g. ``ANTHROPIC_API_KEY``).

    Returns:
        The variable's value as a string.

    Raises:
        KeyError: If the variable is not set in the process environment. The
            error message names the missing variable but never echoes a value.
    """
    try:
        return os.environ[name]
    except KeyError as exc:
        raise KeyError(
            f"required secret {name!r} is not set in the process environment"
        ) from exc


def require(*names: str) -> dict[str, str]:
    """Return a mapping of name → value for every requested secret.

    Pulls all requested secrets in one call and raises a single error listing
    every missing name, rather than failing on the first one. Useful at plugin
    register time when callers want one diagnostic for the full credential set.

    Args:
        *names: Environment variable names to look up.

    Returns:
        Dict mapping each requested name to its environment value.

    Raises:
        KeyError: If one or more names are unset. The error message lists every
            missing name; no values are included.
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.environ.get(name)
        if value is None:
            missing.append(name)
        else:
            resolved[name] = value
    if missing:
        raise KeyError(
            f"required secrets are not set in the process environment: {', '.join(missing)}"
        )
    return resolved
