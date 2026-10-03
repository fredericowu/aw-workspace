"""Lock D verification: THREE real ``WatchdogSupervisor`` instances, each
wired to its own ``FlockLease`` exactly like ``src/api/app.py``'s production
lifespan (register(), no manual pause, then ``lease.start()``; ``on_acquire``
-> ``resume()``, ``on_release`` -> ``pause()``), racing as separate OS
PROCESSES over one lock file.

Replaces ``test_watchdog_lease_multiworker.py``, which proved the same three
properties over ``RedisLease("core")`` — deleted along with that class on
2026-10-03, when leadership became flock-only (Frederico: "for defining the
leader, it should be flock only, it's not a fallback, it's the only logic").
Two differences that matter for what this file can assert:

* **It no longer skips.** The Redis version skipped whenever no Redis was
  reachable, which here is *every* environment (``src/libs/redis_coord.py``'s
  own docstring) and the CI runner — so the one test covering the leaderless
  -watchdog incident was, in practice, never executed. A flock needs nothing
  but a filesystem.
* **Failover is bounded by a poll, not a TTL.** SIGKILL drops the holder's
  flock instantly (kernel), so the survivor takes over on its next
  ``LEASE_POLL_S`` tick rather than after a lease expiry.

Three properties, the same three the Redis version proved:

* (a) at most one process's watchdog task ever actually ticks — checked via
  each process's OWN tick counter, not a shared one, because a losing
  process whose supervisor was never paused ticks at the same rate as the
  real leader and would pass a shared-counter check (that bug was live: W1
  leader-election never paused the losers).
* (b) the loser processes are genuinely paused, and
* (c) SIGKILLing the leader lets a survivor resume ticking.

Counters are files rather than Redis keys for the same reason the lock is:
no external service, nothing to skip on.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The lease's poll cadence for this test. Much tighter than production's 5s
#: so the whole file runs in seconds; the mechanism under test is identical.
POLL_S = 0.2

#: The watchdog task's own cadence. Faster than the lease poll, so "is it
#: still ticking" is answerable inside one poll window.
TICK_S = 0.05


def _ticks(counter_dir: str, pid: int) -> int:
    """How many times THIS pid's watchdog task has run."""
    try:
        with open(os.path.join(counter_dir, str(pid)), encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Worker process body — module-level so it's picklable/forkable.
# ---------------------------------------------------------------------------

def _watchdog_worker(lock_dir: str, counter_dir: str, queue, stop_event) -> None:
    """One process = one production-shaped worker, built EXACTLY like the
    lifespan's: ``register()`` first, then ``lease.start()``, with no manual
    ``pause()`` in between. ``WatchdogSupervisor`` defaults to leader=True
    (ungated), so a losing process has to reach its own pause via
    ``on_release`` — which is the regression this guards."""
    from src.apps.fs_lock import FlockLease
    from src.apps.watchdog import WatchdogSupervisor

    async def main():
        pid = os.getpid()
        wd = WatchdogSupervisor()
        own_counter = os.path.join(counter_dir, str(pid))

        async def tick():
            with open(own_counter, "a", encoding="utf-8") as f:
                f.write("t\n")
                f.flush()

        wd.register("core", "probe", tick, TICK_S)

        async def on_acquire():
            wd.resume()
            queue.put(("acquire", pid, time.time()))

        async def on_release():
            wd.pause()
            queue.put(("release", pid, time.time()))

        lease = FlockLease("w1-flock-test", lock_dir=lock_dir, poll_interval=POLL_S,
                           on_acquire=on_acquire, on_release=on_release)
        await lease.start()
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await lease.stop()

    asyncio.run(main())


def _drain_until(queue, kind: str, timeout: float, exclude_pid=None):
    """Pop events until one matches ``kind`` and isn't from an excluded pid,
    or ``timeout`` elapses. Returns the event tuple or None."""
    if exclude_pid is None:
        exclude_pid = set()
    elif not isinstance(exclude_pid, set):
        exclude_pid = {exclude_pid}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = queue.get(timeout=max(0.05, deadline - time.time()))
        except Exception:
            continue
        if event[0] == kind and event[1] not in exclude_pid:
            return event
    return None


class TestWatchdogFlockLeaseFailover:
    def test_exactly_one_ticks_and_failover_resumes_ticking(self, tmp_path):
        lock_dir = str(tmp_path / "locks")
        counter_dir = str(tmp_path / "counters")
        os.makedirs(counter_dir, exist_ok=True)
        n_procs = 3

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        stop_event = ctx.Event()
        procs = [ctx.Process(target=_watchdog_worker,
                             args=(lock_dir, counter_dir, queue, stop_event))
                 for _ in range(n_procs)]
        for p in procs:
            p.start()

        try:
            first = _drain_until(queue, kind="acquire", timeout=10.0)
            assert first is not None, "no process won the leader flock in time"
            _, leader_pid, _ = first

            # Let steady state settle: every loser's edge-triggered
            # on_release has to land and pause its own watchdog task. With
            # flock that happens on start()'s INLINE attempt, so this is
            # fast — unlike the Redis lease, which needed a renew interval.
            time.sleep(POLL_S * 3)

            # THE regression assertion. Before the W1 fix, a losing process's
            # WatchdogSupervisor was never told to pause and kept ticking at
            # the same rate as the real leader; a shared tick counter cannot
            # attribute ticks to a process, so only per-pid counters catch
            # it. Not "frozen at 0" either — register()'s
            # run_immediately=True fires one tick on every process before any
            # lease resolves, leader and losers alike. A loser's counter must
            # stop GROWING; the leader's must keep growing. Two samples with
            # a gap is what tells those apart.
            t1 = {p.pid: _ticks(counter_dir, p.pid) for p in procs}
            time.sleep(POLL_S * 3)
            t2 = {p.pid: _ticks(counter_dir, p.pid) for p in procs}
            for p in procs:
                if p.pid == leader_pid:
                    assert t2[p.pid] > t1[p.pid], (
                        f"leader pid={p.pid} stopped ticking "
                        f"({t1[p.pid]} -> {t2[p.pid]})")
                else:
                    assert t2[p.pid] == t1[p.pid], (
                        f"loser pid={p.pid} kept ticking after losing the race "
                        f"({t1[p.pid]} -> {t2[p.pid]}) — its WatchdogSupervisor "
                        "was never paused")

            # ...and the leader is a REAL leader, with the lock file naming it.
            from src.apps import fs_lock
            breadcrumb = fs_lock.holder(fs_lock.lock_path("w1-flock-test", lock_dir))
            assert f"pid={leader_pid}" in breadcrumb, (
                f"the lock file says {breadcrumb!r}, but pid={leader_pid} is the "
                "process that fired on_acquire — the breadcrumb is what live "
                "verification cross-checks against the resumed-watchdog log")

            ticks_before_kill = t2[leader_pid]

            leader_proc = next(p for p in procs if p.pid == leader_pid)
            leader_proc.kill()  # SIGKILL — a crash, no graceful release
            leader_proc.join(timeout=5)
            assert not leader_proc.is_alive(), "leader process survived the kill"

            kill_time = time.time()
            second = _drain_until(queue, kind="acquire", timeout=10.0,
                                  exclude_pid=leader_pid)
            assert second is not None, "no survivor took over the leader flock"
            _, new_leader_pid, _ = second
            acquire_took = time.time() - kill_time
            assert new_leader_pid != leader_pid

            # The survivor's WatchdogSupervisor must actually resume TICKING,
            # not merely report is_leader=True.
            deadline = time.time() + 5.0
            resumed = False
            while time.time() < deadline:
                before = _ticks(counter_dir, new_leader_pid)
                time.sleep(TICK_S * 3)
                if _ticks(counter_dir, new_leader_pid) > before:
                    resumed = True
                    break
            resume_took = time.time() - kill_time
            assert resumed, "survivor's watchdog task never resumed ticking"

            # No active-leader gauge is checked post-failover, on purpose: a
            # SIGKILLed process cannot run a release callback, so any gauge
            # would legitimately read 2 and prove nothing. The single-leader
            # proof here is `resumed` (only the survivor's counter moved)
            # plus `leader_proc.is_alive() is False` above — the old leader
            # is provably gone, not double-running.
            print(
                f"[Lock D verify] leader pid={leader_pid} ticked "
                f"{ticks_before_kill}x while {n_procs - 1} losers stayed paused; "
                f"SIGKILLed it; survivor pid={new_leader_pid} won the flock in "
                f"{acquire_took:.2f}s (poll={POLL_S}s) and resumed ticking in "
                f"{resume_took:.2f}s"
            )
        finally:
            stop_event.set()
            for p in procs:
                if p.is_alive():
                    p.join(timeout=5)
                if p.is_alive():
                    p.kill()


def _contender(lock_dir: str, result_dir: str, barrier) -> None:
    """Start a lease, record whether this process won, exit."""
    from src.apps.fs_lock import FlockLease

    async def main():
        lease = FlockLease("w1-one-winner", lock_dir=lock_dir, poll_interval=60.0)
        barrier.wait()
        await lease.start()
        with open(os.path.join(result_dir, str(os.getpid())), "w",
                  encoding="utf-8") as f:
            f.write("leader" if lease.is_leader else "standby")
        # Hold until the parent has read every result: releasing early would
        # let a later contender win too and make "exactly one" meaningless.
        await asyncio.sleep(3.0)
        await lease.stop()

    asyncio.run(main())


def test_exactly_one_contender_of_five_is_the_leader(tmp_path):
    """The property the old mechanism could not guarantee. With Redis
    unreachable, ``RedisLease`` left every worker at "unknown" — no worker
    could confirm a win, and ``GET /api/apps/-/watchdog`` reported no leader
    in 45/45 live samples. ``LOCK_EX|LOCK_NB`` always grants to exactly one
    contender, so "nobody is the leader" is not representable."""
    lock_dir = str(tmp_path / "locks")
    result_dir = str(tmp_path / "results")
    os.makedirs(result_dir, exist_ok=True)
    n = 5

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(n)
    procs = [ctx.Process(target=_contender, args=(lock_dir, result_dir, barrier))
             for _ in range(n)]
    for p in procs:
        p.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(os.listdir(result_dir)) < n:
            time.sleep(0.1)
        results = {}
        for name in os.listdir(result_dir):
            with open(os.path.join(result_dir, name), encoding="utf-8") as f:
                results[name] = f.read().strip()
        assert len(results) == n, f"only {len(results)} of {n} reported: {results}"
        leaders = [pid for pid, state in results.items() if state == "leader"]
        assert len(leaders) == 1, f"expected exactly 1 leader, got {leaders}: {results}"
    finally:
        for p in procs:
            if p.is_alive():
                p.join(timeout=5)
            if p.is_alive():
                p.kill()
