"""Tests for ``operator_seat_facts`` (ss-console#2222 card rows 1 + 7).

Every check names the state it must FAIL on. A grounding tool whose tests pass
on the improvising configuration would measure nothing — the defect it closes is
precisely a fluent answer that looks right.

 T2b REGISTRATION CARRIES NO ``requires_env`` AND NO ``check_fn``. Falsifier: add
     either and the test fails. This is the ``vision_analyze`` failure mode as a
     unit test — that tool is named in ``platform_toolsets.webhook`` on the pilot
     and absent from the live surface because a runtime check dropped it, with
     nothing in the logs.
 T3  EVERY SECTION IS ALWAYS PRESENT, and a ``read: false`` section has a
     matching ``unreadable[]`` entry. Falsifier: omit a section on failure — an
     omitted section reads as "nothing there".
 T4  CONTENT CEILING. Matter names, client names, and matter numbers are seeded
     into every source the handler reads; none may appear anywhere in the
     serialized result. Falsifier: pass a matter field through the routine mapper.
 T5  RUN-HISTORY STRIP. ``last_run_at`` / ``last_status`` / ``next_run_at`` are
     dropped at the read boundary. Falsifier: reuse ``config_snapshot._read_cron_jobs``
     unfiltered.
 T6  SCHEDULE PROSE, CLOSED SET. Three shapes translate, everything else returns
     None; 0 and 12 both render as 12. Falsifier: add a fourth translation shape.
 T7  DISCREPANCY STATES. Four pairings each produce their own state and the two
     layers are never reconciled. Falsifier: make ``authored_no_job`` fall back
     to ``scheduled``.
 T8  PER-SOURCE FAULT ISOLATION. Each source, made to raise independently, yields
     ``read: false`` + an ``unreadable[]`` entry and never an exception out of the
     handler. Falsifier: let the spec-manifest read propagate.

Plus the three-state voice contract (the ss#2234 ``manifest_state`` distinction):
"nothing is installed" and "I cannot see my spec tree" must never render as each
other, which is the one thing ``load_entries``' collapsed ``{}`` cannot express.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_plugin

# --------------------------------------------------------------------------- #
# Fixtures: a faithful-enough config + spec manifest, injectable per test.
# --------------------------------------------------------------------------- #


class _FakeConfig:
    def __init__(self, raw: dict):
        self._raw = raw

    @property
    def raw(self) -> dict:
        return self._raw

    @property
    def personas(self) -> list:
        return self._raw.get("personas") or []

    @property
    def customer_name(self) -> str:
        return self._raw.get("customer_name") or ""

    @property
    def connectors(self) -> dict:
        return self._raw.get("connectors") or {}

    @property
    def output_classes(self) -> dict:
        return self._raw.get("output_classes") or {}


class _FakeEntry:
    def __init__(self, output_class: str, prop: str):
        self.output_class = output_class
        self.prop = prop


class _FakeManifest:
    """Stands in for ``shared.spec_manifest`` with the three states it exposes."""

    STATE_OK = "ok"
    STATE_ABSENT = "absent"
    STATE_UNREADABLE = "unreadable"

    def __init__(self, state: str = "ok", installed: tuple[tuple[str, str], ...] = ()):
        self._state = state
        self._installed = installed
        self.verify_result = True

    def manifest_state(self) -> str:
        return self._state

    def entries_for_class(self, output_class: str) -> list:
        return [_FakeEntry(c, p) for (c, p) in self._installed if c == output_class]

    def verify(self, entry) -> bool:
        return self.verify_result


def _raw_config(**overrides) -> dict:
    base = {
        "customer_name": "Example Firm",
        "personas": [
            {
                "slug": "operator",
                "name": "Operator",
                "title": "AI Case Coordinator",
                "skills": [
                    {
                        "name": "discovery-served-watch",
                        "enabled": True,
                        "initiation": {"manual": True, "scheduled": False, "webhook": True},
                    },
                    {
                        "name": "daily-needs-you-digest",
                        "enabled": True,
                        "initiation": {"manual": True, "scheduled": True, "webhook": False},
                    },
                    {
                        "name": "motion-calendar-tracker",
                        "enabled": True,
                        "initiation": {"manual": False, "scheduled": True, "webhook": False},
                    },
                    {
                        "name": "retired-routine",
                        "enabled": False,
                        "initiation": {"manual": True, "scheduled": False, "webhook": False},
                    },
                    {
                        "name": "operator-introduce",
                        "enabled": True,
                        "initiation": {"manual": True, "scheduled": False, "webhook": False},
                    },
                ],
                "cron": [
                    {"skill": "daily-needs-you-digest", "schedule": "23 6 * * 1-5"},
                    {"skill": "motion-calendar-tracker", "schedule": "27 7 * * 1-5"},
                ],
            }
        ],
        "connectors": {
            "PracticeManagement": {"adapter": "smokeball", "enabled": True},
            "Email": {"adapter": "agentmail", "enabled": True},
            "Calendar": {"adapter": "google", "enabled": False},
        },
        "webhook_triggers": [
            {
                "source": "smokeball",
                "event_type": "matter.updated",
                "skill": "discovery-served-watch",
            }
        ],
        "routine_names": {
            "discovery-served-watch": "Served discovery caught",
            "daily-needs-you-digest": 'Daily "what needs you"',
        },
        "output_classes": {
            "work_product": {"voice_spec": "expected"},
            "staff": {"voice_spec": "none"},
        },
        "voice_cohorts": {"cohorts": ["client", "adjuster"]},
    }
    base.update(overrides)
    return base


def _write_jobs(tmp_path, jobs: list[dict], slug: str = "operator"):
    path = tmp_path / "profiles" / slug / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


@pytest.fixture
def sf():
    return load_plugin("hermes-smd-initiation").seat_facts


def _build(sf, tmp_path, *, raw=None, manifest=None, depth="introduction", loader=None):
    cfg = _FakeConfig(raw if raw is not None else _raw_config())
    return sf.build_facts(
        depth=depth,
        config_loader=loader or (lambda: cfg),
        spec_manifest_module=manifest or _FakeManifest(),
        home=str(tmp_path),
    )


# --------------------------------------------------------------------------- #
# T2b — registration shape
# --------------------------------------------------------------------------- #


class _RecordingCtx:
    def __init__(self):
        self.tools: list[dict] = []
        self.hooks: dict[str, object] = {}

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def test_seat_facts_registers_with_no_requires_env_and_no_check_fn():
    """T2b. ``register_wrapped_tool`` forwards both straight to
    ``ctx.register_tool``, and ``registry.get_definitions`` drops any tool whose
    check fails — silently, because gateway callers pass ``quiet_mode=True``. A
    ``requires_env`` entry added for tidiness would reproduce exactly what
    happened to ``vision_analyze`` on the pilot: named in the config, absent from
    the surface, nothing in the logs."""
    plugin = load_plugin("hermes-smd-initiation")
    ctx = _RecordingCtx()
    plugin.register(ctx)

    entries = [t for t in ctx.tools if t["name"] == plugin.TOOL_SEAT_FACTS]
    assert len(entries) == 1
    entry = entries[0]
    assert "requires_env" not in entry, "a requires_env entry can drop the tool silently"
    assert "check_fn" not in entry, "a check_fn can drop the tool silently"
    # The wrap must produce the OpenAI function shape or the model sees the tool
    # name with empty parameters (the defect shared/tool_registration.py exists
    # to make impossible).
    assert "parameters" in entry["schema"]
    assert entry["schema"]["parameters"]["properties"]["depth"]["enum"] == list(
        plugin.seat_facts.DEPTHS
    )
    assert entry["schema"]["parameters"]["additionalProperties"] is False


def test_register_still_wires_pre_llm_call_and_adds_no_hook():
    """The tool is not a new hook attachment: ``plugin.yaml``'s ``hooks:`` list
    and ``tests/test_hook_parity.py`` must stay correct without an edit."""
    plugin = load_plugin("hermes-smd-initiation")
    ctx = _RecordingCtx()
    plugin.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call"}


def test_tool_description_carries_both_trigger_phrasings_and_the_prohibitions():
    """The description is the one surface always in front of the model on a
    channel where the skill body is absent. Falsifier: trim it to a summary and
    the procedure stops travelling with the tool."""
    plugin = load_plugin("hermes-smd-initiation")
    desc = plugin._SEAT_FACTS_DESCRIPTION.lower()
    assert "introduce yourself and tell me what you can see" in desc
    assert "walk me through what you'll do each day and week" in desc
    assert "counts" in desc
    assert "run history" in desc
    assert "client names" in desc
    assert "matter identifiers" in desc
    assert "never answer these from memory" in desc


# --------------------------------------------------------------------------- #
# T3 — envelope completeness
# --------------------------------------------------------------------------- #


def test_every_section_is_always_present(sf, tmp_path):
    """T3. An omitted section reads as 'nothing there', which is the one thing an
    honest self-description may never say by accident."""
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path)
    assert facts["schema"] == "operator.seat.facts/v1"
    for section in sf.SECTIONS:
        assert section in facts, section
        assert "read" in facts[section], section
    assert isinstance(facts["unreadable"], list)


def test_every_unread_section_has_an_unreadable_entry(sf, tmp_path):
    """T3's second half. ``matters``/``inbox`` are the deliberate exception: they
    are unread by DESIGN and carry an instruction marker, not a failure."""

    def _boom():
        raise OSError("customer.yaml unreadable")

    facts = _build(sf, tmp_path, loader=_boom)
    for section in sf.SECTIONS:
        assert facts[section]["read"] is False, section
    assert facts["unreadable"], "a total fault must name itself"
    # Still an envelope the model can speak from, not a tool error to paraphrase.
    assert facts["depth"] == "introduction"


def test_matters_and_inbox_are_instruction_markers_not_failures(sf, tmp_path):
    """§1.4: the live observations stay model-driven MCP calls so the audit row
    is the proof that a capability was observed THIS TURN. Falsifier: populate
    them here and 'observed live' becomes something the tool asserts."""
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path)
    assert facts["matters"] == {"read": False, "open_count": None, "reason": sf._OBSERVE_YOURSELF}
    assert facts["inbox"] == {"read": False, "unread_count": None, "reason": sf._OBSERVE_YOURSELF}
    assert "observe this yourself" in facts["matters"]["reason"]


def test_depth_is_echoed_and_an_unknown_depth_falls_back(sf, tmp_path):
    """The depth argument exists to make 'depth 2 asked, depth 1 answered'
    decidable from the ledger. It must round-trip, and never raise."""
    _write_jobs(tmp_path, [])
    assert _build(sf, tmp_path, depth="walkthrough")["depth"] == "walkthrough"
    assert _build(sf, tmp_path, depth="nonsense")["depth"] == "introduction"


def test_connections_reports_the_authored_roster_with_unknown_auth(sf, tmp_path):
    """Labelling authored state 'observed' is the exact fabrication shape this
    tool exists to prevent, so the key is ``declared`` and auth is ``unknown``.
    Disabled connectors are not reported at all."""
    _write_jobs(tmp_path, [])
    conns = _build(sf, tmp_path)["connections"]
    assert conns["read"] is True
    capabilities = {c["capability"] for c in conns["declared"]}
    assert capabilities == {"PracticeManagement", "Email"}
    assert all(c["auth"] == "unknown" for c in conns["declared"])
    assert "auth-status" in conns["note"]


def test_identity_reports_no_email_address_when_none_is_authored(sf, tmp_path):
    """AgentMail seats author no address in customer.yaml. ``None`` is 'not
    authored'; inventing one would be the Pattern B failure in miniature."""
    _write_jobs(tmp_path, [])
    identity = _build(sf, tmp_path)["identity"]
    assert identity["read"] is True
    assert identity["persona_name"] == "Operator"
    assert identity["persona_slug"] == "operator"
    assert identity["firm_display_name"] == "Example Firm"
    assert identity["email_address"] is None

    raw = _raw_config()
    raw["connectors"]["Email"] = {
        "adapter": "msgraph",
        "enabled": True,
        "mailbox": "operator@example.com",
    }
    assert _build(sf, tmp_path, raw=raw)["identity"]["email_address"] == "operator@example.com"


# --------------------------------------------------------------------------- #
# T4 — content ceiling
# --------------------------------------------------------------------------- #

_MATTER_MARKERS = (
    "Ramirez v. Delgado Trucking",
    "2024-CV-88213",
    "Maria Ramirez",
)


def test_no_matter_or_client_content_reaches_the_result(sf, tmp_path):
    """T4. Every source the handler reads is seeded with a matter name, a matter
    number, and a client name; none may appear in the serialized envelope. The
    ceiling is an invariant of construction — there is no source here that
    carries one — which is stronger than a gate that strips them afterwards.

    Falsifier: add a ``matter_name`` passthrough to the routine mapper."""
    raw = _raw_config()
    persona = raw["personas"][0]
    persona["matters"] = list(_MATTER_MARKERS)
    persona["skills"][0]["matter"] = _MATTER_MARKERS[0]
    persona["cron"][0]["matter_number"] = _MATTER_MARKERS[1]
    raw["clients"] = [{"name": _MATTER_MARKERS[2]}]
    raw["connectors"]["PracticeManagement"]["last_matter"] = _MATTER_MARKERS[0]
    raw["webhook_triggers"][0]["matter"] = _MATTER_MARKERS[1]
    raw["output_classes"]["work_product"]["example_matter"] = _MATTER_MARKERS[0]
    raw["voice_cohorts"]["example_client"] = _MATTER_MARKERS[2]

    _write_jobs(
        tmp_path,
        [
            {
                "name": "op-managed:operator:daily-needs-you-digest",
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": True,
                "matter": _MATTER_MARKERS[0],
                "client": _MATTER_MARKERS[2],
                "last_status": f"ok for {_MATTER_MARKERS[1]}",
            }
        ],
    )

    serialized = json.dumps(_build(sf, tmp_path, raw=raw))
    for marker in _MATTER_MARKERS:
        assert marker not in serialized, f"{marker!r} reached the model"


def test_authored_labels_pass_through_verbatim_and_that_is_the_contract(sf, tmp_path):
    """The two named carve-outs from T4, pinned so they are deliberate rather
    than accidental: ``routine_names`` values and a job's ``paused_reason`` are
    firm-authored / scheduler-authored OPERATIONAL text, and the introduce skill
    is required to name both ('use the firm-legible name'; 'name paused_reason if
    it carries one'). They are not tenant-record fields, and nothing else on the
    read path is free text."""
    raw = _raw_config()
    raw["routine_names"]["daily-needs-you-digest"] = "Morning digest"
    _write_jobs(
        tmp_path,
        [
            {
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": False,
                "paused_reason": "paused pending vendor credential refresh",
            }
        ],
    )
    items = {i["skill"]: i for i in _build(sf, tmp_path, raw=raw)["routines"]["items"]}
    digest = items["daily-needs-you-digest"]
    assert digest["firm_name"] == "Morning digest"
    assert digest["paused_reason"] == "paused pending vendor credential refresh"


# --------------------------------------------------------------------------- #
# T5 — run-history strip
# --------------------------------------------------------------------------- #


def test_run_history_is_dropped_at_the_read_boundary(sf, tmp_path):
    """T5. The introduce skill forbids every run-history claim, including "it's
    due next at". A rule enforced by the field never reaching the model is
    stronger than the same rule written in prose the model may not be reading on
    this channel.

    Falsifier: reuse ``config_snapshot._read_cron_jobs``, which returns all
    three."""
    path = _write_jobs(
        tmp_path,
        [
            {
                "name": "op-managed:operator:daily-needs-you-digest",
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": True,
                "last_run_at": "2026-08-11T06:23:00Z",
                "last_status": "success",
                "next_run_at": "2026-08-12T06:23:00Z",
            }
        ],
    )
    jobs = sf._read_jobs(path)
    assert set(jobs[0]) == {
        "name",
        "skill",
        "schedule_expr",
        "enabled",
        "state",
        "paused_at",
        "paused_reason",
    }
    serialized = json.dumps(_build(sf, tmp_path))
    for banned in ("last_run_at", "last_status", "next_run_at", "2026-08-12T06:23:00Z"):
        assert banned not in serialized


def test_read_jobs_tolerates_both_schedule_shapes(sf, tmp_path):
    """The store has carried ``schedule`` as a bare string and as
    ``{expr: ...}``. Reading only one shape would silently blank the schedule."""
    path = _write_jobs(
        tmp_path,
        [
            {"skill": "a", "schedule": {"expr": "0 7 * * *"}},
            {"skill": "b", "schedule": "0 8 * * *"},
        ],
    )
    jobs = {j["skill"]: j for j in sf._read_jobs(path)}
    assert jobs["a"]["schedule_expr"] == "0 7 * * *"
    assert jobs["b"]["schedule_expr"] == "0 8 * * *"


# --------------------------------------------------------------------------- #
# T6 — schedule prose, closed set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("0 7 * * *", "Daily at 7:00 a.m."),
        ("23 6 * * 1-5", "Weekdays at 6:23 a.m."),
        ("0 7 * * 2", "Weekly on Tuesday at 7:00 a.m."),
        ("30 13 * * *", "Daily at 1:30 p.m."),
        # 0 and 12 both render as 12 — the skill states this explicitly, and
        # "0:00 a.m." is the wrong-clock bug it is guarding against.
        ("0 0 * * *", "Daily at 12:00 a.m."),
        ("0 12 * * *", "Daily at 12:00 p.m."),
        ("5 0 * * 0", "Weekly on Sunday at 12:05 a.m."),
    ],
)
def test_schedule_prose_translates_the_three_authorized_shapes(sf, expr, expected):
    assert sf.schedule_prose(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "*/15 * * * *",  # step values
        "0 7 1 * *",  # day-of-month
        "0 7 * 3 *",  # month
        "0 7 * * 1,3",  # list
        "0 7 * * 1-3",  # a range that is not the weekday range
        "0 25 * * *",  # out of range
        "not a cron",
        "",
        None,
        {"expr": "0 7 * * *"},
    ],
)
def test_schedule_prose_returns_none_outside_the_closed_set(sf, expr):
    """T6's falsifier. Anything outside the three shapes returns None so the
    model prints the raw expression labelled as raw: a wrong translation is
    fabrication, an untranslated one is just less polish."""
    assert sf.schedule_prose(expr) is None


# --------------------------------------------------------------------------- #
# T7 — discrepancy states
# --------------------------------------------------------------------------- #


def test_the_four_pairings_produce_four_distinct_states(sf, tmp_path):
    """T7. The config layer and the scheduler layer are never reconciled to
    whichever reads better — a disagreement IS the finding.

    Falsifier: make ``authored_no_job`` fall back to ``scheduled``."""
    _write_jobs(
        tmp_path,
        [
            # authored + present + enabled -> scheduled
            {
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": True,
            },
            # authored + present + paused -> paused
            {
                "skill": "motion-calendar-tracker",
                "schedule": {"expr": "27 7 * * 1-5"},
                "enabled": True,
                "paused_at": "2026-08-01T00:00:00Z",
                "paused_reason": "vendor outage",
            },
            # a job the config authors nowhere -> job_not_authored
            {"skill": "ghost-routine", "schedule": {"expr": "0 5 * * *"}, "enabled": True},
        ],
    )
    raw = _raw_config()
    # authored in cron:, no matching job -> authored_no_job
    raw["personas"][0]["cron"].append({"skill": "operator-introduce", "schedule": "0 9 * * *"})

    items = {i["skill"]: i for i in _build(sf, tmp_path, raw=raw)["routines"]["items"]}
    assert items["daily-needs-you-digest"]["state"] == sf.STATE_SCHEDULED
    assert items["motion-calendar-tracker"]["state"] == sf.STATE_PAUSED
    assert items["motion-calendar-tracker"]["paused_reason"] == "vendor outage"
    assert items["operator-introduce"]["state"] == sf.STATE_AUTHORED_NO_JOB
    assert items["ghost-routine"]["state"] == sf.STATE_JOB_NOT_AUTHORED
    assert items["retired-routine"]["state"] == sf.STATE_SWITCHED_OFF
    assert items["discovery-served-watch"]["state"] == sf.STATE_NOT_SCHEDULED
    assert len({i["state"] for i in items.values()}) >= 5


def test_a_job_disabled_by_the_scheduler_reads_paused_not_scheduled(sf, tmp_path):
    _write_jobs(
        tmp_path,
        [
            {
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": False,
            }
        ],
    )
    items = {i["skill"]: i for i in _build(sf, tmp_path)["routines"]["items"]}
    assert items["daily-needs-you-digest"]["state"] == sf.STATE_PAUSED


def test_routines_are_grouped_by_the_skills_own_precedence(sf, tmp_path):
    """A cron entry wins over a webhook grant, which wins over manual. A routine
    that can ALSO be asked for says so on its own line rather than being listed
    twice."""
    _write_jobs(tmp_path, [])
    items = {i["skill"]: i for i in _build(sf, tmp_path)["routines"]["items"]}
    assert items["daily-needs-you-digest"]["group"] == sf.GROUP_SCHEDULE
    assert items["daily-needs-you-digest"]["also_on_request"] is True
    assert items["discovery-served-watch"]["group"] == sf.GROUP_EVENT
    assert items["discovery-served-watch"]["event"] == "smokeball matter.updated"
    assert items["operator-introduce"]["group"] == sf.GROUP_REQUEST
    assert items["operator-introduce"]["also_on_request"] is False


def test_counts_report_what_was_actually_parsed(sf, tmp_path):
    """The counts line exists so a mis-parse is visible to the reader instead of
    silently shortening the roster: a roster that quietly lost half its entries
    reads exactly like a small seat."""
    _write_jobs(
        tmp_path,
        [
            {
                "skill": "daily-needs-you-digest",
                "schedule": {"expr": "23 6 * * 1-5"},
                "enabled": True,
            }
        ],
    )
    counts = _build(sf, tmp_path)["counts"]
    assert counts == {
        "read": True,
        "skill_entries": 5,
        "enabled": 4,
        "scheduled": 2,
        "live_jobs": 1,
    }


def test_an_unreadable_scheduler_keeps_the_authored_layer(sf, tmp_path):
    """The authored layer is still true and must not be discarded because the
    other half is unknown. Every item reports ``authored_layer_only`` and the
    scheduler is named in ``unreadable[]`` — the skill's 'I can tell you what I'm
    configured to do, but not what my scheduler currently has loaded'."""
    path = tmp_path / "profiles" / "operator" / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    facts = _build(sf, tmp_path)
    assert facts["routines"]["read"] is True
    assert {i["state"] for i in facts["routines"]["items"]} == {sf.STATE_AUTHORED_LAYER_ONLY}
    assert any(e["section"] == "scheduler" for e in facts["unreadable"])
    assert facts["counts"]["live_jobs"] is None
    assert facts["counts"]["skill_entries"] == 5


def test_an_absent_jobs_file_is_a_fact_not_a_failed_read(sf, tmp_path):
    """No jobs file means nothing materialized — determinable, and the same call
    ``config_snapshot.read_profiles`` makes. Degrading it would report our
    blindness where the firm has a real (empty) state."""
    facts = _build(sf, tmp_path)
    assert facts["counts"]["live_jobs"] == 0
    assert not any(e["section"] == "scheduler" for e in facts["unreadable"])


# --------------------------------------------------------------------------- #
# Voice: three states, never two
# --------------------------------------------------------------------------- #


def test_voice_reports_installed_when_the_manifest_names_a_verified_spec(sf, tmp_path):
    _write_jobs(tmp_path, [])
    manifest = _FakeManifest(state="ok", installed=(("work_product", "voice"),))
    classes = {c["class"]: c for c in _build(sf, tmp_path, manifest=manifest)["voice"]["classes"]}
    assert classes["work_product"]["status"] == sf.VOICE_INSTALLED
    assert classes["work_product"]["firm_words"] == "your work product"
    assert classes["staff"]["status"] == sf.VOICE_NOT_INSTALLED
    assert classes["staff"]["voice_spec"] == "none"


def test_an_absent_manifest_is_not_installed_not_unreadable(sf, tmp_path):
    """ss#2234's whole point. The spec dir exists and holds no manifest: the
    applier writes ``manifest.json`` LAST as its commit point and
    ``entrypoint.sh`` recreates the dir every boot, so this is the ordinary
    nothing-was-ever-installed state and it is AFFIRMATIVE evidence.

    Falsifier: read ``load_entries``' collapsed ``{}`` instead, which cannot
    tell this apart from the case below."""
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path, manifest=_FakeManifest(state="absent"))
    assert facts["voice"]["manifest_state"] == "absent"
    assert {c["status"] for c in facts["voice"]["classes"]} == {sf.VOICE_NOT_INSTALLED}
    assert not any(e["section"] == "voice" for e in facts["unreadable"])


def test_an_unreadable_manifest_is_unreadable_not_absent(sf, tmp_path):
    """The other half. ``SMD_SPEC_DIR`` unset or a corrupt manifest means this
    process cannot prove ANYTHING — it must never render as 'the firm never
    established a voice', which is a claim about the firm made out of our own
    blindness."""
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path, manifest=_FakeManifest(state="unreadable"))
    assert {c["status"] for c in facts["voice"]["classes"]} == {sf.VOICE_UNREADABLE}
    assert any(e["section"] == "voice" for e in facts["unreadable"])


def test_a_spec_that_fails_verification_is_not_claimed_as_installed(sf, tmp_path):
    """A file that no longer hashes to what root recorded cannot back a claim
    that the seat learned that voice. Under-claiming is the safe direction, and
    it is the same pairing ``spec_control_check`` makes."""
    _write_jobs(tmp_path, [])
    manifest = _FakeManifest(state="ok", installed=(("work_product", "voice"),))
    manifest.verify_result = False
    classes = {c["class"]: c for c in _build(sf, tmp_path, manifest=manifest)["voice"]["classes"]}
    assert classes["work_product"]["status"] == sf.VOICE_NOT_INSTALLED


def test_a_format_spec_does_not_satisfy_the_voice_question(sf, tmp_path):
    _write_jobs(tmp_path, [])
    manifest = _FakeManifest(state="ok", installed=(("work_product", "format"),))
    classes = {c["class"]: c for c in _build(sf, tmp_path, manifest=manifest)["voice"]["classes"]}
    assert classes["work_product"]["status"] == sf.VOICE_NOT_INSTALLED


# --------------------------------------------------------------------------- #
# Cohort discrepancies
# --------------------------------------------------------------------------- #


def test_cohort_directories_outside_the_authored_vocabulary_are_reported(sf, tmp_path):
    _write_jobs(tmp_path, [])
    for name in ("client", "adjuster", "court"):
        (tmp_path / "voice" / "cohort" / name).mkdir(parents=True, exist_ok=True)
    facts = _build(sf, tmp_path)
    assert facts["cohort_discrepancies"]["unauthorized_dirs"] == ["court"]


def test_no_cohort_tree_is_an_empty_result_not_a_degraded_one(sf, tmp_path):
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path)
    assert facts["cohort_discrepancies"] == {"read": True, "unauthorized_dirs": []}


# --------------------------------------------------------------------------- #
# T8 — per-source fault isolation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "attr,section",
    [
        ("_identity_section", "identity"),
        ("_connections_section", "connections"),
        ("_voice_section", "voice"),
        ("_cohort_section", "cohorts"),
        ("_routine_items", "routines"),
    ],
)
def test_each_source_fails_open_independently(sf, tmp_path, monkeypatch, attr, section):
    """T8. One unreadable source degrades its OWN section and nothing else, and
    never raises out of the handler.

    Falsifier: let the spec-manifest read propagate — every other section would
    be lost to a fault in one of them."""

    def _boom(*_a, **_kw):
        raise OSError(f"{attr} exploded")

    monkeypatch.setattr(sf, attr, _boom)
    _write_jobs(tmp_path, [])
    facts = _build(sf, tmp_path)

    reasons = {e["section"] for e in facts["unreadable"]}
    assert section in reasons, f"{section} fault must be named"
    # Everything else still read.
    healthy = [s for s in sf.SECTIONS if s not in {"matters", "inbox"}]
    still_read = [s for s in healthy if facts[s]["read"] is True]
    assert still_read, "a single fault must not empty the envelope"


def test_the_handler_never_raises_and_always_returns_json():
    """The plugin-level wrapper. A raise here would surface to the model as an
    opaque tool error it might paraphrase into a claim; an authored refusal
    cannot be paraphrased into a roster."""
    plugin = load_plugin("hermes-smd-initiation")
    out = plugin._seat_facts_handler({"depth": "walkthrough"})
    parsed = json.loads(out)
    assert parsed["schema"] == "operator.seat.facts/v1"
    # No args at all, and a non-dict args value, both survive.
    assert json.loads(plugin._seat_facts_handler())["schema"] == "operator.seat.facts/v1"
    assert json.loads(plugin._seat_facts_handler("nonsense"))["schema"] == "operator.seat.facts/v1"


def test_handler_returns_an_authored_refusal_when_assembly_itself_fails(monkeypatch):
    plugin = load_plugin("hermes-smd-initiation")

    def _boom(**_kw):
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(plugin.seat_facts, "build_facts", _boom)
    parsed = json.loads(plugin._seat_facts_handler({}))
    assert "could not read my own seat" in parsed["error"]
    assert "from memory" in parsed["error"]


# --------------------------------------------------------------------------- #
# T9 — working rules (ss-console#2338)
#
# The four rules were ALREADY authored in operator-introduce's fixed shape
# before the 2026-08-12 rehearsal, and the introduction still stated none of
# them: on an email turn that file is not in front of the model, and the reply
# that came back mirrored THIS envelope's sections exactly. So the rules belong
# where the reading happens — and two of the four are seat-variable, which is
# why they are read rather than recited.
# --------------------------------------------------------------------------- #


def _with_exposure(**exposure):
    raw = _raw_config()
    raw["personas"][0]["entitlements"] = {"exposure": exposure}
    return raw


def test_send_posture_is_read_per_class_not_asserted(sf, tmp_path):
    """ "I don't send externally on my own" is FALSE on a seat authoring an
    autonomous external send — ADR 0073 proved such a send really sends. So the
    posture is reported per class, exactly as authored, and a mixed seat is
    reported as mixed rather than flattened into a promise.

    FALSIFIER: hardcode the review sentence and this fails on the autonomous
    class below.
    """
    raw = _with_exposure(
        internal_write="autonomous",
        external_send="draft_for_review",
        external_send_client="autonomous",
        destructive="autonomous",
    )
    rules = _build(sf, tmp_path, raw=raw)["working_rules"]
    assert rules["read"] is True
    posture = {p["action_class"]: p["exposure"] for p in rules["send_posture"]}
    assert posture == {
        "external_send": "draft_for_review",
        "external_send_client": "autonomous",
    }, "only outbound classes belong here — internal_write/destructive are not send posture"
    assert "never state a review promise" in rules["send_posture_note"].lower()


def test_unauthored_exposure_reads_as_refused_never_as_unrestricted(sf, tmp_path):
    """The empty list is the trap. Unconfigured is a safety state, not an
    identity (ADR 0037 tenet 3), so an empty posture must render as "I cannot
    send outward" and never as "nothing stops me"."""
    rules = _build(sf, tmp_path)["working_rules"]  # fixture authors no entitlements
    assert rules["read"] is True
    assert rules["send_posture"] == []
    note = rules["send_posture_note"].lower()
    assert "refused rather than unrestricted" in note
    assert "never that you are free to" in note


def test_identifier_rule_follows_the_gate_mode_it_reads(sf, tmp_path, monkeypatch):
    """The A1 gate carries an operator rollback lever
    (SMD_IDENTIFIER_GATE_MODE=report). A seat running in report mode observes
    and does not refuse, so it must not tell a firm it refuses.

    FALSIFIER: state the refusal sentence unconditionally and the report-mode
    branch fails.
    """
    monkeypatch.delenv("SMD_IDENTIFIER_GATE_MODE", raising=False)
    blocking = _build(sf, tmp_path)["working_rules"]["identifier_gate"]
    assert blocking["refusing"] is True
    assert "I say so instead" in blocking["says"]

    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", "report")
    reporting = _build(sf, tmp_path)["working_rules"]["identifier_gate"]
    assert reporting["refusing"] is False
    assert "do not refuse" in reporting["says"]

    # Fail-closed on anything that is not exactly the rollback value.
    monkeypatch.setenv("SMD_IDENTIFIER_GATE_MODE", "block")
    assert _build(sf, tmp_path)["working_rules"]["identifier_gate"]["refusing"] is True


def test_never_computes_rides_the_same_gate_and_says_so(sf, tmp_path):
    """reads-never-computes has no separate mechanism — it is a consequence of
    the provenance register (a computed date is in no source). The section says
    that rather than implying a second gate exists."""
    gate = _build(sf, tmp_path)["working_rules"]["identifier_gate"]
    assert "provenance register" in gate["also_covers"]


def test_the_one_rule_with_no_mechanism_is_labelled_as_policy(sf, tmp_path):
    """no_legal_advice is authored skill policy with no runtime gate. Carrying
    it alongside the read rules without saying so would be exactly the
    dressed-up assertion this tool exists to prevent."""
    rules = _build(sf, tmp_path)["working_rules"]
    policy = {r["rule"]: r for r in rules["policy_rules"]}
    assert set(policy) == {"no_legal_advice"}
    assert "no runtime gate" in policy["no_legal_advice"]["basis"]


def test_working_rules_degrades_alone_when_the_persona_will_not_read(sf, tmp_path):
    """Per-source fault isolation (T8) extends to the new section: an
    unreadable persona costs this section and nothing else, and it lands in
    unreadable[] rather than vanishing."""

    class _Exploding(_FakeConfig):
        @property
        def personas(self):
            raise RuntimeError("persona unreadable")

    facts = sf.build_facts(
        depth="introduction",
        config_loader=lambda: _Exploding(_raw_config()),
        spec_manifest_module=_FakeManifest(),
        home=str(tmp_path),
    )
    assert facts["working_rules"]["read"] is False
    assert any(u["section"] == "working_rules" for u in facts["unreadable"])


# T10 — voice corpus provenance (ss-console#2339)
#
# Asked "what did you review to learn how we write", the rehearsal seat could
# not say. Letter 23 commits us in writing to self-initialization that reads
# the firm's matters to synthesize its voice, and this firm's diligence thread
# is about retention — so "I read your documents but cannot tell you which" is
# the worst available answer. The provenance now rides the root-owned manifest.
#
# The load-bearing test is the ABSENCE one: a spec installed before provenance
# existed must keep rendering as "I cannot name them" and must never become a
# confident empty list. The seat's original refusal was correct behaviour.
# --------------------------------------------------------------------------- #


class _ProvEntry(_FakeEntry):
    def __init__(self, output_class: str, prop: str, provenance: dict | None = None):
        super().__init__(output_class, prop)
        self.provenance = provenance or {}


class _ProvManifest(_FakeManifest):
    """A manifest whose entries carry provenance, as the applier records it."""

    def __init__(self, entries: dict[str, list]):
        super().__init__(state="ok")
        self._by_class = entries

    def entries_for_class(self, output_class: str) -> list:
        return self._by_class.get(output_class, [])


def test_installed_voice_names_the_corpus_it_learned_from(sf, tmp_path):
    """The answer letter 23 promises. Names and a count — never text, and never
    a cohort the record does not carry."""
    manifest = _ProvManifest(
        {
            "work_product": [
                _ProvEntry(
                    "work_product",
                    "voice",
                    {
                        "run_id": "run-7",
                        "document_count": 2,
                        "documents": [
                            {"name": "demand-letter-2025.docx", "sha256": "a" * 64},
                            {"name": "status-update.docx", "sha256": "b" * 64},
                        ],
                    },
                )
            ]
        }
    )
    voice = _build(sf, tmp_path, manifest=manifest)["voice"]
    wp = next(c for c in voice["classes"] if c["class"] == "work_product")
    assert wp["status"] == "installed"
    learned = wp["learned_from"]
    assert learned["document_count"] == 2
    assert [d["name"] for d in learned["documents"]] == [
        "demand-letter-2025.docx",
        "status-update.docx",
    ]
    assert learned["run_ids"] == ["run-7"]
    assert "never claim a cohort" in learned["note"]


def test_no_document_text_reaches_the_answer(sf, tmp_path):
    """Letters 07 and 10 promise the firm we keep no copy of matter files. The
    answer to "what did you read" must not itself become a second copy.

    FALSIFIER: pass the staged text through and this finds it.
    """
    manifest = _ProvManifest(
        {
            "work_product": [
                _ProvEntry(
                    "work_product",
                    "voice",
                    {
                        "run_id": "run-7",
                        "documents": [
                            {
                                "name": "demand.docx",
                                "sha256": "c" * 64,
                                "text": "Dear Ms. Alvarez, we represent",
                            }
                        ],
                    },
                )
            ]
        }
    )
    serialized = json.dumps(_build(sf, tmp_path, manifest=manifest))
    assert "Alvarez" not in serialized
    assert "we represent" not in serialized


def test_a_spec_without_provenance_stays_unanswerable_not_empty(sf, tmp_path):
    """THE one that matters. A spec installed before this field existed carries
    no provenance. Rendering that as an empty document list would tell the firm
    we read nothing — false, and worse than the honest "I cannot name them"
    the seat already said on its own.

    FALSIFIER: return {"documents": []} instead of None and this fails.
    """
    manifest = _ProvManifest({"work_product": [_ProvEntry("work_product", "voice", {})]})
    voice = _build(sf, tmp_path, manifest=manifest)["voice"]
    wp = next(c for c in voice["classes"] if c["class"] == "work_product")
    assert wp["status"] == "installed", "the spec IS installed — only its corpus is unknown"
    assert wp["learned_from"] is None


def test_a_class_with_nothing_installed_has_no_corpus_to_name(sf, tmp_path):
    manifest = _ProvManifest({})
    voice = _build(sf, tmp_path, manifest=manifest)["voice"]
    for entry in voice["classes"]:
        assert entry["status"] == "not_installed"
        assert entry["learned_from"] is None


def test_a_spec_that_no_longer_verifies_names_no_corpus(sf, tmp_path):
    """Under-claim is the safe direction. A file that no longer hashes to what
    root recorded is reported not_installed, and a corpus attributed to a spec
    the seat cannot verify would be a claim about a document it no longer has.
    """
    manifest = _ProvManifest(
        {"work_product": [_ProvEntry("work_product", "voice", {"documents": [{"name": "x.docx"}]})]}
    )
    manifest.verify_result = False
    voice = _build(sf, tmp_path, manifest=manifest)["voice"]
    wp = next(c for c in voice["classes"] if c["class"] == "work_product")
    assert wp["status"] == "not_installed"
    assert wp["learned_from"] is None
