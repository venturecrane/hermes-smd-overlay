"""What object a tool call touched, and what it wrote (ss-console#2497).

THE GAP THIS CLOSES. A ``TOOL_CALL_COMPLETED`` row named the tool, the outcome,
the trust trail and — since #2122 — the matter. It never named the THING. Pulled
over the read seam on 2026-08-21 (``vfy_01M0H8DR6JAPYVHFMNJZXQZ517``), the A&P
ledger could say the Operator read a document on a matter and could not say WHICH
document; it could say a memo was written and not which memo, nor what was in it.
For the cross-matter question (#2167) that is the difference between a record and
a list of verbs: session ``20260820_195837_68d654ce`` read four matters, and the
rows cannot distinguish the read that was legitimate from the one that was not.

TWO FACTS, BOTH CHEAP, NEITHER THE CONTENT:

* the object's own id in the source system — the join to the firm's record;
* ``sha256`` of the body a write actually wrote — the proof that the memo in
  Smokeball today is the memo this row describes, without the ledger ever
  holding a word of it.

OBSERVED SHAPES ONLY. Every key spelling walked below is one this repo has seen
on the wire, and the citation is beside it. Where a tool genuinely returns no id,
this records none: ``smd_deliver_draft`` answers with a sentence and mints
nothing, so its rows carry ``written_body_sha256`` and no draft id. Inventing a
plausible key would produce a field no query ever matches, which is worse than
the absence it papers over — the absence at least reads as absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from shared.outbound_recipient import extract_draft_id_from_result

logger = logging.getLogger(__name__)

#: Smokeball's memo write. Signature ``create_memo(matter_id, text)`` — the body
#: is ``text`` (``operator/connectors/smokeball/smokeball_connector/server.py``).
TOOL_CREATE_MEMO = "mcp_smokeball_create_memo"

#: Server-side text extraction for one matter document. The result echoes the
#: ids; ``hermes-smd-establishment``'s read capture reads exactly these
#: spellings off the live payload, which is where they were observed.
TOOL_READ_DOCUMENT = "mcp_smokeball_read_document"

#: The matter's document listing. Its rows carry ``id`` (the same field
#: ``shared/matter_binding`` walks on connector listings).
TOOL_GET_FILES = "mcp_smokeball_get_files_on_matter"

#: The drafting lane's declared exit (``plugins/hermes-smd-drafting``). Args are
#: ``output_class`` / ``body`` / ``seam``; it returns a sentence, not an id.
TOOL_DELIVER_DRAFT = "smd_deliver_draft"

#: Draft authoring on either mail channel. The id lives in the result and is
#: extracted by the one extractor the send gate already uses.
DRAFT_TOOLS: frozenset[str] = frozenset(
    {
        "mcp_agentmail_create_draft",
        "mcp_agentmail_update_draft",
        "mcp_msgraph_mail_create_draft",
        "agentmail:create_draft",
    }
)

#: Body-argument spellings, in PRECEDENCE ORDER. One field is digested, not a
#: concatenation: ``text`` and ``html`` on a draft are two renderings of one
#: message, and hashing both together would produce a digest that matches
#: neither rendering and cannot be reproduced from what was delivered. The field
#: that was digested is recorded beside the digest so the value is checkable.
_BODY_ARG_KEYS: tuple[str, ...] = ("text", "body", "body_text", "html")

#: How many ids a listing may contribute. A listing takes ``limit`` up to 500 and
#: an audit row is not a place to put 500 GUIDs, so the row carries the first
#: page of them and SAYS when it stopped — a truncated list that admits it is
#: evidence; one that does not is a false negative waiting to be quoted.
_MAX_LISTED_IDS = 20


def body_sha256(text: object) -> str | None:
    """Hex ``sha256`` of a written body, or ``None`` when there is no body.

    An empty string returns ``None`` rather than the digest of "": a row must
    distinguish "wrote nothing" from "wrote something that hashes to e3b0…".
    """
    if not isinstance(text, str) or not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(result: Any) -> dict[str, Any] | None:
    """The tool result as a dict, or ``None``. Never raises, never guesses.

    Hermes hands ``post_tool_call`` a ``str`` that is usually JSON
    (``plugins/hermes-smd-audit/emit.py`` makes the same assumption when it reads
    an outcome), so a JSON object is parsed and anything else — prose, a list, a
    fenced wrap — yields ``None`` and the row simply carries no id.
    """
    obj: Any = result
    if isinstance(obj, str):
        stripped = obj.lstrip()
        if not stripped.startswith("{"):
            return None
        try:
            obj = json.loads(stripped)
        except (ValueError, TypeError):
            return None
    return obj if isinstance(obj, dict) else None


def _first_str(source: dict[str, Any] | None, *keys: str) -> str:
    if not source:
        return ""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _listed_ids(payload: dict[str, Any] | None) -> tuple[list[str], bool]:
    """Ids from a HATEOAS listing envelope. Returns ``(ids, truncated)``.

    ``{"value": [...]}`` is the connector's listing shape everywhere
    (``_slim_memos``, ``_attach_matter_refs_to_list``), so that is the only shape
    read; a listing under any other key contributes nothing rather than a guess.
    """
    if not payload:
        return ([], False)
    rows = payload.get("value")
    if not isinstance(rows, list):
        return ([], False)
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = _first_str(row, "id", "fileId", "file_id", "document_id")
        if row_id and row_id not in ids:
            ids.append(row_id)
        if len(ids) >= _MAX_LISTED_IDS:
            return (ids, len(rows) > _MAX_LISTED_IDS)
    return (ids, False)


def extract(tool_name: str, args: Any, result: Any) -> dict[str, Any]:
    """The object-identity metadata for one tool call. ``{}`` when there is none.

    Total by contract: this runs inside the audit writer on every tool call, and
    a malformed result must cost one enrichment, never the row. The row is the
    obligation; this is decoration on it.

    Precedence is RESULT over ARGS wherever both can answer, matching
    ``hermes-smd-establishment``'s read capture and for its reason: the result is
    what the source system echoed back, the args are what the model composed.
    """
    try:
        if not isinstance(tool_name, str) or not tool_name:
            return {}
        arguments = args if isinstance(args, dict) else {}
        payload = _payload(result)
        out: dict[str, Any] = {}

        if tool_name == TOOL_READ_DOCUMENT:
            document_id = _first_str(payload, "fileId", "file_id", "document_id") or _first_str(
                arguments, "file_id", "document_id"
            )
            if document_id:
                out["document_id"] = document_id

        elif tool_name == TOOL_GET_FILES:
            ids, truncated = _listed_ids(payload)
            if ids:
                out["document_ids"] = ids
                if truncated:
                    # Says so rather than reporting a short list as complete.
                    out["document_ids_truncated"] = True

        elif tool_name == TOOL_CREATE_MEMO:
            memo_id = _first_str(payload, "id", "memoId", "memo_id")
            if not memo_id:
                nested = payload.get("memo") if payload else None
                memo_id = _first_str(nested if isinstance(nested, dict) else None, "id", "memo_id")
            if memo_id:
                out["memo_id"] = memo_id
            digest = body_sha256(arguments.get("text"))
            if digest:
                out["written_body_sha256"] = digest
                out["written_body_field"] = "text"

        elif tool_name == TOOL_DELIVER_DRAFT:
            # No id exists to record: the tool returns an authorization sentence
            # and writes nothing itself. The digest IS the identity here, and it
            # is the same body the seam write is supposed to carry unchanged.
            digest = body_sha256(arguments.get("body"))
            if digest:
                out["written_body_sha256"] = digest
                out["written_body_field"] = "body"
            seam = _first_str(arguments, "seam")
            if seam:
                out["seam"] = seam

        elif tool_name in DRAFT_TOOLS:
            draft_id = extract_draft_id_from_result(result)
            if draft_id:
                out["draft_id"] = draft_id
            for key in _BODY_ARG_KEYS:
                digest = body_sha256(arguments.get(key))
                if digest:
                    out["written_body_sha256"] = digest
                    out["written_body_field"] = key
                    break

        return out
    except Exception:  # noqa: BLE001 — an enrichment must never cost the row
        logger.debug("object_identity: extraction failed for %r", tool_name, exc_info=True)
        return {}


__all__ = [
    "DRAFT_TOOLS",
    "TOOL_CREATE_MEMO",
    "TOOL_DELIVER_DRAFT",
    "TOOL_GET_FILES",
    "TOOL_READ_DOCUMENT",
    "body_sha256",
    "extract",
]
