"""Action-class vocabulary shared by the audit and trust plugins.

Both ``hermes-smd-audit`` and ``hermes-smd-trust`` need the same closed-vocabulary
view of every Hermes tool call: the action class (read / internal_write /
external_send / commitment / destructive), the banned-tool set (Pattern A / B
forbidden capabilities), and the tool-name → action-class registry. This module
is the single source of truth.

Layering:

  - The plugins import these names. Plugins never redefine them. Drift between
    plugins is the failure mode this consolidation prevents (filed as task #33,
    follow-on to the §7 adapter port).
  - Tests assert the registry is closed-vocabulary, disjoint from
    ``BANNED_TOOLS``, and runtime-immutable (``MappingProxyType`` raises
    ``TypeError`` on any mutation attempt).
  - Audit tags every per-tool row with the action class; trust uses the action
    class to decide whether the call clears the resolved ceiling.

Banned reasons:

  ``BANNED_REASON`` maps each banned tool name to a closed-vocabulary category
  code (e.g. ``"banned_tool_pattern_a"``, ``"banned_tool_destructive"``). This
  is the substrate-level classification — audit rows persist it verbatim in
  ``metadata.banned_reason``. The trust plugin renders its own user-visible
  refusal sentence at the policy boundary; it does NOT consume the categorical
  code as message text.
"""

import enum
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocabulary action class
#
# String values match the ss-console adapter exactly so the two enforcement
# surfaces (TS validators on the authoring side, Python enforcement here)
# round-trip through their string representations.
# ---------------------------------------------------------------------------


class ActionClass(str, enum.Enum):
    """Categorization of every tool call by reversibility / blast radius."""

    READ = "read"  # Always allowed
    INTERNAL_WRITE = "internal_write"  # Notes, drafts, internal state — autonomous OK
    EXTERNAL_SEND = "external_send"  # Send to a NON-roster (outside) recipient — gated
    # Send to a human-rostered INTERNAL recipient (own staff). Its own authored,
    # fail-closed ceiling. The recipient axis is resolved in evaluate_tool_call via
    # recipient_classifier; an unclassifiable recipient is forced OUTSIDE (draft),
    # never INTERNAL — a send is never promoted to autonomous on an unknown recipient.
    EXTERNAL_SEND_INTERNAL = "external_send_internal"
    # Send to a firm's own rostered CLIENT / RECORDS VENDOR (scope.outbound_roster).
    # Each carries its own authored, fail-closed ceiling — graduatable to autonomous
    # independently of the outside class (ADR 0075). The recipient axis is resolved
    # in evaluate_tool_call via recipient_classifier.classify_recipients_typed; an
    # unclassifiable recipient is forced OUTSIDE (draft), never CLIENT/VENDOR.
    EXTERNAL_SEND_CLIENT = "external_send_client"
    EXTERNAL_SEND_VENDOR = "external_send_vendor"
    COMMITMENT = "commitment"  # Sign, accept terms, agree to dates — never autonomous
    DESTRUCTIVE = "destructive"  # Delete, drop, irreversible — explicit per-call approval
    CODE_EXECUTION = (
        "code_execution"  # Arbitrary code / shell / subagent — authored-only, fail-closed
    )
    REFUSED = "refused"  # Unknown / unmapped tool — fail-closed terminal class, never executes


# ---------------------------------------------------------------------------
# Vertical-pack safety floors (ADR 0022 / ADR 0037 Tenet 3) — SOURCE OF TRUTH
#
# A vertical pack declares non-raisable safety floors in its manifest
# (``operator/verticals/<vertical>/vertical.yaml`` -> ``compliance:``). A floor
# can only *narrow* a customer's authored ceiling, never raise it. The floor
# SEMANTICS are encoded here keyed by vertical slug -> {action_class -> ceiling},
# both as the closed-vocabulary STRINGS (the ``ActionClass`` values above and the
# ADR 0035 ceiling strings ``refused`` / ``draft_for_review`` / ``autonomous``).
#
# This lives in ``shared/`` — the lowest layer both enforcement surfaces import
# downward — so there is ONE definition:
#   * ``hermes-smd-trust/enforce.py`` derives its enum-keyed runtime map from
#     this (the live ``pre_tool_call`` ceiling resolver), and
#   * ``config_applier/safety.py`` reads it directly (the apply-time floor check)
#     so a config that widens past a floor is rejected before it is written.
# A copy in either consumer would silently drift; this is the single source.
#
# No vertical currently declares a ceiling floor. The law-firm
# ``external-send-draft-floor`` (external_send pinned to draft_for_review) was
# removed 2026-07 by Captain decision: outside-send is a firm-authored dial per
# ADR 0035, and supervision (ABA 512) is held by the audit journal +
# attribution + fail-closed entitlement, not by a non-raisable send gate. The
# machinery stays: a future vertical with a genuinely regulation-compelled
# floor (one a customer must not be able to raise) declares it here. Note the
# law wedge's hard BANS (trust-account writes, principal-identity sends) are
# NOT floors and live in ``BANNED_TOOLS`` below — those are unchanged.
# ---------------------------------------------------------------------------


VERTICAL_FLOORS: dict[str, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# Banned tools — Pattern A / Pattern B forbidden capabilities
#
# A tool name in this set NEVER reaches trust-ceiling enforcement. The
# ``classify_tool()`` helper raises ``BannedToolError`` immediately; both
# plugins translate that into a refusal at their respective seams.
# ---------------------------------------------------------------------------


BANNED_TOOLS: frozenset[str] = frozenset(
    {
        # Principal-identity sends — the Email connector wired to a human
        # principal's own mailbox. The Operator never sends AS a human: a send
        # from the principal's mailbox is indistinguishable from the human
        # having written it, which forges authorship and breaks the
        # attribution the audit journal's supervision record depends on
        # (ABA 512 posture). Drafting INTO the principal's mailbox is allowed
        # (DRAFT_CREATE); the send path is a hard identity-integrity ban, not
        # a configurable ceiling. Autonomous sending happens only from the
        # Operator's own identity (see the AgentMail note below).
        "email_send",
        "email_send_message",
        "email_reply",
        "email_reply_all",
        "email_forward",
        # SMS / messaging — same principal-identity rationale as email_send.
        "sms_send",
        "sms_send_message",
        # Money movement — never autonomous.
        "payments_initiate_transfer",
        "payments_send_payment",
        "payments_refund",
        "payments_authorize_charge",
        "payments_void_authorization",
        # Smokeball trust-account writes — IOLTA fund movement. The law wedge is
        # trust-funds-read-only (vertical floor); these are a HARD BAN, never a
        # configurable ceiling. Runtime names are mcp_smokeball_<tool> (server key
        # "smokeball"). Without these explicit bans they would hit the
        # fail-closed unmapped path; before issue #1327 that path defaulted to
        # READ and slipped every ceiling — the Risk-3 footgun the threat model
        # named.
        "mcp_smokeball_create_transaction",
        "mcp_smokeball_protect_funds",
        "mcp_smokeball_unprotect_funds",
        # Calendar / matter destructive — irreversible state changes.
        "calendar_delete_event",
        "practice_management_delete_matter",
        "practice_management_close_matter_permanent",
        # Connector-level destructive operations.
        "connector_revoke_oauth",
        "connector_unbind_permanent",
        #
        # NOTE on AgentMail sends (`agentmail:send_message`, `send_draft`,
        # `reply_to_message`, `forward_message`): these are NO LONGER banned.
        # ADR 0025 (Captain decision 2026-05-29) overturned the hardcoded
        # autonomous-send refusal — exposure is now a CONFIGURABLE per-action
        # ceiling, not a permanent ban. The agentmail sends are classified
        # ``EXTERNAL_SEND`` in TOOL_ACTION_CLASS_MAP below and governed by the
        # resolved ceiling. Per ADR 0035 there is no default posture: unauthored
        # ``external_send`` is fail-closed (``refused`` — no send, no draft);
        # the authored values are ``autonomous`` / ``confirm`` /
        # ``draft_for_review`` / ``refused``. The content-sensitivity floor
        # (``shared.content_floor``) additionally forces money / contract /
        # scope / legal content to draft even under an autonomous ceiling.
        # The PRINCIPAL-identity sends (`email_send`, `email_reply`, ...) stay
        # banned above — the agent sends only from its OWN identity, never
        # from a human principal's mailbox.
    }
)


# Reason classification for BANNED tool names. Closed vocabulary of category
# codes. The audit plugin persists this verbatim in ``metadata.banned_reason``;
# the trust plugin renders its own sentence-form message at the policy
# boundary and does not consume this code as message text.
BANNED_REASON: Mapping[str, str] = MappingProxyType(
    {
        "email_send": "banned_tool_pattern_a",
        "email_send_message": "banned_tool_pattern_a",
        "email_reply": "banned_tool_pattern_a",
        "email_reply_all": "banned_tool_pattern_a",
        "email_forward": "banned_tool_pattern_a",
        "sms_send": "banned_tool_pattern_a",
        "sms_send_message": "banned_tool_pattern_a",
        "payments_initiate_transfer": "banned_tool_destructive",
        "payments_send_payment": "banned_tool_destructive",
        "payments_refund": "banned_tool_destructive",
        "payments_authorize_charge": "banned_tool_destructive",
        "payments_void_authorization": "banned_tool_destructive",
        "calendar_delete_event": "banned_tool_destructive",
        "practice_management_delete_matter": "banned_tool_destructive",
        "practice_management_close_matter_permanent": "banned_tool_destructive",
        "connector_revoke_oauth": "banned_tool_destructive",
        "connector_unbind_permanent": "banned_tool_destructive",
        "mcp_smokeball_create_transaction": "banned_tool_destructive",
        "mcp_smokeball_protect_funds": "banned_tool_destructive",
        "mcp_smokeball_unprotect_funds": "banned_tool_destructive",
        # agentmail sends are NOT banned (ADR 0025) — see the note in
        # BANNED_TOOLS above. They are EXTERNAL_SEND, ceiling-governed.
    }
)


# ---------------------------------------------------------------------------
# Tool-name → action_class registry
#
# Keys: every tool name the v1 capability surface exposes (read /
# internal-write / commitment). Email send + money movement are DELIBERATELY
# ABSENT from this map and present in BANNED_TOOLS instead — adding them here
# is a P0 doctrine violation.
# ---------------------------------------------------------------------------


_RAW_TOOL_ACTION_CLASS_MAP: dict[str, ActionClass] = {
    # AgentMail MCP — the persona's OWN mailbox (not the principal's Gmail).
    #
    # RUNTIME NAMING (2026-06-12, verified against the live server). Hermes
    # registers MCP tools as ``mcp_<server>_<tool>`` — underscore-joined, with
    # dashes in the server name folded to underscores. The agentmail server key
    # is ``agentmail`` (see bootstrap.mcp_registry), so every tool reaches the
    # classifier as ``mcp_agentmail_<tool>``. The earlier ``agentmail:<tool>``
    # colon spelling was an unverified guess: it matched NOTHING at runtime, so
    # every agentmail send classified READ and slipped the trust ceiling +
    # taint-gate (the 2026-06-12 demo-law live-test P0 — the agent sent a reply
    # autonomously on an inbound-tainted turn). The full 24-tool surface is
    # enumerated below from a live ``tools/list`` so no agentmail tool defaults
    # to READ. The colon spellings are retained ONLY as capability-contract
    # aliases (audit prose / TS-side references); they never occur at runtime.
    #
    # Sends are EXTERNAL_SEND, governed by the resolved per-action ceiling (ADR
    # 0025/0035): unauthored is fail-closed (refused — no send, no draft);
    # draft_for_review and autonomous are both authored in action_ceilings;
    # vertical floor narrows; the content-sensitivity floor (shared.content_floor)
    # forces sensitive content to draft even under autonomous. Drafting
    # (create_draft / update_draft) is INTERNAL_WRITE — the agent's own job.
    # delete_inbox / delete_thread are DESTRUCTIVE (irreversible loss of received
    # mail); delete_draft is INTERNAL_WRITE (discarding the agent's own unsent
    # draft, matching email_delete_draft).
    #
    # --- live runtime names (mcp_agentmail_*) — the ONLY form the agent emits
    "mcp_agentmail_send_message": ActionClass.EXTERNAL_SEND,
    "mcp_agentmail_send_draft": ActionClass.EXTERNAL_SEND,
    "mcp_agentmail_reply_to_message": ActionClass.EXTERNAL_SEND,
    "mcp_agentmail_forward_message": ActionClass.EXTERNAL_SEND,
    "mcp_agentmail_create_draft": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_update_draft": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_create_inbox": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_update_inbox": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_update_thread": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_update_message": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_delete_draft": ActionClass.INTERNAL_WRITE,
    "mcp_agentmail_delete_inbox": ActionClass.DESTRUCTIVE,
    "mcp_agentmail_delete_thread": ActionClass.DESTRUCTIVE,
    "mcp_agentmail_list_inboxes": ActionClass.READ,
    "mcp_agentmail_get_inbox": ActionClass.READ,
    "mcp_agentmail_list_threads": ActionClass.READ,
    "mcp_agentmail_search_threads": ActionClass.READ,
    "mcp_agentmail_get_thread": ActionClass.READ,
    "mcp_agentmail_get_attachment": ActionClass.READ,
    "mcp_agentmail_list_messages": ActionClass.READ,
    "mcp_agentmail_search_messages": ActionClass.READ,
    "mcp_agentmail_list_drafts": ActionClass.READ,
    "mcp_agentmail_get_draft": ActionClass.READ,
    "mcp_agentmail_auth_me": ActionClass.READ,
    # Native web search (WebSearch capability, ADR 0070). Hermes' bundled web
    # providers (plugins/web/*, e.g. brave-free) expose ONE native tool,
    # `web_search` (tools/web_tools.py) — NOT an MCP tool, so the runtime name is
    # bare `web_search` with no mcp_<server>_ prefix. Selected per customer.yaml
    # via connectors.WebSearch.backend: native:<provider> -> Hermes config
    # `web.search_backend`. Read-only: no writes, no sends, nothing irreversible —
    # always allowed even on an inbound-tainted turn. Unmapped => REFUSED
    # (fail-closed), so this entry is what keeps native web search from being dead
    # on arrival (superseded the mcp:brave connector, whose whole layer was the
    # redundant wrapper that MCP-wrapped a native Hermes feature).
    "web_search": ActionClass.READ,
    # --- capability-contract aliases (colon form) — never emitted at runtime,
    #     retained so audit prose / TS-side references still resolve.
    "agentmail:send_message": ActionClass.EXTERNAL_SEND,
    "agentmail:send_draft": ActionClass.EXTERNAL_SEND,
    "agentmail:reply_to_message": ActionClass.EXTERNAL_SEND,
    "agentmail:forward_message": ActionClass.EXTERNAL_SEND,
    "agentmail:create_draft": ActionClass.INTERNAL_WRITE,
    "agentmail:update_draft": ActionClass.INTERNAL_WRITE,
    # Email — read-only + draft-creation only. PRINCIPAL-identity SEND is BANNED.
    "email_list_messages": ActionClass.READ,
    "email_get_message": ActionClass.READ,
    "email_search": ActionClass.READ,
    "email_get_thread": ActionClass.READ,
    "email_list_labels": ActionClass.READ,
    "email_create_draft": ActionClass.INTERNAL_WRITE,
    "email_update_draft": ActionClass.INTERNAL_WRITE,
    "email_delete_draft": ActionClass.INTERNAL_WRITE,
    # SMS — read-only + draft-creation only. SEND is BANNED.
    "sms_list_messages": ActionClass.READ,
    "sms_get_message": ActionClass.READ,
    "sms_create_draft": ActionClass.INTERNAL_WRITE,
    # Calendar — read + non-destructive scheduling state changes.
    "calendar_list_events": ActionClass.READ,
    "calendar_get_event": ActionClass.READ,
    "calendar_search_events": ActionClass.READ,
    "calendar_check_availability": ActionClass.READ,
    "calendar_create_event_draft": ActionClass.INTERNAL_WRITE,
    "calendar_propose_time": ActionClass.COMMITMENT,
    "calendar_respond_invitation_draft": ActionClass.INTERNAL_WRITE,
    # Practice management — read + non-destructive matter updates.
    "practice_management_search_matters": ActionClass.READ,
    "practice_management_get_matter": ActionClass.READ,
    "practice_management_list_documents": ActionClass.READ,
    "practice_management_get_document": ActionClass.READ,
    "practice_management_list_tasks": ActionClass.READ,
    "practice_management_create_note": ActionClass.INTERNAL_WRITE,
    "practice_management_create_task_draft": ActionClass.INTERNAL_WRITE,
    "practice_management_update_matter_field": ActionClass.INTERNAL_WRITE,
    "practice_management_open_matter_draft": ActionClass.COMMITMENT,
    # ----------------------------------------------------------------------
    # Smokeball MCP — the law wedge's system of record. Server key "smokeball",
    # so Hermes registers every tool as mcp_smokeball_<tool>. Native surface from
    # operator/verticals/law-firm/smokeball-surface.md. Reads → READ; the one
    # wedge write is create_memo (INTERNAL_WRITE); create_matter is COMMITMENT
    # (never autonomous — gated draft); contact/task/file/calendar-event/folder
    # writes INTERNAL_WRITE;
    # file delete DESTRUCTIVE. Trust-account writes (create_transaction /
    # protect_funds / unprotect_funds) are NOT here — they are hard-BANNED above.
    # Every Smokeball tool the server exposes MUST appear here or in BANNED_TOOLS;
    # an omission is unreachable until policy classifies it.
    "mcp_smokeball_auth_status": ActionClass.READ,
    "mcp_smokeball_list_matters": ActionClass.READ,
    "mcp_smokeball_get_matter": ActionClass.READ,
    "mcp_smokeball_list_matter_types": ActionClass.READ,
    "mcp_smokeball_get_stage_sets": ActionClass.READ,
    "mcp_smokeball_get_stage_to_matter_mappings": ActionClass.READ,
    "mcp_smokeball_get_contacts": ActionClass.READ,
    "mcp_smokeball_get_contact": ActionClass.READ,
    "mcp_smokeball_get_contact_relations": ActionClass.READ,
    "mcp_smokeball_list_tasks": ActionClass.READ,
    "mcp_smokeball_get_task": ActionClass.READ,
    "mcp_smokeball_search_staff": ActionClass.READ,
    "mcp_smokeball_get_staff": ActionClass.READ,
    "mcp_smokeball_get_roles_on_matter": ActionClass.READ,
    "mcp_smokeball_get_relationships_on_matter": ActionClass.READ,
    "mcp_smokeball_get_files_on_matter": ActionClass.READ,
    "mcp_smokeball_get_file": ActionClass.READ,
    "mcp_smokeball_get_download_url": ActionClass.READ,
    # Server-side presigned fetch + text extraction (PDF/DOCX/text). The FIRST
    # content-bearing Smokeball read: it returns externally-authored document
    # text (opposing counsel, providers), so it is fenced+tainting in
    # hermes-smd-inbound._FENCED_READ_TOOLS — unlike the metadata-only reads
    # around it. Added 2026-07-05 (L2 DISC-1: get_download_url minted URLs no
    # tool could fetch).
    "mcp_smokeball_read_document": ActionClass.READ,
    "mcp_smokeball_get_memos_on_matter": ActionClass.READ,
    "mcp_smokeball_get_bank_accounts": ActionClass.READ,
    "mcp_smokeball_get_matter_balances": ActionClass.READ,
    "mcp_smokeball_get_matter_billing_config": ActionClass.READ,
    "mcp_smokeball_get_fees": ActionClass.READ,
    "mcp_smokeball_get_expenses": ActionClass.READ,
    "mcp_smokeball_get_webhook_subscriptions": ActionClass.READ,
    "mcp_smokeball_get_event_types": ActionClass.READ,
    "mcp_smokeball_list_events": ActionClass.READ,
    "mcp_smokeball_list_folders": ActionClass.READ,
    "mcp_smokeball_create_memo": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_patch_matter": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_contact": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_task": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_update_task": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_add_file": ActionClass.INTERNAL_WRITE,
    # Server-side cross-connector transfer (ss-console #1744): fetches an
    # AgentMail-minted time-limited attachment download_url (host-allowlisted,
    # https-only, size-capped, no redirects) and runs the two-stage matter
    # upload. A matter-file write; returns metadata only (no content) so it is
    # NOT a fenced read — the filed copy is read later via read_document,
    # which fences and taints.
    "mcp_smokeball_file_attachment_to_matter": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_get_upload_url": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_webhook_subscription": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_event": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_update_event": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_event_reminder": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_folder": ActionClass.INTERNAL_WRITE,
    "mcp_smokeball_create_matter": ActionClass.COMMITMENT,
    "mcp_smokeball_delete_file": ActionClass.DESTRUCTIVE,
    # ----------------------------------------------------------------------
    # Clio MCP (oktopeak/clio-mcp v2.0.0) — the law wedge's ORIGINAL
    # practice-management system of record, bound by pilot-law (Ashton & Price)
    # and demo-law as `mcp:clio-oktopeak`. Hermes sanitizes the server key
    # `clio-oktopeak` -> `clio_oktopeak` (sanitize_mcp_name_component: every
    # non-[A-Za-z0-9_] char -> `_`), so every tool registers as
    # mcp_clio_oktopeak_<tool>. Surface pinned in
    # ss-console operator/verticals/law-firm/clio-surface.md (23 tools, read
    # against the connector source 2026-06-05).
    #
    # THIS BLOCK CLOSES A LIVE FAIL-OPEN (EFF-07): the connector materializes
    # and is bound on two running Machines, but had ZERO classified tools — so
    # every Clio write went unmapped->READ->autonomous, fired even on an
    # injection-tainted turn. The pilot-law config comment claiming create_matter
    # / create_calendar_entry are "GATED / fail-closed" described only the SKILL
    # authoring posture (draft-and-surface), which injection bypasses; nothing in
    # the trust layer enforced it. Mirrors the Smokeball classification exactly:
    # reads -> READ; note/task/document writes -> INTERNAL_WRITE; the
    # system-of-record / financial / scheduling commitments (create_matter,
    # create_calendar_entry, log_time_entry, create_activity) -> COMMITMENT
    # (never autonomous). Clio exposes no IOLTA/trust-account tool and no delete
    # (clio-surface.md), so there is nothing to hard-BAN here.
    "mcp_clio_oktopeak_list_matters": ActionClass.READ,
    "mcp_clio_oktopeak_get_matter": ActionClass.READ,
    "mcp_clio_oktopeak_search_contacts": ActionClass.READ,
    "mcp_clio_oktopeak_get_contact": ActionClass.READ,
    "mcp_clio_oktopeak_list_documents": ActionClass.READ,
    "mcp_clio_oktopeak_get_document": ActionClass.READ,
    "mcp_clio_oktopeak_list_tasks": ActionClass.READ,
    "mcp_clio_oktopeak_list_calendars": ActionClass.READ,
    "mcp_clio_oktopeak_list_calendar_entries": ActionClass.READ,
    "mcp_clio_oktopeak_list_time_entries": ActionClass.READ,
    "mcp_clio_oktopeak_get_billing_summary": ActionClass.READ,
    "mcp_clio_oktopeak_list_users": ActionClass.READ,
    "mcp_clio_oktopeak_get_user": ActionClass.READ,
    "mcp_clio_oktopeak_export_audit_log": ActionClass.READ,
    "mcp_clio_oktopeak_create_note": ActionClass.INTERNAL_WRITE,
    "mcp_clio_oktopeak_create_task": ActionClass.INTERNAL_WRITE,
    "mcp_clio_oktopeak_update_task": ActionClass.INTERNAL_WRITE,
    "mcp_clio_oktopeak_complete_task": ActionClass.INTERNAL_WRITE,
    "mcp_clio_oktopeak_upload_document": ActionClass.INTERNAL_WRITE,
    "mcp_clio_oktopeak_create_matter": ActionClass.COMMITMENT,
    "mcp_clio_oktopeak_create_calendar_entry": ActionClass.COMMITMENT,
    "mcp_clio_oktopeak_log_time_entry": ActionClass.COMMITMENT,
    "mcp_clio_oktopeak_create_activity": ActionClass.COMMITMENT,
    # Reference connector (mcp:reference) — the SYNTHETIC author-built connector
    # platform self-test fixture (ss-console operator/connectors/_reference;
    # ADR 0053). echo is a pure read, record an internal write. `surprise` is
    # DELIBERATELY absent here (and from PINNED_CONNECTOR_SURFACES): it is the
    # platform's fail-closed proof — bound on a test seat, mcp_reference_surprise
    # must classify REFUSED and never execute. Runtime prefix mcp_reference_
    # (no dash to fold). These two lines ARE the trust copy; the connector's
    # manifest.toml tool_classes is only the oracle checked against this map.
    "mcp_reference_echo": ActionClass.READ,
    "mcp_reference_record": ActionClass.INTERNAL_WRITE,
    # Memory — read-only via this registry.
    "memory_search": ActionClass.READ,
    "memory_get_rule": ActionClass.READ,
    "memory_list_rules": ActionClass.READ,
    # Native Hermes orientation reads. These must stay reachable under the
    # default non-REFUSED ceiling so the Operator can inspect authored files,
    # search local context, load skills, recall prior sessions, and inspect
    # images without reaching for code or terminal.
    "read_file": ActionClass.READ,
    "search_files": ActionClass.READ,
    "skills_list": ActionClass.READ,
    "skill_view": ActionClass.READ,
    "session_search": ActionClass.READ,
    # Voice gate — read-only against the voice corpus.
    "voice_score_draft": ActionClass.READ,
    "voice_list_judge_history": ActionClass.READ,
    "vision_analyze": ActionClass.READ,
    # Connector lifecycle — read-only here.
    "connector_get_status": ActionClass.READ,
    "connector_list_bindings": ActionClass.READ,
    # Mediated Google Workspace tools. Every privileged provider operation is
    # explicit and classified; no general-purpose tool receives a credential.
    "workspace_gmail_search": ActionClass.READ,
    "workspace_gmail_get": ActionClass.READ,
    "workspace_gmail_create_draft": ActionClass.INTERNAL_WRITE,
    # modify/archive mutate the principal's REAL mailbox (apply/remove INBOX,
    # TRASH, labels) — not internal drafts. DESTRUCTIVE so they require explicit
    # current-turn approval and are refused under a draft_for_review ceiling and
    # on any turn tainted by untrusted inbound content (OP-P0-5). An EA triage
    # run SUGGESTS filing; a human acts on it. (Recoverable from All Mail/Trash,
    # but "silently buried real client mail" is the harm we gate.)
    "workspace_gmail_modify": ActionClass.DESTRUCTIVE,
    "workspace_gmail_archive": ActionClass.DESTRUCTIVE,
    "workspace_calendar_list": ActionClass.READ,
    "workspace_calendar_get": ActionClass.READ,
    "workspace_calendar_create_draft": ActionClass.INTERNAL_WRITE,
    "workspace_calendar_update_draft": ActionClass.INTERNAL_WRITE,
    "workspace_drive_list": ActionClass.READ,
    "workspace_drive_get": ActionClass.READ,
    "workspace_drive_export": ActionClass.READ,
    "workspace_docs_create": ActionClass.INTERNAL_WRITE,
    "workspace_docs_get": ActionClass.READ,
    "workspace_docs_append": ActionClass.INTERNAL_WRITE,
    "workspace_sheets_create": ActionClass.INTERNAL_WRITE,
    "workspace_sheets_get_values": ActionClass.READ,
    "workspace_sheets_update_values": ActionClass.INTERNAL_WRITE,
    # ----------------------------------------------------------------------
    # Hermes CORE high-capability tools (OP-P0-1). These are the agent's own
    # back-door capabilities: arbitrary code, shell, subagent spawn, OS
    # process control, computer/browser automation, self-scheduling, and
    # self-authoring of executable skills. Before this they were UNMAPPED →
    # classified READ → waved through every ceiling (the systemic footgun the
    # threat model named). They are CODE_EXECUTION: fail-closed unless the
    # engagement authors a ``code_execution`` ceiling (ADR 0035), refused on
    # any turn tainted by untrusted inbound content (the taint-gate), and
    # never reachable by a draft_for_review skill. Legitimate use (e.g.
    # ar-chaser's ADR-0021 fetch loop) is authored at the customer level and
    # runs only on untainted turns. NOTE: the broader unmapped→READ default
    # for unknown CORE tools was the residual footgun; it is now closed (issue
    # #1327). ``classify_tool`` fails closed to ``REFUSED`` for any name not in
    # this map, so this map IS the core read-allowlist: a legitimately
    # read-safe core tool must be enumerated here to be reachable.
    "execute_code": ActionClass.CODE_EXECUTION,
    "terminal": ActionClass.CODE_EXECUTION,
    "process": ActionClass.CODE_EXECUTION,
    "delegate_task": ActionClass.CODE_EXECUTION,
    "computer_use": ActionClass.CODE_EXECUTION,
    "cronjob": ActionClass.CODE_EXECUTION,
    "skill_manage": ActionClass.CODE_EXECUTION,
    # In-band session state / prompts. `clarify` asks the current operator
    # through the active session callback; it is not arbitrary external send.
    "todo": ActionClass.INTERNAL_WRITE,
    "record_peer_preference": ActionClass.INTERNAL_WRITE,
    "clarify": ActionClass.INTERNAL_WRITE,
    # File mutation — internal write (not code-exec, but not read either).
    "write_file": ActionClass.INTERNAL_WRITE,
    "patch": ActionClass.INTERNAL_WRITE,
    # Escalation-ledger tools (hermes-smd-escalation, ss #1915). The append is
    # operator communication telemetry through the broker's validated verb —
    # internal state, never client-facing content; the state read folds the
    # read-only ledger twin. These replaced the execute_code append snippet so
    # skills never need a code_execution exposure to run the escalation loop.
    "escalation_append": ActionClass.INTERNAL_WRITE,
    "escalation_state": ActionClass.READ,
    # Durable-job tools (hermes-smd-jobs, ss #1916 — these were unmapped, so the
    # fail-closed REFUSED default made durable jobs inert at runtime).
    # start_background_job launches a budgeted background agent turn with tools:
    # CODE_EXECUTION, consistent with delegate_task (the background turn taints
    # ITSELF when it ingests; see _CODE_NO_INGEST_BY_DESIGN in
    # tests/test_code_execution_taint.py for the parent-turn disposition).
    # job_status is READ over broker-authored metadata only — its projection
    # deliberately excludes the free-text error column (see _job_status), which
    # is the one field a failed job could launder untrusted content through.
    "start_background_job": ActionClass.CODE_EXECUTION,
    "job_status": ActionClass.READ,
    "job_cancel": ActionClass.INTERNAL_WRITE,
    "job_record_sideeffect": ActionClass.INTERNAL_WRITE,
}


# Public read-only view. Callers must not mutate the registry at runtime;
# changes ship as a PR + test + spec update. MappingProxyType raises
# TypeError on any mutation attempt, making the constraint enforceable.
TOOL_ACTION_CLASS_MAP: Mapping[str, ActionClass] = MappingProxyType(_RAW_TOOL_ACTION_CLASS_MAP)


# ---------------------------------------------------------------------------
# Refusal types
# ---------------------------------------------------------------------------


class BannedToolError(Exception):
    """Raised when a tool name appears in ``BANNED_TOOLS``.

    Never reaches policy. The dispatch path catches this and translates to a
    refusal audit row via the per-tool emit helper. ``tool_name`` carries the
    offending name for metadata; ``reason`` is the closed-set category code
    from ``BANNED_REASON`` (``"banned_tool_pattern_a"`` /
    ``"banned_tool_destructive"``).
    """

    def __init__(self, *, tool_name: str, reason: str = "banned_tool") -> None:
        super().__init__(f"tool {tool_name!r} is banned: {reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclass(frozen=True)
class ToolClassification:
    """Outcome of ``classify_tool()``.

    ``action_class`` is the action class the trust-ceiling enforcer should
    use for this tool call. ``unmapped`` is True if the tool name was not
    in ``TOOL_ACTION_CLASS_MAP`` (the helper returned the fail-closed
    ``REFUSED`` default). The flag is retained for audit telemetry even
    though the action class itself now blocks the call.
    """

    action_class: ActionClass
    unmapped: bool


def classify_tool(tool_name: str) -> ToolClassification:
    """Map a tool name to its ``ActionClass``.

    - Empty / missing tool name → ``ValueError``.
    - ``tool_name`` in ``BANNED_TOOLS`` → ``BannedToolError`` (the exception
      carries the categorical ``reason`` code from ``BANNED_REASON``).
    - ``tool_name`` in registry → mapped action class, ``unmapped=False``.
    - Otherwise → ``ActionClass.REFUSED``, ``unmapped=True``.

    The unmapped fallback is fail-closed: an unmapped tool is an UNKNOWN
    capability, not a benign read. The prior READ default (issue #1327)
    silently waved every unregistered tool through every ceiling — any new
    Hermes-core tool, any newly surfaced MCP verb, any rename would slip the
    trust gate as "read". ``REFUSED`` is a terminal class: ``enforce()`` blocks
    it outright and it is deliberately NOT routed through ``resolve_ceiling``,
    so an authored ceiling (even ``code_execution: autonomous``) can never
    widen it. The ``unmapped=True`` flag is preserved so the audit row still
    carries ``metadata.unmapped_tool=true`` — the telemetry that surfaces the
    gap so the registry gets the new name added (the intended remediation,
    versus a silent allow). Legitimately read-safe tools must be enumerated in
    ``TOOL_ACTION_CLASS_MAP`` above; that map IS the core read-allowlist.

    Lookups run against a normalized (trimmed, lowercased) form of the
    name: the registry and ``BANNED_TOOLS`` are all-lowercase, so without
    normalization a runtime surfacing ``Execute_Code`` or ``TERMINAL``
    would miss the CODE_EXECUTION mapping and fall to the READ default —
    a case-sensitivity ceiling bypass (2026-06-12 code review).
    """
    if not tool_name:
        raise ValueError("tool_name is required")

    normalized = tool_name.strip().lower()
    if not normalized:
        raise ValueError("tool_name is required")

    if normalized in BANNED_TOOLS:
        reason = BANNED_REASON.get(normalized, "banned_tool")
        raise BannedToolError(tool_name=normalized, reason=reason)

    mapped = _RAW_TOOL_ACTION_CLASS_MAP.get(normalized)
    if mapped is not None:
        return ToolClassification(action_class=mapped, unmapped=False)

    logger.warning(
        "classify_tool: tool_name=%s not in TOOL_ACTION_CLASS_MAP; "
        "failing closed to REFUSED and tagging metadata.unmapped_tool=true",
        tool_name,
    )
    return ToolClassification(action_class=ActionClass.REFUSED, unmapped=True)


__all__ = [
    "ActionClass",
    "BANNED_REASON",
    "BANNED_TOOLS",
    "BannedToolError",
    "TOOL_ACTION_CLASS_MAP",
    "VERTICAL_FLOORS",
    "ToolClassification",
    "classify_tool",
]
