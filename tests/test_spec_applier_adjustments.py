"""The applier's second authoring route: standing adjustments (ss-console#2529).

A property's text can come from a distilled ``body``, from a list of confirmed
one-sentence ``adjustments``, or from both. What this file pins:

* the render is BYTE-EXACT and includes the precedence sentence, because the
  model reads the installed file and nothing else — a rule it cannot see is a
  rule that is not in effect, and a file that does not say which text wins when
  two disagree leaves that to the model's mood;
* a property carrying ONLY adjustments installs. This is the ordinary state of a
  firm that told its Operator how its letters should read and never handed over
  a corpus, and it is exactly what the parser refused before this change;
* a malformed item refuses the WHOLE document, the assertions/provenance
  posture. Dropping it would leave the firm believing a sentence it confirmed by
  reply is in effect while nothing anywhere carries it;
* the manifest hash is computed over the RENDERED bytes, so the read mark and
  the spec gate need to know nothing about adjustments;
* a body with no adjustments renders byte-identical to the body, so every spec
  installed before this existed keeps its digest.

THE FALSIFIER for this file, run against 349d86b (the parent commit):
``_parse_one`` requires ``body`` to be a non-empty string, so
``test_a_property_with_only_adjustments_installs`` fails with
``classes.outbound.voice.body: must be a non-empty string`` and nothing installs.
"""

from __future__ import annotations

import hashlib
import io
import json

from spec_applier.applier import (
    MANIFEST_NAME,
    MAX_ADJUSTMENTS,
    SCHEMA_VERSION,
    SpecApplyOutcome,
    apply,
    parse_and_verify,
    render_spec,
)

BUCKET = "smd-customer-config"
SLUG = "ashton-price"
KEY = f"vaults/{SLUG}/output-classes.json"


class _NoSuchKey(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self._objects = objects

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803 — boto3 kwargs
        try:
            return {"Body": io.BytesIO(self._objects[Key])}
        except KeyError as exc:
            raise _NoSuchKey(Key) from exc


ADJ_A = {
    "id": "7f3a2c1d",
    "text": (
        "In client letters and letters to opposing counsel, be more formal and "
        "shorter; no pleasantries."
    ),
    "instructed_by": "chris@firm.com",
    "applied_by": "chris@firm.com",
    "at": "2026-08-21T18:04:00Z",
}
ADJ_B = {
    "id": "0b91ee42",
    "text": "Name the deadline in the first paragraph of any letter that has one.",
    "instructed_by": "sarah@firm.com",
    "at": "2026-08-22T09:00:00Z",
}


def _entry(*, body: str | None = None, adjustments: list[dict] | None = None) -> dict:
    entry: dict = {}
    if body is not None:
        entry["body"] = body
        entry["sha256"] = hashlib.sha256(body.encode()).hexdigest()
    if adjustments is not None:
        entry["adjustments"] = adjustments
    return entry


def _doc(entry: dict, *, klass: str = "outbound", prop: str = "voice") -> bytes:
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "classes": {klass: {prop: entry}}}
    ).encode()


# ---------------------------------------------------------------------------
# The render, byte for byte
# ---------------------------------------------------------------------------


EXPECTED_BLOCK = (
    "## Adjustments\n"
    "\n"
    "Standing instructions from the firm, in effect from the date shown. Each "
    "applies on top of everything above; where an adjustment and the text above "
    "disagree, the adjustment is the firm's later instruction and wins.\n"
    "\n"
    "- [rule 7f3a2c1d] (instructed by chris@firm.com, applied by chris@firm.com, "
    "2026-08-21T18:04:00Z): In client letters and letters to opposing counsel, be "
    "more formal and shorter; no pleasantries.\n"
)


def test_the_rendered_block_is_byte_exact():
    assert render_spec("", [ADJ_A]) == EXPECTED_BLOCK


def test_a_body_and_its_adjustments_are_separated_by_one_blank_line():
    assert render_spec("Write plainly.\n", [ADJ_A]) == "Write plainly.\n\n" + EXPECTED_BLOCK


def test_applied_by_is_omitted_when_the_instructor_applied_it_themselves():
    rendered = render_spec("", [ADJ_B])
    assert "applied by" not in rendered
    assert (
        "- [rule 0b91ee42] (instructed by sarah@firm.com, 2026-08-22T09:00:00Z): "
        "Name the deadline in the first paragraph of any letter that has one.\n"
    ) in rendered


def test_adjustments_render_in_list_order_oldest_first():
    rendered = render_spec("", [ADJ_A, ADJ_B])
    assert rendered.index("7f3a2c1d") < rendered.index("0b91ee42")


def test_a_body_with_no_adjustments_renders_byte_identical():
    """Why every spec installed before this existed keeps its exact digest."""
    body = "Short sentences. No hedging.\n"
    assert render_spec(body, []) == body


# ---------------------------------------------------------------------------
# Parse + install
# ---------------------------------------------------------------------------


def test_a_property_with_only_adjustments_installs(tmp_path):
    """THE falsifier case: no body, no declared hash, one confirmed sentence.

    A&P declares ``work_product`` with no voice spec installed. The first thing
    that firm will ever author is a sentence in an email, and before this change
    the parser refused the document it produced.
    """
    s3 = FakeS3({KEY: _doc(_entry(adjustments=[ADJ_A]))})
    result = apply(s3_client=s3, bucket=BUCKET, slug=SLUG, spec_dir=tmp_path)
    assert result.outcome is SpecApplyOutcome.APPLIED
    installed = (tmp_path / "classes/outbound/voice.md").read_text()
    assert installed == EXPECTED_BLOCK


def test_the_manifest_hashes_the_rendered_bytes_not_the_body(tmp_path):
    """So the read mark and the spec gate need to know nothing about any of this."""
    body = "Write plainly."
    s3 = FakeS3({KEY: _doc(_entry(body=body, adjustments=[ADJ_A]))})
    apply(s3_client=s3, bucket=BUCKET, slug=SLUG, spec_dir=tmp_path)
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    entry = manifest["specs"]["classes/outbound/voice.md"]
    on_disk = (tmp_path / "classes/outbound/voice.md").read_bytes()
    assert entry["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert entry["sha256"] != hashlib.sha256(body.encode()).hexdigest()
    assert entry["bytes"] == len(on_disk)


def test_the_body_hash_still_covers_the_body_alone(tmp_path):
    """The declared hash is the author's claim about their body, unchanged.

    If it covered the render, no author could ever compute it: the render
    depends on adjustments the author does not hold.
    """
    body = "Write plainly."
    entry = _entry(body=body, adjustments=[ADJ_A])
    specs, errors = parse_and_verify(_doc(entry))
    assert errors == []
    assert specs[0].body.decode().startswith(body)
    assert specs[0].adjustments == (
        {
            "id": ADJ_A["id"],
            "text": ADJ_A["text"],
            "instructed_by": ADJ_A["instructed_by"],
            "at": ADJ_A["at"],
            "applied_by": ADJ_A["applied_by"],
        },
    )


def test_a_wrong_body_hash_still_refuses_when_adjustments_are_present():
    entry = _entry(body="Write plainly.", adjustments=[ADJ_A])
    entry["sha256"] = "0" * 64
    _specs, errors = parse_and_verify(_doc(entry))
    assert any("does not match" in e for e in errors)


# ---------------------------------------------------------------------------
# Refusals — a malformed item takes the whole document
# ---------------------------------------------------------------------------


def _refusal(bad: object, needle: str):
    _specs, errors = parse_and_verify(_doc(_entry(body="Write plainly.", adjustments=bad)))
    assert errors, f"expected a refusal for {bad!r}"
    assert any(needle in e for e in errors), errors


def test_a_non_list_adjustments_value_refuses():
    _refusal("be formal", "must be a list")


def test_a_non_object_item_refuses():
    _refusal(["be formal"], "must be an object")


def test_a_bad_id_refuses():
    _refusal([dict(ADJ_A, id="7F3A2C1D")], "eight lowercase hex")
    _refusal([dict(ADJ_A, id="7f3a")], "eight lowercase hex")


def test_a_duplicate_id_refuses():
    _refusal([ADJ_A, dict(ADJ_A, text="something else")], "appears twice")


def test_a_missing_required_field_refuses():
    for field in ("text", "instructed_by", "at"):
        _refusal([{k: v for k, v in ADJ_A.items() if k != field}], f"{field}: must be")


def test_an_empty_applied_by_refuses():
    _refusal([dict(ADJ_A, applied_by="  ")], "applied_by: must be")


def test_a_line_break_in_the_text_refuses():
    """One adjustment renders as one bullet.

    A line break would produce a file whose shape does not match what the parser
    believes it wrote. The broker normalizes it away before the person ever
    confirms; this is the half of that agreement that lives on the seat.
    """
    _refusal([dict(ADJ_A, text="Be formal.\nBe short.")], "line break")


def test_an_oversize_text_refuses():
    _refusal([dict(ADJ_A, text="x" * 2001)], "byte ceiling")


def test_past_the_entry_ceiling_refuses():
    many = [dict(ADJ_A, id=f"{i:08x}") for i in range(MAX_ADJUSTMENTS + 1)]
    _refusal(many, f"exceeds the {MAX_ADJUSTMENTS}-entry ceiling")


def test_one_bad_item_discards_the_specs_that_did_verify():
    """All or nothing, across classes — the document's author and its bytes
    disagree somewhere, so nothing in it is trusted more than the part that
    failed."""
    doc = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "classes": {
                "outbound": {"voice": _entry(body="Fine.")},
                "staff": {"voice": _entry(body="Also fine.", adjustments=[{"id": "nope"}])},
            },
        }
    ).encode()
    specs, errors = parse_and_verify(doc)
    assert specs == []
    assert errors


def test_a_property_with_neither_a_body_nor_adjustments_still_refuses():
    _specs, errors = parse_and_verify(_doc({"adjustments": []}))
    assert any("body: must be a non-empty string" in e for e in errors)
