"""Thin HTTP client for the local per-Machine Honcho sidecar.

The Machine runs Honcho as a sidecar on ``HONCHO_BASE_URL`` (typically
``http://localhost:8000``). This module wraps the three operations the
memory-mirror plugin needs:

* :meth:`HonchoClient.list_conclusions` — fetch new/changed conclusions
  for a session (used by :mod:`mirror`).
* :meth:`HonchoClient.delete_conclusion` — physically delete a
  conclusion (used by :mod:`dismiss` and :mod:`archive`). Per ADR 0016 §1,
  the physical-delete posture works around Honcho upstream bug #658
  (corrections do not propagate through the reasoning tree).
* :meth:`HonchoClient.create_conclusion` — restore an archived conclusion
  back into the live store (used by :mod:`archive` restore path).

Implementation uses stdlib ``urllib.request`` to avoid pulling httpx as a
plugin dependency (per AGENTS.md: heavier deps belong in module files,
not plugin __init__.py — and even there, the simplest stdlib path is
preferred when a single sidecar HTTP surface is all that's needed).

Endpoints are derived from Honcho's documented HTTP API surface (see
https://docs.honcho.ai/). All calls are synchronous — the on_session_end
hook fires synchronously from Hermes' dispatcher and the sidecar runs in
the same process group with sub-millisecond latency.

Configuration:
    HONCHO_BASE_URL  — base URL of the local Honcho instance.
    HONCHO_API_KEY   — bearer token for sidecar auth.

Both values are read via ``shared.secrets.require`` at the call site, not
stored on the client; callers construct a new client per operation. The
client never logs token values.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HonchoUnreachable(RuntimeError):
    """Raised when the local Honcho sidecar is unreachable.

    The on_session_end callback catches this and logs a warning so a
    sidecar outage degrades gracefully instead of crashing the session
    (per AGENTS.md hard rule #3 — plugin callbacks must be exception
    safe). The audit plugin emits a ``MEMORY_PROVIDER_DEGRADED`` row when
    this happens; cross-correlate via ``session_id``.
    """


class HonchoClient:
    """Synchronous HTTP client for the per-Machine Honcho sidecar.

    One instance per call site. Holds the base URL and bearer token; does
    not pool connections (stdlib urllib opens one connection per request,
    which is fine for the per-turn cadence — typical session-end produces
    one to three conclusions).

    Construction with empty ``base_url`` or ``api_key`` raises
    :class:`ValueError`; callers should validate via ``shared.secrets``
    before instantiation.
    """

    # Conservative default — Honcho is local, but a hung sidecar shouldn't
    # block the agent loop indefinitely on session end.
    DEFAULT_TIMEOUT_SECONDS: float = 5.0

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url:
            raise ValueError("HonchoClient requires a non-empty base_url")
        if not api_key:
            raise ValueError("HonchoClient requires a non-empty api_key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_conclusions(
        self,
        *,
        session_id: str,
        since: Optional[str] = None,
    ) -> list[dict]:
        """Fetch conclusions for a session, optionally bounded by created_at.

        Args:
            session_id: Honcho session identifier (matches Hermes session_id).
            since: ISO-8601 timestamp; if provided, only conclusions whose
                ``created_at`` is strictly greater are returned. Used by
                :mod:`mirror` to fetch only new conclusions since the last
                mirror pass.

        Returns:
            List of conclusion dicts. Each dict carries at minimum:
            ``id``, ``created_at``, ``body`` (the conclusion payload),
            ``confidence`` (optional), and ``source_message_ids`` (list).
            Empty list when the sidecar has no matching rows.

        Raises:
            HonchoUnreachable: sidecar HTTP error or network failure.
        """
        params: dict[str, str] = {"session_id": session_id}
        if since:
            params["since"] = since
        path = "/conclusions?" + urllib.parse.urlencode(params)
        payload = self._get(path)
        conclusions = payload.get("conclusions")
        if not isinstance(conclusions, list):
            # Honcho returns either {"conclusions": [...]} or a bare list
            # depending on version. Accept both shapes.
            if isinstance(payload, list):
                return [c for c in payload if isinstance(c, dict)]
            return []
        return [c for c in conclusions if isinstance(c, dict)]

    def get_conclusion(self, conclusion_id: str) -> Optional[dict]:
        """Fetch a single conclusion by id.

        Returns ``None`` if Honcho reports a 404 (the row has already been
        physically deleted — that is not an error in the dismiss path,
        which is idempotent).

        Raises:
            HonchoUnreachable: any non-404 HTTP error or network failure.
        """
        if not conclusion_id:
            raise ValueError("conclusion_id is required")
        try:
            return self._get(f"/conclusions/{urllib.parse.quote(conclusion_id, safe='')}")
        except HonchoUnreachable as exc:
            if "404" in str(exc):
                return None
            raise

    def delete_conclusion(self, conclusion_id: str) -> bool:
        """Physically delete a conclusion. Returns ``True`` if the row was
        deleted, ``False`` if it did not exist (404).

        Per ADR 0016 §1, this is a hard delete — no soft-delete flag in
        Honcho propagates through the reasoning tree (upstream bug #658).
        Captain's portal calls this through :mod:`dismiss`; :mod:`archive`
        calls it after the row is safely copied into D1.

        Raises:
            HonchoUnreachable: any non-404 HTTP error or network failure.
        """
        if not conclusion_id:
            raise ValueError("conclusion_id is required")
        path = f"/conclusions/{urllib.parse.quote(conclusion_id, safe='')}"
        try:
            self._request("DELETE", path, body=None)
            return True
        except HonchoUnreachable as exc:
            if "404" in str(exc):
                return False
            raise

    def create_conclusion(self, conclusion: dict) -> dict:
        """Insert a conclusion back into the live store (restore path).

        Used by :mod:`archive`'s restore_from_archive to put an
        archived observation back into Honcho's working set when Captain
        un-archives via the admin portal.

        Args:
            conclusion: full conclusion payload as produced by
                :meth:`list_conclusions`. Honcho assigns a new ``id`` on
                insert; the returned dict carries it.

        Raises:
            HonchoUnreachable: sidecar HTTP error or network failure.
        """
        if not isinstance(conclusion, dict):
            raise ValueError("conclusion must be a dict")
        return self._request("POST", "/conclusions", body=conclusion)

    # ------------------------------------------------------------------
    # Internal HTTP plumbing
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        return self._request("GET", path, body=None)

    def _request(self, method: str, path: str, *, body: Optional[dict]) -> Any:
        url = self._base_url + path
        data: Optional[bytes] = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise HonchoUnreachable(
                        f"honcho {method} {path} returned non-JSON body"
                    ) from exc
        except urllib.error.HTTPError as exc:
            # Include the HTTP status code in the message so callers can
            # discriminate 404 from other failures via substring match.
            raise HonchoUnreachable(
                f"honcho {method} {path} returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise HonchoUnreachable(
                f"honcho {method} {path} unreachable: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise HonchoUnreachable(
                f"honcho {method} {path} timed out after {self._timeout}s"
            ) from exc


__all__ = ["HonchoClient", "HonchoUnreachable"]
