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
# A per-item failure holds the cursor and retries next cycle (overlay#275). This
# caps the retries for one item: after this many consecutive failures (~30 min at
# the 45s default) the raw payload is written to the dead-letter dir on the
# volume, the item is marked seen, and the cursor may advance — bounded, loud,
# payload-preserved loss instead of an unbounded cursor hold behind a poison
# message (which would grow the re-list window and eventually 429 Graph).
_MAX_ITEM_FAILURES = 40
_EMAIL_CAPABILITY = "Email"
_FORWARD_TIMEOUT_S = 30.0


def _hex_hmac_sha256(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


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
        # message id -> consecutive per-item failure count (the overlay#275 retry
        # bound). Small by construction: an id leaves on success or dead-letter.
        self._failures: dict[str, int] = {}
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
        failures = data.get("failures")
        if isinstance(failures, dict):
            for mid, count in failures.items():
                if isinstance(mid, str) and mid and isinstance(count, int) and count > 0:
                    self._failures[mid] = count

    def has_seen(self, message_id: str) -> bool:
        return message_id in self._seen_set

    def record_failure(self, message_id: str) -> int:
        """Bump and return the consecutive failure count for one message id."""
        count = self._failures.get(message_id, 0) + 1
        self._failures[message_id] = count
        return count

    def clear_failure(self, message_id: str) -> None:
        self._failures.pop(message_id, None)

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
            {"delta_link": self.delta_link, "seen_ids": self._seen, "failures": self._failures},
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
        max_item_failures: int = _MAX_ITEM_FAILURES,
    ) -> None:
        self._signing_secret = signing_secret
        self._yaml_path = yaml_path
        self._state_path = state_path or _default_state_path()
        self._client_factory = client_factory or msgraph_client.build_client_from_env
        self._forward_fn = forward_fn or _default_forward
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
        failed = 0
        mailbox = (client.mailbox or "").strip().lower()
        for raw in raw_messages:
            try:
                if self._handle_message(raw, mailbox=mailbox, state=state):
                    forwarded += 1
            except Exception as exc:  # noqa: BLE001 — one bad item must not drop the rest
                if not self._note_item_failure(raw, state, exc):
                    failed += 1
        # Persist AFTER forwarding so a crash mid-batch re-forwards from the last
        # durable cursor (at-least-once; the seen ledger dedupes the replay).
        # A per-item failure HOLDS the cursor for the same reason (overlay#275):
        # advancing past an unhandled item orphans it forever — Graph delta never
        # re-returns it. The old cursor re-lists the batch next cycle; the seen
        # ledger (persisted either way) dedupes the items that did get through,
        # and new mail still arrives because the old cursor covers it too. The
        # retry is BOUNDED: past _MAX_ITEM_FAILURES the item dead-letters (see
        # _note_item_failure) so a poison message cannot wedge the cursor.
        if failed:
            logger.warning(
                "msgraph poller: %d delta item(s) unhandled; cursor held for retry", failed
            )
            state.persist(None)
        else:
            state.persist(delta_link)
        return forwarded

    def _note_item_failure(self, raw: Any, state: DeltaState, exc: Exception) -> bool:
        """Record one per-item failure. Returns True when the item was DEAD-LETTERED
        (payload preserved on the volume, marked seen — the cursor may advance) and
        False when it should retry next cycle (the cursor must hold).

        An item with no extractable id cannot be counted or deduped, so it
        dead-letters immediately — retrying it forever would wedge the cursor with
        no way to ever mark it handled."""
        message_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if not message_id:
            # Unidentifiable: cannot be counted, deduped, or ever marked handled —
            # retrying forever would wedge the cursor. Preserve what we can, move on.
            self._dead_letter(raw, message_id, exc)
            return True
        count = state.record_failure(message_id)
        if count < self._max_item_failures:
            logger.warning(
                "msgraph poller: failed to handle a delta item (%s); retry %d/%d next cycle",
                exc,
                count,
                self._max_item_failures,
            )
            return False
        if not self._dead_letter(raw, message_id, exc):
            return False  # payload NOT preserved — keep retrying rather than drop
        state.mark_seen(message_id)
        state.clear_failure(message_id)
        return True

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
            return False
        logger.error(
            "msgraph poller: delta item exhausted retries (%s); payload preserved at %s — "
            "this message will NOT reach the agent without manual replay",
            exc,
            path,
        )
        return True

    def _handle_message(self, raw: Any, *, mailbox: str, state: DeltaState) -> bool:
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
        from_addr = (dto.get("from_addr") or "").strip().lower()
        if mailbox and from_addr == mailbox:
            # Echo guard: a message the operator itself sent must not wake it in a
            # loop. Mark seen so a cursor reset does not re-evaluate it forever.
            state.mark_seen(message_id)
            return False
        self._forward(dto, message_id)
        state.mark_seen(message_id)
        state.clear_failure(message_id)
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
