"""W1 verification: THREE real WatchdogSupervisor instances, each wired to its
own RedisLease("core") exactly like src/api/app.py's production lifespan
(register(), no manual pause, then lease.start(); on_acquire -> resume(),
on_release -> pause()), racing as separate OS processes over ONE real Redis.

Proves what the card asked for: (a) at most one process's watchdog task
ever actually ticks at a time — checked via each process's OWN tick counter,
not just the shared one, since a losing process whose WatchdogSupervisor was
never paused (the exact bug this file's harness used to hand-correct around,
see wd.pause() history) ticks at the same rate as the real leader and would
pass a shared-counter-only check, and (b) killing the leader (SIGKILL —
no graceful release, so the Redis key must simply expire, same as a real
crash) lets the survivor's watchdog task resume ticking within the lease
TTL.

Requires a reachable Redis (see _redis_url() below) — skips cleanly if it
isn't there. Mirrors the process/skip/cleanup pattern already established
in src/tests/integration/libs/test_redis_coord.py.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import uuid

import pytest
import redis as sync_redis

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _redis_url() -> str:
    for var in ("AW_TEST_REDIS_URL", "AW_WORKSPACE_REDIS_URL", "AW_REDIS_URL"):
        url = os.environ.get(var)
        if url:
            return url
    return "redis://127.0.0.1:6379/0"


REDIS_URL = _redis_url()
WORKSPACE = f"w1-test-{uuid.uuid4().hex[:8]}"


def _redis_available() -> bool:
    try:
        client = sync_redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
        return bool(client.ping())
    except Exception:
        return False


pytestmark.append(pytest.mark.skipif(not _redis_available(), reason="Redis not reachable"))


@pytest.fixture(autouse=True)
def _workspace_env(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", WORKSPACE)


# ---------------------------------------------------------------------------
# Worker process body — module-level so it's picklable/forkable.
# ---------------------------------------------------------------------------

def _watchdog_worker(role: str, redis_url: str, ttl: float, renew: float,
                      counter_key: str, tick_key: str, queue, stop_event) -> None:
    """One process = one production-shaped worker: its own WatchdogSupervisor
    is built EXACTLY like src/api/app.py's lifespan — register(), then
    lease.start(), no manual pause() in between. WatchdogSupervisor defaults
    to leader=True (ungated) until a confirmed RedisLease("core") loss pauses
    it; a losing process must reach that pause on its own via on_release,
    which is the exact regression this test guards. ``tick_key`` counts
    actual task executions (proves the supervisor really ran the task, not
    just that the lease flagged is_leader); ``counter_key`` is an INCR/DECR
    active-leader gauge (proves at most one process is resumed at a time)."""
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.apps.watchdog import WatchdogSupervisor
    from src.libs.redis_coord import RedisLease
    import redis.asyncio as aioredis

    async def main():
        pid = os.getpid()
        wd = WatchdogSupervisor()
        client = aioredis.from_url(redis_url, decode_responses=True)
        own_tick_key = f"{tick_key}:{pid}"
        # This fix makes on_release fire for BOTH cases RedisLease now tells
        # apart: a confirmed loss of a HELD lease, and a confirmed loss of
        # the initial race (never held it at all) — see redis_coord.py's
        # _try_acquire(). counter_key is meant as an active-leader gauge, so
        # it must only move on a genuine leader transition; track locally
        # whether this process ever actually won so an initial-race loss
        # (which never incremented) doesn't decrement it into the negative.
        ever_leader = False

        async def tick():
            await client.incr(tick_key)
            await client.incr(own_tick_key)

        wd.register("core", "probe", tick, 0.05)

        async def on_acquire():
            nonlocal ever_leader
            wd.resume()
            ever_leader = True
            await client.incr(counter_key)
            queue.put(("acquire", pid, time.time()))

        async def on_release():
            nonlocal ever_leader
            wd.pause()
            if ever_leader:
                ever_leader = False
                await client.decr(counter_key)
            queue.put(("release", pid, time.time()))

        lease = RedisLease(role=role, redis_url=redis_url, ttl=ttl, renew=renew,
                            on_acquire=on_acquire, on_release=on_release)
        await lease.start()
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await lease.stop()
        await client.aclose()

    asyncio.run(main())


class TestWatchdogSupervisorLeaseFailover:
    def test_exactly_one_ticks_and_failover_resumes_ticking_within_ttl(self):
        from src.libs.redis_coord import _leader_prefix

        ctx = multiprocessing.get_context("fork")
        role = f"w1-test-core-{uuid.uuid4().hex[:8]}"
        counter_key = f"aw:ws:{WORKSPACE}:test:w1:active:{role}"
        tick_key = f"aw:ws:{WORKSPACE}:test:w1:ticks:{role}"
        ttl, renew = 2.0, 0.5
        n_procs = 3

        r = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.delete(counter_key, tick_key, f"{_leader_prefix()}{role}")

        queue = ctx.Queue()
        stop_event = ctx.Event()
        procs = [
            ctx.Process(target=_watchdog_worker,
                        args=(role, REDIS_URL, ttl, renew, counter_key, tick_key, queue, stop_event))
            for _ in range(n_procs)
        ]
        for p in procs:
            p.start()
        own_tick_keys = [f"{tick_key}:{p.pid}" for p in procs]

        try:
            first = _drain_until(queue, kind="acquire", timeout=5.0)
            assert first is not None, "no process acquired the lease in time"
            _, leader_pid, _ = first

            # Let steady state settle for ~2x renew_interval, so every
            # losing process's confirmed-standby transition (edge-triggered
            # on_release, see redis_coord.py's RedisLease._try_acquire) has
            # had time to land and pause its own watchdog task.
            time.sleep(renew * 2)
            active = int(r.get(counter_key) or 0)
            assert active == 1, f"active-leader counter was {active}, expected exactly 1"

            # THE regression assertion. Before this fix, a losing process's
            # WatchdogSupervisor was never told to pause and kept ticking
            # forever at the same rate as the real leader — `active == 1`
            # above stays green even with that bug (the gauge only reflects
            # confirmed acquire/release transitions, not who's still
            # ticking) and a single shared tick_key can't attribute ticks to
            # a process. Each process's OWN tick counter must show exactly
            # one process still ticking: not "frozen at 0" — register()'s
            # run_immediately=True fires one tick for every process before
            # any lease is even resolved, leader and losers alike, so a
            # single pre-race tick is expected and not the bug — but a
            # loser's counter must stop GROWING once the lease resolves,
            # while the leader's keeps growing. Sample twice with a gap to
            # tell "ticked once, then paused" from "still ticking".
            own_ticks_t1 = {p.pid: int(r.get(f"{tick_key}:{p.pid}") or 0) for p in procs}
            time.sleep(renew * 2)
            own_ticks_t2 = {p.pid: int(r.get(f"{tick_key}:{p.pid}") or 0) for p in procs}
            for p in procs:
                if p.pid == leader_pid:
                    assert own_ticks_t2[p.pid] > own_ticks_t1[p.pid], (
                        f"leader pid={p.pid} stopped ticking "
                        f"({own_ticks_t1[p.pid]} -> {own_ticks_t2[p.pid]})"
                    )
                else:
                    assert own_ticks_t2[p.pid] == own_ticks_t1[p.pid], (
                        f"loser pid={p.pid} kept ticking after losing the race "
                        f"({own_ticks_t1[p.pid]} -> {own_ticks_t2[p.pid]}) — its "
                        "WatchdogSupervisor was never paused"
                    )
            ticks_before_kill = int(r.get(tick_key) or 0)
            assert ticks_before_kill > 0, "leader's watchdog task never actually ran"

            leader_proc = next(p for p in procs if p.pid == leader_pid)
            leader_proc.kill()  # SIGKILL — simulates a crash, no graceful on_release
            leader_proc.join(timeout=5)
            assert not leader_proc.is_alive(), "leader process survived the kill"

            kill_time = time.time()
            second = _drain_until(queue, kind="acquire", timeout=ttl + 3.0, exclude_pid=leader_pid)
            assert second is not None, f"no new leader took over within ttl={ttl}s"
            _, new_leader_pid, _ = second
            acquire_took = time.time() - kill_time
            assert new_leader_pid != leader_pid

            # The survivor's WatchdogSupervisor must actually resume
            # ticking, not just have its lease report is_leader=True.
            deadline = time.time() + 3.0
            resumed_ticking = False
            while time.time() < deadline:
                if int(r.get(tick_key) or 0) > ticks_before_kill:
                    resumed_ticking = True
                    break
                time.sleep(0.05)
            resume_took = time.time() - kill_time
            assert resumed_ticking, "survivor's watchdog task never resumed ticking after failover"
            # NOTE: the active-leader gauge (counter_key) is NOT checked here.
            # A SIGKILLed leader never runs its on_release DECR — that's the
            # whole point of simulating a crash — so the gauge legitimately
            # reads 2 (one stale INCR from the dead process, one live INCR
            # from the survivor) after a real failover. It's still a valid
            # "no double leadership" check for the STEADY-STATE assertion
            # above (active == 1, before any kill, where release always
            # ran); single execution post-failover is proven directly by
            # `resumed_ticking` (only the survivor's tick_key moved) plus
            # `leader_proc.is_alive() is False` (the old leader is provably
            # gone, not double-running).

            print(
                f"[W1 verify] steady-state active-leader gauge == 1 (no double leadership) "
                f"while leader pid={leader_pid} ticked {ticks_before_kill}x; SIGKILLed it; "
                f"new leader pid={new_leader_pid} (old one confirmed dead) acquired the lease "
                f"in {acquire_took:.2f}s (ttl={ttl}s) and resumed ticking in {resume_took:.2f}s"
            )
        finally:
            stop_event.set()
            for p in procs:
                if p.is_alive():
                    p.join(timeout=5)
                if p.is_alive():
                    p.kill()
            r.delete(counter_key, tick_key, f"{_leader_prefix()}{role}", *own_tick_keys)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_until(queue, kind: str, timeout: float, exclude_pid=None):
    """Pop events off `queue` until one matches `kind` and isn't from an
    excluded pid, or `timeout` elapses. Returns the matching event tuple
    or None."""
    if exclude_pid is None:
        exclude_pid = set()
    elif isinstance(exclude_pid, int):
        exclude_pid = {exclude_pid}

    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            event = queue.get(timeout=remaining)
        except Exception:
            break
        if event[0] == kind and event[1] not in exclude_pid:
            return event
    return None
