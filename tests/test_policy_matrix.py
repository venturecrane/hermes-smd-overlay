"""Seat policy matrix: every live seat resolved by the REAL resolver, then the gates.

``tests/contract/policy_inputs.json`` is the ss-console projection (each live
seat's ``seat.role`` and that role's ``policy:`` block, verbatim).
``tests/contract/policy_matrix_expected.json`` is hand-maintained: what each seat
is allowed, seat x gate. A change to what any seat allows is a diff to that file
in review; a change that loosens a seat without saying why fails here.

The resolver is driven exactly as on a seat: a customer.yaml at
``SMD_CUSTOMER_YAML_PATH`` and a roles dir at ``ROLES_DIR``. The gate fixtures go
through ``outbound._check_identifiers`` and the output checklist, not a model of
them.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
import yaml

from shared import output_checklist, policy_profile, provenance
from tests.conftest import load_plugin

CONTRACT = Path(__file__).parent / "contract"
INPUTS = json.loads((CONTRACT / "policy_inputs.json").read_text())["seats"]
EXPECTED_DOC = json.loads((CONTRACT / "policy_matrix_expected.json").read_text())
EXPECTED = EXPECTED_DOC["seats"]
SEATS = sorted(INPUTS)
LAW_SEATS = ("ashton-price", "pilot-smokeball")
HOUSEHOLD_SEAT = "captain-mark"

MATTER_BLOB = json.dumps({"id": "m1", "matterNumber": "2026-PI-101"})
SEND_TOOL = "mcp_agentmail_send_message"


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _occurrence(offset_days: int) -> datetime.date:
    when = _today() + datetime.timedelta(days=offset_days)
    if (when.month, when.day) == (2, 29):  # so .replace(year=...) always exists
        when += datetime.timedelta(days=1)
    return when


def _write_seat(tmp_path: Path, slug: str, monkeypatch, *, role_policy=None, role=None) -> None:
    """Materialize one seat as the image would see it and point the env at it."""
    entry = INPUTS.get(slug, {"role": role, "role_policy": role_policy})
    role = entry["role"] if role is None else role
    role_policy = entry["role_policy"] if role_policy is None else role_policy
    roles = tmp_path / "roles"
    roles.mkdir(exist_ok=True)
    seat: dict = {"kind": "customer"}
    if role:
        seat["role"] = role
        manifest: dict = {"role": role, "persona": "persona.md"}
        if role_policy is not None:
            manifest["policy"] = role_policy
        (roles / role).mkdir(exist_ok=True)
        (roles / role / "role.yaml").write_text(yaml.safe_dump(manifest))
    cfg = tmp_path / f"{slug}.yaml"
    cfg.write_text(yaml.safe_dump({"customer_id": slug, "vertical": "mixed", "seat": seat}))
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(cfg))
    monkeypatch.setenv("ROLES_DIR", str(roles))


def _row(policy: policy_profile.Policy) -> dict:
    return {
        "identifier_dates": (
            "verify_recurring"
            if policy.recurring_dates == policy_profile.RECURRING_VERIFY
            else "block"
        ),
        "own_sources": sorted(policy.provenance_sources),
        "checklist": policy.checklist_profile,
        "floors": "block",
    }


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


def test_the_matrix_names_exactly_the_live_seats() -> None:
    assert sorted(EXPECTED) == SEATS


def test_the_platform_default_row_is_the_resolver_default() -> None:
    assert _row(policy_profile.PLATFORM_DEFAULT) == EXPECTED_DOC["platform_default"]


@pytest.mark.parametrize("slug", SEATS)
def test_every_seat_resolves_to_its_expected_row(slug, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    _write_seat(tmp_path, slug, monkeypatch)
    policy = policy_profile.current()
    assert not policy.source.startswith("fallback:"), (slug, policy.source)
    assert _row(policy) == EXPECTED[slug], f"{slug} resolves to {_row(policy)}"


@pytest.mark.parametrize("slug", SEATS)
def test_a_loosened_seat_carries_its_rationale_and_adr(slug, tmp_path, monkeypatch) -> None:
    _write_seat(tmp_path, slug, monkeypatch)
    policy = policy_profile.current()
    if policy.loosened:
        assert policy.rationale and policy.adr, slug
        assert EXPECTED[slug] != EXPECTED_DOC["platform_default"]


def test_an_uncited_loosening_resolves_strict(tmp_path, monkeypatch) -> None:
    """The falsifier for the citation rule: drop the adr and the seat is default."""
    loose = dict(INPUTS[HOUSEHOLD_SEAT]["role_policy"])
    loose.pop("adr")
    _write_seat(tmp_path, "uncited", monkeypatch, role="couples", role_policy=loose)
    policy = policy_profile.current()
    assert policy.source == "fallback:couples:uncited"
    assert _row(policy) == EXPECTED_DOC["platform_default"]


@pytest.mark.parametrize(
    "bad",
    [
        {"identifier_gate": {"ssn": "report"}, "rationale": "x", "adr": "y"},
        {"identifier_gate": {"recurring_dates": "report"}, "rationale": "x", "adr": "y"},
        {"output_checklist": {"profile": "none"}, "rationale": "x", "adr": "y"},
        {"floors": "off", "rationale": "x", "adr": "y"},
        "verify_by_month_day",
    ],
)
def test_a_malformed_policy_is_the_platform_default(bad, tmp_path, monkeypatch) -> None:
    _write_seat(tmp_path, "bad", monkeypatch, role="couples", role_policy=bad)
    policy = policy_profile.current()
    assert policy.source.startswith("fallback:")
    assert _row(policy) == EXPECTED_DOC["platform_default"]


def test_a_missing_role_file_is_the_platform_default(tmp_path, monkeypatch) -> None:
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    monkeypatch.setenv("ROLES_DIR", str(tmp_path / "nowhere"))
    policy = policy_profile.current()
    assert policy.source == "fallback:couples:no_role_file"
    assert _row(policy) == EXPECTED_DOC["platform_default"]


def test_a_mixed_vertical_with_no_role_is_strict(tmp_path, monkeypatch) -> None:
    _write_seat(tmp_path, "brokery", monkeypatch)
    assert _row(policy_profile.current()) == EXPECTED_DOC["platform_default"]


def test_the_floors_are_not_relaxable() -> None:
    from shared.identifier_filter import IdKind

    assert {
        IdKind.SSN,
        IdKind.A_NUMBER,
        IdKind.RECEIPT_NUMBER,
        IdKind.CASE_NUMBER,
        IdKind.PAIR,
    } <= policy_profile.FLOOR_KINDS
    assert policy_profile.RELAXABLE_KINDS == frozenset({IdKind.DATE})


def test_the_checklist_profiles_agree_across_modules() -> None:
    assert policy_profile.PROFILE_VALUES == output_checklist.PROFILES


# ---------------------------------------------------------------------------
# The gates, per seat
# ---------------------------------------------------------------------------


@pytest.fixture
def trust(monkeypatch):
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    plugin = load_plugin("hermes-smd-trust")
    provenance._reset_for_tests()

    class _Sink:
        def __init__(self) -> None:
            self.rows: list[str] = []

        def execute(self, sql: str, *params):
            if "IDENTIFIER_UNVERIFIED" in params:
                self.rows.append(params[-1])
            return 1

    sink = _Sink()
    monkeypatch.setattr(plugin.outbound, "_AUDIT_CLIENT", sink)
    monkeypatch.setattr(plugin.outbound, "_AUDIT_CUSTOMER_SLUG", "seat")
    monkeypatch.setattr(plugin.outbound, "_AUDIT_WIRED", True)
    yield plugin, sink
    provenance._reset_for_tests()


def _read(plugin, tool: str, result: str, session: str = "s") -> None:
    plugin.on_post_tool_call(tool_name=tool, result=result, session_id=session, tool_call_id="r")


def _read_birthday(plugin, month_day: str) -> None:
    content = f"---\nkind: date\ntitle: Sam's birthday\ndate: {month_day}\nyear: 1990\n---\n"
    _read(
        plugin,
        "record_store_read",
        json.dumps({"store": "dates", "name": "date-sam.md", "content": content}),
    )


def _send(plugin, body: str, session: str = "s"):
    return plugin.outbound._check_identifiers(
        body=body,
        gate="send",
        session_id=session,
        tool_name=SEND_TOOL,
        tool_call_id="c",
        vertical=None,
        cohort=None,
    )


@pytest.mark.parametrize("slug", LAW_SEATS)
def test_an_unread_computed_date_is_refused_on_a_law_seat(slug, trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    _read(plugin, "mcp_smokeball_get_matter", MATTER_BLOB)
    deadline = _occurrence(30)
    directive = _send(plugin, f"The response is due {deadline.isoformat()}.")
    assert directive and directive["action"] == "block"


@pytest.mark.parametrize("slug", LAW_SEATS)
def test_a_record_store_birthday_does_not_verify_on_a_law_seat(slug, trust, tmp_path, monkeypatch):
    """The same read that verifies on the household seat verifies nothing here."""
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    when = _occurrence(40)
    _read_birthday(plugin, when.strftime("%m-%d"))
    directive = _send(plugin, f"Sam's birthday is {when.isoformat()}.")
    assert directive and directive["action"] == "block"


def test_a_yearly_birthday_is_allowed_on_the_household_seat_after_a_store_read(
    trust, tmp_path, monkeypatch
):
    plugin, sink = trust
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    when = _occurrence(40)
    body = f"Sam's birthday is {when.isoformat()}."
    # Before the read it is refused: verify_by_month_day needs a record.
    _read(plugin, "mcp_agentmail_get_message", json.dumps({"text": "hello"}))
    refused = _send(plugin, body)
    assert refused and refused["action"] == "block"
    _read_birthday(plugin, when.strftime("%m-%d"))
    assert _send(plugin, body) is None
    assert '"policy_mode":"verify_recurring"' in sink.rows[0]


def test_next_years_occurrence_verifies_but_a_later_year_does_not(trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    when = _occurrence(40)
    _read_birthday(plugin, when.strftime("%m-%d"))
    next_year = when.replace(year=when.year + 1)
    assert _send(plugin, f"And next year, {next_year.isoformat()}.") is None
    far = when.replace(year=when.year + 3)
    directive = _send(plugin, f"In {far.year}: {far.isoformat()}.")
    assert directive and directive["action"] == "block"


def test_an_invented_trip_date_is_refused_on_the_household_seat(trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    _read_birthday(plugin, _occurrence(40).strftime("%m-%d"))
    trip = _occurrence(55)
    directive = _send(plugin, f"Your flight leaves {trip.isoformat()}.")
    assert directive and directive["action"] == "block"


def test_a_stored_trip_is_not_a_recurring_date(trust, tmp_path, monkeypatch):
    """A dated trip record for this year is a one-off: next year's same day is invented."""
    plugin, _ = trust
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    trip = _occurrence(55)
    _read(
        plugin,
        "record_store_read",
        json.dumps(
            {"store": "trips", "name": "trip-x.md", "content": f"depart: {trip.isoformat()}\n"}
        ),
    )
    assert _send(plugin, f"You leave {trip.isoformat()}.") is None  # read verbatim
    later = trip.replace(year=trip.year + 1)
    directive = _send(plugin, f"Again on {later.isoformat()}.")
    assert directive and directive["action"] == "block"


@pytest.mark.parametrize("slug", SEATS)
@pytest.mark.parametrize(
    "body",
    [
        "Her SSN is 123-45-6789.",
        "Filed under ZZ-9999-0001.",
    ],
    ids=["ssn", "case_number"],
)
def test_the_floors_refuse_on_every_seat(slug, body, trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    _read(plugin, "mcp_smokeball_get_matter", MATTER_BLOB)
    _read_birthday(plugin, _occurrence(40).strftime("%m-%d"))
    directive = _send(plugin, body)
    assert directive and directive["action"] == "block", slug


@pytest.mark.parametrize("slug", SEATS)
def test_a_pair_refuses_on_every_seat_even_with_a_recurring_date(
    slug, trust, tmp_path, monkeypatch
):
    """The date half verifies on the household seat; the pair is a floor and does not."""
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    _read(plugin, "mcp_smokeball_get_matter", MATTER_BLOB)
    when = _occurrence(40)
    _read_birthday(plugin, when.strftime("%m-%d"))
    directive = _send(plugin, f"Matter 2026-PI-101 hearing on {when.isoformat()}.")
    assert directive and directive["action"] == "block", slug


@pytest.mark.parametrize("slug", SEATS)
def test_a_judged_pair_refuses_on_every_seat(slug, trust, tmp_path, monkeypatch):
    """With associations in the register a real PAIR hit is judged, and refuses."""
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    when = _occurrence(40)
    _read(
        plugin,
        "mcp_smokeball_get_matter",
        json.dumps({"matterNumber": "2026-PI-101", "openedDate": "2026-01-05"}),
    )
    # The date is read verbatim too, from a different record, so only the pair is unread.
    _read(plugin, "mcp_smokeball_get_matter", json.dumps({"note": f"Party on {when.isoformat()}"}))
    _read_birthday(plugin, when.strftime("%m-%d"))
    directive = _send(plugin, f"Matter 2026-PI-101 hearing on {when.isoformat()}.")
    assert directive and directive["action"] == "block", slug


def test_a_role_policy_naming_a_floor_cannot_lower_it(trust, tmp_path, monkeypatch):
    plugin, _ = trust
    policy = {
        "identifier_gate": {"recurring_dates": "verify_by_month_day", "ssn": "report"},
        "rationale": "x",
        "adr": "y",
    }
    _write_seat(tmp_path, "falsifier", monkeypatch, role="couples", role_policy=policy)
    assert policy_profile.current().source.startswith("fallback:")
    directive = _send(plugin, "Her SSN is 123-45-6789.")
    assert directive and directive["action"] == "block"


@pytest.mark.parametrize("slug", SEATS)
def test_the_checklist_profile_per_seat(slug, trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    body = "Happy birthday, Sam! See you at DINNER tonight."
    rules = {
        v.rule
        for v in output_checklist.check(
            body, output_checklist.EVERY_OUTPUT, plugin.outbound._checklist_profile()
        )
    }
    if EXPECTED[slug]["checklist"] == "household":
        assert not rules & {"exclamation", "caps_emphasis"}, rules
    else:
        assert {"exclamation", "caps_emphasis"} <= rules, rules
    # Neither profile lets an internal id through.
    leaked = output_checklist.check(
        "Done, task 98570c78.", output_checklist.EVERY_OUTPUT, plugin.outbound._checklist_profile()
    )
    assert "internal_id" in {v.rule for v in leaked}


# ---------------------------------------------------------------------------
# The fleet lever and the marker
# ---------------------------------------------------------------------------


def test_the_lever_outranks_policy_and_the_marker_says_so(tmp_path, monkeypatch) -> None:
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    assert policy_profile.marker() == (
        "policy_profile role=couples identifier=verify_recurring checklist=household "
        "sources=calendar_invite_list,couples_settings_read source=role:couples lever=unset"
    )
    assert policy_profile.main(["--check-lever"]) == 0
    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", "report")
    assert policy_profile.current().identifier_mode == policy_profile.MODE_REPORT_LEVER
    assert policy_profile.marker().endswith("lever=set")
    assert policy_profile.main(["--check-lever"]) == 1


# ---------------------------------------------------------------------------
# recurring_month_days
# ---------------------------------------------------------------------------


def test_recurring_month_days_reads_the_shapes_a_store_holds() -> None:
    today = datetime.date(2026, 10, 19)
    text = json.dumps(
        {
            "content": (
                "date: 06-14\nyear: 2019\n"
                "anniversary: 3/2\n"
                "met: May 9\n"
                "born: 1988-11-30\n"
                "depart: 2026-12-20\n"
                "return: 2027-01-03\n"
            )
        }
    )
    assert policy_profile.recurring_month_days(text, today=today) == frozenset(
        {"06-14", "03-02", "05-09", "11-30"}
    )


def test_recurring_month_days_reads_a_store_listing() -> None:
    listing = json.dumps({"records": [{"name": "date-a.md", "date": "02-29"}], "count": 1})
    assert policy_profile.recurring_month_days(listing) == frozenset({"02-29"})


def test_only_record_store_reads_seed_the_recurring_register() -> None:
    provenance._reset_for_tests()
    provenance.record_recurring_read("s", "mcp_agentmail_get_message", "date: 06-14\n")
    assert provenance.recurring_for("s") == frozenset()
    provenance.record_recurring_read("s", "record_store_read", "date: 06-14\n")
    assert provenance.recurring_for("s") == frozenset({"06-14"})
    provenance.drop("s")
    assert provenance.recurring_for("s") == frozenset()


# ---------------------------------------------------------------------------
# Role-scoped seat-owned sources (identifier_gate.provenance_sources)
# ---------------------------------------------------------------------------


def _calendar_listing(when: datetime.date) -> str:
    return json.dumps(
        {"events": [{"uid": "u1", "title": "Kids weekend", "start": when.isoformat()}]}
    )


def test_the_own_calendar_stays_a_non_source_fleet_wide() -> None:
    """The fleet pin (tests/test_provenance_sources.py) is unchanged: only a
    role naming the tool makes it seed."""
    assert not provenance.seeds_provenance("calendar_invite_list")
    assert not provenance.seeds_provenance("couples_settings_read")


@pytest.mark.parametrize("slug", SEATS)
def test_an_own_calendar_read_seeds_only_where_the_role_names_it(
    slug, trust, tmp_path, monkeypatch
):
    plugin, _ = trust
    _write_seat(tmp_path, slug, monkeypatch)
    when = _occurrence(4)
    _read(plugin, "calendar_invite_list", _calendar_listing(when))
    directive = _send(plugin, f"Reminder: kids weekend starts {when.isoformat()}.")
    if "calendar_invite_list" in EXPECTED[slug]["own_sources"]:
        assert directive is None, slug
    else:
        assert directive and directive["action"] == "block", slug


def test_an_invented_date_beside_a_calendar_read_is_still_refused(trust, tmp_path, monkeypatch):
    plugin, _ = trust
    _write_seat(tmp_path, HOUSEHOLD_SEAT, monkeypatch)
    _read(plugin, "calendar_invite_list", _calendar_listing(_occurrence(4)))
    directive = _send(plugin, f"Your dinner reservation is {_occurrence(9).isoformat()}.")
    assert directive and directive["action"] == "block"


@pytest.mark.parametrize(
    "sources",
    [["read_file"], ["calendar_invite_list", "calendar_invite_list"], "calendar_invite_list", [1]],
    ids=["off_list", "duplicate", "not_a_list", "not_strings"],
)
def test_a_bad_sources_list_is_the_platform_default(sources, tmp_path, monkeypatch) -> None:
    policy = {
        "identifier_gate": {"provenance_sources": sources},
        "rationale": "x",
        "adr": "y",
    }
    _write_seat(tmp_path, "bad-sources", monkeypatch, role="couples", role_policy=policy)
    resolved = policy_profile.current()
    assert resolved.source == "fallback:couples:provenance_sources"
    assert _row(resolved) == EXPECTED_DOC["platform_default"]


def test_naming_a_source_is_a_loosening_that_needs_a_citation(tmp_path, monkeypatch) -> None:
    policy = {"identifier_gate": {"provenance_sources": ["couples_settings_read"]}}
    _write_seat(tmp_path, "uncited-sources", monkeypatch, role="couples", role_policy=policy)
    assert policy_profile.current().source == "fallback:couples:uncited"
