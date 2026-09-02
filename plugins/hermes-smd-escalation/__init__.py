"""Escalation-ledger tools — the agent's mediated door to the escalation state.

WP-A (ss #1894) shipped the escalation ledger with an append procedure that ran
through ``execute_code``: an LLM-turn python snippet opening the broker socket.
The WP-D live proof (ss #1915) found that path dead on the pilot seat — the
``code_execution`` action class has no authored exposure, so the trust layer
refuses the snippet (fail-closed, ADR 0056, correctly) and the agent can never
write ``fired``/``chased``/``acked``/``handed_off``. Tokens suppress nothing,
attempts never count.

These two tools close the gap without widening any entitlement:

* ``escalation_append`` — one validated event through the broker's uid-gated
  ``escalation_event_append`` verb (the broker still owns ALL validation: schema,
  event vocabulary, and the acked-must-reference-a-prior-raise rule). The tool is
  a socket courier, not a second validator. Identity is supplied ONCE, on the
  ``derive_only`` call; the write presents the handle that call returned (ss
  #2304 — see the derive-handle block below).
* ``escalation_state`` — folds the read-only ledger twin
  (``/opt/data/audit/escalation-ledger.jsonl``; the hermes uid is in
  ``audit-readers``) into per-item state + ACK tokens via the vendored
  ``shared/escalation_ledger`` module (byte-identical twin of
  ``operator/workspace_broker/escalation_ledger.py``).

Both tools are mapped in ``shared/action_classes.py`` (``internal_write`` /
``read``) — an unmapped tool is REFUSED by design, which is exactly how the
execute_code gap surfaced.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any

from shared import escalation_ledger, inbound, provenance
from shared.audit_contract import sender_key
from shared.customer_config import CustomerConfig
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)


def _verified_acker(session_id: str) -> dict[str, str] | None:
    """Who actually sent the reply that is acking, or ``None`` (ss#2152).

    The commitment to the firm is that a confirmation is logged with the
    attorney's NAME. Today the ack records that somebody confirmed; this is the
    half that records WHO, and it is deliberately not an argument the model can
    pass.

    THREE SOURCES ARE REFUSED, each for its own reason. The model's own account
    of who replied is a fabricated fact on a legal matter. Smokeball
    ``createdBy`` is whoever clicked Allow at setup under
    ``auth_mode: authorization_code`` — wrong for every multi-attorney firm, and
    the exact trap #2152 was filed to avoid. An email display name is
    attacker-controlled on any inbound.

    The one source that is neither guessable nor spoofable is
    :data:`shared.inbound.SESSION_INBOUND_ORIGIN`, whose entries come only from
    Svix-verified From headers, bound to this turn. The NAME then comes from the
    firm's own authored ``users[]`` — never from the wire.

    SESSION-KEYED ONLY, on purpose. The relay has two recovery paths for a
    missing session binding (``find_for_recipient``, ``claim_unbound``) because a
    reply has to go somewhere. Attribution has the opposite failure economics: a
    reply sent to the right person's older thread is a nuisance, while "Dana
    confirmed" written when Chris replied is a false record on a legal matter. So
    a session with no bound origin attributes NOBODY.

    Returns ``{"name": ..., "key": ...}`` only when the sender is verified AND
    the firm authored a name for them. A verified sender the firm never authored
    yields ``None``: they may well be entitled to reply, but this seat has no
    sanctioned name for them and must not invent one.
    """
    if not session_id:
        return None
    origin = inbound.SESSION_INBOUND_ORIGIN.get(session_id)
    address = getattr(origin, "sender_address", "") if origin is not None else ""
    if not address:
        return None
    try:
        name = CustomerConfig.from_volume().authored_person_name(address)
    except Exception:  # noqa: BLE001 — an unreadable config attributes nobody
        logger.warning("hermes-smd-escalation: could not read authored users for attribution")
        return None
    if not name:
        return None
    key = sender_key(address)
    if not key:
        return None
    return {"name": name, "key": key}


def _resolved_session(kwargs: dict[str, Any]) -> str:
    """The session id the broker's CONFIRM_SEND_* row was keyed under.

    Same reconciliation `_smd_send_message` uses, and deliberately so: the broker
    refuses a raise it cannot join to a dispatch, so the raise and the send must
    agree on the id or a real delivery reads as no delivery. Core drops
    `session_id` at some tool fire sites and passes only `task_id` (overlay #141);
    `provenance.resolve_session` is the single place that reconciles them.

    Never raises. An unresolvable session degrades to the empty string, which the
    broker treats as the pre-plumbing caller shape and falls back to a bounded
    recent-dispatch window rather than refusing outright.
    """
    raw = str(kwargs.get("session_id") or "")
    try:
        return provenance.resolve_session(raw)
    except Exception:  # noqa: BLE001 — an audit join must not break an append
        return raw


_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_LEDGER_PATH_ENV = "SMD_ESCALATION_LEDGER_PATH"
_TIMEOUT_SECONDS = 10

STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}

# ---------------------------------------------------------------------------
# Derive handles (ss #2304)
# ---------------------------------------------------------------------------
# ``derive_only`` and the append used to be two calls with INDEPENDENTLY supplied
# identity components, and the append re-derived from whatever tuple it was
# handed that call. The ACK code a human was shown came from call 1; the ledger
# row was keyed off call 2; nothing bound them. A transposition (one row off in a
# batch of nine) wrote an item the quoted code does not name — the ack is refused,
# or it resolves to a DIFFERENT open item and silences the wrong deadline. Both
# calls are individually well-formed, so no validator could see it.
#
# The fix removes the second derivation. A ``derive_only`` call mints a
# single-use handle for the identity it just derived; the append presents the
# handle and supplies NO identity components at all. So the components of a
# ledger row are typed exactly once, ever, and "the code shown" and "the row
# written" are the same derivation by construction rather than by agreement.
#
# Supplying components to an append is a REFUSAL, not a silently ignored
# argument: the turn that would have transposed a component must see that its
# second call cannot name an item.
#
# Module-level state, deliberately. The handles never outlive the process (a
# restart between derive and append refuses the append, which writes nothing and
# re-fires next run — the safe direction, ss #1935). The map is capped and
# time-bounded; an evicted or expired handle refuses just as loudly as an unknown
# one. This is the issue's option 1 with the state in the tool rather than the
# broker: the broker keeps no per-turn state and gains none here.
_HANDLE_PREFIX = "EDH-"
_MAX_OPEN_HANDLES = 256
_HANDLE_TTL_SECONDS = 3600
_IDENTITY_ARGS = ("matter_id", "source_id", "label", "authored_date")

# The verification chase's per-matter HOLD sentinel source id. Import-free
# mirror of ``HOLD_SOURCE_ID`` in ss-console
# ``operator/skills/client-verification-tracker/pre_run.py`` — the cross-side
# contract: the turn derives a hold with ``source_id="__hold__"``, and THIS
# literal is how the plugin knows a ``resolved`` append releases a hold and
# must carry the determination that justifies it (ss #2402 Part 3). A test on
# each side pins the literal. (medical-records-chaser's hold sentinel is
# ``__mrc_hold__`` and is deliberately NOT listed: its holds are roster and
# receipt ambiguities, its pre_run computes no role snapshot, so a required
# 64-hex snapshot hash would make its holds unreleasable.)
HOLD_SOURCE_ID = "__hold__"

# The determination fields a hold release carries (validated structurally by
# the broker's ``validate_append``; REQUIRED here, at the door the model uses,
# because the broker sees only the hashed item_key and cannot know an item is
# a hold). Accepted residual: a direct-socket agent-uid caller could resolve a
# hold bare; the agent's tool path cannot.
_DETERMINATION_ARGS = ("resolution_note", "role_snapshot_sha256", "confirmed_via")

_handles: OrderedDict[str, dict[str, Any]] = OrderedDict()
_handles_lock = threading.Lock()


def _mint_handle(record: dict[str, Any]) -> str:
    handle = _HANDLE_PREFIX + secrets.token_hex(16)
    with _handles_lock:
        _handles[handle] = {**record, "minted_at": time.monotonic()}
        while len(_handles) > _MAX_OPEN_HANDLES:
            _handles.popitem(last=False)
    return handle


def _refuse_handle(handle: str) -> ValueError:
    return ValueError(
        f"append_handle {handle!r} was not issued by a derive_only call in this "
        "process, or it expired, or it was already used to write a row. Call "
        "escalation_append with derive_only=true for THIS item and present the "
        "handle it returns — never reuse a handle, and never retype the identity "
        "components on the append (ss #2304: the ACK code quoted to a human must "
        "be the code of the row that gets written)"
    )


def _peek_handle(handle: str) -> dict[str, Any]:
    with _handles_lock:
        record = _handles.get(handle)
        if record is None:
            raise _refuse_handle(handle)
        if time.monotonic() - float(record["minted_at"]) > _HANDLE_TTL_SECONDS:
            _handles.pop(handle, None)
            raise _refuse_handle(handle)
    return record


def _consume_handle(handle: str) -> None:
    with _handles_lock:
        _handles.pop(handle, None)


# The tool derives item_key + token from the IDENTITY COMPONENTS — the agent
# never hashes. The first live probe (ss #1915 WP-D) proved why: with item_key
# as an opaque string parameter, the model wrote a human-readable colon-joined
# composite; the broker accepted it, and the pre_run gate's sha256 join could
# never match it — the suppression state silently forked. The tuple here MUST
# be the exact tuple the skill's pre_run computes (see each skill's SKILL.md
# for its authored_date convention — the chase uses None, identity rides on
# the stable task source_id).
_APPEND_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": "The skill writing the event (e.g. deadline-miss-escalator).",
        },
        "matter_id": {
            **NULLABLE_STRING,
            "description": (
                "Smokeball matter id; null for seat-level sentinel items. "
                "DERIVE ONLY — an append presents append_handle instead."
            ),
        },
        "source_id": {
            **NULLABLE_STRING,
            "description": (
                "The item's STABLE Smokeball task/event id (the anti-collision "
                "identity field), copied VERBATIM off the record. null only for "
                "items with no stable id — those get no per-item token "
                "(blanket-ack-only group), as does an item whose matter_id came "
                "back as the 'unknown-matter' sentinel."
            ),
        },
        "label": {
            "type": "string",
            "description": (
                "The item's fixed label, exactly as the skill's pre_run computes "
                "it (e.g. 'client-verification', or the deadline label)."
            ),
        },
        "authored_date": {
            **NULLABLE_STRING,
            "description": (
                "The authored date component of the identity tuple (YYYY-MM-DD), "
                "or null when the skill's identity convention omits it (the "
                "verification chase uses null — a re-dated tracking task must "
                "not change identity). An ISO datetime is accepted and reduced to "
                "its date; anything else is REJECTED rather than hashed as typed, "
                "because an uncanonical date forks item identity. Never compute "
                "or reword this — copy the date off the record."
            ),
        },
        "event": {
            "type": "string",
            "enum": ["fired", "chased", "acked", "handed_off", "resolved"],
            "description": "The ledger event kind.",
        },
        "attempt": {
            "type": "integer",
            "minimum": 0,
            "description": "The attempt number this raise carries (0 for non-raises).",
        },
        "ack_token": {
            **NULLABLE_STRING,
            "description": (
                "For acked events ONLY: the ACK-XXXXXX code quoted in the reply. "
                "The tool resolves it to its item against the ledger's prior "
                "raises — pass this INSTEAD of the identity components."
            ),
        },
        "derive_only": {
            "type": "boolean",
            "description": (
                "When true, derive and return item_key + ACK token + "
                "append_handle from the identity components WITHOUT writing any "
                "event. Use this BEFORE composing an alert so the sent body "
                "quotes the real broker-derived codes (ss #1935: an alert "
                "composed before the ledger writes printed invented or "
                "wrong-item codes). Not valid with ack_token or append_handle. "
                "The send-then-record failure direction is preserved: derive, "
                "send, then append the raise with the handle."
            ),
        },
        "append_handle": {
            **NULLABLE_STRING,
            "description": (
                "REQUIRED on every non-acked append: the single-use EDH-xxxx "
                "handle returned by this item's derive_only call. The append "
                "carries NO identity components — it writes the item the derive "
                "identified, so the ACK code quoted to a human is necessarily the "
                "code of the row written (ss #2304). One handle writes one row; "
                "passing identity components alongside it is refused."
            ),
        },
        "resolution_note": {
            **NULLABLE_STRING,
            "description": (
                "Resolved-on-a-hold only (REQUIRED when releasing a chase hold: "
                "a resolved append whose derive named source_id __hold__): the "
                "determination text, 1..500 chars — what was determined and how "
                "it was verified. When confirmed_via is person, name who "
                "confirmed and when."
            ),
        },
        "role_snapshot_sha256": {
            **NULLABLE_STRING,
            "description": (
                "Resolved-on-a-hold only: the current role-snapshot hash, "
                "COPIED VERBATIM from the wake-line plan's "
                "current_role_snapshot_sha256 (64 lowercase hex). Never compute "
                "or retype it: a mis-copied hash fails safe (the next wake "
                "escalates the mismatch); a hand-built one does not."
            ),
        },
        "confirmed_via": {
            **NULLABLE_STRING,
            "description": (
                "Resolved-on-a-hold only: how the releasing fact was confirmed — "
                "exactly 'matter_record' (the live roles resolve cleanly) or "
                "'person' (a person named the answer; the note says who and "
                "when)."
            ),
        },
    },
    "required": ["skill", "event", "attempt"],
    "additionalProperties": False,
}

_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            **NULLABLE_STRING,
            "description": "Optional filter: only events written by this skill.",
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _broker_request(payload: dict[str, Any]) -> dict[str, Any]:
    socket_path = os.environ.get(_SOCKET_ENV, "")
    if not socket_path:
        raise RuntimeError(f"{_SOCKET_ENV} is unset; cannot reach the broker")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_TIMEOUT_SECONDS)
        sock.connect(socket_path)
        sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            chunk = sock.recv(65_536)
            if not chunk:
                break
            raw += chunk
    return json.loads(raw.decode("utf-8"))


def _resolve_token_identity(ack_token: str) -> tuple[str, str | None]:
    """Resolve an ACK token to (item_key, matter_id) via the ledger's prior
    raises. Raises ValueError when no prior raise carries the token — the same
    verdict the broker would return, surfaced before a malformed event ships.

    A raise from before the item-identity epoch (ss #2151) does NOT resolve. Its
    key came from the superseded derivation that hashed the model-composed label,
    so it names no live item: acking it would report a silenced alarm while the
    deadline kept firing. The epoch test is ``escalation_ledger``'s own, not a
    copy — one authority over one decision.
    """
    path = os.environ.get(_LEDGER_PATH_ENV) or escalation_ledger.DEFAULT_LEDGER_PATH
    stale_only = False
    for event in reversed(escalation_ledger.read_ledger(path)):
        if event.get("token") != ack_token or event.get("event") not in ("fired", "chased"):
            continue
        if escalation_ledger.is_pre_identity_epoch(event):
            stale_only = True
            continue
        return str(event.get("item_key")), event.get("matter_id")
    if stale_only:
        raise ValueError(
            f"token {ack_token!r} was issued before the item-identity fix (ss #2151) and no "
            "longer names a live item; the deadline will re-raise with a current code. Tell "
            "the reader the code is superseded — do not report it as acknowledged"
        )
    raise ValueError(
        f"no prior raise carries token {ack_token!r}; an alarm that never rang cannot be acked"
    )


def _escalation_append(args: dict[str, Any], **kwargs: Any) -> str:
    kind = str(args["event"])
    ack_token = args.get("ack_token")
    handle = args.get("append_handle")
    derive_only = bool(args.get("derive_only"))
    # Presence, not truthiness: `authored_date: null` is a deliberate value at
    # derive time (the verification chase's identity convention), so an append
    # that names the argument at all is retyping identity even when it is null.
    supplied_identity = [name for name in _IDENTITY_ARGS if name in args]
    supplied_determination = [
        name for name in _DETERMINATION_ARGS if args.get(name) not in (None, "")
    ]
    if derive_only and ack_token:
        raise ValueError(
            "derive_only resolves identity for a raise you have not written yet; "
            "an acked event already has its token — pass one or the other"
        )
    if derive_only and handle:
        raise ValueError(
            "append_handle is what a derive RETURNS, not something you pass to "
            "one; drop it to derive this item's identity, or drop derive_only to "
            "write the row the handle already names"
        )
    determination: dict[str, str] | None = None
    if ack_token:
        if kind != "acked":
            raise ValueError("ack_token is only valid for acked events")
        if handle or supplied_identity:
            extra = sorted(supplied_identity + (["append_handle"] if handle else []))
            raise ValueError(
                "an acked event takes its identity from the quoted ACK code and "
                f"nothing else; drop {extra}"
            )
        if supplied_determination:
            raise ValueError(
                "a determination is recorded only on the resolved append that "
                f"releases a chase hold; drop {sorted(supplied_determination)}"
            )
        key, matter_id = _resolve_token_identity(str(ack_token))
        token: str | None = str(ack_token)
    elif handle:
        # THE BINDING (ss #2304). The append derives nothing. It writes the item
        # the derive identified, so the ACK code quoted to a human cannot name a
        # different row than the one written — not "checked", unrepresentable.
        if supplied_identity:
            raise ValueError(
                "an append with append_handle must carry NO identity components — "
                f"drop {sorted(supplied_identity)}. The handle already names the "
                "item, and a retyped component is exactly the transposition that "
                "wrote a row the quoted ACK code did not name (ss #2304). If the "
                "components are the ones you meant, derive again and present the "
                "handle THAT call returns"
            )
        record = _peek_handle(str(handle))
        if record["skill"] != str(args["skill"]):
            raise ValueError(
                f"append_handle was derived for skill {record['skill']!r} but this "
                f"append says {str(args['skill'])!r}; a row filed under the wrong "
                "skill is invisible to that skill's state fold"
            )
        if record["event"] != kind:
            raise ValueError(
                f"append_handle was derived for a {record['event']!r} event but "
                f"this append says {kind!r}; derive the event you intend to write"
            )
        key = str(record["item_key"])
        token = record["token"]
        matter_id = record["matter_id"]
        # THE HOLD-RELEASE SEAM (ss #2402 Part 3). A resolved append whose
        # derive named the chase-hold sentinel releases a hold, and releasing a
        # hold RECORDS the determination that justifies it — a control in code
        # at the door the model uses, not prose. The broker validates the
        # payload's shape; only this seam can see that the item IS a hold
        # (the broker receives the hashed item_key alone).
        if record.get("source_id") == HOLD_SOURCE_ID and kind == "resolved":
            missing = [name for name in _DETERMINATION_ARGS if not args.get(name)]
            if missing:
                raise ValueError(
                    "releasing a hold records the determination that justifies "
                    f"it; this append is missing {missing}. Supply "
                    "resolution_note (what was determined and how it was "
                    "verified), role_snapshot_sha256 (copied verbatim from the "
                    "wake-line plan's current_role_snapshot_sha256; if that "
                    "value was null this run, the snapshot pull failed and the "
                    "release waits for a run where it succeeds — re-surface the "
                    "hold instead), and confirmed_via (matter_record or person)"
                )
            determination = {
                "note": str(args["resolution_note"]),
                "role_snapshot_sha256": str(args["role_snapshot_sha256"]),
                "confirmed_via": str(args["confirmed_via"]),
            }
        elif supplied_determination:
            raise ValueError(
                "a determination is recorded only on the resolved append that "
                "releases a chase hold (a derive whose source_id is the hold "
                f"sentinel); drop {sorted(supplied_determination)}"
            )
    elif derive_only:
        # Derived, never model-authored: the sha256 identity key and its ACK
        # token come from the same vendored helpers the pre_run gates use, so
        # the join can never fork on a hand-typed key (the first live probe's
        # failure mode: a colon-joined composite the sha256 join never matched).
        if supplied_determination:
            raise ValueError(
                "a determination is authored on the resolved append that "
                "releases the hold, not on the derive; drop "
                f"{sorted(supplied_determination)} here and supply them with "
                "the append_handle"
            )
        label = args.get("label")
        if not label:
            raise ValueError("label is required (with matter_id/source_id) unless acking by token")
        matter_id = None if args.get("matter_id") is None else str(args["matter_id"])
        source_id = None if args.get("source_id") is None else str(args["source_id"])
        authored_date = (
            None if args.get("authored_date") in (None, "") else str(args["authored_date"])
        )
        key = escalation_ledger.item_key(matter_id or "", source_id, str(label), authored_date)
        # A per-item token only for items whose identity tuple is entirely READ
        # values (ss #2289 fix 2). The old test was `source_id is not None`, which
        # handed a code to an item whose matter came back as the "unknown-matter"
        # sentinel — that key moves the moment the matter resolves, so the code
        # printed in today's alert names nothing tomorrow. Idless and
        # sentinel-keyed items are blanket-ack only.
        token = (
            escalation_ledger.token_for(key)
            if escalation_ledger.has_stable_identity(source_id, matter_id)
            else None
        )
    else:
        raise ValueError(
            "a non-acked append requires append_handle: call escalation_append "
            "with derive_only=true for this item, quote the ACK token it returns, "
            "then present its append_handle here. Identity components are accepted "
            "on the derive ONLY — re-supplying them on the append is how the code "
            "shown to a human came to name a different row than the one written "
            "(ss #2304)"
        )
    if derive_only:
        # Identity only — NOTHING is written. The turn quotes these codes in the
        # alert it is about to send, then appends the raise after a successful
        # send. A failed send therefore still records nothing (the item re-fires
        # next run: annoying, never dangerous — ss #1935).
        #
        # The handle is the receipt for THIS derivation. It is the only thing the
        # append will accept, and it is single-use: one derive, one row.
        return json.dumps(
            {
                "ok": True,
                "derive_only": True,
                "written": False,
                "item_key": key,
                "token": token,
                "append_handle": _mint_handle(
                    {
                        "item_key": key,
                        "token": token,
                        "matter_id": matter_id,
                        "skill": str(args["skill"]),
                        "event": kind,
                        # Kept so the append can recognize a hold release
                        # (source_id == HOLD_SOURCE_ID) and demand the
                        # determination — the identity is still supplied once;
                        # this is the derive's own record, not a retype.
                        "source_id": source_id,
                    }
                ),
            },
            ensure_ascii=False,
        )
    event = {
        "v": escalation_ledger.SCHEMA_VERSION,
        "ts": None,  # broker stamps ts/id server-side; the agent cannot backdate
        "skill": str(args["skill"]),
        "matter_id": matter_id,
        "item_key": key,
        "event": kind,
        "attempt": int(args["attempt"]),
        "token": token,
        # ss-console: the broker refuses a `fired`/`chased` it did not witness
        # dispatching to a person, and this is the key it joins the raise to the
        # send on. Taken from the RUNTIME kwargs, never from `args`:
        # `_APPEND_SCHEMA` sets `additionalProperties: false` precisely so the
        # model cannot name this field, and a model-supplied session pointing at
        # some other turn where a send did happen would defeat the control it is
        # here to feed. Resolved through `provenance` for the same reason
        # `_smd_send_message` does — core drops `session_id` at some tool fire
        # sites (overlay #141), and the send row this must match was keyed by the
        # resolver's answer, so reading raw kwargs would join on a different id
        # and refuse a raise whose send is sitting right there.
        "session_id": _resolved_session(kwargs),
    }
    if kind == "acked":
        # WHO confirmed (ss#2152). Same posture as `session_id` directly above
        # and for the same reason: `_APPEND_SCHEMA` sets
        # `additionalProperties: false`, so the model cannot name this field, and
        # the value is resolved from the turn's Svix-verified inbound origin plus
        # the firm's own authored users[] — never from anything the model or the
        # wire said.
        #
        # ABSENT MEANS UNATTRIBUTED, and that is a state the record is allowed to
        # be in. `acked_by` present says a named person confirmed; absent says
        # somebody quoting a valid code did, and the seat could not verify who.
        # Writing a plausible name into the second case is the whole failure this
        # issue exists to prevent, so there is no default and no fallback.
        acker = _verified_acker(event["session_id"])
        if acker is not None:
            event["acked_by"] = acker
    if determination is not None:
        # The broker's validate_append enforces the payload's shape (note
        # length, 64-hex snapshot, confirmed_via enum) and refuses it on any
        # non-resolved kind; this seam supplied the requirement.
        event["determination"] = determination
    response = _broker_request({"action": "escalation_event_append", "event": event})
    # The broker's verdict (ok/id or a validation error) goes back verbatim —
    # a rejected acked-with-no-prior-raise must stay visible to the turn. Echo
    # the derived identity so the turn can quote the token in the alert body.
    if isinstance(response, dict):
        response = {**response, "item_key": key, "token": token}
    # Consumed only once a row exists. A broker refusal leaves the handle alive so
    # the turn can retry the SAME identity; a written row retires it, so one
    # derive can never become two rows.
    if handle and isinstance(response, dict) and response.get("ok"):
        _consume_handle(str(handle))
    return json.dumps(response, ensure_ascii=False)


def _state_to_jsonable(state: Any) -> dict[str, Any]:
    row = asdict(state)
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            row[key] = value.isoformat()
    return row


def _escalation_state(args: dict[str, Any], **_: Any) -> str:
    path = os.environ.get(_LEDGER_PATH_ENV) or escalation_ledger.DEFAULT_LEDGER_PATH
    events = escalation_ledger.read_ledger(path)
    skill = args.get("skill")
    if skill:
        events = [e for e in events if e.get("skill") == skill]
    states = escalation_ledger.derive_state(events)
    items = {}
    for key, state in states.items():
        row = _state_to_jsonable(state)
        # ss #2289 fix 3: NO synthesized token. This used to fall back to
        # token_for(key) whenever the ledger rows carried none — which is exactly
        # the blanket-ack-only items, the ones _resolve_token_identity refuses by
        # design (it matches a token stored on a prior raise, and these have no
        # such row). So the turn was handed an ACK code that structurally could
        # not be acked, and quoting it in an alert tells a human to type
        # something that will come back "an alarm that never rang cannot be
        # acked". Making it resolvable was the other option and it is the wrong
        # one: the key of an idless item is (matter, "", date), so one code would
        # silence every same-day item on that matter — the over-ack the
        # blanket-only group exists to prevent.
        #
        # token is therefore whatever the ledger actually recorded, or null.
        # `ackable` says which, so a turn composing an alert routes the item to
        # the blanket-ack group instead of inventing a code for it.
        row["token"] = row.get("token") or None
        row["ackable"] = row["token"] is not None
        items[key] = row
    return json.dumps(
        {"event_count": len(events), "item_count": len(items), "items": items},
        ensure_ascii=False,
    )


TOOLS: dict[str, tuple[str, dict[str, Any], Any]] = {
    "escalation_append": (
        "Append one escalation-ledger event (fired/chased/acked/handed_off/resolved) "
        "through the validated broker seam. The broker stamps ts/id and rejects an "
        "acked event whose token has no prior raise. Two steps, always: "
        "derive_only=true with the identity components returns item_key + ACK token "
        "+ a single-use append_handle and writes NOTHING; the write then presents "
        "append_handle and NO components, so the code quoted to a human is the code "
        "of the row written. acked events identify by ack_token instead. Releasing "
        "a chase hold (a resolved whose derive named source_id __hold__) REQUIRES "
        "the determination fields (resolution_note, role_snapshot_sha256, "
        "confirmed_via); a bare hold release is refused.",
        _APPEND_SCHEMA,
        _escalation_append,
    ),
    "escalation_state": (
        "Read the escalation ledger and return per-item state (attempts, last raise, "
        "acked/handed_off/resolved, ACK token, and any recorded hold-release "
        "determination), optionally filtered by skill.",
        _STATE_SCHEMA,
        _escalation_state,
    ),
}


def register(ctx: Any) -> None:
    """Register the escalation-ledger tools. Both require the broker socket env
    (the state read wants the same Machine layout even though it reads a file)."""
    for name, (description, schema, handler) in TOOLS.items():
        register_wrapped_tool(
            ctx,
            name=name,
            toolset="escalation",
            schema=schema,
            handler=handler,
            requires_env=[_SOCKET_ENV],
            description=description,
            emoji="",
        )
    logger.info("hermes-smd-escalation registered %d ledger tools", len(TOOLS))
