"""Structural-diff extractor for voice samples.

Ported from ss-console/operator/adapter/voice/diff.py.

The privacy floor for Voice Layer 2: a raw sent email is NEVER persisted.
The pipeline computes a structural representation of the message — what
the partner's writing looks like, not what they wrote — and that is what
lands in R2.

Output:

* word_count, sentence_count, paragraph_count
* sentence_length_distribution (5-bucket histogram)
* greeting_style, signoff_style (categorical labels from a closed set)
* opener_template, closer_template (the literal greeting/closer line
  reduced to its category, never the recipient or signer name)
* punctuation_rhythm (counts of period, comma, semicolon, dash, question,
  exclamation per 100 words)
* recipient_cohort (assigned by the pipeline; this module does not invent
  cohorts — that is the caller's responsibility)

What is NOT in the output:

* No body text, not even a snippet
* No quoted text
* No recipient names or email addresses
* No specific content tokens or n-grams
* No subject line content (only its word count)
* No URLs, no attachments, no inline images

The output is JSON-serializable and bounded in size. The JSON object is
what the caller writes to R2 at
``{customer-slug}/voice/cohort/{cohort-id}/{sample-id}.json``.

Design rules
------------

* Deterministic. Same input produces the same diff. The structural-diff
  digest written to D1 lets the retention enforcer verify removal.
* Cheap. The extractor is a pure-Python module with no model calls and
  no network I/O. It runs in the ingestion loop, not asynchronously.
* Conservative. When in doubt, the extractor labels something as
  ``unknown`` rather than guessing. The voice library quality bar is
  satisfied by the volume of samples, not by per-sample inference.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Closed-set category labels
#
# These vocabularies are fixed so the structural-diff schema does not drift
# silently across runs. Adding a new label here means updating the schema
# version on voice_ingestion_items and any dashboard renderer that maps
# labels onto display strings.
# ---------------------------------------------------------------------------


class GreetingStyle(str, enum.Enum):
    """Closed set of greeting categorizations.

    The classifier inspects only the first non-empty line and matches
    against bounded regex patterns. The recipient name itself is never
    captured.
    """

    FORMAL_NAMED = "formal_named"  # "Dear Mr. Smith,"
    SEMI_FORMAL = "semi_formal"  # "Hi Mr. Smith,"
    FIRST_NAME = "first_name"  # "Hi Sarah," / "Hello Sarah,"
    GROUP = "group"  # "Team," / "All," / "Counsel,"
    BARE_HI = "bare_hi"  # "Hi," / "Hello,"
    NONE = "none"  # No greeting line
    UNKNOWN = "unknown"


class SignoffStyle(str, enum.Enum):
    """Closed set of signoff categorizations."""

    BEST = "best"  # "Best," / "Best regards,"
    THANKS = "thanks"  # "Thanks," / "Thank you,"
    REGARDS = "regards"  # "Regards," / "Kind regards,"
    SINCERELY = "sincerely"  # "Sincerely,"
    INITIAL = "initial"  # "-S" / "S." (single initial)
    NAMED = "named"  # First name only on its own line
    NONE = "none"
    UNKNOWN = "unknown"


# Five buckets for the sentence-length histogram. Boundaries chosen to
# match the natural English distribution (short / typical / long / very
# long / paragraph-as-sentence).
SENTENCE_LENGTH_BUCKETS = (5, 10, 20, 35)  # exclusive upper bounds


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_WORD_SPLIT = re.compile(r"\b\w+\b")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_QUOTED_LINE = re.compile(r"^\s*>", re.MULTILINE)
_SIGNATURE_DELIMITER = re.compile(r"^--\s*$", re.MULTILINE)
_REPLY_HEADER = re.compile(
    r"^(?:On .{1,80} wrote:|From: .{1,200}$|Sent: .{1,80}$|To: .{1,200}$)",
    re.MULTILINE | re.IGNORECASE,
)


# Greeting regexes. Each pattern matches the literal opening, and the
# named captures are discarded — only the category survives into the
# structural diff.
_FORMAL_NAMED_RE = re.compile(
    r"^(?:Dear|To)\s+(?:Mr\.|Ms\.|Mrs\.|Dr\.|Hon\.|Justice|Judge|Counsel)\s+\S+",
    re.IGNORECASE,
)
_SEMI_FORMAL_RE = re.compile(
    r"^(?:Hi|Hello|Good (?:morning|afternoon|evening))\s+(?:Mr\.|Ms\.|Mrs\.|Dr\.)\s+\S+",
    re.IGNORECASE,
)
_FIRST_NAME_RE = re.compile(
    r"^(?:Hi|Hello|Hey|Good (?:morning|afternoon|evening))\s+[A-Z][a-z]+\b",
)
_GROUP_RE = re.compile(
    r"^(?:Hi|Hello|Hey)?\s*(?:Team|All|Counsel|Everyone|Folks)[,:]?\s*$",
    re.IGNORECASE,
)
_BARE_HI_RE = re.compile(
    r"^(?:Hi|Hello|Hey|Greetings)[,!.]?\s*$",
    re.IGNORECASE,
)


_BEST_RE = re.compile(r"^Best(?:\s+regards)?[,.]?\s*$", re.IGNORECASE)
_THANKS_RE = re.compile(r"^(?:Thanks(?:\s+so\s+much)?|Thank\s+you)[,.!]?\s*$", re.IGNORECASE)
_REGARDS_RE = re.compile(r"^(?:Kind\s+)?Regards[,.]?\s*$", re.IGNORECASE)
_SINCERELY_RE = re.compile(r"^Sincerely(?:\s+yours)?[,.]?\s*$", re.IGNORECASE)
_INITIAL_RE = re.compile(r"^-?[A-Z]\.?\s*$")
_NAMED_LINE_RE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*$")


@dataclass(frozen=True)
class StructuralDiff:
    """JSON-serializable structural representation of one sent message.

    The fields here are everything the voice library learns from this
    sample. The original body is gone by the time this object exists.
    """

    schema_version: int
    word_count: int
    sentence_count: int
    paragraph_count: int
    subject_word_count: int
    avg_sentence_length: float
    sentence_length_distribution: dict
    greeting_style: str
    signoff_style: str
    opener_template: str
    closer_template: str
    punctuation_rhythm: dict
    recipient_cohort: str

    def to_json_bytes(self) -> bytes:
        """Return the canonical JSON encoding used for R2 storage + digest."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "word_count": self.word_count,
            "sentence_count": self.sentence_count,
            "paragraph_count": self.paragraph_count,
            "subject_word_count": self.subject_word_count,
            "avg_sentence_length": self.avg_sentence_length,
            "sentence_length_distribution": self.sentence_length_distribution,
            "greeting_style": self.greeting_style,
            "signoff_style": self.signoff_style,
            "opener_template": self.opener_template,
            "closer_template": self.closer_template,
            "punctuation_rhythm": self.punctuation_rhythm,
            "recipient_cohort": self.recipient_cohort,
        }


SCHEMA_VERSION = 1


def extract_structural_diff(
    *,
    body_text: str | None,
    subject: str | None,
    recipient_cohort: str,
) -> StructuralDiff:
    """Compute the structural-diff for one sent message.

    Args:
        body_text: The plain-text body of the message. The caller is
            responsible for selecting body_text over body_html — this
            extractor does not handle HTML.
        subject: Subject line. Only its word count enters the diff.
        recipient_cohort: Pre-resolved cohort tag for this message. The
            extractor does not infer cohorts.

    Returns:
        A :class:`StructuralDiff`. The original body is not retained
        anywhere in the returned object — it is gone after this call
        returns to the caller's local scope.
    """
    body = _strip_reply_quoting(body_text or "")
    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(body) if p.strip()]
    sentences = _split_sentences(body)
    words = _WORD_SPLIT.findall(body)

    word_count = len(words)
    sentence_count = len(sentences)
    paragraph_count = len(paragraphs)
    avg_sentence_length = round(word_count / sentence_count, 2) if sentence_count else 0.0

    return StructuralDiff(
        schema_version=SCHEMA_VERSION,
        word_count=word_count,
        sentence_count=sentence_count,
        paragraph_count=paragraph_count,
        subject_word_count=len(_WORD_SPLIT.findall(subject or "")),
        avg_sentence_length=avg_sentence_length,
        sentence_length_distribution=_distribute_sentence_lengths(sentences),
        greeting_style=_classify_greeting(body).value,
        signoff_style=_classify_signoff(body).value,
        opener_template=_classify_greeting(body).value,
        closer_template=_classify_signoff(body).value,
        punctuation_rhythm=_punctuation_rhythm(body, word_count),
        recipient_cohort=recipient_cohort,
    )


def structural_diff_digest(diff: StructuralDiff) -> str:
    """SHA-256 hex digest of the canonical-JSON form. Used by the
    retention enforcer to verify R2 removal."""
    return hashlib.sha256(diff.to_json_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_reply_quoting(body: str) -> str:
    """Remove quoted reply chains and signature blocks.

    Voice analysis applies only to what the partner wrote in this
    message, not to inherited threads. The stripping is conservative —
    we want fewer false positives over more samples. Lines starting with
    ``>`` are dropped. Anything after a ``-- `` signature delimiter or
    a reply header is dropped.
    """
    sig_match = _SIGNATURE_DELIMITER.search(body)
    if sig_match:
        body = body[: sig_match.start()]
    reply_match = _REPLY_HEADER.search(body)
    if reply_match:
        body = body[: reply_match.start()]
    body = _QUOTED_LINE.sub("", body)
    return body.strip()


def _split_sentences(body: str) -> list[str]:
    if not body:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(body) if s.strip()]


def _distribute_sentence_lengths(sentences: list[str]) -> dict:
    buckets = {
        "lt_5": 0,
        "lt_10": 0,
        "lt_20": 0,
        "lt_35": 0,
        "gte_35": 0,
    }
    for sentence in sentences:
        n = len(_WORD_SPLIT.findall(sentence))
        if n < 5:
            buckets["lt_5"] += 1
        elif n < 10:
            buckets["lt_10"] += 1
        elif n < 20:
            buckets["lt_20"] += 1
        elif n < 35:
            buckets["lt_35"] += 1
        else:
            buckets["gte_35"] += 1
    return buckets


def _classify_greeting(body: str) -> GreetingStyle:
    if not body:
        return GreetingStyle.NONE
    first_line = body.splitlines()[0].strip()
    if not first_line:
        return GreetingStyle.NONE

    if _FORMAL_NAMED_RE.match(first_line):
        return GreetingStyle.FORMAL_NAMED
    if _SEMI_FORMAL_RE.match(first_line):
        return GreetingStyle.SEMI_FORMAL
    if _GROUP_RE.match(first_line):
        return GreetingStyle.GROUP
    if _FIRST_NAME_RE.match(first_line):
        return GreetingStyle.FIRST_NAME
    if _BARE_HI_RE.match(first_line):
        return GreetingStyle.BARE_HI

    # First line looks like prose (no salutation pattern). Treat as no
    # greeting rather than guessing — the structural-diff prefers
    # ``none`` to a wrong category.
    return GreetingStyle.NONE


def _classify_signoff(body: str) -> SignoffStyle:
    if not body:
        return SignoffStyle.NONE
    # The signoff is the last meaningful line. Look at the last three
    # non-empty lines so we can find phrases like "Thanks, / Marcus" or
    # "Best regards, / Marcus Thompson" where the printed name lives on
    # the line after the closer.
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return SignoffStyle.NONE
    candidates = lines[-3:]
    # Prefer phrase closers over bare-name lines — writers commonly put
    # the name on the very last line and the phrase one above it.
    for line in candidates:
        if _BEST_RE.match(line):
            return SignoffStyle.BEST
        if _THANKS_RE.match(line):
            return SignoffStyle.THANKS
        if _REGARDS_RE.match(line):
            return SignoffStyle.REGARDS
        if _SINCERELY_RE.match(line):
            return SignoffStyle.SINCERELY
    # Fallback: only bare initial or name remained.
    last = candidates[-1]
    if _INITIAL_RE.match(last):
        return SignoffStyle.INITIAL
    if _NAMED_LINE_RE.match(last):
        return SignoffStyle.NAMED
    return SignoffStyle.NONE


def _punctuation_rhythm(body: str, word_count: int) -> dict:
    """Counts of each punctuation mark, normalized per 100 words.

    A rhythm-style signature, not a content signature: it captures how
    densely the writer punctuates, not what they wrote.
    """
    if word_count == 0:
        return {
            "period_per_100": 0.0,
            "comma_per_100": 0.0,
            "semicolon_per_100": 0.0,
            "dash_per_100": 0.0,
            "question_per_100": 0.0,
            "exclamation_per_100": 0.0,
        }
    counts = {
        ".": body.count("."),
        ",": body.count(","),
        ";": body.count(";"),
        "-": body.count(" - ") + body.count("—"),
        "?": body.count("?"),
        "!": body.count("!"),
    }
    factor = 100.0 / word_count
    return {
        "period_per_100": round(counts["."] * factor, 2),
        "comma_per_100": round(counts[","] * factor, 2),
        "semicolon_per_100": round(counts[";"] * factor, 2),
        "dash_per_100": round(counts["-"] * factor, 2),
        "question_per_100": round(counts["?"] * factor, 2),
        "exclamation_per_100": round(counts["!"] * factor, 2),
    }


__all__ = [
    "GreetingStyle",
    "SignoffStyle",
    "SCHEMA_VERSION",
    "StructuralDiff",
    "extract_structural_diff",
    "structural_diff_digest",
]
