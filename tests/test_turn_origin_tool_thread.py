"""An emailed request binds to the tool that acts on it, on the thread Hermes
actually runs that tool on (2026-10-08 seat defect).

THE DEFECT. An administrator emailed the Operator (Microsoft Graph path). The
router recorded the verified origin, the inbound plugin bound it from the turn
prompt's ``message_id:`` line at ``pre_llm_call``, and the agent called
``drafting_job_submit``, which refused: "this turn was not opened by one". The
binding itself was fine. The TOOL could not find the turn's session:
``demand._origin`` resolved the session with ``provenance.resolve_session_with_mode
(None)``, which reads a THREAD-LOCAL id, and Hermes runs every tool, sequential
ones included, on a fresh worker thread (``agent/tool_executor.py:856-857`` at
v2026.9.14: a new ``DaemonThreadPoolExecutor(max_workers=1)`` per call). That
thread never noted a session, so resolution fell to the process tier, which
switches itself off for good the first time two threads hold two different
sessions (any cron turn plus any email turn) and answers ``ambiguous``. Hermes
was handing the tool the real id all along (``model_tools.py:811``,
``dispatch_kwargs = {"task_id": ..., "session_id": ...}``); the handler dropped
it in ``**_``.

These tests drive the live order: a cron turn on its own thread, the router's
own origin derivation from a Graph-shaped DTO with an empty dispatch session,
the msgraph prompt template rendered the way Hermes renders it, the real
``pre_llm_call`` hooks with Hermes' kwargs on the agent thread, and the tool
handler called the way ``registry.dispatch`` calls it, on a fresh thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

import pytest

from shared import inbound, inbound_message, provenance

ADMIN = "admin@firm.example"
IMID = "<CA1x2y3z@mail.firm.example>"
# A Graph REST item id: base64-ish, carries '/', '+', '-' and a '=' pad.
GRAPH_ID = "AAMkAGI2THVSAAA/x+y-z_0AAAAAAEMAAA2bHV0aW9uLXNlcnZpY2VzAAA="
SESSION = "20261008_163607_fa877f2b"
CRON_SESSION = "cron_abc123def456_20261008_163000"
MATTER = {
    "matter_id": "b041dd06-30a4-4c1f-912b-27724bd77a64",
    "matter_number": "900201",
    "document_class": "mediation_brief",
}
REQUEST = "Please draft the mediation brief for 900201."


_PARKED: list[tuple[threading.Thread, threading.Event]] = []


def _on_thread(fn, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn`` on a brand-new thread and return its result (or raise).

    The thread is PARKED alive until the test ends, as the gateway's agent,
    cron and tool threads are on a seat: a finished thread's ident is reused by
    the next one, which would make three threads look like one to
    ``provenance.note_session``."""
    box: dict[str, Any] = {}
    done, release = threading.Event(), threading.Event()

    def run() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller
            box["error"] = exc
        done.set()
        release.wait()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait()
    _PARKED.append((t, release))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _unpark() -> None:
    while _PARKED:
        t, release = _PARKED.pop()
        release.set()
        t.join()


class _Client:
    def __init__(self) -> None:
        self.drafting: list[dict] = []
        self.medchron: list[dict] = []

    def drafting_submit(self, envelope):
        self.drafting.append(envelope)
        return {"ok": True, "accepted": True, "job_id": "J1", "state": "submitted"}

    def submit(self, envelope):
        self.medchron.append(envelope)
        return {"ok": True, "accepted": True, "job_id": "J2", "state": "submitted"}


@pytest.fixture
def seat(monkeypatch, fake_ctx):
    from tests.conftest import load_plugin

    provenance._reset_for_tests()
    medchron = load_plugin("hermes-smd-medchron")
    client = _Client()
    monkeypatch.setattr(medchron.drafting, "MedchronBrokerClient", lambda: client)
    monkeypatch.setattr(medchron, "MedchronBrokerClient", lambda: client)
    medchron.register(fake_ctx)
    plugins = {
        "inbound": load_plugin("hermes-smd-inbound"),
        "router": load_plugin("hermes-smd-webhook-router"),
        "tools": fake_ctx.tools,
        "client": client,
    }
    yield plugins
    _unpark()
    provenance._reset_for_tests()


def _hermes_render(template: str, payload: dict) -> str:
    """Hermes' webhook ``_render_prompt`` dot-path substitution (pinned ref,
    ``gateway/platforms/webhook.py:733-753``): ``str(value)`` for a scalar."""

    def resolve(m: re.Match) -> str:
        value: Any = payload
        for part in m.group(1).split("."):
            value = value.get(part, "{" + m.group(1) + "}") if isinstance(value, dict) else value
        return str(value)

    return re.sub(r"\{([a-zA-Z0-9_.]+)\}", resolve, template)


def _graph_payload() -> dict:
    return {
        "inbound_message": {
            "provider": "msgraph",
            "mailbox": "operator@firm.example",
            "message_id": GRAPH_ID,
            "from_addr": ADMIN,
            "subject": "mediation brief",
            "body_text": REQUEST,
            "reply_text": REQUEST,
            "provider_refs": {"internet_message_id": IMID, "conversation_id": "AAQkConv="},
        }
    }


def _cron_turn() -> None:
    """A scheduled turn on the cron ticker's own thread: its ``pre_llm_call``
    notes its session exactly as hermes-smd-trust's hook does."""
    _on_thread(provenance.note_session, CRON_SESSION)


def _email_arrives(router) -> str:
    """The router's own origin derivation, recorded under the EMPTY dispatch
    session the live Graph path carries; returns the prompt Hermes dispatches."""
    from bootstrap.translate import _INBOUND_EMAIL_PROMPT_MSGRAPH

    payload = _graph_payload()
    dto = inbound_message.normalize_inbound("msgraph", payload)
    origin = router._origin_from_dto(dto, content=REQUEST)
    assert origin is not None and origin.message_id == GRAPH_ID
    inbound.SESSION_INBOUND_ORIGIN.record("", origin)
    return _hermes_render(_INBOUND_EMAIL_PROMPT_MSGRAPH, payload)


def _pre_llm_call(inbound_plugin, prompt: str, session: str = SESSION) -> None:
    """The agent thread's pre_llm_call, with Hermes' kwargs
    (``agent/turn_context.py:674-686``): the trust plugin's session note, then
    the inbound plugin's bind."""
    kwargs = dict(
        session_id=session,
        task_id="task-1",
        turn_id="turn-1",
        user_message=prompt,
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="webhook",
        parent_session_id="",
        sender_id="",
    )
    provenance.note_session(kwargs["session_id"])
    inbound_plugin.on_pre_llm_call(**kwargs)


def _call_tool(tools, name: str, args: dict, session: str = SESSION) -> dict:
    """``registry.dispatch`` (pinned ref, ``tools/registry.py:826-839``) calls
    ``handler(args, task_id=..., session_id=..., user_task=...)`` on a fresh
    worker thread (``agent/tool_executor.py:856-857``)."""
    handler = tools[name]["handler"]
    out = _on_thread(handler, args, task_id="task-1", session_id=session, user_task=None)
    return json.loads(out)


def _agent_turn(seat, prompt: str) -> None:
    _on_thread(_pre_llm_call, seat["inbound"], prompt)


def test_the_prompt_binds_the_graph_origin_at_pre_llm_call(seat) -> None:
    """The bind is not the defect: the Graph id survives the router, the
    template and the prompt parse, and the session is bound to that email."""
    _cron_turn()
    prompt = _email_arrives(seat["router"])
    assert f"message_id: {GRAPH_ID}\n" in prompt
    _agent_turn(seat, prompt)
    bound = inbound.SESSION_INBOUND_ORIGIN.bound_this_turn(SESSION)
    assert bound is not None and bound.message_id == GRAPH_ID


def test_a_tool_thread_cannot_resolve_the_turn_without_its_id(seat) -> None:
    """The mechanism, pinned: after a cron turn and an email turn on their own
    threads, a fresh tool thread resolving with NO id gets ``ambiguous``. Any
    tool that drops the session id Hermes hands it is blind on a live seat."""
    _cron_turn()
    _agent_turn(seat, _email_arrives(seat["router"]))
    assert _on_thread(provenance.resolve_session_with_mode, None) == ("", provenance.MODE_AMBIGUOUS)
    assert _on_thread(provenance.resolve_session_with_mode, SESSION) == (
        SESSION,
        provenance.MODE_KEYED,
    )


def test_an_emailed_draft_request_queues_after_a_cron_turn(seat) -> None:
    """The live defect, end to end: refused on origin/main because the tool's
    worker thread resolved ``ambiguous`` instead of the turn's own session."""
    _cron_turn()
    _agent_turn(seat, _email_arrives(seat["router"]))
    out = _call_tool(seat["tools"], "drafting_job_submit", MATTER)
    assert out["accepted"] is True, out
    env = seat["client"].drafting[0]
    assert env["requested_by"] == ADMIN
    assert env["request_ref"] == IMID
    assert env["request_text"] == REQUEST


def test_an_emailed_chronology_request_carries_its_requester(seat) -> None:
    _cron_turn()
    _agent_turn(seat, _email_arrives(seat["router"]))
    out = _call_tool(
        seat["tools"],
        "medchron_job_submit",
        {"matter_id": MATTER["matter_id"], "matter_number": "900201", "units": []},
    )
    assert out["accepted"] is True
    env = seat["client"].medchron[0]
    assert env["requested_by"] == ADMIN
    assert env["request_ref"] == IMID


def test_the_tool_never_borrows_another_sessions_email(seat) -> None:
    """Keyed resolution must not widen anything: a tool call on a session the
    email did not open (a concurrent cron turn) still submits nothing."""
    _agent_turn(seat, _email_arrives(seat["router"]))
    out = _call_tool(seat["tools"], "drafting_job_submit", MATTER, session=CRON_SESSION)
    assert out["accepted"] is False
    assert seat["client"].drafting == []


def test_a_later_wake_turn_on_the_email_session_submits_nothing(seat) -> None:
    """Never a sticky origin: a turn of the same session whose prompt carried
    no email (a job wake) does not inherit the earlier email's authority."""
    _agent_turn(seat, _email_arrives(seat["router"]))
    _on_thread(_pre_llm_call, seat["inbound"], "A task was handed to you asynchronously.")
    out = _call_tool(seat["tools"], "drafting_job_submit", MATTER)
    assert out["accepted"] is False
    assert seat["client"].drafting == []


def test_an_email_turn_whose_origin_cannot_bind_is_loud(seat, caplog) -> None:
    """An email-opened turn whose origin did not bind (here: the router never
    recorded it) still queues a chronology, without a requester, and says so
    at WARNING so the break is visible."""
    from bootstrap.translate import _INBOUND_EMAIL_PROMPT_MSGRAPH

    prompt = _hermes_render(_INBOUND_EMAIL_PROMPT_MSGRAPH, _graph_payload())
    _agent_turn(seat, prompt)
    with caplog.at_level(logging.WARNING):
        out = _call_tool(
            seat["tools"],
            "medchron_job_submit",
            {"matter_id": MATTER["matter_id"], "matter_number": "900201", "units": []},
        )
    assert out["accepted"] is True
    assert "requested_by" not in seat["client"].medchron[0]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "medchron_job_submit" in r.getMessage() and "email" in r.getMessage() for r in warnings
    )


def test_a_wake_turn_chronology_is_quiet(seat, caplog) -> None:
    """A turn no email opened (the runner's handoff) is the intended
    origin-less path: no requester, and no warning."""
    _on_thread(_pre_llm_call, seat["inbound"], "A task was handed to you asynchronously.")
    with caplog.at_level(logging.WARNING):
        out = _call_tool(
            seat["tools"],
            "medchron_job_submit",
            {"matter_id": MATTER["matter_id"], "matter_number": "900201", "units": []},
        )
    assert out["accepted"] is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
