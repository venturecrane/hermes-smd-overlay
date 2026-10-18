"""The couples persona's file surface: fenced reads, no writes (ADR 0090 R2, ss-console #3264).

THE DEFECT (captain-mark, 2026-10-18, the first couples seat to boot). The
couples role disabled Hermes' whole ``file`` toolset so that no private fact
could leave by a path. Hermes subtracts disabled toolsets LAST
(``model_tools._select_tool_names``), so that also stripped ``read_file`` out of
the ``smd_webhook_read`` toolset the email surface depends on, and the
activation gate refused to serve: "WEBHOOK TURNS CANNOT READ FILES". The gate
was right: without ``read_file`` an email turn can never set the spec
read-mark, and every voice-gated delivery refuses.

THE FIX. The couples persona disables a runtime toolset,
:data:`COUPLES_NO_FILE_WRITES` (``write_file``, ``patch``, ``search_files``, ``skill_manage``),
instead of ``file``, so ``read_file`` stays and the three others are never
offered. What ``read_file`` may open is decided by the record-store plugin's
path fence (``plugins/hermes-smd-record-store/path_fence.py``), which loads on
every seat that authors a record store (every couples seat does): an allowlist
of the skill roots, the installed spec tree, and this session's own spillover.
A record-store root (``/opt/data/couples/*``), memories, ``state.db``,
sessions, logs and cron output are refused. A record is read only through
``record_store_read``, which applies the space fence.

FAIL CLOSED. The toolset exists only once ``register_couples_toolset`` ran in
the gateway process (the webhook-router plugin calls it at load, beside
``smd_webhook_read``). If it did not, Hermes would treat the disabled name as
unknown and offer the write tools again, silently. So the activation gate
resolves the webhook and telegram surfaces on a seat whose config disables this
toolset and refuses to serve if any of :data:`FORBIDDEN_TOOLS` is offered
(:func:`assert_no_forbidden_tools`).
"""

from __future__ import annotations

from typing import Any

#: The runtime toolset a couples persona disables in place of ``file``.
COUPLES_NO_FILE_WRITES = "smd_couples_no_file_writes"
COUPLES_NO_FILE_WRITES_TOOLS: tuple[str, ...] = (
    "write_file",
    "patch",
    "search_files",
    "skill_manage",
)

#: Never offered on a couples turn, on any platform the seat serves.
FORBIDDEN_TOOLS: tuple[str, ...] = (
    *COUPLES_NO_FILE_WRITES_TOOLS,
    "terminal",
    "process_manage",
    "execute_code",
    "delegate_task",
)
#: The platforms whose resolved surface is checked.
CHECKED_PLATFORMS: tuple[str, ...] = ("webhook", "telegram")

_DESCRIPTION = (
    "SMD couples role: the file tools a couples persona never gets (ADR 0090 R2). "
    "Disabled on that persona so read_file, fenced by the record-store path fence, "
    "stays on the email surface while nothing can write, patch or search by path."
)


class CouplesSurfaceError(RuntimeError):
    """A couples turn would be offered a tool the role never has."""


def register_couples_toolset() -> None:
    """Create the toolset in the live process. Idempotent."""
    from toolsets import create_custom_toolset

    create_custom_toolset(
        COUPLES_NO_FILE_WRITES, _DESCRIPTION, tools=list(COUPLES_NO_FILE_WRITES_TOOLS)
    )


def is_couples_config(config: dict[str, Any]) -> bool:
    """True when the profile config disables the couples toolset (only
    ``bootstrap/translate.py`` for ``seat.role: couples`` writes that)."""
    disabled = (config.get("agent") or {}).get("disabled_toolsets") or []
    return COUPLES_NO_FILE_WRITES in disabled


def assert_no_forbidden_tools(config: dict[str, Any]) -> None:
    """Raise :class:`CouplesSurfaceError` if a checked platform would offer a
    forbidden tool. Uses the same resolution as the webhook read check."""
    from shared.webhook_read_surface import resolve_platform_tool_names

    for platform in CHECKED_PLATFORMS:
        offered = set(FORBIDDEN_TOOLS) & resolve_platform_tool_names(config, platform)
        if offered:
            raise CouplesSurfaceError(
                f"{platform} turns would be offered {sorted(offered)} on a couples persona "
                f"(is the {COUPLES_NO_FILE_WRITES!r} toolset registered?)"
            )


def assert_read_fence_armed() -> None:
    """Raise unless the record-store path fence will load: it registers only on
    a seat that authors a record store, and without it the couples read_file
    would be unfenced."""
    from shared import record_store

    try:
        stores = record_store.authored_stores()
    except record_store.RecordStoreError as exc:
        raise CouplesSurfaceError(
            f"record_stores unreadable ({exc}); read_file is unfenced"
        ) from exc
    if not stores:
        raise CouplesSurfaceError(
            "a couples seat must author record_stores: the path fence that limits "
            "read_file loads only then"
        )


__all__ = [
    "assert_read_fence_armed",
    "CHECKED_PLATFORMS",
    "COUPLES_NO_FILE_WRITES",
    "COUPLES_NO_FILE_WRITES_TOOLS",
    "FORBIDDEN_TOOLS",
    "CouplesSurfaceError",
    "assert_no_forbidden_tools",
    "is_couples_config",
    "register_couples_toolset",
]
