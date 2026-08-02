"""Conversational establishment — an Operator admin teaches the firm's voice/shape.

WHAT THIS CLOSES (ss ADR 0085, ss-console #2160/#2161/#2162). ADR 0083 defined
authoring as plain speech, and the first implementation wave quietly inverted it
into a portal form. This plugin restores the model: an **Operator admin**
(``scope.admins``, the third instance of the authored allow-list shape) points
the Operator at a named set of the firm's own documents and says "establish the
voice" / "establish this document's shape". The Operator reads the documents in
place, stages them through the broker, drafts the spec against a root-computed
profile, and submits — and the ROOT-side intake daemon (``establish_intake/``)
runs the distillation compilers as write gates before anything is installed.
Effect is immediate on completion (ADR 0085 §3): the restriction to admins, the
server-side provenance verification, and the compiler gates are the safety — a
second approval beat is not.

THE TRUST TOPOLOGY, in one paragraph. The agent uid can reach exactly three
wrapped tools, each one broker-verb round trip over the unix socket. The broker
(its own uid) validates, rebuilds every field from a bounded set, and writes
into a spool the agent cannot open. The intake daemon (root, the only holder of
R2 creds) verifies the spool files' uid and hashes, runs the compiler gates as
subprocesses, and only then merge-writes the vault object the fail-static
``spec_applier`` polls. The agent never touches the spool, R2, or the spec tree;
the broker never holds R2 creds; root's input surface is broker-authored files
only. This plugin is the thinnest layer of that stack on purpose: NO validation
beyond shape lives here, because the broker's verdict is the one that counts and
a second, drifting schema in the untrusted layer helps nobody.

WHY THERE IS **NO SESSION-TAINT GATE** HERE, unlike hermes-smd-corrections.
Captain decision, 2026-08-02 (same-breath establishment; recorded in the intake
design's amendment block, point 1): the establishment turn READS the firm's own
documents through a connector, so the very turn that must submit is a turn that
ingested external content — a taint gate would refuse every legitimate
establishment. The threat model was presented and rated acceptable: the admin
allow-list is the gate, the broker verifies provenance server-side, and the
compiler gates (leak check above all) bound what a hostile document could smuggle
into a spec. Do not "fix" this by adding a taint check; that is the one decision
this file is built on.

WHY THE ADMIN CHECK IS A HOOK rather than a check inside the handlers: Hermes
hands a tool handler only ``task_id``/``user_task`` (``model_tools.py``
dispatch) — never ``sender_id`` or ``session_id``. ``pre_llm_call`` is the one
hook that sees ``sender_id``, so admin classification is resolved there against
the LIVE ``scope.admins`` (a config edit applies on the next message, ADR 0044)
and stashed per session; ``pre_tool_call`` — the one hook that can block — reads
the stash. Last-writer-wins on sender-attributed turns: a mid-session non-admin
message downgrades the stash, which is the fail-safe direction. Same split as
``hermes-smd-corrections`` (the exemplar this mirrors).

WHY THERE IS A NUDGE (admin turns only): a tool the model is never told to
reach for is a tool that does not exist (overlay #170 — ``record_peer_preference``
shipped registered and got ZERO rows fleet-wide). Gated on the stash saying this
turn's sender is an admin, so it never advertises what ``pre_tool_call`` would
refuse.

CORRECTION PROMOTION IS A USE OF THIS PATH, NOT A VERB. An admin saying "apply
Sarah's correction" flows the property edit through ``establish_submit`` with
the captured correction cited in ``source_ref`` — the admin (via the agent's
draft) authors the spec bytes, and NOTHING in this plugin or the intake turns a
captured statement into spec bytes automatically. The witness-never-author
invariant moves only for admin-instructed establishment (ADR 0085 §4);
correction capture itself is unchanged for everyone else.

MAILBOX-POSSESSION CEREMONY ON AGENTMAIL CUSTODY (ss ADR 0085 §5, ss#2164).
Admin authority is an email identity, and on an AgentMail-custody seat the
``From`` header is a claim, not proof: the #2164 probe found no per-message
SPF/DKIM/DMARC verdict a seat can require (WEAK). So on those seats — and only
those — an admin's FIRST establishment is withheld behind a one-time ceremony:
the Operator emails a challenge code to the ROSTERED admin address (the
authored ``scope.admins`` entry, never a claimed From display or Reply-To), and
the admin's reply containing the code confirms mailbox possession and
permanently unlocks establishment for that entry. State + rules live in
``shared/admin_possession.py`` (durable SQLite, the exposure-override
precedent); this plugin contributes the three seams:

* the ``pre_tool_call`` gate withholds ``establish_*`` for an unconfirmed admin
  and instructs the ONE challenge send — through the model's own email tools at
  the seat's authored posture, the same seam every system-initiated mail (the
  escalation and report paths) uses, never a raw send that bypasses the trust
  gate;
* a recipient lock in the same hook: any outbound-shaped tool call carrying a
  live challenge code may only ship to exactly the rostered admin address, so a
  prompt-injected "forward that code" or a Reply-To hijack is refused
  mechanically rather than by model goodwill;
* ``pre_llm_call`` detects the confirming reply (rostered admin sender + the
  live code in the message) at the same seam where sender attribution already
  exists.

CHANNEL SCOPING. The ceremony binds only where the authored Email connector is
AgentMail custody. A ``msgraph`` seat is exempt: the instruction arrives
intra-tenant and the firm's own Exchange Online Protection authenticates the
sender before the Operator ever reads it (ADR 0085 §5). A seat with no enabled
Email connector is exempt too — with no mail channel, an admin-classed turn
arrived on a channel that carries its own authentication. The console
``/mcp/turn`` channel is exempt structurally: its dispatch body
(``webhook_gate._drive_agent_turn``) carries no sender identity, so an MCP turn
can never admin-classify against ``scope.admins`` and never reaches this gate;
if connector sender attribution is ever added (ADR 0057 grants on the seat),
that change must carry a channel marker to keep the exemption, because this
gate fails toward requiring the ceremony. Any OTHER mail adapter gets the
ceremony until its inbound-authentication posture is probed — unknown custody
is spoofable until proven otherwise.

THE PER-PERSON LAYER (ADR 0085 §6, ss#2067): SAME PRIMITIVE, NARROWER
PREDICATE. Any rostered person customizes voice/format for THEIR OWN work by
telling the Operator — no admin needed, because the person's own rostered
identity is the authority over their own preferences. The mechanism is the
same ``pre_llm_call`` sender stash the admin gate reads, evaluated by the SAME
``pre_tool_call`` hook under a second predicate: a ``scope: "person"`` submit
passes only when its ``person`` argument EXACTLY equals the stashed attributed
sender. The hook cannot rewrite tool arguments (docs/hook-surface.md — only a
block directive is interpreted), so the "stamp" is enforced as an exact-match
refusal: any submit naming a subject other than the person speaking is
blocked, never repaired, which pins the wire value to the attribution just as
a stamp would. Admin identity does NOT satisfy the person predicate for
someone else's preferences — the person's voice is theirs (an admin may of
course establish their own). Personal preferences REFINE the firm floor; the
firm spec gate's pass conditions are untouched by any of this.

THE POSSESSION CEREMONY BINDS PERSON SCOPE TOO, on the same custody boundary:
sender==subject is only as strong as sender attribution, and on AgentMail
custody a forged ``From`` naming a rostered person could author that person's
preference artifact — which the pointer then feeds into every future draft
produced for them. Same attack as the admin spoof, smaller blast radius, same
fix: the person's FIRST person-scoped establishment on a spoofable-custody
seat is withheld until their mailbox is confirmed once (person table in
``shared/admin_possession.py``; re-arm rule is ROSTER membership rather than
``scope.admins``). A mailbox already confirmed through the ADMIN ceremony
cross-accepts — possession is a fact about a mailbox, not a role. Tenant-
custody and no-mail seats are exempt exactly as for admins. ``establish_status``
is exempt from BOTH ceremonies: it writes nothing, and its results are only
reachable through a broker-minted secret run id from a submit that already
passed the gates.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from shared import admin_possession
from shared.action_classes import ActionClass, BannedToolError, classify_tool
from shared.customer_config import CustomerConfig
from shared.outbound_recipient import DRAFT_RECORD_TOOLS, extract_to_recipients
from shared.tool_registration import register_wrapped_tool

logger = logging.getLogger(__name__)

_SOCKET_ENV = "SMD_WORKSPACE_BROKER_SOCKET"
_TIMEOUT_SECONDS = 15

TOOL_STAGE = "establish_stage_document"
TOOL_SUBMIT = "establish_submit"
TOOL_STATUS = "establish_status"
ESTABLISH_TOOLS = (TOOL_STAGE, TOOL_SUBMIT, TOOL_STATUS)

#: Mirrors the vault schema + broker vocabulary. Declared here only so the model
#: sees a closed enum; the broker and the intake both re-validate it.
SPEC_PROPERTIES = ("voice", "format")

#: Session-keyed admin classification, written by ``on_pre_llm_call`` (the only
#: hook that sees ``sender_id``) and read by ``on_pre_tool_call`` (the only hook
#: that can block). Last-writer-wins per session on sender-attributed turns.
#: In-process state, same lifetime as the gateway singleton the hooks live in —
#: a restart clears it, and an unclassified session fails CLOSED at the gate.
_ADMIN_STASH: dict[str, dict[str, Any]] = {}

_STAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "staging_id": {
            "type": ["string", "null"],
            "description": (
                "The staging set to add this document to. Omit (or null) on the "
                "first document — the broker opens a new set and returns its id; "
                "pass that id for every further document of the same establishment."
            ),
        },
        "name": {
            "type": "string",
            "description": (
                "The document's name as the firm knows it (e.g. the file name). "
                "It labels the document in gate results and demotion reports, so "
                "use the real name, not a paraphrase."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "The document's full text, exactly as read from the source "
                "system. Do not summarize, trim, or clean it: the compilers "
                "derive the firm's voice from what the firm actually wrote."
            ),
        },
        "source": {
            "type": "object",
            "description": "Where this document was read from, for provenance.",
            "properties": {
                "connector": {
                    "type": "string",
                    "description": "The connector it was read through (e.g. 'smokeball').",
                },
                "document_id": {
                    "type": "string",
                    "description": "The source system's id for the document.",
                },
                "matter_id": {
                    "type": ["string", "null"],
                    "description": "The matter/case id it belongs to, if any.",
                },
            },
            "required": ["connector", "document_id"],
        },
    },
    "required": ["name", "text", "source"],
    "additionalProperties": False,
}

_SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {
            "type": ["string", "null"],
            "enum": ["firm", "person", None],
            "description": (
                "'firm' (the default) establishes firm-level voice/shape from a "
                "staged document set — Operator admins only. 'person' records the "
                "SPEAKER's own personal preferences for their own work — no "
                "staging, no corpus; only the person themselves may do it."
            ),
        },
        "person": {
            "type": ["string", "null"],
            "description": (
                "Required for scope 'person': the email address of the person "
                "whose preferences these are. It MUST be exactly the address of "
                "the person speaking to you — the gate refuses any other value."
            ),
        },
        "staging_id": {
            "type": ["string", "null"],
            "description": (
                "The staging set holding the documents this run works from. "
                "Required for firm-scope runs; omit for scope 'person'."
            ),
        },
        "phase": {
            "type": "string",
            "enum": ["analyze", "install"],
            "description": (
                "'analyze' runs the profiler over the staged documents and "
                "returns the profile card plus the fixed strings you MAY use "
                "verbatim — draft the spec against that card. 'install' submits "
                "your drafted spec for the compiler gates and, on pass, installs "
                "it as the class's property, effective immediately."
            ),
        },
        "output_class": {
            "type": "string",
            "description": (
                "The output class the spec belongs to, as the seat's slug "
                "(e.g. 'work_product'). Required for phase 'install'."
            ),
        },
        "property": {
            "type": "string",
            "enum": list(SPEC_PROPERTIES),
            "description": (
                "'voice' when establishing how outputs of this class SOUND; "
                "'format' when establishing their SHAPE. Required for 'install'."
            ),
        },
        "spec_body": {
            "type": "string",
            "description": (
                "The spec you drafted, as prose for the drafting model. Carries "
                "NO client text beyond the approved fixed strings and NO digits "
                "outside {{profile.*}} tokens — the gates refuse both. Required "
                "for 'install'."
            ),
        },
        "assertions": {
            "type": ["object", "null"],
            "description": (
                "Machine-checkable shape rules, installed beside the body for "
                "the runtime format checker. Put selftest-checkable rules under "
                "'rules' ([{id, kind, tier, ...}]); with zero rules the selftest "
                "is recorded NOT RUN, never passed."
            ),
        },
        "corpus_manifest": {
            "type": ["array", "null"],
            "description": (
                "Exactly the staged documents this spec was derived from: "
                "[{doc_id, sha256}] as returned by staging. Binds the spec to "
                "the corpus the gates check it against. Required for 'install'."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "sha256": {"type": "string"},
                },
                "required": ["doc_id", "sha256"],
            },
        },
        "instructed_by": {
            "type": ["string", "null"],
            "description": (
                "Who instructed this establishment, as best you know. Provenance "
                "for the audit trail, never authorization."
            ),
        },
        "source_ref": {
            "type": ["string", "null"],
            "description": (
                "Where the instruction was given (message id, thread). When an "
                "admin asks you to APPLY a captured correction, cite that "
                "correction here — promotion is this same submit, with the "
                "correction as provenance."
            ),
        },
    },
    "required": ["phase"],
    "additionalProperties": False,
}

_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "The run to check, as returned by establish_submit.",
        },
    },
    "required": ["run_id"],
    "additionalProperties": False,
}

_STAGE_DESCRIPTION = (
    "Stage one firm document for an establishment run, when one of the firm's "
    "Operator admins has instructed you to establish or update the firm's voice "
    "or an output shape from named content. Stage each named document with its "
    "full text, then call establish_submit. Only works on an admin's instruction."
)

_SUBMIT_DESCRIPTION = (
    "Run an establishment phase over a staged document set: 'analyze' to get the "
    "profile you draft the spec against, 'install' to submit your drafted spec "
    "through the compiler gates and install it on pass. Returns a run_id to poll "
    "with establish_status. The result names any auto-demoted rules and the "
    "documents that violated them — relay those plainly in your reply."
)

_STATUS_DESCRIPTION = (
    "Check an establishment run. Returns the run's result once: gate verdicts, "
    "demotions, and whether the spec is installed or still converging. Poll "
    "after establish_submit; report the outcome honestly, including rejections."
)

#: One line, appended to ADMIN turns only — the same condition the gate itself
#: requires, so the nudge never advertises what ``on_pre_tool_call`` refuses.
#: (The PERSON nudge below rides every attributed turn, because the person
#: predicate is one any attributed sender can satisfy for themselves —
#: overlay #170 is why nudges exist at all.)
_NUDGE = (
    "This person is one of the firm's Operator admins: if they instruct you to "
    "establish or update the firm's voice or an output's shape from named "
    "documents — or to apply a captured correction — read the documents, stage "
    f"them with {TOOL_STAGE}, and run {TOOL_SUBMIT}; changes take effect on "
    "completion and your reply must name any demoted rules."
)

_REFUSAL_MESSAGE = (
    "Refused: only the firm's Operator admins can establish firm-level "
    "voice/shape (scope.admins, ss ADR 0085 §2). Tell the person who asked that "
    "an Operator admin can apply it, and where their statement described how a "
    "kind of output should look or sound, record it with correction_capture so "
    "an admin can review and apply it."
)

_PERSON_NUDGE = (
    "If this person tells you how THEIR OWN work should sound or be shaped "
    f"(their drafts, their documents), record it with {TOOL_SUBMIT} — scope "
    "'person', person set to exactly their address. It takes effect for their "
    "work immediately and never changes the firm's standards."
)

_PERSON_MISMATCH_MESSAGE = (
    "Refused: personal preferences belong to the person themselves (ss ADR "
    "0085 §6). A person-scoped establishment must name exactly the address of "
    "the person speaking — you may not record preferences for anyone else, and "
    "being an Operator admin does not change that. If they described how a "
    "FIRM output should look or sound, that is firm-level establishment "
    "(admins) or a correction_capture."
)

# ---------------------------------------------------------------------------
# Mailbox-possession ceremony (ss ADR 0085 §5, ss#2164) — see module docstring
# ---------------------------------------------------------------------------

#: Mirrors ``shared.msgraph_poller._EMAIL_CAPABILITY`` — the connectors key the
#: seat's mail rides on.
_EMAIL_CAPABILITY = "Email"

#: The one probed-WEAK custody (ss#2164): inbound From headers unverifiable.
_AGENTMAIL_ADAPTER = "agentmail"

#: Custodies exempt from the ceremony because the client tenant authenticates
#: the sender before the Operator reads the message (ADR 0085 §5). Mirrors
#: ``shared.msgraph_poller.SOURCE`` — the single runtime-wired tenant-mail
#: adapter name. Future adapters join this set only after their
#: inbound-authentication posture is probed and recorded.
_TENANT_AUTH_ADAPTERS: frozenset[str] = frozenset({"msgraph"})

#: Generic draft-authoring capability names, added to the recipient lock beside
#: the live ``mcp_*`` names in ``DRAFT_RECORD_TOOLS`` — a draft can carry the
#: challenge code into a later ``send_draft`` whose args no longer show it.
_EXTRA_DRAFT_TOOLS: frozenset[str] = frozenset({"email_create_draft", "email_update_draft"})

_CONFIG_UNREADABLE_MESSAGE = (
    "Refused: the customer config could not be read, so the mail-custody trust "
    "boundary for this establishment cannot be determined (fail closed). Tell "
    "the admin the change is on hold until the seat's configuration is "
    "repaired, and raise it through the seat's escalation path."
)

_CHALLENGE_ISSUED_MESSAGE = (
    "Withheld: this seat's mail runs on AgentMail custody, where an inbound "
    "From header is not authenticated proof of identity (ss ADR 0085 §5; the "
    "ss#2164 probe found no per-message SPF/DKIM/DMARC verdict to require). "
    "Before this admin's first firm-level establishment, their mailbox must be "
    "confirmed once. Do this now, in this turn: send a FRESH email (never a "
    "reply, never a Reply-To) to exactly {admin}, the address authored on the "
    "firm's admin list, telling them a firm-level change was requested in "
    "their name and asking them to reply keeping this confirmation code in "
    "the reply: {nonce} . Then tell the requester the confirmation email has "
    "been sent and the change proceeds once it is answered. The code must "
    "never go to any other address or channel. This happens once per admin; "
    "after their reply, establishment proceeds immediately."
)

_CHALLENGE_PENDING_MESSAGE = (
    "Withheld: a mailbox-possession confirmation for {admin} is already "
    "outstanding and must be answered before firm-level establishment "
    "proceeds. If and only if the confirmation email has not yet been sent, "
    "send it now to exactly {admin} with the confirmation code {nonce} ; "
    "never send a duplicate, and never send the code to any other address. "
    "Tell the requester the change is waiting on that reply."
)

_NONCE_CONTAINMENT_MESSAGE = (
    "Blocked: this call carries a live mailbox-possession confirmation code, "
    "which may only ever be emailed directly to {admin} (the authored admin "
    "address). It cannot ride a reply, a forward, or a send to any other "
    "recipient. Compose a fresh send to exactly that address, or leave the "
    "code out."
)

_POSSESSION_CONFIRMED_NOTE = (
    "This message contained the mailbox-possession confirmation code for "
    "{sender}: their mailbox is now confirmed and firm-level establishment is "
    "unlocked for them (a one-time ceremony; it re-arms only if the admin "
    "list changes for their entry). Acknowledge the confirmation in your "
    "reply, and if they asked for an establishment earlier, proceed with it "
    "now."
)

_PERSON_CHALLENGE_ISSUED_MESSAGE = (
    "Withheld: this seat's mail runs on AgentMail custody, where an inbound "
    "From header is not authenticated proof of identity (ss ADR 0085 §5/§6). "
    "Before this person's first personal-preference change, their mailbox must "
    "be confirmed once. Do this now, in this turn: send a FRESH email (never a "
    "reply, never a Reply-To) to exactly {person}, telling them a change to "
    "their personal preferences was requested in their name and asking them to "
    "reply keeping this confirmation code in the reply: {nonce} . Then tell "
    "the requester the confirmation email has been sent and their preferences "
    "apply once it is answered. The code must never go to any other address "
    "or channel. This happens once per person; after their reply, it proceeds "
    "immediately."
)

_PERSON_CHALLENGE_PENDING_MESSAGE = (
    "Withheld: a mailbox-possession confirmation for {person} is already "
    "outstanding and must be answered before their personal preferences can "
    "change. If and only if the confirmation email has not yet been sent, "
    "send it now to exactly {person} with the confirmation code {nonce} ; "
    "never send a duplicate, and never send the code to any other address. "
    "Tell the requester the change is waiting on that reply."
)

_PERSON_POSSESSION_CONFIRMED_NOTE = (
    "This message contained the mailbox-possession confirmation code for "
    "{sender}: their mailbox is now confirmed and personal-preference "
    "establishment is unlocked for them (a one-time ceremony; it re-arms only "
    "if they leave the roster). Acknowledge the confirmation in your reply, "
    "and if they stated preferences earlier, record them now."
)


def _broker_request(payload: dict[str, Any]) -> dict[str, Any]:
    """One request/response over the broker's unix socket, verdict verbatim.

    Deliberately NOT ``shared.workspace_broker.request``: that helper raises on
    ``ok != True``, and a broker refusal here (ceiling, unknown staging set,
    expired TTL) must reach the model as the structured reply it is, so the
    agent can re-stage or tell the admin what was refused rather than swallowing
    the refusal into an exception string. Same posture as hermes-smd-corrections.
    """
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


def _stage(args: dict[str, Any], **_: Any) -> str:
    """Stage one document. The broker computes the hash server-side (never
    trusted from the wire), safe-slugs the name, and enforces the ceilings;
    its verdict is returned unchanged."""
    response = _broker_request(
        {
            "action": TOOL_STAGE,
            "staging_id": args.get("staging_id"),
            "name": args.get("name"),
            "text": args.get("text"),
            "source": args.get("source"),
        }
    )
    return json.dumps(response, ensure_ascii=False)


def _submit(args: dict[str, Any], **_: Any) -> str:
    """Submit an analyze/install run. The broker rebuilds the submission from
    this bounded field set — nothing else on the wire reaches the spool."""
    response = _broker_request(
        {
            "action": TOOL_SUBMIT,
            "scope": args.get("scope"),
            "person": args.get("person"),
            "staging_id": args.get("staging_id"),
            "phase": args.get("phase"),
            "output_class": args.get("output_class"),
            "property": args.get("property"),
            "spec_body": args.get("spec_body"),
            "assertions": args.get("assertions"),
            "corpus_manifest": args.get("corpus_manifest"),
            "instructed_by": args.get("instructed_by"),
            "source_ref": args.get("source_ref"),
        }
    )
    return json.dumps(response, ensure_ascii=False)


def _status(args: dict[str, Any], **_: Any) -> str:
    """Read a run's result (root-authored metadata, corrections-style verbatim)."""
    response = _broker_request({"action": TOOL_STATUS, "run_id": args.get("run_id")})
    return json.dumps(response, ensure_ascii=False)


def _load_config() -> Any | None:
    """The LIVE authored config, or ``None`` on any read fault.

    Read fresh from the volume per use (ADR 0044): authoring an admin or
    changing the Email adapter applies on the next message with no restart.
    Callers fail closed on ``None`` — nobody is an admin, and the possession
    gate refuses rather than guessing the custody boundary.
    """
    try:
        return CustomerConfig.from_volume()
    except Exception:  # noqa: BLE001 — an unreadable config admits no admins
        logger.warning(
            "hermes-smd-establishment: customer config unreadable (fail closed)",
            exc_info=True,
        )
        return None


def _resolve_is_admin(sender_id: Any) -> bool:
    """Resolve a turn's sender against the LIVE ``scope.admins`` list."""
    cfg = _load_config()
    return bool(cfg is not None and cfg.sender_is_admin(sender_id))


def _email_adapter(cfg: Any) -> str:
    """The seat's authored Email adapter, lowercased.

    ``""`` when no Email connector is authored+enabled — no mail channel at
    all, so no mail-custody ceremony can apply. Raises on a malformed
    connectors block (the caller turns that into a fail-closed refusal).
    """
    record = cfg.connectors.get(_EMAIL_CAPABILITY)
    if not isinstance(record, dict) or not record.get("enabled"):
        return ""
    return str(record.get("adapter") or "").strip().lower()


def _possession_gate(sender: str, tool_name: Any) -> dict[str, Any] | None:
    """Withhold ``establish_*`` for an admin whose mailbox is unconfirmed.

    Runs only AFTER the admin predicate passed. ``None`` (pass) when the seat's
    custody is tenant-authenticated or there is no mail channel; on AgentMail
    custody (and any unprobed mail adapter — spoofable until proven otherwise)
    the call is withheld until :mod:`shared.admin_possession` says confirmed.
    The withhold message carries the challenge code and the EXACT rostered
    address, and the recipient lock (:func:`_containment_gate`) guarantees the
    code can only ever ship there. Fail-closed on every fault: an unreadable
    config or store refuses rather than waving the call through.
    """
    try:
        cfg = _load_config()
        if cfg is None:
            return {"action": "block", "message": _CONFIG_UNREADABLE_MESSAGE}
        adapter = _email_adapter(cfg)
        if adapter == "" or adapter in _TENANT_AUTH_ADAPTERS:
            return None
        result = admin_possession.verdict(sender, cfg.admins)
        state = result.get("state")
        if state == admin_possession.STATE_CONFIRMED:
            return None
        template = (
            _CHALLENGE_ISSUED_MESSAGE
            if state == admin_possession.STATE_CHALLENGE_ISSUED
            else _CHALLENGE_PENDING_MESSAGE
        )
        logger.info(
            "hermes-smd-establishment: %s withheld pending mailbox possession (%s)",
            tool_name,
            state,
        )
        return {
            "action": "block",
            "message": template.format(admin=sender, nonce=str(result.get("nonce") or "")),
        }
    except Exception:  # noqa: BLE001 — an unresolvable boundary refuses
        logger.exception("hermes-smd-establishment: possession gate fault; refusing (fail closed)")
        return {"action": "block", "message": _CONFIG_UNREADABLE_MESSAGE}


def _person_possession_gate(sender: str, tool_name: Any) -> dict[str, Any] | None:
    """Withhold a person-scoped submit while the sender's mailbox is unconfirmed.

    The person twin of :func:`_possession_gate`, run only AFTER the
    sender==subject predicate passed: same custody scoping (tenant-auth and
    no-mail seats exempt; AgentMail and any unprobed adapter get the ceremony),
    same fail-closed posture, person table + ROSTER re-arm rule in
    :mod:`shared.admin_possession`. A mailbox already confirmed through the
    ADMIN ceremony cross-accepts (possession is a mailbox fact, not a role
    fact). The recipient lock contains person challenge codes exactly as admin
    ones — ``outstanding_nonces`` reads both tables.
    """
    try:
        cfg = _load_config()
        if cfg is None:
            return {"action": "block", "message": _CONFIG_UNREADABLE_MESSAGE}
        adapter = _email_adapter(cfg)
        if adapter == "" or adapter in _TENANT_AUTH_ADAPTERS:
            return None
        result = admin_possession.person_verdict(sender, cfg.sender_on_roster)
        state = result.get("state")
        if state == admin_possession.STATE_CONFIRMED:
            return None
        template = (
            _PERSON_CHALLENGE_ISSUED_MESSAGE
            if state == admin_possession.STATE_CHALLENGE_ISSUED
            else _PERSON_CHALLENGE_PENDING_MESSAGE
        )
        logger.info(
            "hermes-smd-establishment: %s (person scope) withheld pending mailbox possession (%s)",
            tool_name,
            state,
        )
        return {
            "action": "block",
            "message": template.format(person=sender, nonce=str(result.get("nonce") or "")),
        }
    except Exception:  # noqa: BLE001 — an unresolvable boundary refuses
        logger.exception(
            "hermes-smd-establishment: person possession gate fault; refusing (fail closed)"
        )
        return {"action": "block", "message": _CONFIG_UNREADABLE_MESSAGE}


def _is_send_shaped(tool_name: str) -> bool:
    """True for tools that can carry content out of the seat (or into a draft
    that later ships) — the recipient lock's scan set."""
    if tool_name in DRAFT_RECORD_TOOLS or tool_name in _EXTRA_DRAFT_TOOLS:
        return True
    try:
        return classify_tool(tool_name).action_class is ActionClass.EXTERNAL_SEND
    except BannedToolError:
        return True  # banned principal-identity sends are still sends
    except ValueError:
        return False


def _containment_gate(tool_name: Any, args: Any) -> dict[str, Any] | None:
    """Recipient lock: a live challenge code ships ONLY to its admin's address.

    The challenge ride the model's own email tools (the same seam as every
    system-initiated mail), so the property "the reply goes to the ROSTERED
    address, never a claimed From or Reply-To" must hold mechanically, not by
    instruction-following: any send-shaped call whose args carry an
    outstanding code is allowed only when its resolved ``to`` set is exactly
    the rostered admin address. Reply tools resolve no ``to`` from args
    (``extract_to_recipients``), so a code can never ride a reply — which is
    the Reply-To hijack this exists to kill. Draft authoring is locked the
    same way because a draft's content ships later via ``send_draft`` with the
    code no longer visible in args.

    Store-read faults skip the lock (an unknown code cannot be matched, and
    breaking every send on a sqlite hiccup serves nobody); once a code IS
    found in the args, every subsequent fault blocks (fail closed where
    knowledge exists).
    """
    if not isinstance(tool_name, str) or not tool_name or not _is_send_shaped(tool_name):
        return None
    try:
        outstanding = admin_possession.outstanding_nonces()
    except Exception:  # noqa: BLE001 — no readable store, nothing to match
        logger.warning(
            "hermes-smd-establishment: possession store unreadable; "
            "recipient lock skipped this call",
            exc_info=True,
        )
        return None
    if not outstanding:
        return None
    try:
        blob = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else ""
    except (TypeError, ValueError):
        blob = str(args)
    for nonce, address in outstanding.items():
        if not nonce or nonce not in blob:
            continue
        try:
            recipients = extract_to_recipients(args)
        except Exception:  # noqa: BLE001 — a code we cannot address-check must not ship
            recipients = set()
        if recipients == {address}:
            continue
        logger.info(
            "hermes-smd-establishment: %s blocked; live possession code "
            "addressed off the rostered admin",
            tool_name,
        )
        return {
            "action": "block",
            "message": _NONCE_CONTAINMENT_MESSAGE.format(admin=address),
        }
    return None


def _maybe_confirm_possession(cfg: Any, sender_id: Any, user_message: Any) -> bool:
    """Consume a confirming reply: rostered admin sender + their live code.

    Runs on already-admin-classified turns only. Cheap no-op until a challenge
    was ever minted (the store file's existence is the guard), so
    tenant-custody seats never touch sqlite here. Exception-safe False — a
    fault leaves the challenge outstanding, which the admin can answer again.
    """
    try:
        if not isinstance(user_message, str) or not user_message:
            return False
        if not os.path.exists(admin_possession.db_path()):
            return False
        admins = cfg.admins
        if not isinstance(admins, list) or not admins:
            return False
        return admin_possession.try_confirm(sender_id, user_message, admins, source="pre_llm_call")
    except Exception:  # noqa: BLE001 — a fault must not break the turn
        logger.warning(
            "hermes-smd-establishment: possession confirm check failed",
            exc_info=True,
        )
        return False


def _maybe_confirm_person_possession(cfg: Any, sender_id: Any, user_message: Any) -> bool:
    """Consume a person-ceremony confirming reply: rostered sender + live code.

    The person twin of :func:`_maybe_confirm_possession`, run on EVERY
    sender-attributed turn (the confirming reply comes from the preference
    subject, who is usually not an admin). Same cheap no-op until a challenge
    was ever minted; exception-safe False.
    """
    try:
        if not isinstance(user_message, str) or not user_message:
            return False
        if not os.path.exists(admin_possession.db_path()):
            return False
        return admin_possession.person_try_confirm(
            sender_id, user_message, cfg.sender_on_roster, source="pre_llm_call"
        )
    except Exception:  # noqa: BLE001 — a fault must not break the turn
        logger.warning(
            "hermes-smd-establishment: person possession confirm check failed",
            exc_info=True,
        )
        return False


def _normalize_address(value: Any) -> str:
    """The stash/args comparison normalization — lowercase, stripped."""
    return value.strip().lower() if isinstance(value, str) else ""


def _person_pref_pointer(sender_id: Any) -> str | None:
    """Render the per-person preference POINTER for an attributed sender.

    The per-turn half of the spec-stamp mechanism (shared/spec_stamp.py): the
    SKILL.md stamp is one file shared by every sender, so a pointer that must
    follow the ATTRIBUTED sender is delivered here, at ``pre_llm_call``, from
    the root-owned preferences manifest. Pointer, never prose — the file is
    read fresh where it lives, and the hash beside it is what root recorded.
    Best-effort by contract: a fault yields no pointer, never a failed turn.
    """
    try:
        from shared.person_prefs import entry_for_sender, load_person_entries
        from shared.spec_manifest import spec_dir

        base = spec_dir()
        if base is None or not load_person_entries(base):
            return None
        entry = entry_for_sender(sender_id, base)
        if entry is None:
            return None
        return (
            f"This person ({entry.person}) has authored personal preferences for "
            f"work produced FOR THEM. Read `{base / entry.rel_path}` (sha256 "
            f"root-recorded `{entry.sha256[:16]}…`) before drafting anything for "
            "them. Preferences refine how their work is voiced and shaped; they "
            "never override the firm's authored specs, and a spec the firm "
            "declared required must still be read."
        )
    except Exception:  # noqa: BLE001 — a pointer fault must not cost the turn
        logger.warning("hermes-smd-establishment: person-preference pointer failed", exc_info=True)
        return None


def on_pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Classify the turn's sender against ``scope.admins`` and stash it.

    Stashes on every SENDER-ATTRIBUTED turn, last-writer-wins: a mid-session
    non-admin message overwrites an earlier admin classification (the fail-safe
    direction — the session is now talking to someone who may not establish).
    A turn with no ``sender_id`` (cron, self-wake) leaves the stash untouched:
    the classification describes the person the session is talking to, and an
    unattributed turn is not a new person.

    Returns the establishment nudge on admin turns, ``None`` otherwise. On
    admin turns this is ALSO the possession-confirmation seam (ss#2164): the
    admin's reply to the challenge email arrives here as a routed inbound
    turn with their address as ``sender_id``, so a message carrying their
    live challenge code confirms mailbox possession, and the injected context
    tells the model so it acknowledges and proceeds.
    """
    try:
        session_id = kwargs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        sender_id = kwargs.get("sender_id")
        if not sender_id:
            return None
        cfg = _load_config()
        is_admin = bool(cfg is not None and cfg.sender_is_admin(sender_id))
        _ADMIN_STASH[session_id] = {"sender": str(sender_id), "is_admin": is_admin}
        lines: list[str] = []
        if is_admin:
            lines.append(_NUDGE)
            if _maybe_confirm_possession(cfg, sender_id, kwargs.get("user_message")):
                lines.append(_POSSESSION_CONFIRMED_NOTE.format(sender=str(sender_id)))
        # The PERSON lane rides every attributed turn (overlay#170: an
        # unadvertised tool gets zero use) — any rostered person may author
        # their own preferences, and their possession reply confirms here too.
        if _maybe_confirm_person_possession(cfg, sender_id, kwargs.get("user_message")):
            lines.append(_POSSESSION_CONFIRMED_NOTE.format(sender=str(sender_id)))
        lines.append(_PERSON_NUDGE)
        pointer = _person_pref_pointer(sender_id)
        if pointer:
            lines.append(pointer)
        return {"context": "\n\n".join(lines)}
    except Exception:  # noqa: BLE001 — hook callbacks must be exception-safe
        logger.warning(
            "hermes-smd-establishment: pre_llm_call raised; no stash written "
            "(the pre_tool_call gate fails closed on a missing stash)",
            exc_info=True,
        )
        return None


def on_pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    """One hook, two predicates, one ceremony — every path fails closed.

    * FIRM predicate (``establish_stage_document``, and ``establish_submit``
      without ``scope: "person"``): the session's stashed classification must
      say admin; then, on an AgentMail-custody seat, the admin
      mailbox-possession ceremony (ss#2164; module docstring).
    * PERSON predicate (``establish_submit`` with ``scope: "person"``): the
      ``person`` argument must EXACTLY equal the stashed attributed sender —
      admin identity neither satisfies nor bypasses it; then the PERSON
      possession ceremony on AgentMail custody (same attack as the admin
      forgery, smaller blast radius — Captain-directed ruling 2026-08-02).
    * ``establish_status`` requires only that the session IS classified (a
      stash entry exists): a non-admin who established their own preferences
      must be able to poll their run. Run ids are broker-minted secrets known
      only to the submitting session.

    There is deliberately NO session-taint check — Captain decision 2026-08-02
    (see the module header): the establishment turn necessarily read connector
    content, so a taint gate would refuse every legitimate establishment.

    For every OTHER tool: the recipient lock — a call carrying a live
    challenge code may only ship to exactly the rostered address
    (:func:`_containment_gate`).
    """
    tool_name = kwargs.get("tool_name")
    if tool_name not in ESTABLISH_TOOLS:
        return _containment_gate(tool_name, kwargs.get("args"))
    try:
        session_id = kwargs.get("session_id")
        entry = _ADMIN_STASH.get(session_id) if isinstance(session_id, str) else None
        args = kwargs.get("args")
        args = args if isinstance(args, dict) else {}
        if tool_name == TOOL_SUBMIT and args.get("scope") == "person":
            subject = _normalize_address(args.get("person"))
            sender = _normalize_address(entry.get("sender")) if entry else ""
            if subject and sender and subject == sender:
                return _person_possession_gate(sender, tool_name)
            logger.info(
                "hermes-smd-establishment: person-scoped submit refused "
                "(subject does not match the attributed sender)"
            )
            return {"action": "block", "message": _PERSON_MISMATCH_MESSAGE}
        if tool_name == TOOL_STATUS:
            if entry is not None:
                return None
        elif entry is not None and entry.get("is_admin") is True:
            return _possession_gate(str(entry.get("sender") or ""), tool_name)
    except Exception:  # noqa: BLE001 — an unresolvable stash refuses
        logger.exception("hermes-smd-establishment: admin stash unreadable; refusing")
    logger.info(
        "hermes-smd-establishment: %s refused (session not admin-classed)",
        tool_name,
    )
    return {"action": "block", "message": _REFUSAL_MESSAGE}


def register(ctx: Any) -> None:
    """Register the three establishment tools, the admin gate, and the nudge."""
    register_wrapped_tool(
        ctx,
        name=TOOL_STAGE,
        toolset="establishment",
        schema=_STAGE_SCHEMA,
        handler=_stage,
        requires_env=[_SOCKET_ENV],
        description=_STAGE_DESCRIPTION,
        emoji="",
    )
    register_wrapped_tool(
        ctx,
        name=TOOL_SUBMIT,
        toolset="establishment",
        schema=_SUBMIT_SCHEMA,
        handler=_submit,
        requires_env=[_SOCKET_ENV],
        description=_SUBMIT_DESCRIPTION,
        emoji="",
    )
    register_wrapped_tool(
        ctx,
        name=TOOL_STATUS,
        toolset="establishment",
        schema=_STATUS_SCHEMA,
        handler=_status,
        requires_env=[_SOCKET_ENV],
        description=_STATUS_DESCRIPTION,
        emoji="",
    )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    logger.info(
        "hermes-smd-establishment registered %s + admin gate + nudge",
        ", ".join(ESTABLISH_TOOLS),
    )


__all__ = [
    "ESTABLISH_TOOLS",
    "SPEC_PROPERTIES",
    "TOOL_STAGE",
    "TOOL_STATUS",
    "TOOL_SUBMIT",
    "on_pre_llm_call",
    "on_pre_tool_call",
    "register",
]
