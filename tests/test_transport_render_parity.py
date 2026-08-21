"""One body, every transport: the cross-transport rendering instrument (ss-console#2503).

ss#2489 sat live for a month with no failing test. ``shared/report_render.py``
shipped 2026-07-16 and was injected by the trust plugin as ``args["html"]``, a
key only the AgentMail transport consumed. The msgraph transport did not thread
it, so on the paying client's channel the renderer silently did nothing: no
error, no log, no red test. Four replies reached a law firm as one unbroken
block of text. The bug is fixed (overlay#288, overlay#290). This file is the
instrument that was missing, and its job is narrow: **no lane/transport pair
carries a body out of this overlay without a test watching the rendered half.**

TWO LANES CARRY A BODY OUT, and both are pinned here, because covering one
would leave the other as the next silent gap.

Lane 1, the proactive send (``smd_send_message``, EXTERNAL_SEND). ONE render
site, ``hermes-smd-trust._attach_html_body``, gated on
``report_render.looks_like_report``, writes ``args["html"]``. Each transport
then forwards that key through its OWN closed allowlist
(``outbound_send._SEND_BODY_FIELDS`` and ``._MSGRAPH_SEND_FIELDS``). Two
allowlists, one render: an adapter added tomorrow that omits ``html`` from its
allowlist reproduces ss#2489 exactly, and
:func:`test_a_report_body_arrives_rendered_on_both_send_transports` is what
turns red when it does. That is why the fakes below are installed at the BROKER
seam rather than at ``outbound_send``: stubbing ``outbound_send.send_message``
would skip the allowlist, which is the very layer that dropped the key.

Lane 2, the reply relay (``create_draft``, INTERNAL_WRITE). Here the two
transports are ASYMMETRIC, and the asymmetry is PINNED AS INTENDED rather than
flagged as drift. ``_send_msgraph_reply`` renders UNCONDITIONALLY because
Graph's ``/reply`` composes the message in HTML with no plain-text alternative,
so an unrendered prose reply *is* the wall. The AgentMail transport relays only
what the composer authored, because AgentMail's ``send_reply`` transmits ``text``
AND ``html`` (``shared/agentmail_broker.py:84-96``): its plain-text part already
arrives readable, and leaving prose alone keeps those sends byte-identical. The
reasoning is authored at ``plugins/hermes-smd-reply/__init__.py:196-207``.
Deleting either render site is a regression; collapsing them was investigated
and falsified 2026-08-21.

WHAT THIS FILE DOES NOT CLAIM, stated so its green is not read as more than it
is. ``_attach_html_body`` returns early unless the tool is EXTERNAL_SEND
(``plugins/hermes-smd-trust/outbound.py:808-812``) and ``create_draft`` is
INTERNAL_WRITE on both adapters (``shared/action_classes.py:260`` and ``:306``),
so the trust render site never runs on lane 2 at all. On that lane a
report-shaped body therefore reaches an AgentMail seat as markdown with no html
half, while the same body sent proactively on the same seat renders. That pair
is pinned below as OBSERVED behavior with its mechanism cited. It is not
endorsed here, and it is not the ss#2489 wall. See ss-console#2503.
"""

from __future__ import annotations

import json

from shared import inbound
from tests.conftest import load_plugin

_TO = "greg@whitfield.example"

#: Block structure that ``looks_like_report`` recognizes: a heading, a list, a
#: closing paragraph. The shape whose collapse the firm actually saw.
REPORT = "## What I did\n\n- Read the file\n- Logged the call\n\nClosing note.\n"

#: No block structure at all. The body the AgentMail gate deliberately leaves
#: untouched, and the one Graph would otherwise collapse into a single line.
PROSE = "Thanks for the note.\n\nI will get to it tomorrow.\n"

_COMPOSER_HTML = "<p>mine</p>"


# ---------------------------------------------------------------------------
# Lane 1 - proactive send. One render site, two transport allowlists.
# ---------------------------------------------------------------------------

_SEAT_YAML = (
    "customer_id: acme\nvertical: law-firm\nconnectors:\n  Email:\n"
    "    adapter: {adapter}\n    backend: mcp:x\n    enabled: true\n"
)


def _send_body_on(monkeypatch, trust, tmp_path, adapter: str, args: dict) -> dict:
    """Send ``args`` on a seat authoring ``adapter``; return the wire body.

    Both transports are stubbed on every call so a MIS-ROUTE is visible as the
    wrong list filling, rather than as a passing assertion against the transport
    that happened to be asked. The stubs sit on the BROKER (``msgraph_broker`` /
    ``agentmail_broker``), so each transport's closed field allowlist really
    runs. See this module's docstring.
    """
    path = tmp_path / f"customer-{adapter}.yaml"
    path.write_text(_SEAT_YAML.format(adapter=adapter))
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))

    graph: list[dict] = []
    agentmail: list[dict] = []
    monkeypatch.setattr(
        trust.outbound_send.msgraph_broker,
        "send_message",
        lambda payload: graph.append(dict(payload)) or "",
    )
    monkeypatch.setattr(
        trust.outbound_send.agentmail_broker,
        "send_message",
        lambda payload: agentmail.append(dict(payload)) or "am-1",
    )

    result = trust._smd_send_message(dict(args))
    assert not result.startswith("Not sent"), result

    sent = graph if adapter == "msgraph" else agentmail
    other = agentmail if adapter == "msgraph" else graph
    assert other == [], f"a seat authoring {adapter} reached the other transport"
    assert len(sent) == 1, f"expected exactly one {adapter} send, got {len(sent)}"
    return sent[0]


def test_a_report_body_arrives_rendered_on_both_send_transports(monkeypatch, tmp_path):
    """AC1. The same report, both transports, rendered on both.

    This is the assertion ss#2489 needed and did not have. The render happens
    ONCE, on the shared args dict; what is under test is whether each transport
    carries the result to the wire. A transport whose allowlist drops ``html``
    fails here, which is exactly the shape that shipped.
    """
    trust = load_plugin("hermes-smd-trust")
    args = {"to": [_TO], "subject": "Status", "text": REPORT}
    trust._attach_html_body("smd_send_message", args)
    assert args.get("html"), "precondition: the shared render site produced an html half"

    graph = _send_body_on(monkeypatch, trust, tmp_path, "msgraph", args)
    mail = _send_body_on(monkeypatch, trust, tmp_path, "agentmail", args)

    for name, body in (("msgraph", graph), ("agentmail", mail)):
        html = body.get("html") or ""
        assert html, f"the {name} transport dropped the rendered html half"
        # STRUCTURE, not an exact string: what failed live was that every block
        # ran together, so what has to hold is that the blocks arrive as blocks.
        assert "<h2" in html, f"{name}: the heading did not survive"
        assert html.count("<li") == 2, f"{name}: the list did not survive"
        assert "Closing note." in html, f"{name}: the closing paragraph did not survive"

    # One render, two transports: the halves must be the SAME html. A per-adapter
    # render would pass every assertion above and still be the drift this file
    # exists to catch.
    assert graph["html"] == mail["html"]
    # The plain half still rides along on both. It is the fallback part, and the
    # audit digest is taken over the words rather than the markup.
    assert graph["text"] == REPORT
    assert mail["text"] == REPORT


def test_a_prose_send_stays_byte_identical_on_both_transports(monkeypatch, tmp_path):
    """The send lane's gate is SYMMETRIC, unlike the reply lane's.

    ``looks_like_report`` refuses a prose body, so no html is attached and
    neither transport invents one. Pinned because "renders on both" and "leaves
    prose alone on both" are two different promises, and a change that started
    rendering prose here would silently alter every prose send the firm receives.
    """
    trust = load_plugin("hermes-smd-trust")
    args = {"to": [_TO], "subject": "Note", "text": PROSE}
    trust._attach_html_body("smd_send_message", args)
    assert "html" not in args, "precondition: prose is not report-shaped"

    graph = _send_body_on(monkeypatch, trust, tmp_path, "msgraph", args)
    mail = _send_body_on(monkeypatch, trust, tmp_path, "agentmail", args)

    for name, body in (("msgraph", graph), ("agentmail", mail)):
        assert "html" not in body, f"{name} invented an html half for a prose send"
        assert body["text"] == PROSE, f"{name} altered the plain body"


def test_a_composer_authored_html_wins_on_both_send_transports(monkeypatch, tmp_path):
    """AC3, send lane. A model-authored html body is never clobbered, and the
    transports carry the composer's markup rather than a re-render of the text."""
    trust = load_plugin("hermes-smd-trust")
    args = {"to": [_TO], "subject": "Status", "text": REPORT, "html": _COMPOSER_HTML}
    trust._attach_html_body("smd_send_message", args)
    assert args["html"] == _COMPOSER_HTML, "precondition: the composer's html survived the site"

    graph = _send_body_on(monkeypatch, trust, tmp_path, "msgraph", args)
    mail = _send_body_on(monkeypatch, trust, tmp_path, "agentmail", args)

    assert graph["html"] == _COMPOSER_HTML
    assert mail["html"] == _COMPOSER_HTML


# ---------------------------------------------------------------------------
# Lane 2 - reply relay. The asymmetry, pinned as intended.
# ---------------------------------------------------------------------------

_REPLY_YAML = (
    "customer_id: acme\nvertical: law-firm\nconnectors:\n  Email:\n"
    "    adapter: {adapter}\n    backend: mcp:x\n    enabled: true\n"
    "scope:\n  inbound_allow_from:\n    - " + _TO + "\n"
)

_CREATE_DRAFT = {
    "msgraph": "mcp_msgraph_mail_create_draft",
    "agentmail": "mcp_agentmail_create_draft",
}

#: msgraph-mail carries a draft body under ``body_text`` (flat args, ADR 0078 D4);
#: AgentMail carries it under ``text``. Both fold into ``relay.draft_body``.
_BODY_KEY = {"msgraph": "body_text", "agentmail": "text"}


class _FakeD1:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def execute(self, sql, *params):
        self.calls.append((sql, params))
        return 1

    def events(self):
        return [(p[2], json.loads(p[-1]) if p[-1] else {}) for _s, p in self.calls]


def _reply_html_on(monkeypatch, tmp_path, adapter: str, args: dict) -> tuple[str, str]:
    """Relay one draft on a seat authoring ``adapter``; return ``(text, html)``.

    A fresh plugin module per call (``load_plugin`` re-executes), so the
    one-reply-per-inbound ring cannot leak between the two adapters inside a
    single test. The recorded inbound origin is process-global, so it is reset
    here too.
    """
    mod = load_plugin("hermes-smd-reply")
    d1 = _FakeD1()
    path = tmp_path / f"reply-{adapter}.yaml"
    path.write_text(_REPLY_YAML.format(adapter=adapter))
    monkeypatch.setattr(mod, "_INFRA_READY", True, raising=False)
    monkeypatch.setattr(mod, "_CUSTOMER_SLUG", "acme", raising=False)
    monkeypatch.setattr(mod, "_D1_CLIENT", d1, raising=False)
    monkeypatch.setattr(mod, "_LIMITER", mod.relay.RateLimiter(), raising=False)
    monkeypatch.setattr(mod, "_REPLIED", mod.relay.RepliedOnce(), raising=False)
    monkeypatch.setattr(mod, "_YAML_PATH", path, raising=False)

    wire: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod.msgraph_broker,
        "send_reply",
        lambda _mid, comment, *, html="": wire.append((comment, html)) or "",
    )
    monkeypatch.setattr(
        mod.relay.agentmail_broker,
        "send_reply",
        lambda *, message_id, text, html: wire.append((text, html)) or "am-1",
    )

    inbound.SESSION_INBOUND_ORIGIN._origins.clear()
    inbound.SESSION_INBOUND_ORIGIN._by_address.clear()
    inbound.SESSION_INBOUND_ORIGIN.record(
        "s1",
        inbound.InboundOrigin(
            sender_address=_TO, message_id=f"mid-{adapter}", inbox_id="op@client.example"
        ),
    )
    mod.on_post_tool_call(
        tool_name=_CREATE_DRAFT[adapter],
        args={"to": [_TO], "subject": "Re: matter", **args},
        session_id="s1",
    )

    sent = [a for a, _m in d1.events() if a == "REPLY_SENT"]
    assert sent == ["REPLY_SENT"], f"{adapter}: expected exactly one relayed reply, got {sent}"
    assert len(wire) == 1, f"{adapter}: expected one transport call, got {len(wire)}"
    return wire[0]


def test_a_prose_reply_renders_on_msgraph_and_leaves_agentmail_byte_identical(
    monkeypatch, tmp_path
):
    """AC2. The reply lane's asymmetry, PINNED AS INTENDED rather than flagged.

    Graph's ``/reply`` composes in HTML with no plain-text alternative, so an
    unrendered prose reply collapses into one line: on this transport the render
    is the floor, not a nicety. AgentMail's ``send_reply`` transmits ``text`` AND
    ``html`` (``shared/agentmail_broker.py:84-96``), so its plain-text part
    already arrives readable and the gate keeps prose sends byte-identical.
    Reasoning: ``plugins/hermes-smd-reply/__init__.py:196-207``. A future change
    that "harmonizes" these two into one behavior is a regression, and this test
    is where it should stop.
    """
    graph_text, graph_html = _reply_html_on(
        monkeypatch, tmp_path, "msgraph", {_BODY_KEY["msgraph"]: PROSE}
    )
    mail_text, mail_html = _reply_html_on(
        monkeypatch, tmp_path, "agentmail", {_BODY_KEY["agentmail"]: PROSE}
    )

    # msgraph: rendered, and the two paragraphs arrive as two paragraphs.
    assert graph_html.count("<p") == 2, graph_html
    assert "Thanks for the note." in graph_html
    assert "I will get to it tomorrow." in graph_html
    # AgentMail: byte-identical. No html half was invented for prose.
    assert mail_html == ""
    # Both keep the plain body unchanged. On msgraph it is the fallback part; on
    # AgentMail it is the whole readable message.
    assert graph_text == PROSE
    assert mail_text == PROSE


def test_a_composer_authored_html_wins_on_both_reply_transports(monkeypatch, tmp_path):
    """AC3, reply lane. The composer's markup wins on BOTH transports, so the
    unconditional msgraph render never overwrites an authored body."""
    _graph_text, graph_html = _reply_html_on(
        monkeypatch, tmp_path, "msgraph", {_BODY_KEY["msgraph"]: PROSE, "html": _COMPOSER_HTML}
    )
    _mail_text, mail_html = _reply_html_on(
        monkeypatch, tmp_path, "agentmail", {_BODY_KEY["agentmail"]: PROSE, "html": _COMPOSER_HTML}
    )

    assert graph_html == _COMPOSER_HTML
    assert mail_html == _COMPOSER_HTML


def test_a_report_reply_renders_on_msgraph_and_reaches_agentmail_as_markdown(monkeypatch, tmp_path):
    """The lane-2 pair with NO render site, pinned as OBSERVED rather than endorsed.

    ``_attach_html_body`` returns early unless the tool is EXTERNAL_SEND
    (``plugins/hermes-smd-trust/outbound.py:808-812``) and ``create_draft`` is
    INTERNAL_WRITE (``shared/action_classes.py:260``, ``:306``), so the trust
    render site never runs on this lane. A report-shaped reply therefore reaches
    an AgentMail seat as literal markdown with no html half, while the SAME body
    sent proactively on the SAME seat renders (see the send-lane test above).

    This is recorded rather than asserted-as-correct. It is not the ss#2489 wall,
    since AgentMail delivers a real text/plain part and the markdown stays
    legible, but the reasoning authored at
    ``hermes-smd-reply/__init__.py:196-207`` covers prose, not reports, and no
    decision on record covers this pair. If it is later judged a defect, the fix
    changes this test; if it is judged intended, the judgment belongs in that
    docstring. Either way it stops being invisible, which is the point of
    ss-console#2503.
    """
    _graph_text, graph_html = _reply_html_on(
        monkeypatch, tmp_path, "msgraph", {_BODY_KEY["msgraph"]: REPORT}
    )
    mail_text, mail_html = _reply_html_on(
        monkeypatch, tmp_path, "agentmail", {_BODY_KEY["agentmail"]: REPORT}
    )

    assert "<h2" in graph_html
    assert graph_html.count("<li") == 2
    assert mail_html == ""
    assert mail_text == REPORT


def test_the_msgraph_reply_escapes_model_authored_markup(monkeypatch, tmp_path):
    """Escape-by-default is what makes rendering safe on the transport that
    renders unconditionally. It is inherited from ``report_render``, and
    inheriting a safety property silently is how it gets lost in a refactor."""
    _text, html = _reply_html_on(
        monkeypatch, tmp_path, "msgraph", {_BODY_KEY["msgraph"]: "<script>alert(1)</script>"}
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
