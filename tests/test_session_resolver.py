"""Session resolver for the provenance register (overlay #141).

Hermes core's three pre_tool_call fire sites pass task_id only — never
session_id — so the gate consulted the register under "" while reads were
recorded under the real id: 111/111 historical tier3 rows carried
register_was_empty=true and no session_id key. The resolver notes the real
id where core provides one (pre_llm_call, post_tool_call) and consulting
hooks fall back to it. A resolver miss degrades to the OLD behavior (empty
register — over-report, no exemption), never a widened one.
"""

import importlib
import sys
import threading

sys.path.insert(0, ".")

from shared import provenance
from shared.identifier_filter import ProvenanceRegister


def _fresh_provenance():
    importlib.reload(provenance)
    return provenance


def _run(*targets) -> list:
    """Start one thread per target, join them all, return collected errors."""
    errors: list = []

    def _wrap(fn):
        def _target() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — surfaced by the caller
                errors.append(exc)

        return _target

    threads = [threading.Thread(target=_wrap(fn)) for fn in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "worker thread hung"
    return errors


def test_resolve_prefers_given_id() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-old")
    assert prov.resolve_session("sess-new") == "sess-new"


def test_resolve_falls_back_to_last_noted() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-real")
    assert prov.resolve_session("") == "sess-real"
    assert prov.resolve_session(None) == "sess-real"


def test_resolver_miss_degrades_to_empty_never_widens() -> None:
    prov = _fresh_provenance()
    # Nothing noted yet: resolve("") -> "" -> register_for("") is EMPTY.
    assert prov.resolve_session("") == ""
    reg = prov.register_for("")
    assert isinstance(reg, ProvenanceRegister)
    assert not reg.captions()


def test_note_ignores_empty() -> None:
    prov = _fresh_provenance()
    prov.note_session("sess-real")
    prov.note_session("")
    prov.note_session(None)
    assert prov.resolve_session("") == "sess-real"


def test_end_to_end_record_under_real_consult_under_missing() -> None:
    """The #141 failure mode, fixed: post_tool_call records under the real id;
    a pre_tool_call consult with NO id resolves to the same register."""
    prov = _fresh_provenance()
    sid = "20260707_000001_e2e141"
    # turn start: pre_llm_call notes the real id
    prov.note_session(sid)
    # post_tool_call: read recorded under the real id
    prov.record_read(sid, "Discovery capture on Alvarez v. Draper, matter 2026-PI-101.")
    # pre_tool_call: core drops the id; resolver recovers the same register
    reg = prov.register_for(prov.resolve_session(""))
    assert "alvarez v. draper" in reg.captions()
    assert bool(reg)


# ---------------------------------------------------------------------------
# Concurrency (ss-console #2288) — the fallback must not cross sessions
#
# The resolver's original comment rested on "One Machine = one agent process =
# sequential sessions". ``shared/trust_decision.py:55-72`` refutes exactly that
# premise for the module next door, citing core's ``_get_worker_loop``
# (``/opt/hermes/model_tools.py:66-80``): "Each worker thread (e.g.,
# delegate_task's ThreadPoolExecutor threads) gets its own long-lived loop
# stored in thread-local storage", and ADR 0021 has ``delegate_task`` as a
# native primitive. Two agent threads in one process is therefore a live
# configuration, and the value this resolver returns is the primary key of the
# trust gate, the matter gate's party sets, spec status, voice status and the
# read-capture windows.
# ---------------------------------------------------------------------------


def test_concurrent_sessions_each_resolve_their_own():
    """Both threads note their own session, then both hit the missing-id path.

    A single process-global last-seen slot holds ONE value, so whichever thread
    noted last wins it and the other is handed a session it has nothing to do
    with. The barrier makes that deterministic rather than lucky: every note
    lands before any resolve.
    """
    prov = _fresh_provenance()
    barrier = threading.Barrier(2, timeout=5)
    got: dict[str, str] = {}

    def worker(tag: str, sid: str):
        def _body() -> None:
            prov.note_session(sid)  # this thread's turn start (pre_llm_call)
            barrier.wait()  # both noted; nothing resolved yet
            got[tag] = prov.resolve_session("")  # pre_tool_call — core drops the id

        return _body

    errors = _run(worker("a", "sess-A"), worker("b", "sess-B"))
    assert not errors, errors
    assert got == {"a": "sess-A", "b": "sess-B"}


def test_a_peers_note_cannot_displace_this_threads_session():
    """The mis-attribution ordering, pinned: A notes, B notes, A resolves.

    Against the process-global slot A resolves to ``sess-B`` — thread A's gate
    then reads thread B's register. Nothing about that is visible afterwards,
    which is why the defect survived four months of live rows.
    """
    prov = _fresh_provenance()
    a_noted, b_noted = threading.Event(), threading.Event()
    got: dict[str, str] = {}

    def thread_a() -> None:
        prov.note_session("sess-A")
        a_noted.set()
        assert b_noted.wait(timeout=5), "thread B never noted"
        got["a"] = prov.resolve_session("")

    def thread_b() -> None:
        try:
            assert a_noted.wait(timeout=5), "thread A never noted"
            prov.note_session("sess-B")
        finally:
            b_noted.set()  # never hang the peer on our failure

    errors = _run(thread_a, thread_b)
    assert not errors, errors
    assert got["a"] == "sess-A"


def test_a_peers_reads_cannot_exempt_this_threads_draft():
    """The blast radius in one assertion.

    Thread B never read the Alvarez matter. If B's missing-id resolve lands on
    A's session, B inherits A's provenance register — and a caption A read
    becomes a citation B is allowed to quote (``hermes-smd-reply`` consults
    ``register_for(resolve_session(...))`` for exactly that exemption). One
    thread's reads must never certify another thread's outbound text.
    """
    prov = _fresh_provenance()
    b_noted, a_read = threading.Event(), threading.Event()
    got: dict[str, frozenset] = {}

    def thread_a() -> None:
        try:
            assert b_noted.wait(timeout=5), "thread B never noted"
            prov.note_session("sess-A")
            prov.record_read("sess-A", "Deposition in Alvarez v. Draper, matter 2026-PI-101.")
        finally:
            a_read.set()

    def thread_b() -> None:
        prov.note_session("sess-B")  # B's own turn; B reads nothing
        b_noted.set()
        assert a_read.wait(timeout=5), "thread A never recorded its read"
        got["b"] = prov.register_for(prov.resolve_session("")).captions()

    errors = _run(thread_a, thread_b)
    assert not errors, errors
    assert "alvarez v. draper" not in got["b"], (
        f"thread B inherited thread A's captions: {sorted(got['b'])}"
    )


def test_a_thread_that_never_noted_refuses_to_guess_between_sessions():
    """With two sessions live on two threads, a third thread that never noted
    one has no basis to pick either. It resolves to nothing and SAYS so, rather
    than silently adopting whichever agent spoke most recently."""
    prov = _fresh_provenance()
    barrier = threading.Barrier(3, timeout=5)
    got: dict[str, tuple[str, str]] = {}

    def noter(sid: str):
        def _body() -> None:
            prov.note_session(sid)
            barrier.wait()

        return _body

    def bystander() -> None:
        barrier.wait()
        got["c"] = prov.resolve_session_with_mode("")

    errors = _run(noter("sess-A"), noter("sess-B"), bystander)
    assert not errors, errors
    assert got["c"] == ("", provenance.MODE_AMBIGUOUS)


# ---------------------------------------------------------------------------
# The resolution is DECLARED (ss-console #2288, part 2)
#
# ``trust_decision`` stamps ``trust_decision_match`` on every audit row so an
# auditor never has to guess how the join was made. Session resolution had no
# equivalent, so a cross-attribution left no trace at all — the reason this
# defect was invisible rather than merely unfixed.
# ---------------------------------------------------------------------------


def test_mode_is_recorded_on_the_keyed_path():
    prov = _fresh_provenance()
    prov.note_session("sess-noted")
    assert prov.resolve_session_with_mode("sess-explicit") == ("sess-explicit", prov.MODE_KEYED)
    assert prov.last_resolution() == ("sess-explicit", prov.MODE_KEYED)


def test_mode_is_recorded_on_the_fallback_path():
    prov = _fresh_provenance()
    prov.note_session("sess-noted")
    assert prov.resolve_session_with_mode("") == ("sess-noted", prov.MODE_THREAD)
    assert prov.last_resolution() == ("sess-noted", prov.MODE_THREAD)


def test_mode_is_recorded_when_nothing_resolves():
    prov = _fresh_provenance()
    assert prov.resolve_session_with_mode("") == ("", prov.MODE_NONE)
    assert prov.last_resolution() == ("", prov.MODE_NONE)


def test_an_unnoting_thread_still_resolves_the_one_live_session():
    """Behavior preservation for the configuration the original comment
    described. One agent, one session: a helper thread whose first hook is a
    CONSULT (core drops the id there, so it has never noted) resolves to that
    one session exactly as it does today — and the mode says the resolution
    came from the process, not from this thread."""
    prov = _fresh_provenance()
    prov.note_session("sess-only")
    got: dict[str, tuple[str, str]] = {}

    def helper() -> None:
        got["h"] = prov.resolve_session_with_mode("")

    errors = _run(helper)
    assert not errors, errors
    assert got["h"] == ("sess-only", prov.MODE_PROCESS)


def test_sequential_sessions_on_one_thread_stay_unambiguous():
    """A long-lived Machine runs many sessions in a row on the same thread.
    That is succession, not concurrency, and it must not disable the
    process-wide fallback for helper threads."""
    prov = _fresh_provenance()
    prov.note_session("sess-1")
    prov.note_session("sess-2")
    got: dict[str, tuple[str, str]] = {}

    def helper() -> None:
        got["h"] = prov.resolve_session_with_mode("")

    errors = _run(helper)
    assert not errors, errors
    assert got["h"] == ("sess-2", prov.MODE_PROCESS)
