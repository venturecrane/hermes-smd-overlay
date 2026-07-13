"""Cross-plugin voice runtime signal — the runtime half of the ADR 0028 §2 voice gate.

The voice plugin (``hermes-smd-voice``) is the PRODUCER and the trust plugin
(``hermes-smd-trust``) is the CONSUMER. The two plugins cannot import each other
(hyphenated package dirs are not valid dotted module paths), so ``shared/`` is
the only seam — the exact pattern ``shared.inbound.SESSION_TAINT`` uses (inbound
plugin marks, trust plugin reads). Process-wide singleton; single tenant per
Machine (AGENTS.md #5).

Two distinct signals, one register:

1. **Samples probe (binding-time).** The voice plugin owns "how to check whether
   voice samples are retrievable for this seat" — it holds the bound R2 reader
   and the customer slug. It publishes an opaque zero-arg callable here at
   ``bind_runtime``; the trust gate invokes it via :meth:`samples_available`.
   Unbound (no probe published) ⇒ ``samples_available()`` is ``False``
   (fail-closed): an authored-voice seat whose runtime is not actually
   delivering samples downgrades its autonomous sends to draft.

2. **Per-turn transform mark.** Set from the voice plugin's
   ``transform_llm_output`` hook on a successful (``TRANSFORMED``) reshape, and
   CLEARED at the start of every turn (``pre_llm_call``) so it can never be
   sticky across turns. A send whose turn did not apply the transform reads no
   mark ⇒ the gate downgrades. Keyed by the resolved session id (the same
   ``shared.provenance.resolve_session`` value the trust gate reads under), so
   the producer and consumer agree on the key.

The gate READS both; it never mutates them. All mutation is the voice plugin's,
mirroring the ``SESSION_TAINT`` producer/consumer split.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VoiceRuntimeStatus:
    """Runtime voice signals shared between the voice and trust plugins.

    Bounded (FIFO eviction at ``max_sessions``) so a long-lived Machine cannot
    leak the per-turn marks unboundedly if session ids churn. The samples probe
    is a single slot (single tenant per Machine)."""

    max_sessions: int = 512
    _samples_probe: Callable[[], bool] | None = None
    _applied: OrderedDict[str, bool] = field(default_factory=OrderedDict)

    # ------------------------------------------------------------------
    # Samples probe — published by the voice plugin at bind time
    # ------------------------------------------------------------------

    def publish_samples_probe(self, probe: Callable[[], bool] | None) -> None:
        """Publish the voice plugin's zero-arg 'are ≥1 samples retrievable?' probe.

        ``None`` clears it (an INACTIVE voice runtime), which makes
        :meth:`samples_available` fail closed to ``False``."""
        self._samples_probe = probe

    def samples_available(self) -> bool:
        """True iff the published probe reports ≥1 retrievable sample right now.

        FAIL CLOSED: no probe published (voice runtime unbound) ⇒ ``False``; a
        probe that raises ⇒ ``False`` with a logged warning. The gate treats
        "cannot confirm samples" as "no samples" — the safe direction is a draft
        downgrade, never a certified autonomous send."""
        probe = self._samples_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — a probe fault must not certify a send
            logger.warning(
                "voice_status: samples probe raised; treating as NO samples (fail-closed)",
                exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # Per-turn transform mark — set at transform, cleared each turn
    # ------------------------------------------------------------------

    def mark_applied(self, session_id: str) -> None:
        """Record that the voice transform reshaped this session's turn output.

        No-op for an empty session id. Set from ``transform_llm_output`` on a
        successful transform ONLY (never on passthrough / swallowed exception),
        so its presence means "voice demonstrably ran on this turn"."""
        if not session_id:
            return
        self._applied[session_id] = True
        self._applied.move_to_end(session_id)
        while len(self._applied) > self.max_sessions:
            self._applied.popitem(last=False)

    def clear_turn(self, session_id: str) -> None:
        """Clear the per-turn mark for ``session_id`` (start of every turn).

        Called from ``pre_llm_call`` so a prior turn's successful transform can
        never certify THIS turn's send. Idempotent; no-op for an empty id."""
        if not session_id:
            return
        self._applied.pop(session_id, None)

    def was_applied(self, session_id: str) -> bool:
        """True iff the voice transform ran on this session's current turn.

        Empty id ⇒ ``False`` (fail-closed: an untracked turn cannot certify)."""
        if not session_id:
            return False
        return bool(self._applied.get(session_id))


# Process-wide singleton — the voice plugin publishes/marks, the trust gate reads.
VOICE_STATUS = VoiceRuntimeStatus()


__all__ = ["VOICE_STATUS", "VoiceRuntimeStatus"]
