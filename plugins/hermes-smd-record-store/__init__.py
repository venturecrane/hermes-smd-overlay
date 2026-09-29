"""The record-store tool trio: the only write an inbound turn is offered.

WHY THIS PLUGIN EXISTS (ss-console#2793, proven live 2026-09-25). A real
estate agent emailed a dictated open-house conversation to their seat. The
skill tried ``write_file``; Hermes auto-repaired the call to ``read_file``
because the webhook platform is never offered the ``file`` toolset (an email
body is untrusted content, and a write on such a turn is what the safe default
denies). The records directory stayed empty and the seat's own draft said
"write_file is not available". Capture by email could not work as built.

The answer is not to give inbound turns ``write_file``. It is a write that
CANNOT take a path: the model names a store the engagement authored in
``customer.yaml`` (``record_stores``) and a record inside it, and
``shared.record_store`` resolves the two to a file or refuses. The directory is
the fence. See that module for the rules.

``record_store_list``   the records in a store: name, size, modified.
``record_store_read``   one record's text.
``record_store_write``  one record, atomically; refuses to replace unless told.

THE GATE IS THE AUTHORED STORE, NOT A KEY. ``register`` asks whether this
seat authors any store. A seat that authors none gets no tools, so every other
seat's inbound surface is unchanged by this plugin's existence. There is no
``requires_env``: a failing one drops tools silently, and a capture turn
would again answer "not available" with nothing in the log.

THE WRITE REFUSES ON A TAINTED TURN (``pre_tool_call``). A turn that read
content from outside the firm may not put that content into a store an
unfenced read will later trust; the taint register (``shared.inbound``) is
the same one the trust gate consults. Fail-closed: an unresolvable taint state
refuses the write. The cost is that a person re-sends a note.

Exception-safe is NOT the contract for the handlers (that rule governs
hooks): a tool that cannot write must say so to the model, so these raise a
reason Hermes surfaces. The hook IS exception-safe, and its failure mode is
"block".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared import record_store
from shared.inbound import SESSION_TAINT
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

STRING = {"type": "string"}

TOOL_LIST = "record_store_list"
TOOL_READ = "record_store_read"
TOOL_WRITE = "record_store_write"

_STORE_ARG = {
    **STRING,
    "description": "The store's authored name (customer.yaml record_stores[].name).",
}
_NAME_ARG = {
    **STRING,
    "description": (
        "The record's file name inside the store: a single name ending in .md, "
        ".txt, .json or .yaml. Never a path."
    ),
}

#: Every tool this plugin registers. The classification-completeness suite,
#: the inbound-fence suite and the provenance-sources suite all read this.
TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    TOOL_LIST: (
        "List the records in one of this seat's authored record stores, newest "
        "first: each record's name, size and modified time. An empty list is the "
        "only thing that means the store holds nothing. Record names were chosen "
        "by this seat when it wrote them.",
        {
            "type": "object",
            "properties": {"store": _STORE_ARG},
            "required": ["store"],
            "additionalProperties": False,
        },
    ),
    TOOL_READ: (
        "Read one record from an authored record store, by store name and record "
        "name, and return its text exactly as it was written.",
        {
            "type": "object",
            "properties": {"store": _STORE_ARG, "name": _NAME_ARG},
            "required": ["store", "name"],
            "additionalProperties": False,
        },
    ),
    TOOL_WRITE: (
        "Write one record into an authored record store. The record lands inside "
        "that store's directory and nowhere else; you name the store and the file, "
        "never a path. Refuses to replace an existing record unless overwrite is "
        "true, so a new visit never silently erases an old one. Refused on a turn "
        "that read untrusted content.",
        {
            "type": "object",
            "properties": {
                "store": _STORE_ARG,
                "name": _NAME_ARG,
                "content": {**STRING, "description": "The record's full text."},
                "overwrite": {
                    "type": "boolean",
                    "description": "True to replace a record that already exists. Default false.",
                },
            },
            "required": ["store", "name", "content"],
            "additionalProperties": False,
        },
    ),
}


def _list_handler(args: dict[str, Any], **_: Any) -> str:
    try:
        records = record_store.list_records(str(args.get("store") or ""))
    except record_store.RecordStoreError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.dumps(
        {"store": args.get("store"), "records": records, "count": len(records)},
        ensure_ascii=False,
    )


def _read_handler(args: dict[str, Any], **_: Any) -> str:
    try:
        text = record_store.read_record(str(args.get("store") or ""), str(args.get("name") or ""))
    except record_store.RecordStoreError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.dumps(
        {"store": args.get("store"), "name": args.get("name"), "content": text},
        ensure_ascii=False,
    )


def _write_handler(args: dict[str, Any], **_: Any) -> str:
    try:
        receipt = record_store.write_record(
            str(args.get("store") or ""),
            str(args.get("name") or ""),
            args.get("content") if isinstance(args.get("content"), str) else "",
            overwrite=bool(args.get("overwrite", False)),
        )
    except record_store.RecordStoreError as exc:
        raise RuntimeError(str(exc)) from exc
    return json.dumps(receipt, ensure_ascii=False)


_HANDLERS = {
    TOOL_LIST: _list_handler,
    TOOL_READ: _read_handler,
    TOOL_WRITE: _write_handler,
}

_TAINTED_MESSAGE = (
    "record_store_write refused: this turn read content from outside the firm, "
    "and a record store keeps only what a rostered person said. Ask the person "
    "to send the note again on its own."
)


def _turn_is_tainted(session_id: Any) -> bool:
    """True when this session ingested content from outside the firm.

    Fail-closed: an unreadable taint register reads as tainted.
    """
    try:
        return SESSION_TAINT.is_tainted(str(session_id or ""))
    except Exception:  # noqa: BLE001 — an unresolvable taint state refuses
        logger.exception("hermes-smd-record-store: taint unresolved; refusing write")
        return True


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    """Block ``record_store_write`` on a tainted session. Exception-safe."""
    try:
        if (kwargs.get("tool_name") or "") != TOOL_WRITE:
            return None
        if _turn_is_tainted(kwargs.get("session_id")):
            logger.info("hermes-smd-record-store: write refused (tainted turn)")
            return {"action": "block", "message": _TAINTED_MESSAGE}
        return None
    except Exception:  # noqa: BLE001 — a hook must never raise; refusing is the safe shape
        logger.exception("hermes-smd-record-store: pre_tool_call failed; refusing write")
        return {"action": "block", "message": _TAINTED_MESSAGE}


def register(ctx: Any) -> None:
    """Register the trio on a seat that authors at least one record store."""
    try:
        stores = record_store.authored_stores()
    except record_store.RecordStoreError as exc:
        logger.warning(
            "hermes-smd-record-store: record_stores unreadable (%s); registering no tools", exc
        )
        return
    if not stores:
        logger.info("hermes-smd-record-store: no record store authored on this seat; no tools")
        return
    for name, (description, schema) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="record_store",
            schema=schema,
            handler=_HANDLERS[name],
            description=description,
            emoji="",
        )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    logger.info(
        "hermes-smd-record-store registered %d tools over stores %s",
        len(TOOLS),
        sorted(stores),
    )


__all__ = ["TOOLS", "TOOL_LIST", "TOOL_READ", "TOOL_WRITE", "on_pre_tool_call", "register"]
