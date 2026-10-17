"""A couples-shaped seat for the privacy-by-space suites (ss-console #3264).

Two people, each with a Telegram id and an approved address, and one shared
group. ``write_seat`` puts the customer.yaml on disk and points
``SMD_CUSTOMER_YAML_PATH`` at it; the ``telegram_dm`` / ``telegram_group`` /
``cron_to`` / ``email_turn`` helpers bind a turn's session context the way the
gateway does (Hermes reads ``os.environ`` when its ContextVars are unbound).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shared import space
from shared.inbound import SESSION_INBOUND_ORIGIN, InboundOrigin

A_TG = "1001"
B_TG = "2002"
A_EMAIL = "ana@example.com"
B_EMAIL = "ben@example.com"
GROUP = "-1001234567890"
OTHER_GROUP = "-1009999999999"

SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_USER_ID",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_ID",
    "HERMES_CRON_SESSION",
    "HERMES_CRON_AUTO_DELIVER_PLATFORM",
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID",
)


def couples_yaml(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "customer_id": "captain-mark",
        "customer_name": "Captain and Mark",
        "seat": {"kind": "internal", "product": "hosted-agent", "role": "couples"},
        "people": [
            {"id": "p1", "name": "Ana", "telegram_id": A_TG, "email": A_EMAIL},
            {"id": "p2", "name": "Ben", "telegram_id": B_TG, "email": B_EMAIL},
        ],
        "spaces": {"shared_telegram_chat": GROUP},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
    }
    doc.update(overrides)
    return doc


def write_seat(monkeypatch, tmp_path: Path, doc: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "customer.yaml"
    path.write_text(yaml.safe_dump(couples_yaml() if doc is None else doc))
    monkeypatch.setenv("SMD_CUSTOMER_YAML_PATH", str(path))
    space._reset_cache_for_tests()
    return path


def clear_turn(monkeypatch) -> None:
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)


def telegram_dm(monkeypatch, user: str) -> None:
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", user)


def telegram_group(monkeypatch, user: str, chat: str = GROUP) -> None:
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat)


def cron_to(monkeypatch, chat: str, platform: str = "telegram") -> None:
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", platform)
    monkeypatch.setenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", chat)


def email_turn(monkeypatch, session_id: str, sender: str) -> None:
    clear_turn(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webhook")
    monkeypatch.setenv("HERMES_SESSION_ID", session_id)
    SESSION_INBOUND_ORIGIN.record(
        session_id, InboundOrigin(sender_address=sender, message_id=f"<{session_id}@mail>")
    )
