"""Peel a ``post_tool_call`` result down to the tool's own answer.

ONE place for the two wrappers every post-hook consumer has to see through,
because three consumers each learned them separately and each learned them from
a live failure:

* the INBOUND FENCE (``unwrap_inbound``). Hermes v0.20.4 fires
  ``transform_tool_result`` BEFORE ``post_tool_call``, so a fenced read tool's
  result arrives at the post hook already wrapped in the quarantine fence
  (ss#2444, ``plugins/hermes-smd-establishment`` and ``shared.read_volume``);
* the DISPATCHER ENVELOPE (``peel_envelopes``): ``{"result": "<the tool's JSON,
  as a string>"}`` (live-caught on pilot-smokeball 2026-08-11).

The audit plugin's outcome inference is the third consumer. It read neither
wrapper, so every fenced or enveloped result — a refusal, an unreadable scan, a
spent allowance — scored ``ok`` (ss-console shortfall notifications).

``shared.inbound.unwrap_inbound`` and the establishment plugin's
``_unwrap_read_result`` delegate here, so the behaviour they had is the
behaviour this has; nothing about either changed in the move.
"""

from __future__ import annotations

import json
import re
from typing import Any

# The inverse of :func:`wrap_inbound`. The nonce is matched as a BACKREFERENCE so
# a body containing a forged or prior sentinel cannot terminate the real fence —
# the same unguessable-nonce property the wrap relies on, read in reverse.
_FENCE_RE = re.compile(
    r"<<<INBOUND_DATA_BEGIN (?P<nonce>[^>]+)>>>\n(?P<body>.*)\n<<<INBOUND_DATA_END (?P=nonce)>>>",
    re.DOTALL,
)


def unwrap_inbound(text: str) -> str:
    """Return the content inside a quarantine fence, or ``text`` unchanged.

    WHY THIS EXISTS (ss#2444, live-caught on hermes-ashton-price 2026-08-20).
    Hermes v0.20.4 INVERTED the order two hooks fire in. On v0.18 the order was
    ``pre_tool_call -> post_tool_call -> transform_tool_result``; on v0.20.4 it
    is ``pre_tool_call -> transform_tool_result -> post_tool_call``, observed on
    five consecutive tool calls on the same seat and log
    (``vfy_01M0G7DYTBHAGDRQYXX02DKMZJ``).

    ``hermes-smd-inbound`` applies the fence at ``transform_tool_result``, so an
    ``on_post_tool_call`` consumer that used to receive the connector's raw text
    now receives the FENCED text. ``hermes-smd-establishment`` parsed that with
    ``json.loads`` and threw on 100% of document reads — char 0 is ``[``, char 1
    is ``U`` of ``UNTRUSTED``, which is exactly
    ``JSONDecodeError: Expecting value: line 1 column 2 (char 1)``.

    Pass-through on unfenced input is deliberate and load-bearing: it makes the
    consumer correct under BOTH hook orders, so this does not become a second
    invariant to break the next time upstream reorders. Callers must not use the
    return value to decide whether content was untrusted — the envelope, not the
    absence of a fence, is what carries provenance.
    """
    if not isinstance(text, str) or "<<<INBOUND_DATA_BEGIN " not in text:
        return text
    m = _FENCE_RE.search(text)
    return m.group("body") if m else text


def peel_envelopes(payload: Any) -> Any:
    """Peel the dispatcher envelopes off a ``post_tool_call`` result.

    LIVE-CAUGHT (pilot-smokeball, 2026-08-11T17:25, first reference-staging run):
    the hook's ``result`` string is not the connector's JSON — it is
    ``{"result": "<the connector's JSON, as a string>"}``. The capture parsed
    the outer object, found no top-level ``text`` key, and returned through the
    silent no-``text`` guard: no warning, no capture, and every stage in the
    turn refused ``no_capture`` while the model had genuinely read all four
    documents. The Operator's report of that failure was exactly honest, which
    is the one part of the run that worked as designed.

    Two envelope shapes are peeled, at most twice (a wrapper of a wrapper),
    conservatively — anything unrecognized is returned as-is so the existing
    guards keep their meaning:

    * ``{"result": <str|dict>}`` — the live dispatcher wrapper. A ``str`` value
      that parses as JSON is parsed; a dict value is taken directly.
    * ``{"content": [{"type": "text", "text": <str>}, ...]}`` — the MCP
      content-block envelope, in case a future Hermes hands the protocol shape
      through. The first text block that parses as a JSON object wins.

    The unwrap stops as soon as the current object looks like the connector's
    own read result (a dict carrying ``text``), so a connector that one day
    returns a field literally named ``result`` alongside ``text`` is not
    re-unwrapped into garbage.
    """
    for _ in range(2):
        if not isinstance(payload, dict) or "text" in payload:
            return payload
        if "result" in payload and len(payload) <= 2:
            inner = payload["result"]
            if isinstance(inner, str):
                try:
                    payload = json.loads(inner)
                except (TypeError, ValueError):
                    return payload
                continue
            if isinstance(inner, dict):
                payload = inner
                continue
            return payload
        blocks = payload.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        candidate = json.loads(block["text"])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(candidate, dict):
                        payload = candidate
                        break
            else:
                return payload
            continue
        return payload
    return payload


def parse_tool_result(result: Any) -> Any:
    """The tool's own answer from a raw ``post_tool_call`` result, or ``None``.

    Strips the inbound fence, parses a JSON object, and peels the dispatcher
    envelopes. A dict passes straight to the peel. Anything that is not a JSON
    object after the fence comes off (prose, a list, unparseable text) is
    ``None``: there is nothing structured to read, and a caller must not invent
    a verdict from it.
    """
    if isinstance(result, dict):
        return peel_envelopes(result)
    if not isinstance(result, str) or not result:
        return None
    text = unwrap_inbound(result).strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return peel_envelopes(parsed)


__all__ = ["parse_tool_result", "peel_envelopes", "unwrap_inbound"]
