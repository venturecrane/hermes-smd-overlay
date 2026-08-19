"""Microsoft Graph delta poller — the D1 inbound path (ADR 0078 / email-channel-seam).

The operator's client-custody mailbox has NO push webhook (spec D1): inbound mail
is PULLED over an authenticated channel via Graph delta query, keeping a
``deltaLink`` cursor per seat. This module is that poller. It is hosted as a
daemon THREAD inside the always-on webhook-gate process — the one non-agent
process on every Machine, the same host the heartbeat emitter rides — so it adds
NO new daemon process and inherits the gate's ``MSGRAPH_*`` / ``WEBHOOK_SECRET_*``
env. It is NOT a Hermes-native cron: the 45s default cadence is sub-minute, and
the poll is pure infrastructure that must not wake the agent on a schedule.

The security-load-bearing property (spec D3, the F1 fix): a polled message enters
the model ONLY through the SAME gate→router enqueue that push mail uses. The
poller does not deliver text to the model any other way — it re-injects each new
message as a stamped webhook (``source: msgraph``, ``event_type:
message.received``, the normalized ``InboundMessage`` DTO under ``inbound_message``)
by POSTing to Hermes' own webhook adapter on the loopback (the same path the MCP
gate's ``_drive_agent_turn`` and the handoff endpoint already use). Hermes builds
the ``MessageEvent`` and fires ``pre_gateway_dispatch``, where the webhook router
(slice 3) normalizes, fences+taints (unless the sender is on the roster), records
the recipient-lock origin, and routes to the authored skill — identical treatment
to AgentMail's push path. The router's ``msgraph`` normalizer accepts exactly this
shape.

Durability + safety:
  * The delta cursor + a bounded seen-id ledger persist to the volume so a
    restart resumes where it left off; on a 410 cursor reset the batch is a
    re-sync and the seen-id ledger DEDUPES it so old mail is not replayed as new
    turns.
  * Self-sent mail (``from_addr == mailbox``) is skipped so a reply the operator
    itself sent can never wake it in a loop (mirrors the AgentMail echo posture).
  * Exception-safe: a poll failure logs and skips the cycle; the thread never
    dies and never raises. A missing credential / signing secret means the poller
    does not start at all (fail-closed) rather than a live path reporting success.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from shared import msgraph_client
from shared.customer_config import CustomerConfig, CustomerConfigError

logger = logging.getLogger("hermes-smd-msgraph-poller")

# Loopback target: the Hermes webhook adapter on this same Machine (same
# constants the gate's other forwards use). host:port are fixed — not an SSRF
# surface. translate.py materializes the ``msgraph`` route so the adapter accepts
# POST /webhooks/msgraph and re-verifies the X-Webhook-Signature.
_GATEWAY_HOST = os.environ.get("WEBHOOK_GATEWAY_HOST", "127.0.0.1")
_GATEWAY_PORT = int(os.environ.get("WEBHOOK_GATEWAY_PORT", "8644"))

ROUTE = "msgraph"
SOURCE = "msgraph"
EVENT_TYPE = "message.received"
_SIGNING_SECRET_ENV = "WEBHOOK_SECRET_MSGRAPH"

DEFAULT_POLL_SECONDS = 45
_MIN_POLL_SECONDS = 5
# Bound the seen-id ledger. Sized well above any freshly-provisioned operator
# mailbox so a 410 full-inbox re-sync can never out-run the dedupe window —
# eviction during a re-list would re-forward old mail as fresh turns. Still a
# trivially-serializable slice of the volume (~1.5 MB worst case).
_MAX_SEEN_IDS = 10000
# A per-item failure holds the cursor and retries next cycle (overlay#275).
# Dead-lettering requires PROOF the failure is item-specific, not systemic: an
# item accrues poison-count only in cycles where at least one peer forwarded
# successfully (the sink demonstrably works, this item demonstrably doesn't).
# At this many mixed-cycle failures the raw payload is written to the
# dead-letter dir on the volume, the item is marked seen, and the cursor may
# advance — bounded, loud, payload-preserved. Systemic faults (gate down,
# secret drift: every item failing) hold the cursor INDEFINITELY and page via
# Sentry instead: a held cursor still delivers new mail, so holding is cheap,
# and 30 minutes of outage must never convert the firm's whole inbound stream
# into dead-letter files (the failure mode a single per-item bound had).
_MAX_POISON_FAILURES = 5
_EMAIL_CAPABILITY = "Email"
_FORWARD_TIMEOUT_S = 30.0


def _hex_hmac_sha256(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _parse_iso(value: Any) -> datetime | None:
    """Graph ``receivedDateTime`` → aware datetime, or None when absent/unparseable.
    Fail open: a None here must always mean "treat the item as new enough"."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Durable per-seat cursor + seen-id ledger
# ---------------------------------------------------------------------------


class DeltaState:
    """Durable ``{delta_link, seen_ids}`` for one seat, persisted to the volume.

    ``delta_link`` resumes the Graph delta across restarts/deploys; ``seen_ids``
    is a bounded, insertion-ordered ledger of recently-forwarded message ids so a
    410 cursor reset (which re-lists the whole inbox) cannot replay already-handled
    mail as fresh turns. Writes are atomic (temp + ``os.replace``); a missing /
    unreadable file is an empty state (first run), never a crash."""

    def __init__(self, path: str, *, max_seen: int = _MAX_SEEN_IDS) -> None:
        self._path = path
        self._max_seen = max_seen
        self.delta_link: str | None = None
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        # Two counters per failing message id (overlay#275). ``hold`` counts every
        # failing cycle — observability only, no bound. ``poison`` counts only the
        # cycles where a peer forwarded (proof the failure is item-specific) and is
        # what the dead-letter bound reads. Both small by construction: an id
        # leaves on success or dead-letter.
        self._failures: dict[str, int] = {}
        self._poison: dict[str, int] = {}
        # High-water mark of ``receivedDateTime`` from CLEAN cycles only: on a 410
        # cursor reset, unseen mail older than this is dedupe-skipped so a full
        # re-list of a mature inbox cannot replay history as fresh turns. Never
        # advanced while anything is held/failing, and a held id is never
        # watermark-skipped — otherwise a reset during a hold would silently eat
        # exactly the mail the cursor hold exists to protect.
        self.watermark: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, OSError):
            return
        except (ValueError, TypeError):
            logger.warning("msgraph poller: cursor file %s unparseable; starting fresh", self._path)
            return
        if not isinstance(data, dict):
            return
        link = data.get("delta_link")
        self.delta_link = link if isinstance(link, str) and link else None
        seen = data.get("seen_ids")
        if isinstance(seen, list):
            for mid in seen:
                if isinstance(mid, str) and mid and mid not in self._seen_set:
                    self._seen.append(mid)
                    self._seen_set.add(mid)
        for key, target in (("failures", self._failures), ("poison_counts", self._poison)):
            counts = data.get(key)
            if isinstance(counts, dict):
                for mid, count in counts.items():
                    if isinstance(mid, str) and mid and isinstance(count, int) and count > 0:
                        target[mid] = count
        watermark = data.get("watermark")
        if isinstance(watermark, str) and _parse_iso(watermark) is not None:
            self.watermark = watermark

    def has_seen(self, message_id: str) -> bool:
        return message_id in self._seen_set

    def record_hold(self, message_id: str) -> int:
        """Bump and return the every-cycle failure count for one message id."""
        count = self._failures.get(message_id, 0) + 1
        self._failures[message_id] = count
        return count

    def record_poison(self, message_id: str) -> int:
        """Bump and return the mixed-cycle (peer-succeeded) failure count."""
        count = self._poison.get(message_id, 0) + 1
        self._poison[message_id] = count
        return count

    def is_failing(self, message_id: str) -> bool:
        return message_id in self._failures or message_id in self._poison

    def clear_failure(self, message_id: str) -> None:
        self._failures.pop(message_id, None)
        self._poison.pop(message_id, None)

    def clear_all_failures(self) -> None:
        """A clean cursor advance proves nothing is currently failing — any
        leftover entries belong to vanished/tombstoned items and would otherwise
        persist forever."""
        self._failures.clear()
        self._poison.clear()

    def advance_watermark(self, candidates: list[str]) -> None:
        """Raise the clean-cycle high-water mark to the max parseable candidate.
        Callers only pass timestamps from cycles that advanced the cursor clean."""
        best = _parse_iso(self.watermark)
        best_raw = self.watermark
        for raw in candidates:
            parsed = _parse_iso(raw)
            if parsed is not None and (best is None or parsed > best):
                best, best_raw = parsed, raw
        self.watermark = best_raw

    def mark_seen(self, message_id: str) -> None:
        if not message_id or message_id in self._seen_set:
            return
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        while len(self._seen) > self._max_seen:
            evicted = self._seen.pop(0)
            self._seen_set.discard(evicted)

    def persist(self, delta_link: str | None) -> None:
        """Atomically write the current cursor + seen ledger + failure counts.

        ``delta_link=None`` KEEPS the current cursor — the hold-on-failure path
        (overlay#275): the ledger of handled items is made durable while the
        cursor stays behind the unhandled ones. A persist failure is logged, not
        raised — the next cycle simply re-forwards from the last durable cursor
        (at-least-once, deduped by the seen ledger)."""
        if delta_link:
            self.delta_link = delta_link
        payload = json.dumps(
            {
                "delta_link": self.delta_link,
                "seen_ids": self._seen,
                "failures": self._failures,
                "poison_counts": self._poison,
                "watermark": self.watermark,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(self._path) or ".", prefix=".delta.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning(
                "msgraph poller: cursor persist failed (%s); state not durable this cycle", exc
            )


def _default_state_path() -> str:
    """The cursor file path: env override, else ``$HERMES_HOME/msgraph/delta-state.json``
    (falls back to /opt/data — the same volume the escalation ledger uses)."""
    override = os.environ.get("SMD_MSGRAPH_STATE_PATH")
    if override:
        return override
    home = os.environ.get("HERMES_HOME") or "/opt/data"
    return os.path.join(home, "msgraph", "delta-state.json")


# ---------------------------------------------------------------------------
# Loopback enqueue — the ONLY door to the model (spec D3)
# ---------------------------------------------------------------------------


def _default_forward(*, body: bytes, signature: str, request_id: str) -> int:
    """POST a stamped webhook to the Hermes adapter loopback; return the status.

    Mirrors the gate's own forward (``_drive_agent_turn`` / ``_handle_handoff``):
    ``X-Webhook-Signature`` = hex HMAC over the exact bytes with the route secret,
    ``X-Request-ID`` = the Graph message id (the adapter's idempotency key). Raises
    on a transport failure; the caller is exception-safe."""
    conn = http.client.HTTPConnection(_GATEWAY_HOST, _GATEWAY_PORT, timeout=_FORWARD_TIMEOUT_S)
    try:
        conn.request(
            "POST",
            f"/webhooks/{ROUTE}",
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
                "X-Request-ID": request_id,
            },
        )
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class MsGraphPoller:
    """Background delta poller (daemon thread), hosted in the webhook-gate process.

    Constructed once from the gate env; :meth:`start` launches the thread only when
    the seat's Email adapter is ``msgraph`` + enabled AND the credentials + signing
    secret are present (fail-closed: a misconfigured seat does not start a poller
    that reports success). ``client_factory`` / ``forward_fn`` / ``state`` /
    ``sleep_fn`` are injectable so the tick logic is unit-tested without a socket,
    a live Graph tenant, or a real clock."""

    def __init__(
        self,
        *,
        signing_secret: str | None,
        yaml_path: str | None = None,
        state_path: str | None = None,
        client_factory: Callable[[], msgraph_client.MsGraphClient | None] | None = None,
        forward_fn: Callable[..., int] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        default_poll_seconds: int = DEFAULT_POLL_SECONDS,
        max_item_failures: int = _MAX_POISON_FAILURES,
    ) -> None:
        self._signing_secret = signing_secret
        self._yaml_path = yaml_path
        self._state_path = state_path or _default_state_path()
        self._client_factory = client_factory or msgraph_client.build_client_from_env
        self._forward_fn = forward_fn or _default_forward
        # The POISON bound: mixed-cycle failures before an item dead-letters.
        self._max_item_failures = max(1, max_item_failures)
        self._default_poll_seconds = max(_MIN_POLL_SECONDS, default_poll_seconds)
        self._stop = threading.Event()
        self._sleep_fn = sleep_fn or self._stop.wait
        self._thread: threading.Thread | None = None
        self._client: msgraph_client.MsGraphClient | None = None
        self._state: DeltaState | None = None

    # ---- config resolution ------------------------------------------------
    def _email_connector(self) -> dict[str, Any] | None:
        """The authored Email connector record, or None when unreadable/absent.
        Read live so a cadence change (``poll_seconds``, non-structural) applies
        without a restart."""
        try:
            cfg = CustomerConfig.from_volume(self._yaml_path)
            return cfg.connectors.get(_EMAIL_CAPABILITY)
        except (CustomerConfigError, OSError):
            return None

    def _is_msgraph_inbound(self, record: dict[str, Any] | None) -> bool:
        return bool(
            isinstance(record, dict)
            and record.get("enabled")
            and str(record.get("adapter") or "") == SOURCE
        )

    def _poll_seconds(self, record: dict[str, Any] | None) -> int:
        raw = record.get("poll_seconds") if isinstance(record, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            return self._default_poll_seconds
        return max(_MIN_POLL_SECONDS, raw)

    def _ready(self) -> bool:
        """Fail-closed startup gate: the seat is msgraph-inbound AND the signing
        secret + Graph client are available."""
        if not self._signing_secret:
            logger.info(
                "msgraph poller: %s unset; poller not started (fail-closed)", _SIGNING_SECRET_ENV
            )
            return False
        if not self._is_msgraph_inbound(self._email_connector()):
            logger.info("msgraph poller: Email adapter is not msgraph/enabled; poller not started")
            return False
        client = self._client_factory()
        if client is None:
            logger.warning(
                "msgraph poller: Graph client unavailable (MSGRAPH_* unset); poller not started"
            )
            return False
        self._client = client
        self._state = DeltaState(self._state_path)
        return True

    # ---- one poll cycle ---------------------------------------------------
    def poll_once(self) -> int:
        """Run one delta poll and forward every NEW, non-self message. Returns the
        count forwarded. Exception-safe: any failure logs and returns 0 (the cursor
        is untouched, so the next cycle retries)."""
        client = self._client
        state = self._state
        if client is None or state is None or not self._signing_secret:
            return 0
        try:
            raw_messages, delta_link, cursor_reset = client.poll_delta(state.delta_link)
        except Exception as exc:  # noqa: BLE001 — a poll failure must never kill the loop
            logger.warning("msgraph poller: delta poll failed (%s); skipping this cycle", exc)
            return 0
        if cursor_reset:
            logger.info("msgraph poller: delta cursor reset (410); re-syncing with dedupe")

        forwarded = 0
        watermark_skipped = 0
        cycle_received: list[str] = []
        failures: list[tuple[Any, str, Exception]] = []
        mailbox = (client.mailbox or "").strip().lower()
        for raw in raw_messages:
            if cursor_reset and self._watermark_skip(raw, state):
                watermark_skipped += 1
                continue
            try:
                if self._handle_message(
                    raw, mailbox=mailbox, state=state, cycle_received=cycle_received
                ):
                    forwarded += 1
            except Exception as exc:  # noqa: BLE001 — one bad item must not drop the rest
                message_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
                if not message_id:
                    # Unidentifiable: cannot be counted, deduped, or ever marked
                    # handled — retrying forever would wedge the cursor. Preserve
                    # what we can, move on. Exempt from the peer-success rule by
                    # necessity: there is no id under which it could ever resolve.
                    self._dead_letter(raw, message_id, exc)
                    continue
                failures.append((raw, message_id, exc))
        if watermark_skipped:
            logger.info(
                "msgraph poller: cursor reset re-list — %d pre-watermark item(s) dedupe-skipped",
                watermark_skipped,
            )

        # Failure accounting AFTER the loop, because the dead-letter decision needs
        # the cycle's outcome: an item may only dead-letter when a peer forwarded
        # in the SAME cycle (the sink demonstrably works → the failure is the
        # item's). All-fail cycles are systemic: hold indefinitely, count for
        # observability, page via Sentry — never dead-letter, so a 30-minute
        # outage cannot convert the backlog into dead-letter files, and counts
        # accrued during an outage cannot condemn an item on its first
        # post-recovery blip (poison-count starts moving only in mixed cycles).
        remaining = 0
        for raw, message_id, exc in failures:
            hold = state.record_hold(message_id)
            if forwarded > 0:
                poison = state.record_poison(message_id)
                if poison >= self._max_item_failures and self._dead_letter(raw, message_id, exc):
                    state.mark_seen(message_id)
                    state.clear_failure(message_id)
                    continue
                logger.warning(
                    "msgraph poller: delta item failed while peers succeeded (%s); "
                    "poison %d/%d, retry next cycle",
                    exc,
                    poison,
                    self._max_item_failures,
                )
            else:
                logger.warning(
                    "msgraph poller: failed to handle a delta item (%s); held %d cycle(s), "
                    "retry next cycle",
                    exc,
                    hold,
                )
            remaining += 1

        # Persist AFTER forwarding so a crash mid-batch re-forwards from the last
        # durable cursor (at-least-once; the seen ledger dedupes the replay).
        # A per-item failure HOLDS the cursor (overlay#275): advancing past an
        # unhandled item orphans it forever — Graph delta never re-returns it.
        # The old cursor re-lists the batch next cycle; the seen ledger (persisted
        # either way) dedupes the items that did get through, and new mail still
        # arrives because the old cursor covers it too.
        if remaining:
            logger.warning(
                "msgraph poller: %d delta item(s) unhandled; cursor held for retry", remaining
            )
            if forwarded == 0:
                # Systemic signature: everything attempted this cycle failed.
                self._sentry_note(
                    "msgraph poller: inbound forwarding failing; cursor held",
                    "warning",
                    extra={"failed_items": remaining},
                )
            state.persist(None)
        else:
            state.clear_all_failures()
            # The watermark advances only on a clean cursor advance, from the
            # timestamps of items this cycle actually handled — never from
            # dead-letter mark_seen, and never while anything is held, so a 410
            # during a hold can never skip the held mail.
            if cycle_received:
                state.advance_watermark(cycle_received)
            state.persist(delta_link)
        return forwarded

    def _watermark_skip(self, raw: Any, state: DeltaState) -> bool:
        """On a 410 re-list only: mark-seen-and-skip an UNSEEN item that is older
        than the clean-cycle watermark — unless it is currently held/failing, in
        which case it must retry normally. Anything unparseable forwards (fail
        open to delivery)."""
        if not isinstance(raw, dict):
            return False
        message_id = str(raw.get("id") or "")
        if not message_id or state.has_seen(message_id) or state.is_failing(message_id):
            return False
        watermark = _parse_iso(state.watermark)
        received = _parse_iso(raw.get("receivedDateTime"))
        if watermark is None or received is None or received >= watermark:
            return False
        state.mark_seen(message_id)
        return True

    def _sentry_note(
        self,
        message: str,
        level: str,
        *,
        tags: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort page signal (the audit trail is the log; this is the page).
        Constant messages so events group; identifiers ride as digests only; the
        sentry_init throttle makes a 45s loop page-safe. Never raises."""
        try:
            import sentry_sdk

            with sentry_sdk.new_scope() as scope:
                scope.set_tag("component", "msgraph-poller")
                for key, value in (tags or {}).items():
                    scope.set_tag(key, value)
                for key, value in (extra or {}).items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_message(message, level=level)
        except Exception:  # noqa: BLE001 — observability must never break polling
            logger.debug("msgraph poller: sentry note failed", exc_info=True)

    def _dead_letter(self, raw: Any, message_id: str, exc: Exception) -> bool:
        """Preserve a permanently-failing item's payload on the volume, loudly.
        Returns False when the write itself failed (payload NOT preserved)."""
        digest = hashlib.sha256(
            (message_id or json.dumps(raw, sort_keys=True, default=str)).encode("utf-8")
        ).hexdigest()[:16]
        path = os.path.join(os.path.dirname(self._state_path), "dead-letter", f"{digest}.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"reason": str(exc), "message_id": message_id, "raw": raw}, fh)
        except (OSError, TypeError, ValueError) as write_exc:
            logger.error(
                "msgraph poller: dead-letter write failed (%s) for item that failed with %s",
                write_exc,
                exc,
            )
            self._sentry_note(
                "msgraph poller: dead-letter write failed",
                "error",
                extra={"message_digest": digest[:8], "write_error": type(write_exc).__name__},
            )
            return False
        logger.error(
            "msgraph poller: delta item exhausted retries (%s); payload preserved at %s — "
            "this message will NOT reach the agent without manual replay "
            "(python3 -m shared.msgraph_replay <file>)",
            exc,
            path,
        )
        self._sentry_note(
            "msgraph poller: dead-letter written",
            "warning",
            extra={"message_digest": digest[:8], "reason_class": type(exc).__name__},
        )
        return True

    def _handle_message(
        self, raw: Any, *, mailbox: str, state: DeltaState, cycle_received: list[str]
    ) -> bool:
        if not isinstance(raw, dict):
            return False
        dto = msgraph_client.normalize_message(
            raw, mailbox=self._client.mailbox if self._client else ""
        )
        message_id = dto.get("message_id") or ""
        if not message_id:
            return False
        if state.has_seen(message_id):
            return False  # dedupe: already forwarded (cursor-reset replay)
        received_at = dto.get("received_at")
        from_addr = (dto.get("from_addr") or "").strip().lower()
        if mailbox and from_addr == mailbox:
            # Echo guard: a message the operator itself sent must not wake it in a
            # loop. Mark seen so a cursor reset does not re-evaluate it forever.
            state.mark_seen(message_id)
            if isinstance(received_at, str):
                cycle_received.append(received_at)
            return False
        self._forward(dto, message_id)
        state.mark_seen(message_id)
        state.clear_failure(message_id)
        if isinstance(received_at, str):
            cycle_received.append(received_at)
        # Persist IMMEDIATELY after each accepted forward: the adapter has the
        # message, so a crash before end-of-batch must not re-forward it under a
        # different idempotency-key format on the next boot (the sha256 request-id
        # migration would otherwise slip a duplicate turn past the adapter's
        # dedupe cache exactly once, across the upgrade reprovision).
        state.persist(None)
        return True

    def _forward(self, dto: dict[str, Any], message_id: str) -> None:
        body = json.dumps(
            {
                "source": SOURCE,
                "event_type": EVENT_TYPE,
                "inbound_message": dto,
                "event_id": message_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        signature = _hex_hmac_sha256(body, self._signing_secret or "")
        # X-Request-ID is the adapter's idempotency key. Hash rather than truncate:
        # Graph message ids are long base64 whose VARYING bytes are at the end, so
        # a [:64] prefix can collide across messages in one mailbox — and a
        # colliding key would dedupe a fresh message as a replay (silent loss, the
        # overlay#275 class). sha256 hex is exactly 64 chars and collision-free.
        request_id = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        status = self._forward_fn(body=body, signature=signature, request_id=request_id)
        if not 200 <= status < 300:
            # A rejected forward is the same loss class as a raised one: the adapter
            # did NOT accept the message, so marking it seen + advancing the cursor
            # would orphan it (overlay#275). Raise into poll_once's per-item failure
            # path so the cursor holds and the item retries next cycle (any 2xx on
            # the retry — including an idempotent-replay answer — counts as accepted;
            # a persistently non-2xx item dead-letters at the retry bound).
            raise RuntimeError(f"forward returned HTTP {status}")

    # ---- thread lifecycle -------------------------------------------------
    def start(self) -> bool:
        """Launch the poller daemon thread. Returns False (and logs) when the seat
        is not msgraph-inbound or a credential/secret is missing — fail-closed."""
        if not self._ready():
            return False
        self._thread = threading.Thread(target=self._run, name="smd-msgraph-poller", daemon=True)
        self._thread.start()
        logger.info(
            "msgraph poller: started (mailbox pinned, cursor=%s, forwarding to %s:%d/webhooks/%s)",
            "resumed" if (self._state and self._state.delta_link) else "fresh",
            _GATEWAY_HOST,
            _GATEWAY_PORT,
            ROUTE,
        )
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            period = self._poll_seconds(self._email_connector())
            # self._stop.wait(period) returns True on stop → exit promptly.
            if self._sleep_fn(period):
                return


def poller_from_env(**overrides: Any) -> MsGraphPoller:
    """Build a :class:`MsGraphPoller` from the gate-process environment.

    Reads the route signing secret through the same env the translate.py-
    materialized route uses (``WEBHOOK_SECRET_MSGRAPH``). Overrides are for tests.
    """
    try:
        period = int(os.environ.get("MSGRAPH_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))
    except ValueError:
        period = DEFAULT_POLL_SECONDS
    kwargs: dict[str, Any] = {
        "signing_secret": os.environ.get(_SIGNING_SECRET_ENV),
        "default_poll_seconds": period,
    }
    kwargs.update(overrides)
    return MsGraphPoller(**kwargs)


__all__ = ["DeltaState", "MsGraphPoller", "poller_from_env"]
