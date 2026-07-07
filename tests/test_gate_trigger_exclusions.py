"""Authored webhook-trigger exceptions (gate_trigger_exclusions) — the
ops-matter and principal-actor cases, generalized. Pins: trigger-scoped
matching, matter/actor exclusion, fail-open on every malformed input, and
the never-suppress-unauthored default (ADR 0035).

Run::

    pytest tests/test_gate_trigger_exclusions.py -q
"""

import json

from shared.gate_trigger_exclusions import check_excluded, resolve_exclusions

_OPS_MATTER = "3c191bed-cdda-48b9-a6ed-a51a349f3f94"
_CHRIS = "aaaa1111-2222-3333-4444-bbbbcccc0001"

_CONFIG = {
    "webhook_triggers": [
        {
            "source": "smokeball",
            "event_type": "matter.updated",
            "skill": "matter-memo-on-update",
            "persona": "quinn",
            "exclude": {"matters": [_OPS_MATTER], "actors": [_CHRIS.upper()]},
        },
        # A second trigger with no exclude block — untouched.
        {"source": "agentmail", "event_type": "message.received", "skill": "matter-inbox-router"},
    ]
}


def _body(**fields) -> bytes:
    return json.dumps({"type": "matter.updated", **fields}).encode()


def test_resolve_extracts_trigger_scoped_ids_case_insensitively() -> None:
    ex = resolve_exclusions(_CONFIG)
    rule = ex[("smokeball", "matter.updated")]
    assert _OPS_MATTER in rule["matters"]
    assert _CHRIS in rule["actors"]  # lowered despite upper-case authoring
    assert ("agentmail", "message.received") not in ex


def test_excluded_matter_suppresses() -> None:
    ex = resolve_exclusions(_CONFIG)
    reason = check_excluded(route="smokeball", body=_body(id=_OPS_MATTER.upper()), exclusions=ex)
    assert reason == f"excluded-matter:{_OPS_MATTER}"
    # matterId spelling too
    assert check_excluded(route="smokeball", body=_body(matterId=_OPS_MATTER), exclusions=ex)


def test_excluded_actor_suppresses() -> None:
    ex = resolve_exclusions(_CONFIG)
    reason = check_excluded(
        route="smokeball", body=_body(id="some-other-matter", userId=_CHRIS), exclusions=ex
    )
    assert reason == f"excluded-actor:{_CHRIS}"


def test_excluded_matter_suppresses_despite_foreign_top_level_id() -> None:
    """THE live envelope shape (proven by signed probes 2026-07-07): Smokeball
    deliveries carry a top-level ``id`` that is NOT the matter id. First-
    present-wins precedence forwarded the excluded matter; any-candidate
    matching must suppress it."""
    ex = resolve_exclusions(_CONFIG)
    body = json.dumps(
        {
            "type": "matter.updated",
            "id": "de11very-0000-4444-8888-aaaaaaaaaaaa",
            "matterId": _OPS_MATTER,
        }
    ).encode()
    assert check_excluded(route="smokeball", body=body, exclusions=ex) == (
        f"excluded-matter:{_OPS_MATTER}"
    )


def test_non_excluded_delivery_forwards() -> None:
    ex = resolve_exclusions(_CONFIG)
    assert (
        check_excluded(
            route="smokeball", body=_body(id="client-matter", userId="paralegal-1"), exclusions=ex
        )
        is None
    )


def test_other_event_and_other_route_forward() -> None:
    ex = resolve_exclusions(_CONFIG)
    other_event = json.dumps({"type": "task.created", "id": _OPS_MATTER}).encode()
    assert check_excluded(route="smokeball", body=other_event, exclusions=ex) is None
    assert check_excluded(route="agentmail", body=_body(id=_OPS_MATTER), exclusions=ex) is None


def test_unauthored_and_malformed_fail_open() -> None:
    assert resolve_exclusions({}) == {}
    assert resolve_exclusions({"webhook_triggers": "nope"}) == {}
    assert resolve_exclusions({"webhook_triggers": [{"exclude": {"matters": [_OPS_MATTER]}}]}) == {}
    ex = resolve_exclusions(_CONFIG)
    # non-JSON body, non-object body, absent event/matter/actor fields
    assert check_excluded(route="smokeball", body=b"not json", exclusions=ex) is None
    assert check_excluded(route="smokeball", body=b'"str"', exclusions=ex) is None
    assert check_excluded(route="smokeball", body=b"{}", exclusions=ex) is None
    # empty exclusions short-circuits
    assert check_excluded(route="smokeball", body=_body(id=_OPS_MATTER), exclusions={}) is None
