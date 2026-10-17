"""Two-person approval for what a couple decides together (ADR 0090, ss-console #3283).

WHY. The Captain's decision for the couples role: a change to what the couple
does together (their check-in topics, their shared schedule, anything that
changes the other partner's settings) needs BOTH partners' yes. Until now that
rested on skill prose ("only when the other partner says yes there, write
joint-settings.md"). Prose lowers the rate; it cannot guarantee. This module is
the guarantee.

WHAT IS GATED. A ``record_store_write`` of a JOINT record: one named
``joint-settings.md`` (the couples-checkin skill's record of the couple's
topics, cadence and timezone), or any record whose frontmatter ``kind`` is
``joint-settings``, written or already stored. Another partner's own settings
(``settings-<id>.md``, private to them) are already unwritable by the space
fence, so "changing the other partner's settings" cannot happen at all; the
shared schedule (the routines) lives in customer.yaml and changes only through
a reviewed PR, which the skill records as a request.

HOW IT WORKS.

1. A partner asks for the change, in their DM or in the group. The write is
   held as a :class:`JointAct`: the exact payload, the proposer, and the
   proposer's yes (asking is agreeing). Nothing is written.
2. The seat itself sends every OTHER partner, in their own DM, the exact change
   (``shared.space_writes.post_notice``), and asks them to reply ``agree`` or
   ``reject``. If a notice cannot be sent, nothing is held.
3. Each partner answers in their OWN DM. ``agree`` adds that person's yes; the
   same person saying it twice is still one yes. ``reject`` from either partner
   cancels the act. When every authored partner has said yes, the seat writes
   the stored payload itself (not the model, and not a re-composition), with
   ``confirmed_by`` set to the people who agreed, and tells both partners.
4. An act expires after seven days.

One act is open at a time per seat: a bare "agree" must have exactly one
meaning. The proposer may replace their own open proposal for the same record
(their partner's yes, if given, is reset); anyone else must wait for it to be
decided.

The register persists to a JSON file under ``HERMES_HOME`` (seven days outlives
any process); a missing or unreadable file reads as no open act, and a write
that cannot be persisted refuses the proposal.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shared import record_store
from shared import space as space_mod

#: Seven days, per the Captain's decision.
TTL_SECONDS = 7 * 24 * 3600

JOINT_RECORD_NAMES = frozenset({"joint-settings.md"})
JOINT_KINDS = frozenset({"joint-settings"})

STATUS_OPEN = "open"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_SUPERSEDED = "superseded"

_PATH_ENV = "SMD_JOINT_ACTS_PATH"
_STATE_FILE = "smd-state/joint-acts.json"


class JointActError(RuntimeError):
    """A joint change cannot be proposed or applied. Written for the model."""


@dataclass
class JointAct:
    id: str
    store: str
    name: str
    args: dict[str, Any]
    proposer: str
    approvals: list[str]
    created_at: float
    status: str = STATUS_OPEN
    decided_by: str = ""
    history: list[str] = field(default_factory=list)

    def expired(self, now: float | None = None) -> bool:
        return ((now or time.time()) - self.created_at) > TTL_SECONDS


# ---------------------------------------------------------------------------
# What is joint
# ---------------------------------------------------------------------------


def is_joint_write(tool_name: str, args: Any, policies: dict[str, Any] | None = None) -> bool:
    """Whether this call writes a joint record (see the module docstring)."""
    if tool_name != "record_store_write" or not isinstance(args, dict):
        return False
    store, name = args.get("store"), args.get("name")
    if not isinstance(store, str) or not isinstance(name, str):
        return False
    if name in JOINT_RECORD_NAMES:
        return True
    content = args.get("content")
    if (
        isinstance(content, str)
        and record_store.read_frontmatter(content).get("kind") in JOINT_KINDS
    ):
        return True
    policies = policies if policies is not None else record_store.authored_policies()
    policy = policies.get(store)
    if policy is None or not record_store.RECORD_NAME_RE.match(name):
        return False
    try:
        existing = record_store.read_record(store, name, stores={store: policy.root})
    except record_store.RecordStoreError:
        return False
    return record_store.read_frontmatter(existing).get("kind") in JOINT_KINDS


def required_people(cfg: space_mod.SpaceConfig) -> list[str]:
    """Every authored person: a joint change needs all of them."""
    return [p.id for p in cfg.people]


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def _path() -> Path:
    explicit = os.environ.get(_PATH_ENV)
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("HERMES_HOME") or "/opt/data") / _STATE_FILE


def _load() -> list[JointAct]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return []
    out: list[JointAct] = []
    for item in raw.get("acts", []) if isinstance(raw, dict) else []:
        try:
            out.append(JointAct(**item))
        except TypeError:
            continue
    return out


def _save(acts: list[JointAct]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".joint-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"acts": [asdict(a) for a in acts[-200:]]}, handle, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _expire(acts: list[JointAct], now: float) -> bool:
    changed = False
    for act in acts:
        if act.status == STATUS_OPEN and act.expired(now):
            act.status = STATUS_EXPIRED
            changed = True
    return changed


def open_act(now: float | None = None) -> JointAct | None:
    """The one open act, or None (expired acts are closed on the way)."""
    now = time.time() if now is None else now
    acts = _load()
    if _expire(acts, now):
        _save(acts)
    return next((a for a in acts if a.status == STATUS_OPEN), None)


def propose(args: dict[str, Any], proposer: str, *, now: float | None = None) -> JointAct:
    """Hold a joint change. The proposer's request is their yes. Raises
    :class:`JointActError` when another act is open, or the change is not a
    shared record (a joint record belongs to both: ``owner: shared``)."""
    now = time.time() if now is None else now
    store, name = str(args.get("store")), str(args.get("name"))
    policy = record_store.authored_policies().get(store)
    if policy is None:
        raise JointActError(f"no record store named {store!r} is authored on this seat")
    content = args.get("content") if isinstance(args.get("content"), str) else ""
    if record_store.target_space(policy, content) != space_mod.SHARED:
        raise JointActError(
            "a change both partners decide is kept as a shared record "
            f"({policy.owner_field or 'owner'}: shared)"
        )
    acts = _load()
    _expire(acts, now)
    current = next((a for a in acts if a.status == STATUS_OPEN), None)
    if current is not None:
        if current.proposer == proposer and (current.store, current.name) == (store, name):
            current.status = STATUS_SUPERSEDED
        else:
            raise JointActError(
                "another change the two of you decide together is still waiting for an "
                "answer; it has to be agreed or rejected first"
            )
    act = JointAct(
        id=uuid.uuid4().hex[:12],
        store=store,
        name=name,
        args=copy.deepcopy(args),
        proposer=proposer,
        approvals=[proposer],
        created_at=now,
    )
    acts.append(act)
    _save(acts)
    return act


def respond(
    person: str, agree: bool, cfg: space_mod.SpaceConfig, *, now: float | None = None
) -> tuple[JointAct | None, str]:
    """Record one partner's answer to the open act.

    Returns ``(act, outcome)``: ``none`` (nothing open), ``already`` (this
    person had said yes), ``waiting`` (a yes recorded, someone still to answer),
    ``applied`` (everyone agreed; the change was written), ``rejected``."""
    now = time.time() if now is None else now
    acts = _load()
    _expire(acts, now)
    act = next((a for a in acts if a.status == STATUS_OPEN), None)
    if act is None:
        _save(acts)
        return None, "none"
    if not agree:
        act.status = STATUS_REJECTED
        act.decided_by = person
        _save(acts)
        return act, "rejected"
    if person in act.approvals:
        return act, "already"
    act.approvals.append(person)
    if set(required_people(cfg)) <= set(act.approvals):
        _apply(act)
        act.status = STATUS_APPLIED
        _save(acts)
        return act, "applied"
    _save(acts)
    return act, "waiting"


def _apply(act: JointAct) -> None:
    """Write the stored payload, stamped with who agreed. Raises on failure (the
    act stays open, so nothing is lost)."""
    content = _with_confirmed_by(str(act.args.get("content") or ""), act.approvals)
    record_store.write_record(act.store, act.name, content, overwrite=True)


def _with_confirmed_by(content: str, people: list[str]) -> str:
    who = ", ".join(sorted(set(people)))
    lines = content.split("\n")
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                head = [h for h in lines[1:i] if h.split(":", 1)[0].strip() != "confirmed_by"]
                return "\n".join([lines[0], *head, f"confirmed_by: {who}", *lines[i:]])
    return f"---\nconfirmed_by: {who}\n---\n{content}"


def payload_text(act: JointAct) -> str:
    """The exact change the other partner is shown: the stored record text."""
    return str(act.args.get("content") or "")


def _reset_for_tests() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "JOINT_KINDS",
    "JOINT_RECORD_NAMES",
    "STATUS_APPLIED",
    "STATUS_EXPIRED",
    "STATUS_OPEN",
    "STATUS_REJECTED",
    "TTL_SECONDS",
    "JointAct",
    "JointActError",
    "is_joint_write",
    "open_act",
    "payload_text",
    "propose",
    "required_people",
    "respond",
]
