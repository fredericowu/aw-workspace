"""W5 — terminal sessions are no longer owned by one uvicorn worker.

A PTY master fd cannot cross a process boundary, so before W5 the worker that
forked a terminal was the only one that could ever serve it. The fix restores
the GNU ``screen`` backing (ported from aw-backend's F5,
``repos/aw-backend/src/api/terminal_manager.py``): the screen server is
external to every worker, so any worker can attach.

Two independent ``TerminalManager`` objects stand in for two worker processes
throughout — that is faithful, because the thing being tested is precisely
that nothing needed to serve a session lives in the creating object. The
three guarantees, one section each:

1. Attach is always ``screen -x`` (shared), never ``-r`` (which would steal
   the session from whoever is already attached).
2. Session metadata lives in a Redis hash, visible to every worker at once.
3. Concurrent creation of one screen name is deduped with ``SET NX EX 30``.

Plus the end-to-end claim the card actually cares about: create a session on
"worker A", attach from "worker B", and prove bytes flow BOTH ways.

Requires a real Redis and a real ``screen``. Both are provided in CI (see
.github/workflows/test.yml — the ephemeral Redis container, and ``screen`` in
the pytest container's apt line). The screen-path tests FAIL rather than skip
when ``screen`` is missing: a skip here is how this whole card would quietly
stop being tested, which is the failure mode this repo has already been bitten
by twice.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.api import terminal_manager as tm
from src.api.terminal_manager import (
    SessionMetaStore,
    TerminalManager,
    _claim_screen_creation,
    _get_redis,
    _screen_exists,
    _screen_name_for,
    screen_backing_enabled,
)

pytestmark = [pytest.mark.integration]


def _redis_available() -> bool:
    tm._reset_redis_client()
    return _get_redis() is not None


pytestmark.append(
    pytest.mark.skipif(not _redis_available(), reason="Redis not reachable")
)


@pytest.fixture(autouse=True)
def _clean_redis():
    """Wipe this file's own keys either side of every test.

    Scoped to the ``w5test-`` prefix, never a flush: this Redis is shared with
    the running workspace in local dev, and a FLUSHDB here would take out live
    leases and broadcast state.
    """
    client = _get_redis()

    def _wipe():
        if client is None:
            return
        for pattern in ("*term:meta:w5test-*", "*term:creating:*w5test-*"):
            for key in client.scan_iter(match=pattern):
                client.delete(key)

    _wipe()
    yield
    _wipe()


@pytest.fixture
def screens():
    """Track screen names created by a test and destroy them afterwards.

    A leaked screen is a leaked process tree that outlives the test run —
    exactly the operational cost the card names as the price of this design,
    so the suite pays it explicitly rather than littering the CI container.
    """
    names: list[str] = []
    yield names
    for name in names:
        tm._destroy_screen(name)


def _require_screen():
    """Fail loudly unless screen is present AND actually works here.

    Presence is not enough: a host can ship the binary and still be unable to
    start a server (no writable socket dir, no /dev/pts, a screenrc its
    version rejects). In that state terminal_manager degrades to a
    worker-owned PTY and every test below would pass while asserting nothing
    about W5 — a green run covering none of the card. So probe for real.
    """
    assert screen_backing_enabled(), (
        "no `screen` binary — terminal_manager falls back to a worker-owned "
        "PTY, so this test would pass while asserting nothing about W5. "
        "Install screen (CI does, in test.yml's apt line)."
    )
    probe = f"aw-terminal-w5test-probe-{uuid.uuid4().hex[:8]}"
    try:
        tm._create_screen(probe, "sleep 5")
        assert _screen_exists(probe), (
            "`screen` is installed but cannot start a server here — see the "
            "warning terminal_manager logs with screen's own stderr."
        )
    finally:
        tm._destroy_screen(probe)


def _read_until(session, needle: bytes, timeout: float = 10.0) -> bytes:
    """Drain a session's PTY until ``needle`` shows up (or time runs out).

    Reads the fd directly rather than through ``subscribe()``: the fan-out
    reader is driven by ``loop.add_reader``, and there is no running event
    loop in a sync test.
    """
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = os.read(session.fd, 65536)
        except (BlockingIOError, OSError):
            chunk = b""
        if chunk:
            buf.extend(chunk)
            if needle in buf:
                return bytes(buf)
        else:
            time.sleep(0.05)
    return bytes(buf)


# ---------------------------------------------------------------------------
# 1. Attach is `screen -x`, never `-r`
# ---------------------------------------------------------------------------


def test_attach_uses_dash_x_not_dash_r(monkeypatch):
    """No worker owns a screen, so attach must be shared (`-x`).

    `-r` would detach whoever is already attached — turning a second worker
    (or a second browser tab) into a session hijack instead of a second view.
    """
    _require_screen()
    mgr = TerminalManager()
    captured = {}

    def fake_fork_exec(cmd_parts, rows=24, cols=80):
        captured["cmd_parts"] = cmd_parts
        return (os.open(os.devnull, os.O_RDWR), 12345)

    monkeypatch.setattr(mgr, "_fork_exec", fake_fork_exec)
    mgr._attach_screen("w5test-id", "w5-name", "aw-terminal-w5test-name")

    assert "-x" in captured["cmd_parts"]
    assert "-r" not in captured["cmd_parts"]


def test_create_goes_through_attach_screen(monkeypatch):
    """The `-x` guarantee is worthless if `create()` reaches the PTY by some
    other path, so pin that it does not."""
    _require_screen()
    mgr = TerminalManager()
    calls = []
    monkeypatch.setattr(tm, "_create_screen", lambda name, inner: calls.append(("create", name)))
    monkeypatch.setattr(
        mgr, "_attach_screen",
        lambda *a, **k: calls.append(("attach", a[2])) or _Stub(a[2]))

    mgr.create(name="w5test", session_id="w5test-create")

    assert [c[0] for c in calls] == ["create", "attach"]
    assert calls[0][1] == calls[1][1] == _screen_name_for("w5test-create", "terminal")


class _Stub:
    """Minimal stand-in for a TerminalSession (create() reads these fields)."""

    def __init__(self, screen_name):
        self.screen_name = screen_name
        self.insecure = False
        self.agent_session_id = None
        self.pid = 0


# ---------------------------------------------------------------------------
# 2. Session metadata lives in Redis, shared across workers
# ---------------------------------------------------------------------------


def test_session_meta_visible_across_two_worker_instances():
    """Two independent stores (= two worker processes) see the same data."""
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    worker_a, worker_b = SessionMetaStore(), SessionMetaStore()

    worker_a.update(session_id, name="from-worker-a", type="terminal", insecure=False)

    seen = worker_b.get(session_id)
    assert seen["name"] == "from-worker-a"
    assert seen["type"] == "terminal"
    assert seen["insecure"] is False

    worker_b.update(session_id, insecure=True)
    assert worker_a.get(session_id)["insecure"] is True


def test_meta_delete_is_visible_to_the_other_worker():
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    a, b = SessionMetaStore(), SessionMetaStore()
    a.update(session_id, name="doomed")
    assert b.get(session_id)["name"] == "doomed"
    a.delete(session_id)
    assert b.get(session_id) == {}


def test_rename_on_one_worker_is_readable_on_another(monkeypatch):
    """`set_name` must write through to Redis — a rename that only touched
    the local dict would show the old name on every other worker."""
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    mgr1, mgr2 = TerminalManager(), TerminalManager()

    mgr1._meta.update(session_id, name="before", type="terminal")
    mgr1.set_name(session_id, "after")

    assert mgr2._meta.get(session_id)["name"] == "after"


def test_meta_keys_are_scoped_to_this_workspace(monkeypatch):
    """One shared Redis can host several workspaces; an unscoped key would
    hand one workspace's shell to another."""
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("AW_WORKSPACE", "ws-one")
    SessionMetaStore().update(session_id, name="belongs-to-one")

    monkeypatch.setenv("AW_WORKSPACE", "ws-two")
    assert SessionMetaStore().get(session_id) == {}

    monkeypatch.setenv("AW_WORKSPACE", "ws-one")
    assert SessionMetaStore().get(session_id)["name"] == "belongs-to-one"


# ---------------------------------------------------------------------------
# 3. Concurrent creation of one screen name is deduped
# ---------------------------------------------------------------------------


def test_concurrent_claim_lets_exactly_one_worker_through():
    """Eight 'workers' racing on one name — SETNX must elect exactly one, so
    only one of them ever runs `screen -dmS`."""
    screen_name = f"aw-terminal-w5test-{uuid.uuid4().hex[:8]}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _claim_screen_creation(screen_name), range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_claim_is_held_against_a_later_racer():
    screen_name = f"aw-terminal-w5test-{uuid.uuid4().hex[:8]}"
    assert _claim_screen_creation(screen_name) is True
    assert _claim_screen_creation(screen_name) is False


def test_concurrent_create_of_one_session_id_yields_one_screen(screens):
    """The card's VERIFY, as a test: two workers handed the same create must
    produce ONE screen, not two servers fighting over one name."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screen_name = _screen_name_for(session_id, "terminal")
    screens.append(screen_name)

    def do_create(mgr):
        return mgr.create(name="race", session_id=session_id,
                          command="sleep 30", session_type="terminal")

    managers = [TerminalManager(), TerminalManager()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(do_create, managers))

    assert len(tm._screen_server_pids(screen_name)) == 1

    for m in managers:
        for s in list(m.sessions.values()):
            s.kill()


# ---------------------------------------------------------------------------
# The whole point: bytes flow both ways from a worker that did not create it
# ---------------------------------------------------------------------------


def test_session_created_on_worker_a_is_attachable_from_worker_b(screens):
    """Create on A, attach from B, and prove BOTH directions.

    This is the W5 bug reduced to one test: before the fix, `worker_b.get()`
    returned None for a session it had not created, and `/ws/terminal/<id>`
    answered 4004 on N-1 of N workers.
    """
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screens.append(_screen_name_for(session_id, "terminal"))

    worker_a, worker_b = TerminalManager(), TerminalManager()

    session_a = worker_a.create(name="cross", session_id=session_id)
    assert session_a.screen_name, "session should be screen-backed"

    # B has never seen this session and holds no fd for it.
    assert session_id not in worker_b.sessions

    session_b = worker_b.get(session_id)
    assert session_b is not None, "worker B could not attach — W5 regression"
    assert session_b.screen_name == session_a.screen_name
    assert session_b.fd != session_a.fd, "each worker gets its own PTY"

    # Drain the login-shell banner both sides emit on attach.
    _read_until(session_a, b"$", timeout=5)
    _read_until(session_b, b"$", timeout=5)

    # B -> shell: keystrokes written on the non-creating worker must run.
    marker_b = f"MARKER_FROM_B_{uuid.uuid4().hex[:6]}"
    session_b.write(f"echo {marker_b}\r".encode())
    assert marker_b.encode() in _read_until(session_b, marker_b.encode()), \
        "input written on worker B never reached the shell"

    # shell -> A: and the SAME output must reach the other worker's PTY,
    # because both are attached to one screen.
    assert marker_b.encode() in _read_until(session_a, marker_b.encode()), \
        "output of B's command never reached worker A"

    # A -> shell -> B, to prove it is not one-directional.
    marker_a = f"MARKER_FROM_A_{uuid.uuid4().hex[:6]}"
    session_a.write(f"echo {marker_a}\r".encode())
    assert marker_a.encode() in _read_until(session_b, marker_a.encode()), \
        "output of A's command never reached worker B"

    session_a.kill()
    session_b.kill()


def test_session_survives_the_worker_that_created_it(screens):
    """A screen outlives its attach, so a worker restart does not kill a
    user's shell — the restart-persistence win that falls out of W5."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screen_name = _screen_name_for(session_id, "terminal")
    screens.append(screen_name)

    worker_a = TerminalManager()
    worker_a.create(name="survivor", session_id=session_id)

    # "Worker A goes away" — cleanup() is what lifespan shutdown calls.
    worker_a.cleanup()
    assert _screen_exists(screen_name), \
        "cleanup() destroyed the screen — a deploy would kill every live shell"

    worker_b = TerminalManager()
    assert worker_b.get(session_id) is not None
    worker_b.remove(session_id)
    assert not _screen_exists(screen_name)


def test_list_sessions_shows_sessions_created_on_another_worker(screens):
    """The SPA's terminal list must be the same whichever worker serves it."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screens.append(_screen_name_for(session_id, "terminal"))

    worker_a, worker_b = TerminalManager(), TerminalManager()
    worker_a.create(name="listed-everywhere", session_id=session_id)

    ids = {s["id"]: s for s in worker_b.list_sessions()}
    assert session_id in ids
    assert ids[session_id]["name"] == "listed-everywhere"

    worker_b.remove(session_id)
    assert session_id not in {s["id"] for s in worker_a.list_sessions()}


def test_remove_from_a_non_owning_worker_ends_the_session(screens):
    """A DELETE lands on any worker; it must still end the session rather
    than leaving an unreachable shell running forever."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screen_name = _screen_name_for(session_id, "terminal")
    screens.append(screen_name)

    worker_a, worker_b = TerminalManager(), TerminalManager()
    worker_a.create(name="doomed", session_id=session_id, command="sleep 60")
    assert _screen_exists(screen_name)

    worker_b.remove(session_id)  # worker B holds no PTY for this session

    assert not _screen_exists(screen_name)
    assert worker_b._meta.get(session_id) == {}


def test_child_procs_reads_the_screen_server_not_the_attach(screens):
    """`session.pid` is only the `screen -x` client; the shell hangs off the
    screen SERVER. Rooting the tree at the attach finds nothing, which would
    empty the SPA's process badge and make its kill action reject every pid."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screens.append(_screen_name_for(session_id, "terminal"))

    mgr = TerminalManager()
    session = mgr.create(name="procs", session_id=session_id,
                         command="sleep 47; echo done")

    deadline = time.monotonic() + 10
    names: list[str] = []
    while time.monotonic() < deadline:
        names = [p["name"] for p in session.child_procs()]
        if "sleep" in names:
            break
        time.sleep(0.2)

    assert "sleep" in names, f"expected the command in the tree, saw {names}"
    assert "screen" not in [n.lower() for n in names], \
        "screen plumbing must not be offered as a killable process"

    session.kill()


def test_detaching_does_not_leak_a_defunct_screen_attach(screens):
    """Each attach is a forked child of this process, so each detach must be
    reaped — otherwise every terminal a user opens and closes leaves a
    ``<defunct>`` for the life of the container, which is the exact leak
    test_closed_terminal_does_not_leak_a_defunct_shell was written for on the
    direct-PTY backing (25 of them found in a workspace up 2.7 days)."""
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screens.append(_screen_name_for(session_id, "terminal"))

    mgr = TerminalManager()
    session = mgr.create(name="leak", session_id=session_id, command="sleep 60")
    attach_pid = session.pid
    assert attach_pid in tm._OWN_CHILD_PIDS, "attach PTY was never registered"

    session.kill()  # detach only — the screen stays up

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        procs = tm._ps_snapshot()
        info = procs.get(attach_pid)
        if info is None:
            break
        time.sleep(0.2)
    assert tm._ps_snapshot().get(attach_pid) is None, \
        f"screen attach {attach_pid} was never reaped"
    assert attach_pid not in tm._OWN_CHILD_PIDS

    mgr.remove(session_id)


def test_a_dead_screen_socket_is_not_reported_as_a_live_session(screens):
    """A container restart kills every screen server and leaves its socket,
    which `screen -ls` still lists in the same shape as a live one. Counting
    those as live would keep every pre-restart terminal in the SPA's list,
    attachable to nothing — a terminal that opens blank and never responds.
    Found on the live workspace, which had four of them after a restart.
    """
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screen_name = _screen_name_for(session_id, "terminal")
    screens.append(screen_name)

    worker_a, worker_b = TerminalManager(), TerminalManager()
    worker_a.create(name="soon-dead", session_id=session_id, command="sleep 120")
    pids = tm._screen_server_pids(screen_name)
    assert pids, "screen should be live to begin with"

    # SIGKILL the server: no chance to clean up, so the socket is left behind
    # exactly as a container restart leaves it.
    os.kill(pids[0], 9)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _screen_exists(screen_name):
        time.sleep(0.2)

    assert not _screen_exists(screen_name), "dead socket counted as a live screen"
    # worker_b holds no local PTY, so this is purely the discover-and-attach
    # path — the one a restarted fleet actually takes for a pre-restart id.
    assert worker_b.get(session_id) is None, "attached to a screen with no server"
    assert session_id not in {s["id"] for s in worker_b.list_sessions()}
    # And the worker that DID create it must stop listing it too, rather than
    # keeping a ghost terminal in the SPA off its local cache.
    assert session_id not in {s["id"] for s in worker_a.list_sessions()}


def test_restart_reuses_the_name_within_the_claim_ttl(screens):
    """A restart destroys and immediately re-creates one screen NAME. The
    creation claim has a 30s TTL, so unless destroying releases it the
    re-create takes the "someone else is making it" branch and attaches to a
    screen nobody is making — a terminal that opens blank and never responds.
    """
    _require_screen()
    session_id = f"w5test-{uuid.uuid4().hex[:8]}"
    screen_name = _screen_name_for(session_id, "terminal")
    screens.append(screen_name)

    mgr = TerminalManager()
    mgr.create(name="restarter", session_id=session_id, command="sleep 60")
    first_pids = tm._screen_server_pids(screen_name)
    assert first_pids

    restarted = mgr.restart(session_id, name="restarter")  # well inside the TTL

    assert restarted is not None
    second_pids = tm._screen_server_pids(screen_name)
    assert second_pids, "restart left no screen — the stale claim blocked it"
    assert second_pids != first_pids, "restart reused the old screen server"
    assert session_id in {s["id"] for s in mgr.list_sessions()}

    mgr.remove(session_id)


# ---------------------------------------------------------------------------
# Screen work must not run on the event-loop thread
# ---------------------------------------------------------------------------


def test_terminal_routes_never_call_the_manager_from_an_async_def():
    """Every blocking manager call must go through ``asyncio.to_thread``.

    A screen-backed create runs ``screen -dmS``, polls ``screen -ls`` until
    the server answers, then forks — seconds of blocking work. Called straight
    from an ``async def`` it freezes EVERY in-flight request on this worker,
    which is the 2026-09-02 event-loop freeze all over again. It really
    happened here: the first POST /api/terminals after this landed took over
    10s on the live workspace.

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
# Golden rule: nothing above may change single-worker behaviour
# ---------------------------------------------------------------------------


def test_no_screen_binary_falls_back_to_a_direct_pty(monkeypatch):
    """`/opt/aw-workspace` is a bind mount, so a core deploy lands new code on
    a running container that may predate the Dockerfile's `screen`. That must
    degrade to today's worker-owned PTY, not to a dead terminal."""
    monkeypatch.setattr(tm, "_SCREEN_BIN", None)
    monkeypatch.setattr(tm, "_find_screen", lambda: None)

    mgr = TerminalManager()
    session = mgr.create(name="fallback", command="echo FELLBACK; sleep 5")
    try:
        assert session.screen_name is None
        assert session.proc_root_pid() == session.pid
        assert b"FELLBACK" in _read_until(session, b"FELLBACK")
    finally:
        mgr.remove(session.id)


def test_no_redis_still_claims_and_serves(monkeypatch):
    """With no Redis the creation claim must always succeed — otherwise a
    single-worker workspace with a dead Redis could never open a terminal."""
    monkeypatch.setattr(tm, "_get_redis", lambda: None)

    assert _claim_screen_creation("aw-terminal-w5test-noredis") is True
    store = SessionMetaStore()
    store.update("w5test-noredis", name="ignored")
    assert store.get("w5test-noredis") == {}
    assert store.all() == {}
