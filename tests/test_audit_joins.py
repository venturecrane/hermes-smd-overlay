"""The ledger can name the person, the message, and the matter (ss-console#2497).

WHAT THIS PINS, AND WHY THE PINS ARE SHAPED THIS WAY. Measured on the live
ashton-price ledger (1,473 rows, 2026-08-21, ``vfy_01M0H8DR6JAPYVHFMNJZXQZ517``)
and on the pilot's 8,913: an ``INBOUND_RECEIVED`` row carried a locally minted
``item_id`` and named no sender; ``REPLY_SENT`` carried ``session_id`` on 0 of 8
rows and ``matter_ref`` on none; ``TOOL_CALL_COMPLETED`` named the tool and not
the object. Reconstructing one real trace worked only because the rows happened
to be seconds apart — a join by timestamp adjacency, which is not a join.

Every assertion below is written so it FAILS on the code that shipped before this
change, and each falsifier is stated at its test. A test that would pass either
way measures nothing (Law 12).

Nothing here asserts on a raw email address, deliberately: the issue's non-goal
is that no row may carry one, and a test that pinned an address in a fixture
would be arguing the opposite case in the same repo.
"""

from __future__ import annotations

import hashlib

import pytest

from shared import matter_binding, matter_gate, object_identity
from shared.audit_contract import (
    COLUMNS,
    JOIN_KEYS,
    agent_event_params,
    canonical_address,
    sender_key,
)

_ADDRESS = "Paralegal <Paralegal@example.test>"
_BARE = "paralegal@example.test"


# ---------------------------------------------------------------------------
# sender_key — the person, as a key rather than an address
# ---------------------------------------------------------------------------


def test_sender_key_is_the_sha256_of_the_canonical_address():
    """FALSIFIER: hashing the raw string instead of the canonical form gives a
    different digest, and this equality is the one the ss-console broker helper
    must also produce. If either side stops canonicalizing, the two ends of the
    join stop meeting."""
    assert canonical_address(_BARE) == _BARE
    assert sender_key(_BARE) == hashlib.sha256(_BARE.encode("utf-8")).hexdigest()
    assert sender_key(_BARE) != hashlib.sha256(b"Paralegal@example.test").hexdigest()


def test_sender_key_folds_case_and_surrounding_space():
    """One person must produce ONE key. FALSIFIER: drop ``.strip().lower()`` and
    the same human arrives as three different identities in the ledger."""
    assert sender_key("  Paralegal@Example.TEST  ") == sender_key(_BARE)


def test_sender_key_folds_the_two_unicode_encodings_of_one_name():
    """NFC is the load-bearing half. FALSIFIER: remove the normalize() call and
    these two spellings of the same mailbox hash differently, so the ledger
    reports two people where the mail system sees one."""
    precomposed = "rené@example.test"
    decomposed = "rené@example.test"
    assert precomposed != decomposed
    assert sender_key(precomposed) == sender_key(decomposed)


def test_sender_key_is_absent_rather_than_empty_when_there_is_no_address():
    """Absent must read as absent. FALSIFIER: return the digest of "" and every
    unattributed row shares one plausible-looking identity."""
    assert sender_key("") is None
    assert sender_key("   ") is None
    assert sender_key(None) is None
    assert sender_key(42) is None


def test_sender_key_never_contains_the_address():
    """The whole point of hashing: an export leaves the Machine."""
    key = sender_key(_ADDRESS)
    assert key is not None
    assert "@" not in key
    assert "example.test" not in key


# ---------------------------------------------------------------------------
# agent_event_params — session in metadata, matter in the COLUMN
# ---------------------------------------------------------------------------


def _by_column(params):
    return dict(zip(COLUMNS, params, strict=True))


def test_matter_ref_lands_in_the_column_not_only_metadata():
    """The AC's exact requirement. FALSIFIER: before this change
    ``agent_event_params`` had no ``matter_ref`` argument at all, so every send
    row left the column NULL while the portal record
    (``object-audit-record.ts``) filters on precisely that column."""
    row = _by_column(agent_event_params(action_type="REPLY_SENT", matter_ref="m-1"))
    assert row["matter_ref"] == "m-1"


def test_session_id_lands_in_metadata_under_the_canonical_spelling():
    """There is no session column, so the join is a json_extract and every
    emitter must spell it identically. FALSIFIER: name it ``session`` and the
    reply rows stop joining to the per-tool rows that already say
    ``session_id``."""
    row = _by_column(agent_event_params(action_type="REPLY_SENT", session_id="s-1"))
    assert '"session_id":"s-1"' in row["metadata"]


def test_an_explicit_session_id_wins_over_one_a_caller_left_in_metadata():
    row = _by_column(
        agent_event_params(
            action_type="REPLY_SENT",
            metadata={"session_id": "stale"},
            session_id="s-1",
        )
    )
    assert '"session_id":"s-1"' in row["metadata"]
    assert "stale" not in row["metadata"]


def test_absent_joins_stay_null_rather_than_empty_strings():
    """The chain canonicalizes "" distinctly from NULL, and an empty matter_ref
    reads as a reference that is present and blank. FALSIFIER: pass the raw
    argument through and a send with no matter records one."""
    row = _by_column(agent_event_params(action_type="REPLY_SENT", session_id="", matter_ref=""))
    assert row["matter_ref"] is None
    assert row["metadata"] is None


def test_the_join_vocabulary_is_pinned():
    """Two repos and six writers spell these; a rename in one place is a query
    that silently reaches half the rows."""
    assert set(JOIN_KEYS) == {
        "sender_key",
        "vendor_message_id",
        "session_id",
        "matter_ref",
        "document_id",
        "memo_id",
        "draft_id",
        "written_body_sha256",
    }


def test_the_hash_canonicalization_is_untouched():
    """No new COLUMN, so no existing row's stored hash can stop verifying. The
    twelve hashed columns are the contract (``shared/audit_chain.py``)."""
    from shared import audit_chain

    assert len(COLUMNS) == 12
    values = agent_event_params(action_type="REPLY_SENT", session_id="s", matter_ref="m", now_ms=0)
    assert len(values) == 12
    # Two rows differing only in metadata hash differently; that is the whole
    # reason a new metadata KEY is safe while a new column would not be.
    a = audit_chain.compute_row_hash("prev", list(values))
    b = audit_chain.compute_row_hash("prev", list(values[:-1]) + [None])
    assert a != b


# ---------------------------------------------------------------------------
# matter_ref_for — the matter, or honestly nothing
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_bindings():
    matter_binding._reset_for_tests()
    yield
    matter_binding._reset_for_tests()


_M_A = "11111111-1111-1111-1111-111111111111"
_M_B = "22222222-2222-2222-2222-222222222222"
_NUM_A = "2026-PI-101"


def test_a_single_read_matter_becomes_the_matter_ref():
    matter_binding.membership_for("s1").note_content_read(_M_A)
    assert matter_gate.matter_ref_for("s1") == _M_A


def test_two_read_matters_resolve_to_nothing():
    """THE #2167 PROPERTY. FALSIFIER: return ``sorted(read)[0]`` and the row for
    session 20260820_195837_68d654ce names one of the four matters it touched,
    which reads as an exoneration for the other three."""
    matter_binding.membership_for("s1").note_content_read(_M_A)
    matter_binding.membership_for("s1").note_content_read(_M_B)
    assert matter_gate.matter_ref_for("s1") is None


def test_a_cited_number_resolves_to_the_connector_id():
    """The column must hold ONE kind of value. The per-tool writer puts the
    connector id there, so a firm-facing number must be joined to its id rather
    than written raw. FALSIFIER: return the token and the column carries ids on
    some rows and numbers on others, which cannot be joined at all."""
    membership = matter_binding.membership_for("s1")
    membership.add_alias(_NUM_A, _M_A)
    membership.note_content_read(_M_B)
    assert matter_gate.matter_ref_for("s1", (_NUM_A,)) == _M_A


def test_a_body_citing_two_matters_resolves_to_nothing():
    membership = matter_binding.membership_for("s1")
    membership.add_alias(_NUM_A, _M_A)
    membership.add_alias("2026-PI-102", _M_B)
    assert matter_gate.matter_ref_for("s1", (_NUM_A, "2026-PI-102")) is None


def test_a_falsy_session_never_borrows_the_shared_bucket():
    """``resolve_session`` returns "" under MODE_AMBIGUOUS / MODE_NONE and every
    unkeyed context shares that one bucket, so a ref read from it could name
    another session's matter. FALSIFIER: drop the guard and this returns the
    unkeyed bucket's matter."""
    matter_binding.membership_for("").note_content_read(_M_A)
    assert matter_gate.matter_ref_for("") is None
    assert matter_gate.matter_ref_for(None) is None


# ---------------------------------------------------------------------------
# object_identity — which document, which memo, and what was written
# ---------------------------------------------------------------------------


def test_read_document_names_the_document_from_the_result():
    """Shape pinned to the one ``hermes-smd-establishment``'s read capture reads
    off the live payload (``fileId`` / ``matterId`` / ``text``)."""
    out = object_identity.extract(
        "mcp_smokeball_read_document",
        {"matter_id": _M_A, "file_id": "args-doc"},
        f'{{"fileId": "result-doc", "matterId": "{_M_A}", "text": "x", "total_chars": 1}}',
    )
    # RESULT over ARGS: the result is the source system's echo, the args are the
    # model's composition.
    assert out["document_id"] == "result-doc"


def test_read_document_falls_back_to_the_args_when_the_result_names_nothing():
    out = object_identity.extract(
        "mcp_smokeball_read_document", {"file_id": "args-doc"}, "not json at all"
    )
    assert out["document_id"] == "args-doc"


def test_a_file_listing_names_what_it_exposed_and_admits_truncation():
    rows = ",".join(f'{{"id": "f{i}"}}' for i in range(25))
    out = object_identity.extract(
        "mcp_smokeball_get_files_on_matter", {"matter_id": _M_A}, f'{{"value": [{rows}]}}'
    )
    assert out["document_ids"][0] == "f0"
    assert len(out["document_ids"]) == 20
    assert out["document_ids_truncated"] is True


def test_create_memo_carries_the_memo_id_and_a_digest_of_what_it_wrote():
    """FALSIFIER: before this change the row said a memo was written on a matter
    and could name neither the memo nor its content, so nothing tied the row to
    the memo sitting in the firm's system."""
    out = object_identity.extract(
        "mcp_smokeball_create_memo",
        {"matter_id": _M_A, "text": "the note body"},
        '{"id": "memo-9"}',
    )
    assert out["memo_id"] == "memo-9"
    assert out["written_body_sha256"] == hashlib.sha256(b"the note body").hexdigest()
    assert out["written_body_field"] == "text"


def test_no_audit_metadata_ever_carries_the_body_itself():
    """The digest is the whole point: it proves WHICH body without holding one."""
    body = "Confidential settlement figure and the client's own words."
    out = object_identity.extract("mcp_smokeball_create_memo", {"text": body}, '{"id": "memo-9"}')
    for value in out.values():
        assert body not in str(value)
        assert "settlement" not in str(value)


def test_deliver_draft_records_the_digest_and_seam_and_invents_no_id():
    """``smd_deliver_draft`` returns an authorization SENTENCE and mints no id
    (``plugins/hermes-smd-drafting``). Recording a plausible ``draft_id`` here
    would be a field no query ever matches."""
    out = object_identity.extract(
        "smd_deliver_draft",
        {"output_class": "work_product", "body": "the draft", "seam": "smokeball_memo"},
        "Authorized: this 'work_product' draft satisfies the authored spec.",
    )
    assert out["written_body_sha256"] == hashlib.sha256(b"the draft").hexdigest()
    assert out["seam"] == "smokeball_memo"
    assert "draft_id" not in out


def test_create_draft_carries_the_draft_id_and_the_body_field_it_digested():
    out = object_identity.extract(
        "mcp_agentmail_create_draft",
        {"to": ["x@y.test"], "text": "hello", "html": "<p>hello</p>"},
        '{"draft_id": "d-1"}',
    )
    assert out["draft_id"] == "d-1"
    assert out["written_body_sha256"] == hashlib.sha256(b"hello").hexdigest()
    # Naming the digested field is what makes the digest reproducible rather
    # than something a reader has to trust.
    assert out["written_body_field"] == "text"


def test_an_empty_body_records_no_digest():
    """A row must distinguish "wrote nothing" from "wrote something that hashes
    to e3b0c442…"."""
    assert object_identity.body_sha256("") is None
    out = object_identity.extract("smd_deliver_draft", {"body": ""}, "ok")
    assert "written_body_sha256" not in out


def test_a_tool_with_no_object_contributes_no_keys():
    assert object_identity.extract("mcp_smokeball_get_matter", {"matter_id": _M_A}, "{}") == {}


def test_extraction_never_raises_on_a_malformed_result():
    """It runs inside the audit writer on every tool call; the ROW is the
    obligation and this is decoration on it."""
    for bad in (None, 3, [], object(), '{"value": "not a list"}', "{"):
        assert isinstance(object_identity.extract("mcp_smokeball_read_document", {}, bad), dict)
