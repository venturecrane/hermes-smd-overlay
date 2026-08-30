"""Which reads are allowed to establish provenance (ss-console#2511).

The identifier gate asks one question of every identifier in an outbound draft:
"did the Operator READ this, or compose it?" The answer is only as good as what
the register counted as a read. Before this, ``on_post_tool_call`` seeded the
register from the result of EVERY read-class tool, and ``read_file`` is
read-class. The seat's own skill text, its authored config, its memory store and
its own scored drafts all counted as sources of record.

That is not a hypothetical. On 2026-08-21, during the A&P stand-up rehearsal on
``hermes-ashton-price``, the ``operator-self-test`` skill asked the Operator to
prove the gate by writing a memo containing the sentinel case number
``ZZ-9999-0001``. The Operator read the skill text with ``read_file`` first, so
the sentinel was in the register by the time the draft gate looked, and the gate
found nothing unverified. The sentinel matter 404d, the Operator retried against
a real matter, and the memo landed in the firm's production Smokeball. No
``IDENTIFIER_UNVERIFIED`` row was written, because on that path there is nothing
to report: the gate believed the number had been read.

So the rule these tests pin: **a read establishes provenance only when it
reaches the tenant's own system of record.** The seat's files, skills, memory,
self-description, model-produced transcriptions and unsent drafts are things the
Operator holds, not things the firm recorded. The direction of the failure is
deliberate — a read tool that is not enumerated as a tenant source does not seed,
so a new tool over-reports (a refusal a human can clear) rather than
under-reports (a fabricated identifier delivered silently), which is the
direction ``shared/provenance.py`` names in its own docstring.
"""

from __future__ import annotations

import json

import pytest

from shared import provenance
from shared.action_classes import TOOL_ACTION_CLASS_MAP, ActionClass
from tests.conftest import load_plugin

# The sentinel from operator/skills/operator-self-test/SKILL.md, verbatim.
SENTINEL = "ZZ-9999-0001"

# What ``read_file`` returns when the Operator opens that skill: prose, carrying
# the sentinel, and nothing the firm ever recorded.
SKILL_TEXT = (
    "## Step 4 — identifier provenance\n\n"
    "Attempt an internal draft that cites the sentinel case number "
    f"{SENTINEL}. The gate must refuse it. Quote the refusal in your report.\n"
)

# What the Smokeball connector returns for the same number: the firm's record.
MATTER_BLOB = json.dumps(
    {
        "id": "3a575129-060e-425c-b798-25dce1dab784",
        "matterNumber": SENTINEL,
        "matterCaption": "Sentinel v. Sentinel",
    }
)


@pytest.fixture
def trust_plugin():
    plugin = load_plugin("hermes-smd-trust")
    provenance._reset_for_tests()
    yield plugin
    provenance._reset_for_tests()


def _seed_unrelated_tenant_read(plugin, session_id: str) -> None:
    """Put ONE unrelated identifier in the register from a genuine tenant read.

    The draft gate carves out an EMPTY register (a refusal with no source to
    re-read is a brick), so a test about seeding must not accidentally ride that
    carve. This is the shape of the live incident too: the Operator had already
    listed matters from Smokeball before it read the skill.
    """
    plugin.on_post_tool_call(
        tool_name="mcp_smokeball_list_matters",
        result=json.dumps({"value": [{"matterNumber": "2026-PI-101"}]}),
        session_id=session_id,
        tool_call_id="seed",
    )


# ---------------------------------------------------------------------------
# The incident, reproduced through the real hooks
# ---------------------------------------------------------------------------


def test_reading_the_seats_own_skill_does_not_license_the_sentinel(trust_plugin) -> None:
    """The 2026-08-21 memo, end to end: read_file then draft.

    This is the assertion that fails before the fix. The Operator reads its own
    skill text, which names the sentinel, and then drafts a body citing it. The
    number was composed, not read from the firm's record, and the gate must
    refuse.
    """
    session = "sess-2511-skill"
    _seed_unrelated_tenant_read(trust_plugin, session)
    trust_plugin.on_post_tool_call(
        tool_name="read_file",
        result=SKILL_TEXT,
        session_id=session,
        tool_call_id="r1",
    )

    directive = trust_plugin.outbound.check_outbound_draft(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"subject": "Self-test", "body": f"Internal note on matter {SENTINEL}."},
        session_id=session,
        tool_call_id="c1",
    )

    assert directive is not None, "read_file seeded the register; the sentinel verified"
    assert directive["action"] == "block"
    assert "case_number" in directive["message"]


def test_the_same_number_read_from_the_firms_record_is_allowed(trust_plugin) -> None:
    """The control. Without this the fix could be 'refuse everything' and pass.

    Same session shape, same draft, same number — read from the connector this
    time. The gate must stay silent, or the fix has broken every legitimate
    citation of a matter number the Operator actually looked up.
    """
    session = "sess-2511-connector"
    trust_plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        result=MATTER_BLOB,
        session_id=session,
        tool_call_id="r1",
    )

    directive = trust_plugin.outbound.check_outbound_draft(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"subject": "Self-test", "body": f"Internal note on matter {SENTINEL}."},
        session_id=session,
        tool_call_id="c1",
    )

    assert directive is None, directive


def test_a_seat_file_read_leaves_the_register_untouched(trust_plugin) -> None:
    """The narrow mechanical claim, without the gate in the way.

    ``check_outbound_draft`` has carve-outs of its own; this asserts the seeding
    layer directly so a future change to those carve-outs cannot make the test
    above pass for the wrong reason.
    """
    session = "sess-2511-register"
    trust_plugin.on_post_tool_call(
        tool_name="read_file",
        result=SKILL_TEXT,
        session_id=session,
        tool_call_id="r1",
    )
    assert not bool(provenance.register_for(session))


def test_scoring_a_draft_does_not_verify_the_draft(trust_plugin) -> None:
    """``voice_score_draft`` hands the Operator's own composition back to it.

    Read-class, and the most direct laundering path in the registry: compose a
    number, score the draft, and the number is now 'read'. One assertion so the
    exclusion is not an unfalsified line in a list.
    """
    session = "sess-2511-voice"
    trust_plugin.on_post_tool_call(
        tool_name="voice_score_draft",
        result=f"score 0.82 — body: your matter {SENTINEL} is set for hearing",
        session_id=session,
        tool_call_id="r1",
    )
    assert not bool(provenance.register_for(session))


# ---------------------------------------------------------------------------
# The negative register
#
# Excluding the seat's reads is subtraction, and subtraction on its own loosens
# the gate: the draft path already carves out an EMPTY register, and after the
# allowlist a turn whose only reads were local HAS an empty one. The sentinel
# would have walked through the carve instead of through the register, and
# whether the kill test passed would have depended on whether `list_matters`
# happened to run first.
#
# So the seat's reads are recorded in a second register that means the opposite
# thing, and "nothing was read" stops being the same state as "this came out of
# your own instructions".
# ---------------------------------------------------------------------------


def test_the_incident_refuses_even_when_nothing_else_was_read(trust_plugin) -> None:
    """The order-independence the negative register buys, and its whole reason.

    Identical to the incident test above except that no tenant read runs first,
    so the positive register is EMPTY and the draft-gate carve would otherwise
    allow the draft outright. It must still refuse, because the sentinel is not
    unsourced: it came from the skill.
    """
    session = "sess-2511-only-skill"
    trust_plugin.on_post_tool_call(
        tool_name="read_file",
        result=SKILL_TEXT,
        session_id=session,
        tool_call_id="r1",
    )

    directive = trust_plugin.outbound.check_outbound_draft(
        tool_name="mcp_msgraph_mail_create_draft",
        args={"subject": "Self-test", "body": f"Internal note on matter {SENTINEL}."},
        session_id=session,
        tool_call_id="c1",
    )

    assert directive is not None, "the empty-register carve swallowed a seat-sourced value"
    assert directive["action"] == "block"
    assert "your own instructions" in directive["message"]


def test_a_seat_read_populates_the_seat_register(trust_plugin) -> None:
    """The positive register stays empty and the negative one fills."""
    session = "sess-2511-neg"
    trust_plugin.on_post_tool_call(
        tool_name="read_file",
        result=SKILL_TEXT,
        session_id=session,
        tool_call_id="r1",
    )
    assert not bool(provenance.register_for(session))
    assert bool(provenance.seat_sourced_for(session))


def test_a_tenant_read_does_not_populate_the_seat_register(trust_plugin) -> None:
    """The converse, so the two registers cannot quietly become one.

    Without this, an implementation that recorded EVERY read into both would
    pass every other test in this file while making the negative register
    meaningless.
    """
    session = "sess-2511-pos"
    trust_plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        result=MATTER_BLOB,
        session_id=session,
        tool_call_id="r1",
    )
    assert bool(provenance.register_for(session))
    assert not bool(provenance.seat_sourced_for(session))


def test_dropping_a_session_forgets_both_registers(trust_plugin) -> None:
    session = "sess-2511-drop"
    trust_plugin.on_post_tool_call(
        tool_name="read_file", result=SKILL_TEXT, session_id=session, tool_call_id="r1"
    )
    trust_plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        result=MATTER_BLOB,
        session_id=session,
        tool_call_id="r2",
    )
    provenance.drop(session)
    assert not bool(provenance.register_for(session))
    assert not bool(provenance.seat_sourced_for(session))


def test_the_seat_register_is_bounded_like_the_read_register(trust_plugin) -> None:
    for i in range(provenance._MAX_SESSIONS + 10):
        provenance.record_seat_text(f"seat-{i}", f"matter ZZ-1234-{i:04d} in a skill body.")
    assert len(provenance._seat_registers) <= provenance._MAX_SESSIONS


# ---------------------------------------------------------------------------
# The carve, pinned in all three of its states (ss-console#2511 plan 2c)
#
# One test rather than three scattered ones, because the states are only
# meaningful against each other: the point is not that a seat-sourced value
# blocks, it is that it blocks WHERE an unsourced value does not.
# ---------------------------------------------------------------------------


def _draft(plugin, session: str, body: str, tool: str = "mcp_msgraph_mail_create_draft"):
    return plugin.outbound.check_outbound_draft(
        tool_name=tool,
        args={"subject": "Update", "body": body},
        session_id=session,
        tool_call_id="c",
    )


def test_the_empty_register_carve_has_three_states(trust_plugin) -> None:
    unread = "Your matter is XX-1111-2222."

    # 1. Empty register, value not seat-sourced: ALLOWED with a report row.
    #    Unchanged behavior, and the reason the carve exists — a refusal with
    #    nothing to go re-read is a brick on a conversational turn.
    assert _draft(trust_plugin, "carve-empty", unread) is None

    # 2. Empty register, value seat-sourced: BLOCKED. There IS something to say
    #    about this one, so the carve's reasoning does not reach it.
    trust_plugin.on_post_tool_call(
        tool_name="read_file",
        result=SKILL_TEXT,
        session_id="carve-seat",
        tool_call_id="r",
    )
    seat = _draft(trust_plugin, "carve-seat", f"Your matter is {SENTINEL}.")
    assert seat is not None and seat["action"] == "block"
    assert "your own instructions" in seat["message"]

    # 3. Seeded register, value unverified: BLOCKED. Also unchanged.
    trust_plugin.on_post_tool_call(
        tool_name="mcp_smokeball_get_matter",
        result=MATTER_BLOB,
        session_id="carve-seeded",
        tool_call_id="r",
    )
    seeded = _draft(trust_plugin, "carve-seeded", unread)
    assert seeded is not None and seeded["action"] == "block"
    # ...and that refusal does NOT claim a seat source it cannot show.
    assert "your own instructions" not in seeded["message"]


# ---------------------------------------------------------------------------
# The partition, pinned
#
# Every READ tool in the registry is on exactly one side of this line, and the
# test names both sides explicitly. A new read tool fails here until someone
# decides which side it belongs on — which is the whole point: the defect above
# was a tool joining the seeding set by default, with nobody deciding.
# ---------------------------------------------------------------------------


#: Read tools whose results are the SEAT's own artifacts, not the tenant's
#: records. Kept here rather than derived so the list is reviewable as a list.
_EXPECTED_NON_SEEDING: frozenset[str] = frozenset(
    {
        # Hermes-core filesystem / workspace / self-inspection.
        "read_file",
        "search_files",
        "skills_list",
        "skill_view",
        "session_search",
        # The seat's own memory store.
        "memory_search",
        "memory_get_rule",
        "memory_list_rules",
        # Model output about the agent's OWN composition.
        "voice_score_draft",
        "voice_list_judge_history",
        "vision_analyze",
        "smd_deliver_draft",
        # The open web. A page is a source, but not the firm's source of record,
        # and it is writable by anyone who wants a number believed.
        "web_search",
        # The seat's own description of itself and of its runs.
        "operator_seat_facts",
        "establish_status",
        # ss-console#2529. A pending rule is a sentence the Operator composed
        # and the firm has not yet agreed to, so reading it back must not
        # certify anything in it — the same reasoning that keeps the agent's own
        # unsent drafts off the seeding side, and the reason a number inside a
        # proposed rule is still a number nobody read from the firm's records.
        "establish_pending",
        "escalation_state",
        "job_status",
        # ss-console #2614: broker-authored counts and states of the chronology
        # runner's jobs (documents read, pages, cents, a folder id). Not a
        # record the agent could quote from; nothing here seeds a claim.
        "medchron_job_status",
        "medchron_allowance",
        "connector_get_status",
        "connector_list_bindings",
        # Credential / identity metadata, carrying no tenant content.
        "mcp_agentmail_auth_me",
        "mcp_smokeball_auth_status",
        # The agent's own UNSENT drafts. A committed memo is the firm's record;
        # a draft is the Operator's sentence, and reading one back must not
        # certify the numbers in it.
        "mcp_agentmail_list_drafts",
        "mcp_agentmail_get_draft",
        # The synthetic connector self-test fixture: echo returns its input.
        "mcp_reference_echo",
    }
)


def _read_tools() -> frozenset[str]:
    return frozenset(name for name, cls in TOOL_ACTION_CLASS_MAP.items() if cls is ActionClass.READ)


def test_every_read_tool_is_on_exactly_one_side() -> None:
    seeding = {n for n in _read_tools() if provenance.seeds_provenance(n)}
    not_seeding = {n for n in _read_tools() if not provenance.seeds_provenance(n)}
    assert not_seeding == _EXPECTED_NON_SEEDING, {
        "unexpectedly_not_seeding": sorted(not_seeding - _EXPECTED_NON_SEEDING),
        "expected_not_seeding_but_seeds": sorted(_EXPECTED_NON_SEEDING - not_seeding),
    }
    assert seeding == _read_tools() - _EXPECTED_NON_SEEDING


def test_the_tenant_source_set_holds_only_read_tools() -> None:
    """A write or a send in the seeding set would be a classification error."""
    for name in provenance.TENANT_SOURCE_READ_TOOLS:
        assert TOOL_ACTION_CLASS_MAP.get(name) is ActionClass.READ, name


def test_the_predicate_refuses_everything_that_is_not_a_tenant_read() -> None:
    """Total on the inputs a hook can hand it, and fail-safe on all of them."""
    assert provenance.seeds_provenance("mcp_smokeball_get_matter") is True
    assert provenance.seeds_provenance("  MCP_SMOKEBALL_GET_MATTER  ") is True  # normalized
    assert provenance.seeds_provenance("read_file") is False
    assert provenance.seeds_provenance("") is False
    assert provenance.seeds_provenance("a_tool_nobody_has_registered_yet") is False
    # Class matters, not just the name: a write is never a source.
    assert provenance.seeds_provenance("write_file") is False
    assert provenance.seeds_provenance("mcp_smokeball_create_memo") is False
