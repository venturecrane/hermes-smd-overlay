"""Credential-custody guard — ADR 0044 Decision 8 / ADR 0045 §7 (ss #1841).

Pins: non-refused ``code_execution`` exposure is rejected whenever an authored
surface implies a raw credential in the gateway env (enabled non-broker
connector, ``telegram`` channel, agentmail send identity), unless each surface
is explicitly accepted in the top-level ``custody_exceptions`` list;
eligibility is enum-limited to identity-channel adapters — client-data
connectors can never be excepted.

Run::

    pytest tests/test_validate_custody_guard.py -q
"""

from pathlib import Path
from textwrap import dedent

from bootstrap.validate import validate_customer_yaml

_BASE = dedent(
    """\
    schema_version: 1
    customer_id: acme
    customer_name: Acme Corp
    vertical: law-firm
    fly_region: iad
    model: claude-opus-4-7
    hermes_ref: v2026.5.16-smd.0
    personas:
      - slug: marcus
        status: active
        name: Marcus
        title: AI Associate
        entitlements:
          exposure:
            internal_write: autonomous
    """
)


def _validate(tmp_path: Path, extra: str, base: str = _BASE) -> list[str]:
    p = tmp_path / "customer.yaml"
    p.write_text(base + extra)
    return validate_customer_yaml(p)


def _base_with_code_execution(ceiling: str) -> str:
    return _BASE.replace(
        "internal_write: autonomous",
        f"internal_write: autonomous\n        code_execution: {ceiling}",
    )


_SMOKEBALL = dedent(
    """\
    connectors:
      PracticeManagement:
        adapter: smokeball
        backend: mcp:smokeball
        enabled: true
    """
)

_TELEGRAM = dedent(
    """\
    telegram:
      enabled: true
      allow_from:
        - '7367659986'
    """
)


def _custody_errors(errors: list[str]) -> list[str]:
    return [e for e in errors if "code_execution" in e or "custody_exceptions" in e]


# ------------------------------------------------------------ guard trips


def test_code_execution_with_gateway_connector_rejected(tmp_path: Path) -> None:
    errors = _validate(tmp_path, _SMOKEBALL, base=_base_with_code_execution("autonomous"))
    hits = _custody_errors(errors)
    assert hits, f"expected custody-guard rejection, got: {errors}"
    assert "smokeball" in hits[0]


def test_confirm_and_draft_ceilings_also_trip(tmp_path: Path) -> None:
    """Any non-refused ceiling means executed code CAN run — the env read
    happens inside the allowed execution, so only `refused` is safe."""
    for ceiling in ("confirm", "draft_for_review"):
        errors = _validate(tmp_path, _SMOKEBALL, base=_base_with_code_execution(ceiling))
        assert _custody_errors(errors), f"{ceiling}: expected rejection"


def test_telegram_channel_counts_as_a_surface(tmp_path: Path) -> None:
    errors = _validate(tmp_path, _TELEGRAM, base=_base_with_code_execution("autonomous"))
    hits = _custody_errors(errors)
    assert hits and "telegram" in hits[0]


def test_agentmail_send_identity_counts_as_a_surface(tmp_path: Path) -> None:
    base = _base_with_code_execution("autonomous").replace(
        "    entitlements:",
        "    send_as:\n      agentmail_identity: marcus@acme.agents.smd.services\n    entitlements:",
    )
    errors = _validate(tmp_path, "", base=base)
    hits = _custody_errors(errors)
    assert hits and "agentmail" in hits[0]


def test_client_data_connector_can_never_be_excepted(tmp_path: Path) -> None:
    extra = _SMOKEBALL + "custody_exceptions:\n  - smokeball\n"
    errors = _validate(tmp_path, extra, base=_base_with_code_execution("autonomous"))
    assert any("not exception-eligible" in e for e in errors)


# ------------------------------------------------------------ guard passes


def test_refused_code_execution_with_connectors_accepted(tmp_path: Path) -> None:
    errors = _validate(tmp_path, _SMOKEBALL, base=_base_with_code_execution("refused"))
    assert not _custody_errors(errors), errors


def test_unauthored_code_execution_with_connectors_accepted(tmp_path: Path) -> None:
    errors = _validate(tmp_path, _SMOKEBALL)
    assert not _custody_errors(errors), errors


def test_identity_channel_exception_accepts(tmp_path: Path) -> None:
    """The smd shape: code_execution authored, telegram is the only surface,
    exception explicitly authored (Captain decision 2026-07-13)."""
    extra = _TELEGRAM + "custody_exceptions:\n  - telegram\n"
    errors = _validate(tmp_path, extra, base=_base_with_code_execution("autonomous"))
    assert not _custody_errors(errors), errors


def test_exception_covers_only_its_own_surface(tmp_path: Path) -> None:
    extra = _TELEGRAM + _SMOKEBALL + "custody_exceptions:\n  - telegram\n"
    errors = _validate(tmp_path, extra, base=_base_with_code_execution("autonomous"))
    hits = _custody_errors(errors)
    assert (
        hits
        and "smokeball" in hits[0]
        and "telegram" not in hits[0].split("custody exception")[1].split("]")[0]
    )


def test_disabled_connector_and_disabled_telegram_are_not_surfaces(tmp_path: Path) -> None:
    extra = _SMOKEBALL.replace("enabled: true", "enabled: false") + _TELEGRAM.replace(
        "enabled: true", "enabled: false"
    )
    errors = _validate(tmp_path, extra, base=_base_with_code_execution("autonomous"))
    assert not _custody_errors(errors), errors


# ------------------------------------------------------------ exception shape


def test_exceptions_list_shape_and_duplicates_reject(tmp_path: Path) -> None:
    for bad in (
        "custody_exceptions: telegram\n",
        "custody_exceptions:\n  - telegram\n  - telegram\n",
    ):
        errors = _validate(tmp_path, _TELEGRAM + bad, base=_base_with_code_execution("autonomous"))
        assert any("custody_exceptions" in e for e in errors), bad
