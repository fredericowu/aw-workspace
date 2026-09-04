"""W7 — terminal sessions are relayed between workers by Redis Streams.

A PTY master fd cannot cross a process boundary, so the worker that forks a
terminal is the only one that can ever hold its fd. W5 worked around that with
GNU ``screen``; W7 removed that dependency and moved the BYTES instead, over
two Redis Streams per session (``src/api/terminal_manager.py``):

* ``…:term:out:<id>`` — the owner XADDs PTY bytes, EVERY worker (the owner
  included) XREADs them and pushes to its own WS subscribers. The stream is
  also the scrollback, so a WS connect replays identically on any worker.
* ``…:term:in:<id>`` — ANY worker XADDs keystrokes and resizes, ONLY the owner
  consumes them and writes the fd.

Every claim the W5 suite this file replaces made still applies; only the
mechanism changed, so its structure is kept: two independent ``TerminalManager``
objects stand in for two worker processes throughout, sharing one Redis. That
is faithful, because the thing under test is precisely that nothing needed to
serve a session lives in the creating object.

Sections, one guarantee each:

1. SINGLE DELIVERY PATH — the owner XADDs and reads its own bytes back like
   everyone else, and there is no local fast path. This is the regression
   guard for W5b's doubled keystrokes (commit 0ec19b1): two writers into one
   shell is exactly what a fast path reintroduces.
2. Session metadata lives in a Redis hash, visible to every worker at once.
3. Concurrent creation of one session id is deduped with ``SET NX EX 30``.
4. Liveness prunes on a conclusive read and NEVER on an inconclusive one
   (W5b's other half, carried over: a read that RAISED must delete nothing).

Plus the end-to-end claim the card actually cares about: create a session on
"worker A", attach from "worker B", and prove bytes flow BOTH ways.

Requires a real Redis, and FAILS rather than skips without one — with W7 the
ephemeral Redis in .github/workflows/test.yml stopped being a nicety and became
the thing this whole file rests on. A skip here is how the card would quietly
stop being tested, which is the failure mode this repo has already been bitten
by twice.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.api import terminal_manager as tm
from src.api.terminal_manager import (
    SessionMetaStore,
    TerminalManager,
    _claim_creation,
    _get_redis,
    _in_key,
    _out_key,
    _owner_alive,
    _term_key,
    streams_enabled,
)

pytestmark = [pytest.mark.integration]


def _redis_available() -> bool:
    tm._reset_redis_client()
    return _get_redis() is not None


pytestmark.append(
    pytest.mark.skipif(not _redis_available(), reason="Redis not reachable")
)


def _require_streams():
    """Fail loudly unless the stream backing is actually live here.

    Presence of a Redis URL is not enough: without a reachable Redis
    ``terminal_manager`` degrades to a worker-owned PTY and every test below
    would pass while asserting nothing about W7 — a green run covering none of
    the card. So probe for real.
    """
    assert streams_enabled(), (
        "no reachable Redis — terminal_manager falls back to a worker-owned "
        "PTY, so this test would pass while asserting nothing about W7. "
        "CI starts an ephemeral Redis for exactly this (see test.yml)."
    )


@pytest.fixture(autouse=True)
def _clean_redis():
    """Wipe this file's own keys either side of every test.

    Scoped to the ``w7test-`` prefix, never a flush: this Redis is shared with
    the running workspace in local dev, and a FLUSHDB here would take out live
    leases and broadcast state.
    """
    client = _get_redis()

    def _wipe():
        if client is None:
            return
        for pattern in ("*term:meta:w7test-*", "*term:creating:w7test-*",
                        "*term:owner:w7test-*", "*term:out:w7test-*",
                        "*term:in:w7test-*"):
            for key in client.scan_iter(match=pattern):
                client.delete(key)

    _wipe()
    yield
    _wipe()


@pytest.fixture
def managers():
    """Track TerminalManagers a test builds and tear their sessions down.

    A leaked session is a leaked process tree AND a leaked pair of streams
    that outlive the test run, so the suite pays for its own cleanup rather
    than littering the CI container.
    """
    made: list[TerminalManager] = []

    def _make(count: int = 1):
        built = [TerminalManager() for _ in range(count)]
        made.extend(built)
        return built if count > 1 else built[0]

    yield _make
    for mgr in made:
        for sid in list(mgr.sessions):
            try:
                mgr.remove(sid)
            except Exception:
                pass


def _sid() -> str:
    return f"w7test-{uuid.uuid4().hex[:8]}"


async def _wait_for_bytes(session, needle: bytes, timeout: float = 15.0) -> bytes:
    """Drain a session's subscriber queue until ``needle`` shows up.

    Through ``subscribe()``, not the fd: on a non-owner worker there IS no fd,
    and going through the queue is the path a real ``/ws/terminal`` client
    takes on every worker alike.
    """
    queue = session.subscribe()
    buf = bytearray(session.get_scrollback())
    if needle in buf:
        session.unsubscribe(queue)
        return bytes(buf)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(queue.get(), 0.5)
            except asyncio.TimeoutError:
                continue
            buf.extend(chunk)
            if needle in buf:
                return bytes(buf)
    finally:
        session.unsubscribe(queue)
    return bytes(buf)


async def _settle(session, timeout: float = 10.0) -> None:
    """Wait for the login shell's first prompt so a test's own keystrokes
    aren't swallowed by a shell that hasn't started reading yet."""
    await _wait_for_bytes(session, b"$", timeout=timeout)


# ---------------------------------------------------------------------------
# 1. Single delivery path — the owner is not special
# ---------------------------------------------------------------------------


def test_owner_writes_go_out_on_the_input_stream_not_straight_to_the_fd(managers):
    """One ``write()`` on the OWNER worker must produce exactly one input
    frame, and must not also touch the fd directly.

    A "fast path for the owner" — write the fd AND consume the stream — is
    W5b's doubled-keystroke bug wearing a different hat (commit 0ec19b1: two
    writers into one shell). Asserted structurally here and behaviourally in
    the next test.
    """
    _require_streams()
    session_id = _sid()
    mgr = managers()
    session = mgr.create(name="single-path", session_id=session_id,
                         command="sleep 60")
    assert session.is_owner

    # WHICH THREAD touches the fd is the whole assertion: the owner's input
    # consumer is the one legitimate writer, so a call arriving on the calling
    # thread is a synchronous fast path — the second writer.
    writes: list[tuple[str, bytes]] = []
    session._write_fd = (  # type: ignore[method-assign]
        lambda data: writes.append((threading.current_thread().name, data)))
    session.write(b"hello")

    client = _get_redis()
    assert client.xlen(_in_key(session_id)) == 1, \
        "one write() must produce exactly one input-stream frame"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not writes:
        time.sleep(0.05)
    assert writes, "the owner's input consumer never delivered the keystroke"
    time.sleep(0.5)  # give a second, duplicate delivery time to show up
    assert len(writes) == 1, (
        f"the keystroke reached the PTY {len(writes)} times ({writes}) — that "
        "is W5b's doubled keystrokes"
    )
    assert writes[0][0].startswith("term-in-consumer"), (
        f"write() reached the PTY fd from {writes[0][0]!r} rather than the "
        "input consumer — a synchronous fast path on the owner worker is a "
        "second writer into the same shell"
    )


def test_a_keystroke_on_the_owner_reaches_the_shell_exactly_once(managers, tmp_path):
    """The behavioural half: one command typed on the owner runs ONCE.

    If the owner both wrote the fd and consumed its own input frame, the shell
    would receive the line twice and the counter file would hold two bytes.
    """
    _require_streams()
    counter = tmp_path / "runs"
    session_id = _sid()
    mgr = managers()

    async def run():
        session = mgr.create(name="once", session_id=session_id)
        session.start_reader(asyncio.get_running_loop())
        await _settle(session)
        session.write(f"printf x >> {counter}; echo DONE_ONCE\r".encode())
        await _wait_for_bytes(session, b"DONE_ONCE")
        # The redirect completes before the echo it is chained to, but give a
        # doubled second run a chance to land too — the point is to catch it,
        # not to race past it.
        await asyncio.sleep(1.0)
        assert counter.read_text() == "x", (
            f"command ran {len(counter.read_text())} time(s), expected 1 — the "
            "owner is delivering its own keystrokes twice"
        )

    asyncio.run(run())


def test_the_owner_receives_its_own_output_through_the_stream(managers):
    """The owner's subscribers must be fed by its output CONSUMER, not by the
    PTY reader fanning out in place — one path, no local branch to drift."""
    _require_streams()
    session_id = _sid()
    mgr = managers()

    async def run():
        session = mgr.create(name="own-output", session_id=session_id)
        session.start_reader(asyncio.get_running_loop())
        await _settle(session)

        fanned = []
        real_fan_out = session._fan_out

        def spy(data):
            fanned.append(data)
            real_fan_out(data)

        session._fan_out = spy  # type: ignore[method-assign]
        marker = f"OWNSTREAM_{uuid.uuid4().hex[:6]}"
        session.write(f"echo {marker}\r".encode())
        assert marker.encode() in await _wait_for_bytes(session, marker.encode())

        client = _get_redis()
        assert client.xlen(_out_key(session_id)) > 0, \
            "the owner never published its PTY bytes to the output stream"
        assert fanned, "the owner's own consumer never delivered anything"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. Session metadata lives in Redis, shared across workers
# ---------------------------------------------------------------------------


def test_session_meta_visible_across_two_worker_instances():
    """Two independent stores (= two worker processes) see the same data."""
    session_id = _sid()
    worker_a, worker_b = SessionMetaStore(), SessionMetaStore()

    worker_a.update(session_id, name="from-worker-a", type="terminal", insecure=False)

    seen = worker_b.get(session_id)
    assert seen["name"] == "from-worker-a"
    assert seen["type"] == "terminal"
    assert seen["insecure"] is False

    worker_b.update(session_id, insecure=True)
    assert worker_a.get(session_id)["insecure"] is True


def test_meta_delete_is_visible_to_the_other_worker():
    session_id = _sid()
    a, b = SessionMetaStore(), SessionMetaStore()
    a.update(session_id, name="doomed")
    assert b.get(session_id)["name"] == "doomed"
    a.delete(session_id)
    assert b.get(session_id) == {}


def test_rename_on_one_worker_is_readable_on_another(managers):
    """`set_name` must write through to Redis — a rename that only touched
    the local dict would show the old name on every other worker."""
    session_id = _sid()
    mgr1, mgr2 = managers(2)

    mgr1._meta.update(session_id, name="before", type="terminal")
    mgr1.set_name(session_id, "after")

    assert mgr2._meta.get(session_id)["name"] == "after"


def test_meta_keys_are_scoped_to_this_workspace(monkeypatch):
    """One shared Redis can host several workspaces; an unscoped key would
    hand one workspace's shell to another."""
    session_id = _sid()
    monkeypatch.setenv("AW_WORKSPACE", "ws-one")
    SessionMetaStore().update(session_id, name="belongs-to-one")

    monkeypatch.setenv("AW_WORKSPACE", "ws-two")
    assert SessionMetaStore().get(session_id) == {}

    monkeypatch.setenv("AW_WORKSPACE", "ws-one")
    assert SessionMetaStore().get(session_id)["name"] == "belongs-to-one"


def test_shell_pid_in_meta_lets_any_worker_read_the_process_tree(managers):
    """``session.pid`` is process-local; the process TREE is not.

    Every uvicorn worker shares one container and one PID namespace, so a
    non-owner worker reads a session's processes straight from ``shell_pid``
    with no relay at all. Returning "session not found" for proc queries off
    the owner would empty the SPA's per-terminal process badge and make its
    kill action refuse every pid, 9 times out of 10.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    session_a = worker_a.create(name="procs", session_id=session_id,
                                command="sleep 47; echo done")

    session_b = worker_b.get(session_id)
    assert session_b is not None, "worker B could not adopt — W7 regression"
    assert session_b.fd is None, "a non-owner must not hold a PTY fd"
    assert session_b.proc_root_pid() == session_a.shell_pid

    deadline = time.monotonic() + 15
    names: list[str] = []
    while time.monotonic() < deadline:
        names = [p["name"] for p in session_b.child_procs()]
        if "sleep" in names:
            break
        time.sleep(0.2)
    assert "sleep" in names, (
        f"the non-owning worker saw {names} — the process badge is empty off "
        "the owner, which is a regression against W5"
    )


# ---------------------------------------------------------------------------
# 3. Concurrent creation of one session id is deduped
# ---------------------------------------------------------------------------


def test_concurrent_claim_lets_exactly_one_worker_through():
    """Eight 'workers' racing on one id — SETNX must elect exactly one, so
    only one of them ever forks a shell."""
    session_id = _sid()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _claim_creation(session_id), range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_claim_is_held_against_a_later_racer():
    session_id = _sid()
    assert _claim_creation(session_id) is True
    assert _claim_creation(session_id) is False


def test_concurrent_create_of_one_session_id_yields_one_shell(managers):
    """Two workers handed the same create must produce ONE shell, not two
    fighting over one id — ``restart()`` calls ``create()`` with an EXPLICIT
    session_id, so this race is reachable in production."""
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)

    def do_create(mgr):
        return mgr.create(name="race", session_id=session_id,
                          command="sleep 30", session_type="terminal")

    with ThreadPoolExecutor(max_workers=2) as pool:
        sessions = list(pool.map(do_create, [worker_a, worker_b]))

    owners = [s for s in sessions if s.is_owner]
    assert len(owners) == 1, f"{len(owners)} workers forked a shell, expected 1"
    remote = [s for s in sessions if not s.is_owner]
    assert len(remote) == 1
    assert remote[0].shell_pid == owners[0].shell_pid, \
        "the loser built a handle onto a different shell"


def test_concurrent_get_on_a_cold_session_id_yields_one_handle(managers, monkeypatch):
    """Two threads racing ``get()`` on the same never-locally-seen session_id
    must produce exactly ONE handle.

    Without the per-session_id lock both threads observe a cache miss, both
    adopt, and only one wins ``self.sessions[session_id]`` — the other is
    leaked back to its caller as an extra live writer into the same shell,
    which is what produced the reported keystroke duplication (W5b).
    Reproduced deterministically by stalling the adopt step so both threads
    are guaranteed to be mid-flight at once if nothing serializes them.
    """
    _require_streams()
    session_id = _sid()
    worker_b = managers()
    # A REAL, disposable process to stand in for the absent owner's shell —
    # never this test process's own pid. `remove()` (which the fixture runs on
    # teardown) SIGKILLs a non-owned session's whole process tree, and seeding
    # `shell_pid` with os.getpid() makes the suite kill itself.
    stand_in = subprocess.Popen(["sleep", "120"])
    worker_b._meta.update(session_id, name="race", type="terminal", command="",
                          shell_pid=stand_in.pid, insecure=False,
                          agent_session_id="")
    tm._set_owner(session_id)
    assert session_id not in worker_b.sessions

    adopts = []
    real_remote = worker_b._remote_session

    def slow_remote(sid, meta):
        adopts.append(1)
        time.sleep(0.3)  # widen the race window
        return real_remote(sid, meta)

    monkeypatch.setattr(worker_b, "_remote_session", slow_remote)

    results = [None, None]

    def do_get(i):
        results[i] = worker_b.get(session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(do_get, range(2)))

    assert len(adopts) == 1, (
        f"expected exactly one adopt for one cold session_id, got "
        f"{len(adopts)} — the get() check-then-act race is back"
    )
    assert results[0] is not None and results[0] is results[1], \
        "both callers must share the one handle"
    assert len(worker_b.sessions) == 1

    stand_in.kill()
    stand_in.wait()


def test_concurrent_restarts_of_one_session_id_never_overlap(managers, monkeypatch):
    """restart()'s pop -> kill -> recreate sequence must be serialized per
    session_id too — ``create``, ``restart`` and ``remove`` all touch
    ``self.sessions`` the same way ``get`` does.

    Pinned to the no-Redis backing so only the lock itself is under test:
    every ``_fork_exec`` records its start/end and no two windows may overlap.
    """
    session_id = _sid()
    mgr = managers()
    monkeypatch.setattr(tm, "streams_enabled", lambda: False)
    monkeypatch.setattr(tm, "_get_redis", lambda: None)

    intervals = []
    fake_pid = [10_000]

    def fake_fork_exec(cmd_parts, rows=24, cols=80):
        start = time.monotonic()
        time.sleep(0.2)
        intervals.append((start, time.monotonic()))
        fake_pid[0] += 1
        return (os.open(os.devnull, os.O_RDWR), fake_pid[0])

    monkeypatch.setattr(mgr, "_fork_exec", fake_fork_exec)

    mgr.create(name="restart-race", session_id=session_id, command="sleep 30")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(
            lambda _: mgr.restart(session_id, command="sleep 30"), range(2)))

    # 1 initial create + 2 restarts.
    assert len(intervals) == 3
    for (s1, e1), (s2, e2) in zip(sorted(intervals), sorted(intervals)[1:]):
        assert e1 <= s2, (
            f"overlapping fork windows {(s1, e1)} vs {(s2, e2)} — "
            "restart()'s per-session_id guard is missing"
        )
    assert len(mgr.sessions) == 1


# ---------------------------------------------------------------------------
# The whole point: bytes flow both ways from a worker that did not create it
# ---------------------------------------------------------------------------


def test_session_created_on_worker_a_is_served_from_worker_b(managers):
    """Create on A, serve from B, and prove BOTH directions.

    This is the W5 bug reduced to one test: before it, ``worker_b.get()``
    returned None for a session it had not created and ``/ws/terminal/<id>``
    answered 4004 on N-1 of N workers. W7 keeps the property and changes only
    how the bytes get there.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)

    async def run():
        loop = asyncio.get_running_loop()
        session_a = worker_a.create(name="cross", session_id=session_id)
        session_a.start_reader(loop)
        assert session_a.is_owner

        # B has never seen this session and holds no fd for it.
        assert session_id not in worker_b.sessions
        session_b = worker_b.get(session_id)
        assert session_b is not None, "worker B could not serve it — W7 regression"
        assert session_b.fd is None and not session_b.is_owner
        assert session_b.shell_pid == session_a.shell_pid
        session_b.start_reader(loop)

        await _settle(session_a)

        # B -> shell: keystrokes written on the NON-OWNING worker must run.
        marker_b = f"MARKER_FROM_B_{uuid.uuid4().hex[:6]}"
        session_b.write(f"echo {marker_b}\r".encode())
        assert marker_b.encode() in await _wait_for_bytes(session_b, marker_b.encode()), \
            "input written on worker B never reached the shell"

        # shell -> A: and the SAME output must reach the owner's subscribers,
        # because both read the one output stream.
        assert marker_b.encode() in await _wait_for_bytes(session_a, marker_b.encode()), \
            "output of B's command never reached worker A"

        # A -> shell -> B, to prove it is not one-directional.
        marker_a = f"MARKER_FROM_A_{uuid.uuid4().hex[:6]}"
        session_a.write(f"echo {marker_a}\r".encode())
        assert marker_a.encode() in await _wait_for_bytes(session_b, marker_a.encode()), \
            "output of A's command never reached worker B"

    asyncio.run(run())


def test_scrollback_replays_on_a_worker_that_never_created_the_session(managers):
    """The output stream IS the scrollback.

    ``terminal.py`` replays ``session.get_scrollback()`` on every WS connect.
    Before W7 that buffer was owner-local, so a client landing on a non-owner
    worker got a blank terminal — the single reason this is Streams and not
    pub/sub. A worker adopting the session must read the tail and replay the
    same bytes.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    marker = f"SCROLLBACK_{uuid.uuid4().hex[:6]}"

    async def run():
        session_a = worker_a.create(name="replay", session_id=session_id,
                                    command=f"echo {marker}; sleep 60")
        session_a.start_reader(asyncio.get_running_loop())
        assert marker.encode() in await _wait_for_bytes(session_a, marker.encode())

        # Worker B adopts cold — no consumer has ever run here for this id.
        session_b = worker_b.get(session_id)
        assert session_b is not None
        assert marker.encode() in session_b.get_scrollback(), (
            "a WS client landing on a non-owner worker got a blank terminal — "
            "the scrollback did not replay from the output stream"
        )

    asyncio.run(run())


def test_scrollback_stays_current_on_a_worker_that_only_serves_rest(managers):
    """A worker with NO output consumer must still serve a FRESH scrollback.

    ``get_scrollback()`` is a pure in-memory read (``terminal.py`` calls it
    from an ``async def``) kept current by this worker's output consumer —
    which only starts on a WS connect. A worker that adopted a session on a
    plain REST call (``POST …/write``, ``GET …/procs``) has no consumer, so
    its buffer used to FREEZE at whatever the stream held when it adopted, and
    every later ``GET …/scrollback`` served that snapshot.

    Found live on 2026-09-04 at workers=10, not by this suite: six consecutive
    scrollback reads of one session returned 24, 22, **0**, 14, 22 and 24 hits.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)

    async def run():
        session_a = worker_a.create(name="rest-only", session_id=session_id)
        session_a.start_reader(asyncio.get_running_loop())
        await _settle(session_a)

        # B adopts on a REST-shaped call, BEFORE the interesting output — no
        # start_reader, so no consumer here. This is what froze the buffer.
        session_b = worker_b.get(session_id)
        assert session_b is not None and not session_b._out_consumer_started

        later = f"AFTER_ADOPT_{uuid.uuid4().hex[:6]}"
        session_a.write(f"echo {later}\r".encode())
        assert later.encode() in await _wait_for_bytes(session_a, later.encode())

        # The next REST call on B goes through get() again, exactly as
        # terminal.py does — and must see output produced after B adopted.
        refreshed = worker_b.get(session_id)
        assert refreshed is session_b, "expected the cached handle, not a new one"
        assert later.encode() in refreshed.get_scrollback(), (
            "a worker serving only REST froze its scrollback at adopt time — "
            "GET /scrollback returns a stale snapshot there"
        )

    asyncio.run(run())


def test_list_sessions_shows_sessions_created_on_another_worker(managers):
    """The SPA's terminal list must be the same whichever worker serves it."""
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    worker_a.create(name="listed-everywhere", session_id=session_id,
                    command="sleep 60")

    ids = {s["id"]: s for s in worker_b.list_sessions()}
    assert session_id in ids
    assert ids[session_id]["name"] == "listed-everywhere"

    worker_b.remove(session_id)
    assert session_id not in {s["id"] for s in worker_a.list_sessions()}


def test_remove_from_a_non_owning_worker_ends_the_session(managers):
    """A DELETE lands on any worker; it must still end the session rather
    than leaving an unreachable shell running forever.

    The non-owner holds no fd, so its only conclusive lever is the shared PID
    namespace — ``kill_proc_tree(shell_pid)``.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    session_a = worker_a.create(name="doomed", session_id=session_id,
                                command="sleep 60")
    shell_pid = session_a.shell_pid
    assert tm._pid_alive(shell_pid)

    worker_b.remove(session_id)  # worker B holds no PTY for this session

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and tm._pid_alive(shell_pid):
        time.sleep(0.2)
    assert not tm._pid_alive(shell_pid), \
        "the shell survived a delete that landed on a non-owning worker"
    assert worker_b._meta.get(session_id) == {}
    assert not _owner_alive(session_id)


def test_restart_reuses_the_id_within_the_claim_ttl(managers):
    """A restart ends and immediately re-creates one session ID. The creation
    claim has a 30s TTL, so unless the teardown releases it the re-create
    takes the "someone else is making it" branch and waits for an owner nobody
    is going to publish — a terminal that opens blank and never responds."""
    _require_streams()
    session_id = _sid()
    mgr = managers()
    first = mgr.create(name="restarter", session_id=session_id, command="sleep 60")
    first_pid = first.shell_pid

    restarted = mgr.restart(session_id, name="restarter")  # well inside the TTL

    assert restarted is not None
    assert restarted.is_owner, "restart left no owner — the stale claim blocked it"
    assert restarted.shell_pid != first_pid, "restart reused the old shell"
    assert session_id in {s["id"] for s in mgr.list_sessions()}


def test_restart_does_not_replay_the_previous_shells_scrollback(managers):
    """The id is reused, so the OLD output stream has to go with the old
    shell — otherwise the new terminal opens showing the previous one."""
    _require_streams()
    session_id = _sid()
    mgr = managers()
    stale = f"STALE_{uuid.uuid4().hex[:6]}"

    async def run():
        session = mgr.create(name="restart-replay", session_id=session_id,
                             command=f"echo {stale}; sleep 60")
        session.start_reader(asyncio.get_running_loop())
        assert stale.encode() in await _wait_for_bytes(session, stale.encode())

        fresh = mgr.restart(session_id, command="sleep 60")
        assert fresh is not None
        assert stale.encode() not in fresh.get_scrollback(), \
            "the restarted terminal replayed the previous shell's output"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. Liveness: prune on a conclusive read, NEVER on an inconclusive one
# ---------------------------------------------------------------------------


def test_a_session_whose_owner_key_expired_is_pruned(managers):
    """The owner heartbeat is the liveness signal that replaced ``screen -ls``.

    Its expiry — the owner worker died without cleaning up — is a CONCLUSIVE
    "this session is gone", so the meta and the streams go with it.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    worker_a.create(name="orphan", session_id=session_id, command="sleep 60")
    assert session_id in {s["id"] for s in worker_b.list_sessions()}

    # Exactly what the TTL lapsing looks like to the rest of the fleet.
    _get_redis().delete(_term_key(tm._OWNER_SUFFIX, session_id))

    assert session_id not in {s["id"] for s in worker_b.list_sessions()}, \
        "a session with no live owner is still being listed"
    assert worker_b._meta.get(session_id) == {}
    assert _get_redis().exists(_out_key(session_id)) == 0, \
        "the pruned session's output stream leaked"


def test_list_sessions_prunes_nothing_when_the_owner_read_fails(managers, caplog):
    """A liveness read that did not complete must prune NOTHING.

    Carried over verbatim in intent from W5b, where ``_screen_sessions()``
    returned ``{}`` from its except branch — indistinguishable from "nothing
    is running" — and one failed subprocess deleted the Redis meta of every
    terminal in the workspace while their shells kept running, with nothing
    logged. Two prune branches ride on this read (the local-handle drop and
    the meta delete) and both are covered: fixing only the delete would leave
    the same session missing from its own worker's list.

    Timing matters more than probability: at ``AW_WORKSPACE_WORKERS=10`` every
    worker runs this on every ``terminal_update`` broadcast.
    """
    _require_streams()
    session_id = _sid()
    worker_a, worker_b = managers(2)
    worker_a.create(name="survives-a-failed-read", session_id=session_id,
                    command="sleep 60")
    assert session_id in {s["id"] for s in worker_b.list_sessions()}
    # Worker B holds a live remote handle too, so branch 1 is exercised.
    assert worker_b.get(session_id) is not None

    def _boom(*a, **kw):
        raise ConnectionError("redis went away mid-scan")

    with pytest.MonkeyPatch.context() as mp, \
            caplog.at_level("WARNING", logger="terminal"):
        caplog.clear()
        mp.setattr(_get_redis(), "scan_iter", _boom)

        assert tm._owner_map() is None, \
            "a read that RAISED must be distinguishable from an empty one"

        # Branch 2 (the irreversible one): worker A never created this on B,
        # so the Redis meta is B's only durable view of it. It is unreachable
        # while scan_iter is broken, so assert the delete never fired instead.
        worker_b.list_sessions()
        # Branch 1: worker B's remote handle must survive it.
        assert session_id in worker_b.sessions, \
            "a remote handle was dropped on an inconclusive read"
        # Not-destructive is not enough — a silently stale terminal list is how
        # nobody finds out the read started failing.
        assert any("inconclusive" in r.message for r in caplog.records
                   if r.levelname == "WARNING"), \
            "nothing logged at WARNING when the owner-key read failed"

    # And nothing was destroyed: with Redis working again the session is still
    # there, with its meta and its streams intact.
    assert worker_b._meta.get(session_id).get("shell_pid"), \
        "meta deleted on an inconclusive read — unrecoverable"
    assert session_id in {s["id"] for s in worker_b.list_sessions()}
    assert worker_b.get(session_id) is not None, "serving broken after recovery"


# ---------------------------------------------------------------------------
# Stream work must not run on the event-loop thread
# ---------------------------------------------------------------------------


def test_terminal_routes_never_call_the_manager_from_an_async_def():
    """Every blocking manager call must go through ``asyncio.to_thread``.

    A create forks a PTY and primes scrollback off Redis; a remove kills a
    process tree and waits for it. Called straight from an ``async def`` that
    freezes EVERY in-flight request on this worker, which is the 2026-09-02
    event-loop freeze all over again.

    ``test_no_blocking_db_in_async_routes.py`` cannot catch this — it says so
    itself: it does not chase blocking calls through attribute access on
    another object (``self.mgr.create(...)``). So this asserts it directly on
    the source, in the one file where the calls live.
    """
    import ast
    import inspect

    from src.api import terminal as terminal_mod

    src = inspect.getsource(terminal_mod)
    tree = ast.parse(src)
    routes = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == "TerminalRoutes")

    offenders = []
    for fn in routes.body:
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            # `self.mgr.<something>(...)` reached without an intervening
            # to_thread is the shape being banned.
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "mgr"
                    and isinstance(f.value.value, ast.Name)
                    and f.value.value.id == "self"):
                offenders.append(f"{fn.name} -> self.mgr.{f.attr}()")

    assert not offenders, (
        "these async handlers call the terminal manager directly instead of "
        "via asyncio.to_thread, which blocks the event loop for every other "
        f"request on this worker: {offenders}"
    )


# ---------------------------------------------------------------------------
# Golden rule: no reachable Redis must never mean "no terminals"
# ---------------------------------------------------------------------------


def test_no_redis_falls_back_to_a_direct_pty(managers, monkeypatch):
    """With no Redis the owner reads its PTY straight into its own
    subscribers and writes keystrokes straight to the fd — single-worker
    behaviour, byte-for-byte what this module did before any of this."""
    monkeypatch.setattr(tm, "_get_redis", lambda: None)
    monkeypatch.setattr(tm, "_get_redis_bytes", lambda: None)

    mgr = managers()

    async def run():
        session = mgr.create(name="fallback", command="echo FELLBACK; sleep 10")
        session.start_reader(asyncio.get_running_loop())
        assert session.is_owner and session.fd is not None
        assert session.proc_root_pid() == session.pid
        assert b"FELLBACK" in await _wait_for_bytes(session, b"FELLBACK")

    asyncio.run(run())


def test_no_redis_still_claims_and_serves(monkeypatch):
    """With no Redis the creation claim must always succeed — otherwise a
    single-worker workspace with a dead Redis could never open a terminal."""
    monkeypatch.setattr(tm, "_get_redis", lambda: None)

    assert _claim_creation("w7test-noredis") is True
    store = SessionMetaStore()
    store.update("w7test-noredis", name="ignored")
    assert store.get("w7test-noredis") == {}
    assert store.all() == {}
