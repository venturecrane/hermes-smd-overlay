"""An SMD-staged send: exact words and exact files, delivered once through the gate.

WHY THIS EXISTS. SMD sometimes needs a seat to send one specific message the
Captain approved word for word, carrying specific files SMD made (the first
case: two pilot flyers, as PDFs, to The Brokery's broker). Asking the model to
write it means the model chooses the words and, worse, would have to choose the
files; sending it from a laptop with the vendor key skips the seat's gate and
leaves no row. Neither is allowed. This is the third way, and it adds no new
sending path: it writes the SAME consume-once envelope a routine's pre_run
writes (:mod:`shared.prerendered_dispatch`), so the message goes out of turn
through the trust plugin's full gate, the broker's recipient and participant
fences, and the broker's audit row, exactly like a routine's report.

WHO MAY RUN IT. Root on the seat, never the agent. The envelope lands under the
persona home's ``.smd/pre_run/``, which the trust plugin fences against every
write-class tool, so a turn cannot stage one. The files are read from paths the
operator names, each pinned by its sha256 on the command line: the model never
names, chooses or sees a file.

TWO STEPS, the SMD-note shape (``--dry-run`` then ``--confirm <digest>``):

1. A dry run (the default) validates everything and prints who it reaches, the
   subject, the words, each file's name, size and sha256 and the text the gate
   will read in it, and a digest over all of it. It writes nothing.
2. ``--confirm <digest>`` repeats the checks, refuses unless the digest is the
   one printed (so nothing changed since the Captain read it), and writes the
   envelope and its provenance handoff, owned by the persona home's owner.

Then a one-off MANAGED cron job for that persona and skill fires the turn whose
``pre_llm_call`` dispatches it (the command is printed). The envelope binds for
:data:`shared.pre_run_handoff.DEFAULT_WINDOW` (twenty minutes) after it is
written, and is consumed by the first turn of that routine that takes it.

WHAT IS DIFFERENT FROM A ROUTINE'S ENVELOPE, and only this:

* ``attachments_required: true``. The files are the message, so the dispatcher
  sends nothing (and says so) if any one is stripped or refused, rather than
  the routine's fallback of the body without its workbook. There is no
  skeleton for the same reason.
* The provenance handoff seeds the DATES the staged words and files carry. The
  identifier gate refuses an identifier no source this session read; for a
  routine the source is what its pre_run read from the firm's system, and here
  it is the text SMD staged and the Captain approved. Only dates seed (the
  handoff's own projection); any other identifier kind in the text (a case or
  receipt number, an A-number, an SSN) refuses the staging, because nothing
  could verify it and it has no place in a staged send.
* ``in_turn`` with ``in_turn_enforce``: the woken turn may send only the one
  fixed line :data:`DONE_LINE`, so it cannot compose a second message.

Usage, as root::

    /opt/hermes/.venv/bin/python -m shared.staged_send \\
        --persona brokery-agent --skill open-house-visitor-capture \\
        --to tim@thebrokery.com --subject "..." --body-file /root/staged/body.txt \\
        --attach /root/staged/flyer-a.pdf=<sha256> --attach /root/staged/flyer-b.pdf=<sha256>
    # read it, get the Captain's yes on that exact output, then the same command plus
    #   --confirm <digest>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shared import identifier_filter, outbound_attachment, output_checklist, pre_run_handoff
from shared.prerendered_dispatch import canonical_body_sha256

_DEFAULT_HERMES_HOME = "/opt/data"
_MAX_RECIPIENTS = 20
_MAX_SUBJECT_CHARS = 500
_MAX_BODY_CHARS = 131_072
_ADDRESS_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: The only body the woken turn may send (``in_turn_enforce``). It is never
#: the staged message, so a model that tries to resend cannot.
DONE_LINE = "SMD staged message: nothing further to send."

#: The prompt for the one-off cron job that wakes the dispatching turn.
CRON_PROMPT = (
    "SMD staged one message for this run, and the system delivers it before you "
    "begin. Do not compose, attach or send anything. Reply with the one bracketed "
    "line you were given about it, unchanged."
)

_CONTENT_TYPES = {
    ".pdf": outbound_attachment.PDF_CONTENT_TYPE,
    ".xlsx": outbound_attachment.XLSX_CONTENT_TYPE,
}


class StageError(ValueError):
    """The staging is refused. The message says what to fix."""


@dataclass(frozen=True)
class Staged:
    """Everything one staged send carries, validated."""

    persona: str
    skill: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[tuple[dict[str, str], bytes], ...]
    texts: tuple[str, ...]
    dates: tuple[str, ...]
    advisories: tuple[str, ...]

    def digest(self) -> str:
        """sha256 over who, what and which files; never over a timestamp."""
        canon = {
            "persona": self.persona,
            "skill": self.skill,
            "to": list(self.to),
            "cc": list(self.cc),
            "subject": self.subject,
            "body_sha256": canonical_body_sha256(self.body),
            "attachments": [[d["name"], d["sha256"]] for d, _ in self.attachments],
        }
        raw = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _addresses(values: list[str] | None, label: str) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or []:
        address = value.strip().lower()
        if not _ADDRESS_RE.fullmatch(address):
            raise StageError(f"{label} {value!r} is not an email address")
        if address not in out:
            out.append(address)
    return tuple(out)


def _attachment(spec: str) -> tuple[dict[str, str], bytes]:
    """``<path>=<sha256>`` -> a validated descriptor and its bytes."""
    path_text, sep, pin = spec.rpartition("=")
    if not sep or not _SHA256_RE.fullmatch(pin.strip().lower()):
        raise StageError(f"--attach {spec!r} is not <path>=<sha256>")
    path = Path(path_text)
    content_type = _CONTENT_TYPES.get(path.suffix.lower())
    if content_type is None:
        raise StageError(f"{path.name}: only .pdf and .xlsx files can be attached")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StageError(f"{path}: cannot be read ({exc.strerror})") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != pin.strip().lower():
        raise StageError(f"{path.name}: sha256 is {actual}, not the pinned {pin.strip()}")
    descriptor = {
        "name": path.name,
        "content_type": content_type,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": actual,
    }
    return descriptor, data


def build(
    *,
    persona: str,
    skill: str,
    to: list[str],
    cc: list[str] | None,
    subject: str,
    body: str,
    attach: list[str],
) -> Staged:
    """Validate one staged send. Raises :class:`StageError` on anything wrong."""
    if not _SLUG_RE.fullmatch(persona or ""):
        raise StageError("--persona is not a persona slug")
    if not _SLUG_RE.fullmatch(skill or ""):
        raise StageError("--skill is not a skill name")
    recipients = _addresses(to, "--to")
    copies = _addresses(cc, "--cc")
    if not recipients:
        raise StageError("a staged send needs at least one --to")
    if len(recipients) + len(copies) > _MAX_RECIPIENTS:
        raise StageError(f"more than {_MAX_RECIPIENTS} recipients")
    subject = (subject or "").strip()
    if not subject or len(subject) > _MAX_SUBJECT_CHARS or "\n" in subject:
        raise StageError("the subject is empty, longer than 500 characters, or two lines")
    if not isinstance(body, str) or not body.strip() or len(body) > _MAX_BODY_CHARS:
        raise StageError("the body is empty or too long")
    validated = [_attachment(spec) for spec in attach or []]
    if not validated:
        raise StageError("a staged send carries at least one --attach (the files are its purpose)")
    try:
        outbound_attachment.validate_all([d for d, _ in validated])
        texts = tuple(outbound_attachment.extract_text(d, data) for d, data in validated)
    except outbound_attachment.AttachmentError as exc:
        raise StageError(f"attachment refused: {exc}") from exc
    dates: list[str] = []
    for text in (subject, body, *texts):
        for hit in identifier_filter._extract(text, include_names=False):
            if hit.kind is not identifier_filter.IdKind.DATE:
                raise StageError(
                    f"the text carries a {hit.kind.value} ({hit.raw!r}), which nothing on "
                    "the seat could verify; take it out of the words or the file"
                )
            if hit.raw not in dates:
                dates.append(hit.raw)
    advisories = _checklist_advisories(subject, body)
    return Staged(
        persona=persona,
        skill=skill,
        to=recipients,
        cc=copies,
        subject=subject,
        body=body,
        attachments=tuple(validated),
        texts=texts,
        dates=tuple(dates),
        advisories=advisories,
    )


def _checklist_advisories(subject: str, body: str) -> tuple[str, ...]:
    """What the send gate's output checklist would refuse, read early.

    Advisory only: the gate decides on the seat, with the surface it derives
    from the recipient, and a refusal there sends nothing (there is no fallback
    body). Read here on the stricter staff-send surface with the seat's own
    profile, so the dry run says so before the Captain approves the words."""
    try:
        from shared import policy_profile

        profile = policy_profile.current().checklist_profile
    except Exception:  # noqa: BLE001 - the strict profile on any fault
        profile = output_checklist.PROFILE_PARALEGAL
    found = []
    for text in (subject, body):
        found.extend(output_checklist.check(text, output_checklist.STAFF_SEND, profile))
    return tuple(dict.fromkeys(str(v) for v in output_checklist.refusing(found)))


def envelope(staged: Staged, started_at: datetime) -> dict:
    """The dispatch envelope (:mod:`shared.prerendered_dispatch`) for ``staged``."""
    return {
        "skill": staged.skill,
        "render_mode": "templated",
        "origin": "smd_staged",
        "staged_digest": staged.digest(),
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "dispatches": [
            {
                "recipients": list(staged.to),
                "cc": list(staged.cc),
                "subject": staged.subject,
                "full_body": staged.body,
                "body_sha256_full": canonical_body_sha256(staged.body),
                "attachments": [d for d, _ in staged.attachments],
                "attachments_required": True,
                "appends": [],
            }
        ],
        "unroutable": [],
        "memo_matters": [],
        "in_turn": [{"name": "smd_staged_done", "template": DONE_LINE, "slots": {}}],
        "in_turn_enforce": True,
    }


def _persona_home(hermes_home: str, persona: str) -> Path:
    home = Path(hermes_home) / "profiles" / persona
    if not home.is_dir():
        raise StageError(f"{home} does not exist: no such persona on this seat")
    return home


def _owned_dir(path: Path, uid: int, gid: int) -> None:
    """Create ``path`` (and missing parents below the persona home) 0700, owned
    by the persona home's owner, so the agent can consume what root writes."""
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(directory, uid, gid)


def _write_owned(path: Path, payload: dict, uid: int, gid: int) -> None:
    """Atomic write, 0600, owned like the persona home."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".staged-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:
            os.chown(tmp, uid, gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write(staged: Staged, *, hermes_home: str = _DEFAULT_HERMES_HOME, now=None) -> list[Path]:
    """Write the envelope and its provenance handoff. Refuses to overwrite an
    unconsumed one: it may be a routine's own, or a staging not yet sent."""
    persona_home = _persona_home(hermes_home, staged.persona)
    owner = persona_home.stat()
    directory = pre_run_handoff.handoff_dir(str(persona_home))
    _owned_dir(directory, owner.st_uid, owner.st_gid)
    envelope_path = directory / f"{pre_run_handoff._safe_skill(staged.skill)}.dispatch.json"
    handoff_path = pre_run_handoff.handoff_path(staged.skill, str(persona_home))
    for existing in (envelope_path, handoff_path):
        if existing.exists():
            raise StageError(
                f"{existing} already exists and has not been consumed; nothing was written. "
                "Find out whose it is before removing it."
            )
    moment = now or datetime.now(timezone.utc)
    handoff = {
        "skill": staged.skill,
        "started_at": moment.astimezone(timezone.utc).isoformat(),
        "dates": list(staged.dates),
        "matter_ids": [],
    }
    # The handoff first: an envelope with no handoff beside it would dispatch
    # with nothing seeded and be refused on its own dates.
    _write_owned(handoff_path, handoff, owner.st_uid, owner.st_gid)
    _write_owned(envelope_path, envelope(staged, moment), owner.st_uid, owner.st_gid)
    return [handoff_path, envelope_path]


def cron_command(staged: Staged) -> str:
    """The one-off MANAGED job that wakes the turn whose hook dispatches it."""
    return (
        f"hermes -p {staged.persona} cron create 1m {json.dumps(CRON_PROMPT)} "
        f"--repeat 1 --name op-managed:{staged.persona}:{staged.skill}"
    )


def describe(staged: Staged) -> str:
    """What the Captain approves: every fact the digest covers, in words."""
    lines = [
        f"persona:  {staged.persona}",
        f"routine:  {staged.skill}",
        f"to:       {', '.join(staged.to)}",
        f"cc:       {', '.join(staged.cc) or '(none)'}",
        f"subject:  {staged.subject}",
        "words (sent exactly as below):",
        "-" * 60,
        staged.body.rstrip("\n"),
        "-" * 60,
    ]
    for (descriptor, data), text in zip(staged.attachments, staged.texts, strict=True):
        lines += [
            f"file:     {descriptor['name']}  {len(data)} bytes  sha256 {descriptor['sha256']}",
            "  text the gate reads in it:",
            *("    " + line for line in text.splitlines()),
        ]
    lines.append(
        "dates seeded as staged sources: " + (", ".join(staged.dates) if staged.dates else "(none)")
    )
    for advisory in staged.advisories:
        lines.append(f"WARNING, the send gate may refuse these words: {advisory}")
    lines.append(f"digest:   {staged.digest()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shared.staged_send", description=__doc__)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--skill", required=True, help="the routine whose turn dispatches it")
    parser.add_argument("--to", action="append", required=True)
    parser.add_argument("--cc", action="append")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body-file", required=True, type=Path)
    parser.add_argument("--attach", action="append", required=True, help="<path>=<sha256>")
    parser.add_argument(
        "--hermes-home", default=os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME
    )
    parser.add_argument(
        "--confirm", metavar="DIGEST", help="write it; must repeat the dry run's digest"
    )
    args = parser.parse_args(argv)
    try:
        body = args.body_file.read_text(encoding="utf-8")
        staged = build(
            persona=args.persona,
            skill=args.skill,
            to=args.to,
            cc=args.cc,
            subject=args.subject,
            body=body,
            attach=args.attach,
        )
        print(describe(staged))
        if args.confirm is None:
            print("dry run: nothing written. Repeat with --confirm <digest> to stage it.")
            return 0
        if args.confirm.strip().lower() != staged.digest():
            raise StageError(
                "--confirm does not match this digest; something changed. Dry-run again."
            )
        paths = write(staged, hermes_home=args.hermes_home)
    except (StageError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    for path in paths:
        print(f"wrote {path}")
    print("Within 20 minutes, wake the turn that sends it (as the seat's hermes user):")
    print("  " + cron_command(staged))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script on the seat
    sys.exit(main())
