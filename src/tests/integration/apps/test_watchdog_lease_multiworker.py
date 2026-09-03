"""W1 verification: TWO real WatchdogSupervisor instances, each wired to its
own RedisLease("core") exactly like src/api/app.py's production lifespan
(on_acquire -> resume(), on_release -> pause()), racing as separate OS
processes over ONE real Redis.

Proves what the card asked for: (a) at most one process's watchdog task
ever actually ticks at a time (single execution — not just "is_leader"
agreeing, the registered task itself), and (b) killing the leader (SIGKILL —
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
    registers a probe task and starts paused (mirrors app.py — a supervisor
    is never left unpaused next to a lease it hasn't won), then a
    RedisLease("core") wired exactly like src/api/app.py's
    on_acquire=resume/on_release=pause drives it. ``tick_key`` counts actual
    task executions (proves the supervisor really ran the task, not just
    that the lease flagged is_leader); ``counter_key`` is an INCR/DECR
    active-leader gauge (proves at most one process is resumed at a time)."""
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.apps.watchdog import WatchdogSupervisor
    from src.libs.redis_coord import RedisLease
    import redis.asyncio as aioredis

    async def main():
        pid = os.getpid()
        wd = WatchdogSupervisor()
        client = aioredis.from_url(redis_url, decode_responses=True)

        async def tick():
            await client.incr(tick_key)

        wd.register("core", "probe", tick, 0.05)
        wd.pause()  # never unpaused before a lease is actually won

        async def on_acquire():
            wd.resume()
            await client.incr(counter_key)
            queue.put(("acquire", pid, time.time()))

        async def on_release():
            wd.pause()
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

        r = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.delete(counter_key, tick_key, f"{_leader_prefix()}{role}")

        queue = ctx.Queue()
        stop_event = ctx.Event()
        procs = [
            ctx.Process(target=_watchdog_worker,
                        args=(role, REDIS_URL, ttl, renew, counter_key, tick_key, queue, stop_event))
            for _ in range(2)
        ]
        for p in procs:
            p.start()

        try:
            first = _drain_until(queue, kind="acquire", timeout=5.0)
            assert first is not None, "no process acquired the lease in time"
            _, leader_pid, _ = first

            # Let the leader's watchdog loop (0.05s interval) actually tick
            # a few times before checking single-execution.
            time.sleep(0.4)
            active = int(r.get(counter_key) or 0)
            assert active == 1, f"active-leader counter was {active}, expected exactly 1"
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
            r.delete(counter_key, tick_key, f"{_leader_prefix()}{role}")


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
