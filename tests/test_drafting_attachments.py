"""A discovery-set request carries the request email's own Word documents
(hermes-smd-medchron/drafting.py ``_request_attachments``): an attorney's own
sets, for the job to place in the court's caption on the firm's paper
(ss-console drafting/place.py). Read by the turn's VERIFIED origin message id,
never an id or a file the model names. Each test fails if its guard is removed."""

from __future__ import annotations

import base64
import hashlib
import json

from tests.test_drafting_tools import MATTER, _email, drafting  # noqa: F401  (the fixture)


class _Graph:
    def __init__(self, found, blobs, fail=None):
        self.found, self.blobs, self.fail, self.listed = found, blobs, fail, []

    def list_attachments(self, message_id):
        self.listed.append(message_id)
        if self.fail:
            raise self.fail
        return self.found

    def spool_attachment(self, message_id, attachment_id):
        blob = self.blobs[attachment_id]
        return {
            "spool_token": attachment_id,
            "filename": f"{attachment_id}.docx",
            "sha256": hashlib.sha256(blob).hexdigest(),
        }


def _graph(monkeypatch, mod, graph):
    monkeypatch.setattr(mod.email_adapter, "email_adapter", lambda: mod.email_adapter.ADAPTER_MSGRAPH)
    monkeypatch.setattr(mod.msgraph_attachments, "list_attachments", graph.list_attachments)
    monkeypatch.setattr(mod.msgraph_attachments, "spool_attachment", graph.spool_attachment)
    monkeypatch.setattr(mod.attachment_spool, "read", lambda token, expected_sha256="": graph.blobs[token])


def _entry(aid, name, inline=False, refused=""):
    return {"attachment_id": aid, "filename": name, "is_inline": inline, "refused": refused}


def test_a_discovery_set_request_carries_its_own_word_documents(drafting, monkeypatch) -> None:  # noqa: F811
    mod, client = drafting
    graph = _Graph(
        [_entry("set1", "set1.docx"), _entry("logo", "logo.png", inline=True), _entry("memo", "memo.pdf")],
        {"set1": b"PK his set"},
    )
    _graph(monkeypatch, mod, graph)
    _email(graph_id="AAMkREQ=")
    out = json.loads(mod.drafting_job_submit({**MATTER, "document_class": "discovery_set"}))
    assert out["accepted"] is True
    env = client.envelopes[0]
    assert graph.listed == ["AAMkREQ="]
    assert [a["filename"] for a in env["attachments"]] == ["set1.docx"]
    assert base64.b64decode(env["attachments"][0]["b64"]) == b"PK his set"


def test_other_classes_carry_no_attachments(drafting, monkeypatch) -> None:  # noqa: F811
    mod, client = drafting
    graph = _Graph([_entry("set1", "set1.docx")], {"set1": b"x"})
    _graph(monkeypatch, mod, graph)
    _email()
    mod.drafting_job_submit(MATTER)
    assert "attachments" not in client.envelopes[0] and graph.listed == []


def test_attachments_that_cannot_be_read_queue_nothing(drafting, monkeypatch) -> None:  # noqa: F811
    mod, client = drafting
    graph = _Graph([], {}, fail=mod.msgraph_attachments.MsGraphAttachmentError("timed out"))
    _graph(monkeypatch, mod, graph)
    _email()
    out = json.loads(mod.drafting_job_submit({**MATTER, "document_class": "discovery_set"}))
    assert out["accepted"] is False and "could not be read" in out["reason"]
    assert client.envelopes == []


def test_too_many_word_documents_queue_nothing(drafting, monkeypatch) -> None:  # noqa: F811
    mod, client = drafting
    graph = _Graph([_entry(f"s{i}", f"s{i}.docx") for i in range(7)], {f"s{i}": b"x" for i in range(7)})
    _graph(monkeypatch, mod, graph)
    _email()
    out = json.loads(mod.drafting_job_submit({**MATTER, "document_class": "discovery_set"}))
    assert out["accepted"] is False and "at most 6" in out["reason"]


def test_a_request_with_no_word_documents_queues_as_before(drafting, monkeypatch) -> None:  # noqa: F811
    mod, client = drafting
    _graph(monkeypatch, mod, _Graph([], {}))
    _email()
    out = json.loads(mod.drafting_job_submit({**MATTER, "document_class": "discovery_set"}))
    assert out["accepted"] is True and "attachments" not in client.envelopes[0]
