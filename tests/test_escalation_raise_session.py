"""The raise carries the session the send was keyed under (ss-console).

The broker refuses a ``fired``/``chased`` it did not witness dispatching to a
person, and joins the raise to its ``CONFIRM_SEND_DISPATCHED`` row on
``session_id``. Two ways that plumbing can rot, both silent:

* the field stops arriving — every real delivery then reads as no delivery, and
  the escalator re-raises daily forever with nothing in the ledger;
* it arrives under a different resolution than ``_smd_send_message`` used — same
  outcome, because the send row was keyed by the resolver's answer.

So these tests assert the field is present, that it comes from the RUNTIME
rather than the model, and that an unresolvable session degrades instead of
raising.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_plugin


@pytest.fixture
def escalation(monkeypatch):
    plugin = load_plugin("hermes-smd-escalation")
    requests: list[dict] = []

    def fake_broker_request(payload):
        requests.append(payload)
        return {"ok": True, "id": "evt-1"}

    monkeypatch.setattr(plugin, "_broker_request", fake_broker_request)
    return plugin, requests


def _components(**overrides) -> dict:
    base = {
        "skill": "deadline-miss-escalator",
        "event": "fired",
        "attempt": 1,
        "matter_id": "m-1",
        "source_id": "task-1",
        "label": "task-deadline",
        "authored_date": "2026-08-26",
    }
    base.update(overrides)
    return base


def _derive_then_append(plugin, components: dict) -> dict:
    derived = json.loads(plugin._escalation_append({**components, "derive_only": True}))
    json.loads(
        plugin._escalation_append(
            {
                "skill": components["skill"],
                "event": components["event"],
                "attempt": components["attempt"],
                "append_handle": derived["append_handle"],
            }
        )
    )
    return derived


def _written(requests: list[dict]) -> dict:
    appends = [r for r in requests if r.get("action") == "escalation_event_append"]
    assert appends, "no append reached the broker"
    return appends[-1]["event"]


def test_the_written_raise_carries_a_session_id(escalation, monkeypatch):
    plugin, requests = escalation
    monkeypatch.setattr(plugin, "_resolved_session", lambda _kwargs: "cron_abc_20260826_070050")
    _derive_then_append(plugin, _components())
    assert _written(requests)["session_id"] == "cron_abc_20260826_070050"


def test_the_session_comes_from_the_runtime_not_the_model(escalation):
    """``_APPEND_SCHEMA`` sets ``additionalProperties: false``, so the model cannot
    name ``session_id`` as a tool argument. That is load-bearing, not incidental:
    a model-supplied session pointing at another turn where a send DID happen
    would satisfy the broker's witness for a raise that reached nobody."""
    plugin, _ = escalation
    schema = plugin._APPEND_SCHEMA
    assert schema.get("additionalProperties") is False
    assert "session_id" not in schema.get("properties", {})


def test_the_resolver_is_consulted_with_the_runtime_kwargs(escalation, monkeypatch):
    """It must go through ``provenance.resolve_session`` — the same reconciliation
    ``_smd_send_message`` uses — because core drops ``session_id`` at some tool
    fire sites and the send row was keyed by the resolver's answer."""
    plugin, requests = escalation
    seen: list[str] = []

    def _record(raw):
        seen.append(raw)
        return "resolved-id"

    monkeypatch.setattr(plugin.provenance, "resolve_session", _record)
    _derive_then_append(plugin, _components())
    assert seen, "provenance.resolve_session was never consulted"
    assert _written(requests)["session_id"] == "resolved-id"


def test_an_unresolvable_session_degrades_to_empty_not_a_crash(escalation, monkeypatch):
    """The broker reads an empty session as the pre-plumbing caller shape and
    falls back to a bounded recent-dispatch window. An append must not become an
    exception because an audit join could not be made."""
    plugin, requests = escalation

    def _boom(_raw):
        raise RuntimeError("provenance unavailable")

    monkeypatch.setattr(plugin.provenance, "resolve_session", _boom)
    _derive_then_append(plugin, _components())
    assert _written(requests)["session_id"] == ""


def test_a_chased_raise_carries_it_too(escalation, monkeypatch):
    """``chased`` is the other RAISING_EVENTS member and is gated identically."""
    plugin, requests = escalation
    monkeypatch.setattr(plugin, "_resolved_session", lambda _kwargs: "cron_xyz")
    _derive_then_append(plugin, _components(event="chased"))
    assert _written(requests)["session_id"] == "cron_xyz"
