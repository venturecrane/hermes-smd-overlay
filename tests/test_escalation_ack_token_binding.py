"""An ack's token must belong to the item it acknowledges (J-b).

``validate_append`` matched an ``acked`` event to a prior raise by token OR by
item_key, so an ack naming item A while quoting item B's code found B's raise
by token and was accepted. ``derive_state`` folds by item_key, so A read as
acknowledged although nobody confirmed A. The token is derived from the
item_key (``token_for``), so the two must agree before any raise is searched.
"""

from __future__ import annotations

import pytest

from shared import escalation_ledger as el


def _raise(key: str) -> dict:
    return el.make_event(
        skill="deadline-miss-escalator",
        matter_id="m-1",
        item_key=key,
        event="fired",
        attempt=1,
        token=el.token_for(key),
    )


def _ack(key: str, token: str | None) -> dict:
    return el.make_event(
        skill="deadline-miss-escalator",
        matter_id="m-1",
        item_key=key,
        event="acked",
        attempt=1,
        token=token,
    )


KEY_A = "a" * 64
KEY_B = "b" * 64


def test_ack_quoting_another_items_token_is_refused() -> None:
    existing = [_raise(KEY_A), _raise(KEY_B)]
    with pytest.raises(ValueError, match="different item"):
        el.validate_append(existing, _ack(KEY_A, el.token_for(KEY_B)), send_witness=None)


def test_ack_whose_item_was_never_raised_cannot_borrow_a_raised_token() -> None:
    existing = [_raise(KEY_B)]
    with pytest.raises(ValueError):
        el.validate_append(existing, _ack(KEY_A, el.token_for(KEY_B)), send_witness=None)


def test_ack_with_its_own_token_is_accepted() -> None:
    existing = [_raise(KEY_A), _raise(KEY_B)]
    el.validate_append(existing, _ack(KEY_A, el.token_for(KEY_A)), send_witness=None)


def test_tokenless_ack_still_matches_by_item_key() -> None:
    el.validate_append([_raise(KEY_A)], _ack(KEY_A, None), send_witness=None)
