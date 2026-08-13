"""Verbatim envelope capture (gate_envelope_capture) — pins the two conditions,
the fail-CLOSED direction, the bounds, and that truncation is never silent.

The suite is built around one question: **would these tests notice if the guard
stopped working?** A "customer seat does not capture" assertion passes just as
happily when capture is broken outright, so every negative case here is paired
with the positive control that proves the mechanism was live at the time.

Run::

    pytest tests/test_gate_envelope_capture.py -q
"""

import json
import os

import pytest

import shared.customer_config as customer_config_module
from shared.gate_envelope_capture import (
    CAPTURE_DIR_ENV,
    MAX_BYTES_ENV,
    MAX_FILES_ENV,
    build_record,
    capture,
    seat_is_proving,
)

_BODY = json.dumps(
    {
        "accountId": "5233e204-9661-4d46-853f-e408a0ca7f0b",
        "userId": None,
        "type": "matter.updated",
        "source": "API",
        "payload": {
            "id": "3c191bed-cdda-48b9-a6ed-a51a349f3f94",
            "versionId": "639189930090653628",
        },
    }
).encode()


def _seat(monkeypatch, data):
    """Install a fake volume config. `data` is the raw customer.yaml mapping."""

    class FakeConfig:
        _data = data

        @classmethod
        def from_volume(cls):
            return cls()

    monkeypatch.setattr(customer_config_module, "CustomerConfig", FakeConfig)


def _proving(monkeypatch):
    _seat(monkeypatch, {"seat": {"kind": "proving"}})


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(CAPTURE_DIR_ENV, str(tmp_path / "captures"))


def _written(tmp_path):
    directory = tmp_path / "captures"
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix == ".json")


# ---------------------------------------------------------------- seat_is_proving


@pytest.mark.parametrize(
    "config",
    [
        {"seat": {"kind": "customer"}},
        {"seat": {"kind": "Customer"}},
        {"seat": {}},
        {"seat": None},
        {"seat": "proving"},
        {"seat": {"kind": None}},
        {},
        None,
        "not-a-mapping",
    ],
)
def test_seat_is_proving_fails_closed_on_everything_ambiguous(config) -> None:
    """Anything that is not exactly `kind: proving` is not a proving rig.

    CustomerConfig.seat's own contract: 'treat an unauthored seat with
    customer-grade caution rather than assuming it is a proving rig.'
    """
    assert seat_is_proving(config) is False


@pytest.mark.parametrize("kind", ["proving", "  proving  ", "PROVING"])
def test_seat_is_proving_accepts_the_authored_value(kind) -> None:
    """The positive control for the parametrized refusals above — without this,
    a seat_is_proving that returned False unconditionally would pass them all."""
    assert seat_is_proving({"seat": {"kind": kind}}) is True


# ---------------------------------------------------------------- the two conditions


def test_disabled_by_default_writes_nothing(monkeypatch, tmp_path) -> None:
    """No env var — the state of every seat, every day."""
    monkeypatch.delenv(CAPTURE_DIR_ENV, raising=False)
    _proving(monkeypatch)
    assert capture(route="smokeball", request_id="r1", body=_BODY) is None
    assert _written(tmp_path) == []


def test_customer_seat_never_captures_even_when_enabled(monkeypatch, tmp_path) -> None:
    """THE guard. Enabled by env, refused by config."""
    _enable(monkeypatch, tmp_path)
    _seat(monkeypatch, {"seat": {"kind": "customer"}})
    assert capture(route="smokeball", request_id="r1", body=_BODY) is None
    assert _written(tmp_path) == []


def test_unauthored_seat_never_captures_even_when_enabled(monkeypatch, tmp_path) -> None:
    """Absent seat block = customer-grade caution, not a proving rig."""
    _enable(monkeypatch, tmp_path)
    _seat(monkeypatch, {})
    assert capture(route="smokeball", request_id="r1", body=_BODY) is None
    assert _written(tmp_path) == []


def test_proving_seat_with_env_captures(monkeypatch, tmp_path) -> None:
    """The positive control for the two refusals above. If this fails, those two
    prove nothing — they would pass against a capture() that never writes."""
    _enable(monkeypatch, tmp_path)
    _proving(monkeypatch)
    path = capture(route="smokeball", request_id="req-1", body=_BODY)
    assert path is not None
    written = _written(tmp_path)
    assert len(written) == 1
    record = json.loads(written[0].read_text())
    assert record["route"] == "smokeball"
    assert record["request_id"] == "req-1"
    assert record["truncated"] is False
    # Verbatim — the whole point. Byte-for-byte what the vendor sent.
    assert record["body"].encode() == _BODY
    assert json.loads(record["body"])["payload"]["versionId"] == "639189930090653628"


def test_written_file_is_owner_only(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    _proving(monkeypatch)
    path = capture(route="smokeball", request_id="req-1", body=_BODY)
    assert path is not None
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- fail-closed direction


def test_unreadable_config_fails_closed(monkeypatch, tmp_path) -> None:
    """The inverse of the suppression modules beside it. There a failed config
    read forwards (fail-open); here it must NOT capture."""
    _enable(monkeypatch, tmp_path)

    class Exploding:
        @classmethod
        def from_volume(cls):
            raise RuntimeError("volume unreadable")

    monkeypatch.setattr(customer_config_module, "CustomerConfig", Exploding)
    assert capture(route="smokeball", request_id="r1", body=_BODY) is None
    assert _written(tmp_path) == []


def test_undeletable_directory_never_raises(monkeypatch, tmp_path) -> None:
    """An instrument must never break the gate it observes."""
    _proving(monkeypatch)
    # A path whose parent is a file, so makedirs cannot succeed.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(CAPTURE_DIR_ENV, str(blocker / "captures"))
    assert capture(route="smokeball", request_id="r1", body=_BODY) is None


# ---------------------------------------------------------------- bounds


def test_file_cap_stops_capturing(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv(MAX_FILES_ENV, "3")
    _proving(monkeypatch)
    for i in range(6):
        capture(route="smokeball", request_id=f"req-{i}", body=_BODY, now=1000.0 + i)
    assert len(_written(tmp_path)) == 3


def test_oversize_body_is_truncated_and_marked(monkeypatch, tmp_path) -> None:
    """Truncation is never silent — a truncated capture must not be mistakable
    for a short envelope, or the measurement it feeds is wrong."""
    _enable(monkeypatch, tmp_path)
    monkeypatch.setenv(MAX_BYTES_ENV, "32")
    _proving(monkeypatch)
    capture(route="smokeball", request_id="r1", body=_BODY)
    record = json.loads(_written(tmp_path)[0].read_text())
    assert record["truncated"] is True
    assert record["body_bytes"] == len(_BODY)
    assert len(record["body"]) == 32


def test_non_utf8_body_is_base64_not_lossy(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    _proving(monkeypatch)
    raw = b"\xff\xfe\x00binary"
    capture(route="smokeball", request_id="r1", body=raw)
    record = json.loads(_written(tmp_path)[0].read_text())
    assert record["body_encoding"] == "base64"
    import base64 as b64

    assert b64.b64decode(record["body"]) == raw


def test_unsafe_request_id_cannot_escape_the_directory(monkeypatch, tmp_path) -> None:
    _enable(monkeypatch, tmp_path)
    _proving(monkeypatch)
    path = capture(route="smokeball", request_id="../../etc/passwd", body=_BODY)
    assert path is not None
    assert os.path.dirname(path) == str(tmp_path / "captures")
    assert len(_written(tmp_path)) == 1


# ---------------------------------------------------------------- record shape


def test_build_record_keeps_the_body_verbatim() -> None:
    record = build_record(route="smokeball", request_id="r", body=_BODY, max_bytes=10_000, now=1.0)
    assert record["body"].encode() == _BODY
    assert record["body_encoding"] == "utf-8"
    assert record["truncated"] is False
    assert record["body_bytes"] == len(_BODY)
