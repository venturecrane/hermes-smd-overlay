"""Seed the couples check-in records from onboarding (ss-console ADR 0090 decision 4).

The couples skills read the couple's joint answers from the shared record
``couples-checkin/joint-settings.md`` (timezone, topics) and each partner's
``settings-<id>.md`` (quiet hours). Both partners confirm those answers in the
ss-console portal; the generator authors them on the seat as the
``couples-checkin`` skill's settings (``timezone``, ``topics``,
``quiet_hours``, ``preferred_channels``, ``onboarding_version``).

The couples persona has no file tools (ADR 0090 R2), so it cannot read its own
skill settings. This module turns them into the records the skills already
read, once, at boot:

* ``joint-settings.md`` is written when it is absent, or when the existing one
  was itself written by onboarding (``source: onboarding``) at a LOWER
  ``onboarding_version``. A record the couple changed together in the group
  carries no ``source: onboarding`` and is never overwritten.
* ``settings-<id>.md`` (a partner's own quiet hours) is written only when
  absent: a partner's own later change always wins.

Nothing here is model-written or inferred: every value is copied from the
authored settings. Never raises: a seed that fails leaves the skills on their
documented behaviour (no joint settings: check-ins stay silent).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from shared import record_store

logger = logging.getLogger(__name__)

STORE = "couples-checkin"
SKILL = "couples-checkin"
JOINT_RECORD = "joint-settings.md"

_SAFE_VALUE = re.compile(r"^[^\n\r]{1,1200}$")


def _checkin_settings(raw: dict[str, Any]) -> dict[str, Any] | None:
    for persona in raw.get("personas") or []:
        if not isinstance(persona, dict):
            continue
        for skill in persona.get("skills") or []:
            if isinstance(skill, dict) and skill.get("name") == SKILL:
                settings = skill.get("settings")
                return settings if isinstance(settings, dict) else None
    return None


def _value(settings: dict[str, Any], key: str) -> str | None:
    v = settings.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    return v if isinstance(v, str) and _SAFE_VALUE.match(v) else None


def joint_record(settings: dict[str, Any], person_ids: list[str], today: str) -> str | None:
    """The joint-settings record text, or None when the settings are incomplete."""
    tz = _value(settings, "timezone")
    topics = _value(settings, "topics")
    version = _value(settings, "onboarding_version")
    if not tz or not topics or not version or not version.isdigit():
        return None
    lines = [
        "---",
        "owner: shared",
        "kind: joint-settings",
        f"timezone: {tz}",
        f"topics: {topics}",
        f"confirmed_by: {', '.join(person_ids)}",
        f"updated: {today}",
        "source: onboarding",
        f"onboarding_version: {version}",
    ]
    channels = _value(settings, "preferred_channels")
    if channels:
        lines.append(f"preferred_channels: {channels}")
    lines += ["---", "", "What both partners confirmed in onboarding.", ""]
    return "\n".join(lines)


def person_record(person_id: str, quiet_hours: str, today: str) -> str:
    return "\n".join(
        [
            "---",
            f"owner: {person_id}",
            "kind: settings",
            f"quiet_hours: {quiet_hours}",
            f"updated: {today}",
            "source: onboarding",
            "---",
            "",
        ]
    )


def _existing(name: str, stores: dict[str, Any] | None) -> str | None:
    try:
        return record_store.read_record(STORE, name, stores=stores)
    except record_store.RecordStoreError:
        return None


def _should_replace_joint(existing: str | None, version: str) -> bool:
    if existing is None:
        return True
    front = record_store.read_frontmatter(existing)
    if front.get("source") != "onboarding":
        return False  # the couple changed it together; theirs wins
    old = front.get("onboarding_version", "")
    return not old.isdigit() or int(old) < int(version)


def seed(raw: dict[str, Any], *, stores: dict[str, Any] | None = None) -> list[str]:
    """Write the onboarding records that are due. Returns the names written."""
    written: list[str] = []
    try:
        settings = _checkin_settings(raw)
        people = [p.get("id") for p in raw.get("people") or [] if isinstance(p, dict)]
        ids = [p for p in people if isinstance(p, str) and p]
        if not settings or not ids:
            return written
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        joint = joint_record(settings, ids, today)
        version = _value(settings, "onboarding_version") or ""
        if joint and _should_replace_joint(_existing(JOINT_RECORD, stores), version):
            record_store.write_record(STORE, JOINT_RECORD, joint, overwrite=True, stores=stores)
            written.append(JOINT_RECORD)
        quiet = _value(settings, "quiet_hours")
        if quiet:
            for pid in ids:
                name = f"settings-{pid}.md"
                if _existing(name, stores) is None:
                    record_store.write_record(
                        STORE, name, person_record(pid, quiet, today), stores=stores
                    )
                    written.append(name)
    except Exception as exc:  # noqa: BLE001 — never break boot
        logger.error("couples-seed: seeding failed (%s)", type(exc).__name__)
    return written
