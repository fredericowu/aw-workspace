"""The restart loop in ``ServiceSupervisor`` (design §B.2).

A clean core restart on 2026-10-11 killed ``proxy:proxy-server`` with every
other Tier-1 managed service, and nothing ever started it again — there was
no restart loop and no health check anywhere in the module, so an outage that
lasted ≥27 minutes was only ever going to end when a human noticed.

These tests pin the four things that make a restart loop a fix rather than a
second outage:

1. a service that dies comes back (the whole point);
2. a service that was **stopped on purpose** does NOT come back — tested
   through the exact interleaving that breaks it, with the dying process's
   log pipe still open so the pump's ``finally`` lands *after* ``stop()``
   returned;
3. a service that cannot start gives up and **stays** given up, instead of
   respawning forever;
4. a bind collision gives up immediately and names the orphan it suspects,
   because no amount of backoff wins against a port someone else holds.

The last group covers what the first version of this loop got wrong: a start
the cross-worker ownership gate DECLINED was dropped on the floor. The gate is
right to refuse while a peer worker's lease is live, but that lease is a 30s
TTL kept alive by a heartbeat, so a worker that died leaves a **ghost** — a
key that outlives its owner and then expires with nothing running anywhere.
Reproduced on card 3f65bf3b (2026-10-11): ``desired = running``, no process,
no retry, and ``doctor`` announcing it as "deliberately stopped". The three
tests pin the refusal, the eventual takeover, and the thing a retry path must
never break — that a deliberate stop mid-wait still wins.

Timings are shrunk via the module's own constants so the suite stays fast;
the arithmetic under test is the same.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid

import pytest

from src.apps import services as services_mod
from src.apps.service_lease import (ServiceLease, gave_up_markers,
                                    is_lease_held_anywhere)
from src.apps.services import (STATE_BACKING_OFF, STATE_GAVE_UP,
                               STATE_STOPPED, ServiceSupervisor)


@pytest.fixture()
def fast_lease_retry(monkeypatch):
    """5s × 12 attempts is the shipped schedule for a declined start; 100ms ×
    12 is the same shape, 50x faster. ``_LEASE_RETRY_ATTEMPTS`` is left alone
    except where a test is specifically about the budget running out — the
    count is part of the contract, not a timing detail."""
    monkeypatch.setattr(services_mod, "_LEASE_RETRY_S", 0.1)


@pytest.fixture()
def ghost_lease():
    """A lease nobody is renewing — the exact artefact a worker leaves behind
    when it dies, as opposed to one it released on its way out.

    Needs real Redis: ``ServiceLease.acquire`` deliberately degrades OPEN, so
    with Redis away the "ghost" is never recorded and the supervisor's own
    acquire succeeds too. That is correct single-worker behaviour and there is
    nothing to test, hence the skip rather than a fake client — the whole
    point of these three is the real expiry arithmetic.
    """
    leases: list[ServiceLease] = []

    def _make(app_id: str, service_id: str, *, ttl: float) -> ServiceLease:
        lease = ServiceLease(app_id, service_id, ttl=ttl)
        assert lease.acquire() is True
        leases.append(lease)
        if not is_lease_held_anywhere(app_id, service_id):
            pytest.skip("Redis is unreachable, so ServiceLease.acquire degraded "
                        "open and there is no lease to be a ghost of")
        return lease

    yield _make
    for lease in leases:
        lease.release()


@pytest.fixture()
def fast_backoff(monkeypatch):
    """1s/2s/4s/8s/16s is the shipped schedule; 20ms/40ms/... is the same
    schedule, 50x faster. ``_GIVE_UP_AFTER`` is deliberately NOT touched —
    five is part of the contract being tested."""
    monkeypatch.setattr(services_mod, "_BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(services_mod, "_BACKOFF_CAP_S", 0.1)


@pytest.fixture()
def supervisor():
    sup = ServiceSupervisor()
    yield sup
    for app_id, service_id in sup.registered():
        try:
            sup.stop(app_id, service_id)
        except Exception:
            pass
        sup.stop_all_for(app_id)


def _ids():
    return f"svc-{uuid.uuid4().hex[:8]}", "worker"


def _wait(predicate, timeout=20.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_a_killed_service_is_respawned(supervisor, fast_backoff, tmp_path):
    """``kill -9`` on a running service: the reader thread observes the death
    the instant stdout closes, and the service is back with a new pid. This
    is the behaviour the incident needed and did not have."""
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id, "sleep 60", str(tmp_path), autostart=False)

    first = supervisor.start(app_id, service_id)
    pid = first["pid"]
    assert pid

    os.kill(pid, signal.SIGKILL)

    assert _wait(lambda: (supervisor.status(app_id, service_id).get("pid") or pid) != pid), \
        "the service was never respawned after being killed"
    after = supervisor.status(app_id, service_id)
    assert after["running"] is True
    assert after["pid"] != pid
    assert after["state"] == "running"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_deliberate_stop_is_not_respawned(supervisor, fast_backoff, tmp_path):
    """The ``stop()``-then-respawn race, forced rather than hoped for.

    ``_pump``'s ``finally`` runs *after* ``stop()`` returns, so if
    ``desired = stopped`` were set after the SIGTERM instead of before it, the
    pump would read a stale ``running`` and respawn the service the operator
    just stopped — a deliberate stop would become impossible.

    ``setsid sleep 5`` inherits the captured stdout but lands in its OWN
    session, so ``stop()``'s killpg cannot reach it and the pipe stays open
    for ~5s after the service process itself is gone. That puts the pump's
    ``finally`` deterministically after ``stop()`` has returned, which is
    the window the bug lives in.
    """
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id,
                        "sh -c 'setsid sleep 5 & echo ready; sleep 60'",
                        str(tmp_path), autostart=False)

    supervisor.start(app_id, service_id)
    svc = supervisor._require(app_id, service_id)
    pump = svc._reader_thread
    assert _wait(lambda: "ready" in supervisor.logs(app_id, service_id)), \
        "the service never reported its detached child"

    supervisor.stop(app_id, service_id)
    assert pump.is_alive() is True, (
        "the log pipe closed before stop() returned — this test cannot reach "
        "the interleaving it exists for")
    assert svc.desired == "stopped"

    pump.join(timeout=30)
    assert pump.is_alive() is False
    time.sleep(0.5)  # 25x the shrunk backoff — a respawn would have landed

    status = supervisor.status(app_id, service_id)
    assert status["running"] is False, "a deliberate stop was undone by the restart loop"
    assert status["state"] == "stopped"
    assert is_lease_held_anywhere(app_id, service_id) is False


def test_a_straggler_pump_does_not_spawn_a_third_copy(supervisor, fast_backoff, tmp_path):
    """A restart is ``stop()`` + ``start()`` back to back, and the OLD
    process's pump can reach its ``finally`` long after the replacement is
    live. Without the ``svc.proc is proc`` identity check it would see
    ``desired == running``, conclude "my process died", and spawn a third
    copy next to the one already running."""
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id,
                        "sh -c 'setsid sleep 5 & echo ready; sleep 60'",
                        str(tmp_path), autostart=False)

    first = supervisor.start(app_id, service_id)
    old_pump = supervisor._require(app_id, service_id)._reader_thread
    assert _wait(lambda: "ready" in supervisor.logs(app_id, service_id))

    second = supervisor.restart(app_id, service_id)
    assert second["pid"] != first["pid"]
    assert old_pump.is_alive() is True, (
        "the old pipe closed before the restart completed — this test cannot "
        "reach the window the identity check exists for")

    old_pump.join(timeout=30)
    time.sleep(0.5)

    status = supervisor.status(app_id, service_id)
    assert status["pid"] == second["pid"], (
        "the straggler pump respawned the service on top of its own replacement")
    assert status["running"] is True


def test_five_startup_crashes_give_up_and_it_sticks(supervisor, fast_backoff, tmp_path):
    """An unconditional respawn of a service that crashes on startup is a
    tighter loop than the bug it is fixing: port flapping, CPU burn, log
    spam. After five consecutive startup-window crashes the supervisor stops
    — in memory, in ``status()``, and in a durable Redis marker every other
    worker, ``doctor`` and ``/api/health`` can read."""
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id, "sh -c 'exit 7'",
                        str(tmp_path), autostart=False)

    supervisor.start(app_id, service_id)
    svc = supervisor._require(app_id, service_id)

    assert _wait(lambda: svc.state == STATE_GAVE_UP), \
        f"never gave up (state={svc.state}, crashes={svc.consecutive_fast_exits})"
    assert svc.consecutive_fast_exits == services_mod._GIVE_UP_AFTER == 5

    # And it STAYS given up: no sixth spawn, ever.
    spawned_at = svc.last_spawn_monotonic
    time.sleep(1.0)  # 50x the shrunk backoff
    assert svc.last_spawn_monotonic == spawned_at, "the supervisor respawned after giving up"
    assert svc.state == STATE_GAVE_UP

    status = supervisor.status(app_id, service_id)
    assert status["running"] is False
    assert status["state"] == STATE_GAVE_UP
    assert status["give_up_reason"] == "crash-loop"

    markers, redis_ok = gave_up_markers([(app_id, service_id)])
    if redis_ok:
        assert markers[(app_id, service_id)]["reason"] == "crash-loop"
        assert markers[(app_id, service_id)]["exit_code"] == 7

    # A manual start is one of the three things that re-arms it (the others
    # being an app update/reinstall and a config save).
    svc.start = "sleep 60"
    supervisor.start(app_id, service_id)
    assert supervisor.status(app_id, service_id)["running"] is True
    assert svc.state == "running"
    assert svc.consecutive_fast_exits == 0
    markers, redis_ok = gave_up_markers([(app_id, service_id)])
    if redis_ok:
        assert (app_id, service_id) not in markers


def test_a_config_save_rearms_a_gave_up_service_without_starting_it(
        supervisor, fast_backoff, tmp_path):
    """``clear_gave_up_for`` is the config-save half of the re-arm rule. It
    must not START anything — a save must never resurrect a component the
    user deliberately stopped (the same rule ``_apply_runtime_config``
    follows for containers)."""
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id, "sh -c 'exit 7'",
                        str(tmp_path), autostart=False)
    supervisor.start(app_id, service_id)
    svc = supervisor._require(app_id, service_id)
    assert _wait(lambda: svc.state == STATE_GAVE_UP)

    supervisor.clear_gave_up_for(app_id)

    assert svc.state == "stopped"
    assert svc.give_up_reason is None
    assert supervisor.status(app_id, service_id)["running"] is False
    markers, redis_ok = gave_up_markers([(app_id, service_id)])
    if redis_ok:
        assert (app_id, service_id) not in markers


def test_a_bind_collision_gives_up_immediately_as_an_orphan(
        supervisor, fast_backoff, tmp_path):
    """``start_new_session=True`` means a managed service outlives the worker
    that spawned it — process alive, port still bound, lease heartbeat dead.
    A respawn then loses to ``OSError: Address already in use`` every time,
    so backing off 1s, 2s, 4s... just burns five attempts on a race that
    cannot be won. Give up on the FIRST one and name what it looks like."""
    app_id, service_id = _ids()
    supervisor.register(
        app_id, service_id,
        "sh -c 'echo \"OSError: [Errno 98] Address already in use\" >&2; exit 1'",
        str(tmp_path), autostart=False)

    supervisor.start(app_id, service_id)
    svc = supervisor._require(app_id, service_id)

    assert _wait(lambda: svc.state == STATE_GAVE_UP)
    assert svc.consecutive_fast_exits == 1, "it entered the backoff loop instead of giving up"
    assert svc.give_up_reason == "orphan-suspected"
    assert supervisor.status(app_id, service_id)["give_up_reason"] == "orphan-suspected"


def _spawn_real_orphan(command: list[str], cwd) -> int:
    """A process that is genuinely orphaned, not merely detached.

    ``Popen(..., start_new_session=True)`` is still a live child of this
    process, which is exactly what the reap refuses to touch. Shelling out
    through an ``sh`` that exits immediately reproduces the real thing: the
    service survives, its parent is gone, and it has been reparented to pid 1
    — a Tier-1 service whose uvicorn worker died.
    """
    subprocess.run(["sh", "-c", f"setsid {' '.join(command)} &"], cwd=str(cwd),
                   check=True, timeout=15)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for pid in ServiceSupervisor._candidate_pids():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if [p.decode() for p in f.read().split(b"\0") if p] != command:
                        continue
                if os.path.realpath(f"/proc/{pid}/cwd") != os.path.realpath(str(cwd)):
                    continue
                if ServiceSupervisor._ppid(pid) != 1:
                    continue
                return pid
            except (OSError, UnicodeDecodeError):
                continue
        time.sleep(0.05)
    raise AssertionError("could not produce an orphaned process to reap")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def test_start_reaps_a_verified_orphan_before_spawning(supervisor, tmp_path):
    """The §D.2 contract: ``start()`` reaps an orphan of THIS service first.

    The orphan stands in for a service that survived its worker — same argv,
    same package dir, its own session leader (what ``start_new_session=True``
    produces), reparented to pid 1, and no live lease. Without the reap the
    spawn below is guaranteed to lose to whatever port the orphan still
    holds.
    """
    app_id, service_id = _ids()
    orphan_pid = _spawn_real_orphan(["sleep", "47"], tmp_path)
    try:
        supervisor.register(app_id, service_id, "sleep 47", str(tmp_path),
                            autostart=False)
        started = supervisor.start(app_id, service_id)

        assert _wait(lambda: not _alive(orphan_pid), timeout=15), \
            "the orphan was not reaped"
        assert started["running"] is True
        assert started["pid"] != orphan_pid
    finally:
        if _alive(orphan_pid):
            os.kill(orphan_pid, signal.SIGKILL)


def test_an_orphan_somewhere_else_is_never_reaped(supervisor, tmp_path):
    """The reap verifies argv AND cwd AND session leadership AND reparenting
    before it signals anything. A process that merely shares the command line
    but runs somewhere else is someone else's, and killing it would be far
    worse than the bind collision the reap exists to avoid."""
    app_id, service_id = _ids()
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    bystander_pid = _spawn_real_orphan(["sleep", "48"], other_cwd)
    try:
        supervisor.register(app_id, service_id, "sleep 48", str(tmp_path),
                            autostart=False)
        supervisor.start(app_id, service_id)
        time.sleep(0.3)
        assert _alive(bystander_pid), "the reap killed a process in another cwd"
    finally:
        os.kill(bystander_pid, signal.SIGKILL)


def test_a_live_peer_workers_service_is_never_reaped(supervisor, tmp_path):
    """``ServiceLease.acquire`` degrades OPEN, so with Redis away two workers
    can both believe they own a service. The reparenting check is what keeps
    that from turning a visibility problem into this worker killing a live
    process another one is running: a peer's child has a living parent."""
    app_id, service_id = _ids()
    peers_child = subprocess.Popen(["sleep", "49"], cwd=str(tmp_path),
                                   start_new_session=True)
    try:
        supervisor.register(app_id, service_id, "sleep 49", str(tmp_path),
                            autostart=False)
        supervisor.start(app_id, service_id)
        time.sleep(0.3)
        assert peers_child.poll() is None, \
            "the reap killed a process whose parent is still alive"
    finally:
        peers_child.kill()
        peers_child.wait(timeout=10)


def test_backoff_grows_and_is_reported_as_backing_off(supervisor, monkeypatch, tmp_path):
    """The state a human needs between "it died" and "it is back": the
    restart loop is already on it, so the ``doctor`` line that names it will
    clear itself. Backoff is deliberately slowed here so the window is
    observable at all."""
    monkeypatch.setattr(services_mod, "_BACKOFF_BASE_S", 2.0)
    app_id, service_id = _ids()
    supervisor.register(app_id, service_id, "sh -c 'exit 3'", str(tmp_path),
                        autostart=False)

    supervisor.start(app_id, service_id)
    svc = supervisor._require(app_id, service_id)
    assert _wait(lambda: svc.state == STATE_BACKING_OFF, timeout=10)
    assert supervisor.status(app_id, service_id)["state"] == STATE_BACKING_OFF
    assert supervisor.status(app_id, service_id)["running"] is False

    # A stop during backoff disarms it rather than waiting it out.
    supervisor.stop(app_id, service_id)
    time.sleep(3.0)
    assert supervisor.status(app_id, service_id)["running"] is False
    assert svc.state == "stopped"


# ---------------------------------------------------------------------------
# The declined start (card 3f65bf3b) — see the module docstring
# ---------------------------------------------------------------------------


def test_a_live_peer_lease_still_refuses_the_spawn(
        supervisor, ghost_lease, fast_lease_retry, tmp_path):
    """The anti-collision gate is not what gets relaxed to fix the ghost.

    A lease held for longer than the whole retry window stands in for a peer
    worker that really is running the service. Every retry has to keep
    refusing: spawning here would put a second copy on the first one's port,
    which is the bug the lease exists for.
    """
    app_id, service_id = _ids()
    ghost_lease(app_id, service_id, ttl=30.0)

    supervisor.register(app_id, service_id, "sleep 60", str(tmp_path), autostart=True)
    svc = supervisor._require(app_id, service_id)

    assert _wait(lambda: svc.lease_waits >= 3, timeout=10), \
        f"the declined start was not retried at all (lease_waits={svc.lease_waits})"
    assert svc.proc is None, "it spawned a duplicate next to the lease holder"
    assert svc.consecutive_fast_exits == 0, \
        "a declined start spent the CRASH budget, which would eventually " \
        "hand a healthy service a gave_up for the sin of having a peer"
    assert svc.state != STATE_GAVE_UP
    # Reported honestly meanwhile: somebody's lease is live, so the service
    # IS running fleet-wide, just not here.
    status = supervisor.status(app_id, service_id)
    assert status["running"] is True
    assert status["remote_worker"] is True
    assert status["pid"] is None


def test_a_ghost_lease_defers_the_start_instead_of_losing_it(
        supervisor, ghost_lease, fast_lease_retry, tmp_path):
    """The reproduction from the card, as a regression test.

    A dead worker's lease is still in Redis with its TTL counting down from
    the last heartbeat it managed to send. ``register(autostart=True)`` is
    declined — correctly — and then the lease expires with no owner. Nothing
    external ever calls ``start()`` again in this test: the service coming up
    is the supervisor re-asking on its own, which is the whole fix.
    """
    app_id, service_id = _ids()
    ghost_lease(app_id, service_id, ttl=0.5)

    supervisor.register(app_id, service_id, "sleep 60", str(tmp_path), autostart=True)
    svc = supervisor._require(app_id, service_id)

    assert svc.desired == "running", "nothing stopped it"
    assert svc.lease_waits >= 1, \
        "the declined start was forgotten rather than deferred — this is the bug"

    assert _wait(lambda: svc.proc is not None and svc.proc.poll() is None,
                 timeout=15), (
        "the service never came up after the ghost lease expired "
        f"(state={svc.state}, lease_waits={svc.lease_waits}) — permanently "
        "stranded with desired=running, exactly as reported")
    status = supervisor.status(app_id, service_id)
    assert status["running"] is True
    assert status["pid"]
    assert status["state"] == "running"
    assert status["supervised_here"] is True


def test_a_stop_during_a_lease_wait_is_not_undone_when_the_lease_frees(
        supervisor, ghost_lease, fast_lease_retry, tmp_path):
    """The rule the retry path must not break.

    A pending retry is as dangerous as a pending crash backoff: if the
    operator stops the service while it waits, the lease freeing a moment
    later must not resurrect it. ``stop()`` sets ``desired = stopped`` and
    cancels the armed timer in the same locked instant — the same discipline
    the crash loop already follows.
    """
    app_id, service_id = _ids()
    ghost_lease(app_id, service_id, ttl=0.5)

    supervisor.register(app_id, service_id, "sleep 60", str(tmp_path), autostart=True)
    svc = supervisor._require(app_id, service_id)
    assert svc.lease_waits >= 1

    supervisor.stop(app_id, service_id)
    assert svc.desired == "stopped"

    # Well past the ghost's expiry and ~20 shrunk retry intervals.
    time.sleep(2.0)
    assert is_lease_held_anywhere(app_id, service_id) is False, \
        "the ghost never expired — this test cannot reach what it exists for"
    assert svc.proc is None, "a deliberate stop was undone by the lease retry"
    status = supervisor.status(app_id, service_id)
    assert status["running"] is False
    assert status["state"] == STATE_STOPPED
    assert status["desired"] == "stopped"


def test_a_peer_that_never_lets_go_is_eventually_taken_at_its_word(
        supervisor, ghost_lease, monkeypatch, tmp_path):
    """The retry is bounded, and what it leaves behind is honest.

    A lease renewed for the entire window belongs to a worker that is alive
    and running the service, so polling it forever buys nothing — and a
    ``desired = running`` carried indefinitely on a worker that will never act
    on it is how a later deliberate stop elsewhere gets undone. Standing down
    must not look like a give-up (nothing crashed) nor like a deliberate stop
    (nobody stopped it): ``state = stopped`` with ``desired = running`` is the
    pair, and it is the pair ``doctor`` reports as broken.
    """
    monkeypatch.setattr(services_mod, "_LEASE_RETRY_S", 0.05)
    monkeypatch.setattr(services_mod, "_LEASE_RETRY_ATTEMPTS", 3)
    app_id, service_id = _ids()
    ghost_lease(app_id, service_id, ttl=30.0)

    supervisor.register(app_id, service_id, "sleep 60", str(tmp_path), autostart=True)
    svc = supervisor._require(app_id, service_id)

    assert _wait(lambda: svc.state == STATE_STOPPED and svc.lease_waits == 3,
                 timeout=10), \
        f"never stood down (state={svc.state}, lease_waits={svc.lease_waits})"
    assert svc.proc is None
    assert svc.desired == "running"
    assert svc.state != STATE_GAVE_UP
    assert svc.give_up_reason is None
    time.sleep(0.5)  # 10x the shrunk interval — a 4th attempt would have landed
    assert svc.lease_waits == 3, "the budget was not respected"
