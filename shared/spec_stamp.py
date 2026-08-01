"""The authored-spec POINTER stamp — rendered, and kept current (ss ADR 0083).

WHY THIS MODULE EXISTS AT ALL. The stamp used to live entirely inside
``bootstrap/translate.py``, which runs exactly once, at boot, after the
privilege drop. That made a defect nobody had noticed until the first live
authoring attempt:

    A client's FIRST spec did not take effect until the Machine rebooted.

The root poller installs a newly-authored spec on a running Machine within
seconds and writes the root-owned manifest — but nothing re-rendered the
pointer, and ``render_pointer_block`` returns ``""`` when no specs are
installed. So a Machine that booted with none carried NO pointer at all, and
the model was never told the spec existed. The applier's own docstring drew the
line precisely and only in passing: the poll loop "picks up a portal edit… no
reboot… a **replaced** body takes effect on the next read." True for editing an
existing spec. False for the first one.

That gap is the difference between the product's promise — *type it and from
then on it comes out that way* — and *type it, then reboot*. A shape honoured
only after a restart nobody mentioned is exactly the silent approximation this
system refuses.

WHY THE REFRESH IS HERMES-SIDE AND NOT IN THE ROOT APPLIER. The obvious fix is
to have the applier re-stamp after installing. It is the wrong fix, and it
would have been expensive. ``spec_applier`` runs as ROOT before the privilege
drop; ``bootstrap.sh`` — and therefore ``translate`` — runs as hermes AFTER it
(``entrypoint.sh:511``), and ``<profile>/skills/`` is hermes-owned. A root
re-stamp leaves root-owned SKILL.md files that the next boot's hermes-run
copytree cannot overwrite. That is precisely the 2026-07-16 outage: a root
probe wrote ``cron/jobs.json`` root-owned, the hermes scheduler could not read
its own job store, and nothing fired for eight days while the Machine stayed
green.

So the refresh runs in the agent's own process, at turn start, as hermes.
Ownership is correct by construction rather than by a chown nobody re-checks.

WHAT IT COSTS. Nothing on the common path: the refresh compares a digest of the
current manifest against the last one it stamped and returns immediately when
they match, and ``write_if_changed`` means an identical stamp never touches the
volume.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

SPEC_STAMP_BEGIN = "<!-- SMD-AUTHORED-SPEC-POINTER:BEGIN -->"
SPEC_STAMP_END = "<!-- SMD-AUTHORED-SPEC-POINTER:END -->"


def write_if_changed(target: Path, content: bytes) -> bool:
    """Atomically write ``content`` to ``target``, only if the bytes differ.

    A local copy of ``translate._write_if_changed``'s contract rather than an
    import, because ``translate`` imports ``shared`` and the reverse would
    cycle. Same two properties that matter: ``os.replace`` swaps the directory
    entry using the PARENT's permissions, so it can replace a target left owned
    by another principal, and a reader never observes a half-written file.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            if target.read_bytes() == content:
                return False
        except OSError:
            pass  # unreadable existing file — fall through and replace it
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True


def render_pointer_block() -> str:
    """Render the authored-spec POINTER stamp, or ``""`` when nothing is installed.

    THE POINTER, NEVER THE PROSE. This block names the class, the property, the
    on-disk path, and the sha256 root recorded — and stops. It does not embed
    the spec text, and that restraint is the design, not thrift: the spec tree
    is refreshed under a RUNNING Machine by the root poller, so an embedded
    prose copy would drift from the file it claims to reproduce. A confidently
    served stale spec is worse than no read at all, because nothing about it
    looks wrong. A pointer cannot go stale in a way that matters: the file it
    names is read fresh, and the hash beside it is what the root-owned manifest
    says right now.

    THE STAMP IS NOT TRUSTED, and says so in its own text. ``<profile>/skills/``
    is hermes-owned, so an agent can rewrite this block and forge both the path
    and the hash. It is DELIVERY — it tells the model where to look. Enforcement
    reads ``shared.spec_manifest`` (root-owned) and never this stamp.

    Only INSTALLED specs are listed. A class the seat declares ``expected``
    whose spec never arrived is deliberately absent rather than named as
    missing: a second, forgeable authority stating what an output must not do
    invites the model to negotiate with it, and the gate at the send site
    already refuses that class outright.
    """
    from shared import spec_manifest

    entries = sorted(spec_manifest.load_entries().values(), key=lambda e: (e.output_class, e.prop))
    if not entries:
        return ""
    base = spec_manifest.spec_dir()
    if base is None:
        return ""
    lines = [
        SPEC_STAMP_BEGIN,
        "",
        "## Authored specs on this seat",
        "",
        "The firm authored these specifications for what you produce. Read the one",
        "matching what you are about to write, BEFORE you write it. For an output class",
        "the firm declared a spec for, an unread spec means the output does not go out:",
        "a send is refused and routed to a draft, and an internal artifact is refused at",
        "delivery — reading it is not optional and not a formality.",
        "",
        "**Precedence: the drafting discipline outranks the voice.** Never invent, cite",
        "the record, refuse rather than guess, escalate rather than nag. A spec shapes",
        "how something is said; it never licenses saying something the record does not",
        "support. Court-bound work takes the court register regardless of any spec here.",
        "",
        "| Output class | Property | Read this file | sha256 (root-recorded) |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            f"| `{entry.output_class}` | {entry.prop} | `{base / entry.rel_path}` "
            f"| `{entry.sha256[:16]}…` |"
        )
    lines += [
        "",
        "This block is regenerated from the root-owned spec manifest — at boot, and again",
        "whenever the installed specs change under a running Machine. Any edit to it is",
        "overwritten. It is a pointer, not an authority: the specs themselves live in a",
        "directory this agent cannot write.",
        "",
        SPEC_STAMP_END,
    ]
    return "\n".join(lines) + "\n"


def strip_stamp(text: str) -> str:
    """Remove every sentinel-delimited stamp region from ``text``.

    Loops rather than handling one region: a SKILL.md that somehow accumulated
    two stamps (an interrupted boot, a hand-edit) must come out with none, not
    with one fewer. An unterminated BEGIN truncates from the sentinel — the
    remainder is a half-written stamp and keeping it would leave a pointer table
    with no closing marker for the next pass to find.
    """
    while SPEC_STAMP_BEGIN in text:
        head, _, rest = text.partition(SPEC_STAMP_BEGIN)
        if SPEC_STAMP_END in rest:
            _, _, tail = rest.partition(SPEC_STAMP_END)
        else:
            tail = ""
        text = head.rstrip("\n") + ("\n" + tail.lstrip("\n") if tail.strip() else "\n")
    return text


def stamp_skill_md(skill_md: Path, block: str) -> bool:
    """Apply, refresh, or remove the pointer block in one ``SKILL.md``.

    Returns True when the file changed. IDEMPOTENT AND NON-STACKING is the whole
    contract: ``_install_persona_skills`` copytrees the catalog over the profile
    on every boot, which restores an unstamped SKILL.md, and any stamp that
    appended rather than replaced would grow a copy per boot. So the existing
    region is excised first and the fresh block appended once. An empty
    ``block`` excises without re-adding — a seat whose specs were removed loses
    its pointers rather than keeping a stamp to files that are gone.
    """
    try:
        current = skill_md.read_text()
    except OSError:
        return False
    stripped = strip_stamp(current)
    desired = stripped if not block else stripped.rstrip("\n") + "\n\n" + block
    if desired == current:
        return False
    return write_if_changed(skill_md, desired.encode())


def manifest_fingerprint() -> str:
    """A cheap digest of what is installed right now, or ``""`` if unreadable.

    The refresh guard. Covers class, property and content digest for every
    entry, so a replaced body with the same path still reads as a change — the
    stamp carries the hash, so a body swap must re-render it.
    """
    try:
        from shared import spec_manifest

        entries = sorted(
            spec_manifest.load_entries().values(), key=lambda e: (e.output_class, e.prop)
        )
    except Exception:  # noqa: BLE001 — an unreadable manifest is not this function's to raise on
        return ""
    joined = "\n".join(f"{e.output_class}/{e.prop}/{e.rel_path}/{e.sha256}" for e in entries)
    return hashlib.sha256(joined.encode()).hexdigest()


#: Last fingerprint successfully stamped in this process. Process-wide singleton
#: for the same reason ``spec_status`` is one: one tenant per Machine.
_LAST_STAMPED: str | None = None


def refresh_profile_stamps(profiles_root: Path) -> int:
    """Re-render the pointer into every profile skill, if the manifest moved.

    Returns the number of ``SKILL.md`` files changed; 0 is the overwhelmingly
    common answer and costs one manifest read.

    This is what makes a newly authored spec take effect WITHOUT A REBOOT. Call
    it at turn start from the agent's own process — never from the root applier,
    for the ownership reason in the module docstring.
    """
    global _LAST_STAMPED

    fingerprint = manifest_fingerprint()
    if fingerprint == _LAST_STAMPED:
        return 0

    block = render_pointer_block()
    changed = 0
    try:
        profile_dirs = sorted(p for p in profiles_root.iterdir() if p.is_dir())
    except OSError:
        return 0
    for profile in profile_dirs:
        skills = profile / "skills"
        if not skills.is_dir():
            continue
        for skill in sorted(p for p in skills.iterdir() if p.is_dir()):
            try:
                if stamp_skill_md(skill / "SKILL.md", block):
                    changed += 1
            except OSError:
                # One unwritable skill must not abort the rest: a partial
                # refresh beats none, and the next turn retries because the
                # fingerprint is only recorded on a clean pass.
                logger.debug("spec stamp: could not refresh %s", skill, exc_info=True)
                return changed
    _LAST_STAMPED = fingerprint
    if changed:
        logger.info(
            "spec stamp: refreshed pointer in %d skill file(s) after a manifest change", changed
        )
    return changed


def _reset_for_tests() -> None:
    global _LAST_STAMPED
    _LAST_STAMPED = None


__all__ = [
    "SPEC_STAMP_BEGIN",
    "SPEC_STAMP_END",
    "manifest_fingerprint",
    "refresh_profile_stamps",
    "render_pointer_block",
    "stamp_skill_md",
    "strip_stamp",
    "write_if_changed",
]
