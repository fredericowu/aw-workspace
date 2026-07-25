"""Multi-process integration tests for src/libs/redis_coord.py (F5b port of
aw-backend's F0 module — see that repo's own
src/tests/integration/libs/test_redis_coord.py for the un-ported original).

These spawn REAL OS processes (multiprocessing, fork) racing against each
other over a shared Redis instance — the whole point of RedisLease /
RedisBroadcaster is cross-process coordination, so a single-process /
single-event-loop test wouldn't actually exercise the guarantee. Requires
a reachable Redis at AW_TEST_REDIS_URL (defaults to the same
127.0.0.1:6379 address the F5a companion is deterministically reachable
at) — skips cleanly if it isn't there.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import random
import time
import uuid

import pytest
import redis as sync_redis

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REDIS_URL = os.environ.get("AW_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
WORKSPACE = f"f5b-test-{uuid.uuid4().hex[:8]}"


def _redis_available() -> bool:
    try:
        client = sync_redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1)
        return bool(client.ping())
    except Exception:
        return False


pytestmark.append(pytest.mark.skipif(not _redis_available(), reason="Redis not reachable"))


@pytest.fixture(autouse=True)
def _workspace_env(monkeypatch):
    """Force AW_WORKSPACE for the parent process too, not just the forked
    workers below — module-level `os.environ.setdefault` would silently
    lose to whichever sibling test module under src/tests/integration/libs/
    happens to be collected first, since setdefault is a no-op once any
    module has already set the var."""
    monkeypatch.setenv("AW_WORKSPACE", WORKSPACE)


# ---------------------------------------------------------------------------
# Worker process bodies — module-level so they're picklable/forkable.
# ---------------------------------------------------------------------------

def _lease_failover_worker(role: str, redis_url: str, ttl: float, renew: float, queue, stop_event) -> None:
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.libs.redis_coord import RedisLease

    async def main():
        pid = os.getpid()

        async def on_acquire():
            queue.put(("acquire", pid, lease.token, time.time()))

        async def on_release():
            queue.put(("release", pid, lease.token, time.time()))

        lease = RedisLease(role=role, redis_url=redis_url, ttl=ttl, renew=renew,
                            on_acquire=on_acquire, on_release=on_release)
        await lease.start()
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await lease.stop()

    asyncio.run(main())


def _lease_churn_worker(role: str, redis_url: str, ttl: float, renew: float,
                         counter_key: str, rounds: int, stop_event) -> None:
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.libs.redis_coord import RedisLease
    import redis.asyncio as aioredis

    async def main():
        client = aioredis.from_url(redis_url, decode_responses=True)
        for _ in range(rounds):
            if stop_event.is_set():
                break

            async def on_acquire():
                await client.incr(counter_key)

            async def on_release():
                await client.decr(counter_key)

            lease = RedisLease(role=role, redis_url=redis_url, ttl=ttl, renew=renew,
                                on_acquire=on_acquire, on_release=on_release)
            await lease.start()
            deadline = time.time() + ttl * 3
            while not lease.is_leader and time.time() < deadline:
                await asyncio.sleep(0.05)
            if lease.is_leader:
                await asyncio.sleep(random.uniform(0.2, 0.5))
            await lease.stop()
            await asyncio.sleep(random.uniform(0.05, 0.15))
        await client.aclose()

    asyncio.run(main())


def _broadcast_subscriber_worker(topic: str, redis_url: str, queue, ready_event, stop_event) -> None:
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.libs.redis_coord import RedisBroadcaster

    async def main():
        broadcaster = RedisBroadcaster(redis_url=redis_url)

        async def handler(recv_topic, payload):
            queue.put((recv_topic, payload))

        await broadcaster.start_relay(handler)
        ready_event.set()
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await broadcaster.stop()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRedisLeaseFailover:
    def test_exactly_one_new_leader_within_ttl_after_kill(self):
        from src.libs.redis_coord import _leader_prefix

        ctx = multiprocessing.get_context("fork")
        role = f"f5b-test-failover-{uuid.uuid4().hex[:8]}"
        ttl, renew = 2.0, 0.5

        queue = ctx.Queue()
        stop_event = ctx.Event()
        procs = [
            ctx.Process(target=_lease_failover_worker, args=(role, REDIS_URL, ttl, renew, queue, stop_event))
            for _ in range(3)
        ]
        for p in procs:
            p.start()

        try:
            # Wait for the first leader to emerge.
            first = _drain_until(queue, kind="acquire", timeout=5.0)
            assert first is not None, "no process acquired the lease in time"
            _, leader_pid, leader_token, _ = first

            leader_proc = next(p for p in procs if p.pid == leader_pid)
            leader_proc.kill()  # SIGKILL — no graceful release, simulates a crash
            leader_proc.join(timeout=5)

            # A survivor must take over within ttl (+ a scheduling buffer).
            second = _drain_until(queue, kind="acquire", timeout=ttl + 3.0,
                                   exclude_pid=leader_pid)
            assert second is not None, f"no new leader took over within ttl={ttl}s"
            _, new_leader_pid, new_leader_token, _ = second
            assert new_leader_pid != leader_pid
            assert new_leader_token != leader_token

            # Exactly one new leader: draining a bit longer must not reveal
            # a second, different process also believing it's leader.
            extra = _drain_until(queue, kind="acquire", timeout=1.5,
                                  exclude_pid={leader_pid, new_leader_pid})
            assert extra is None, f"double leadership detected: {extra}"
        finally:
            stop_event.set()
            for p in procs:
                if p.is_alive():
                    p.join(timeout=5)
                if p.is_alive():
                    p.kill()
            sync_redis.Redis.from_url(REDIS_URL).delete(f"{_leader_prefix()}{role}")


class TestRedisLeaseNoDoubleLeadership:
    def test_atomic_active_leader_count_never_exceeds_one(self):
        from src.libs.redis_coord import _leader_prefix

        ctx = multiprocessing.get_context("fork")
        role = f"f5b-test-counter-{uuid.uuid4().hex[:8]}"
        counter_key = f"aw:ws:{WORKSPACE}:test:active:{role}"
        ttl, renew = 1.5, 0.4
        rounds = 4

        r = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
        r.delete(counter_key)
        r.delete(f"{_leader_prefix()}{role}")

        stop_event = ctx.Event()
        procs = [
            ctx.Process(target=_lease_churn_worker,
                        args=(role, REDIS_URL, ttl, renew, counter_key, rounds, stop_event))
            for _ in range(4)
        ]
        for p in procs:
            p.start()

        observed_max = 0
        observed_any_leader = False
        deadline = time.time() + rounds * (ttl * 3 + 1.0) + 5.0
        try:
            while time.time() < deadline and any(p.is_alive() for p in procs):
                val = int(r.get(counter_key) or 0)
                observed_max = max(observed_max, val)
                observed_any_leader = observed_any_leader or val >= 1
                time.sleep(0.05)
        finally:
            stop_event.set()
            for p in procs:
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()
            final = int(r.get(counter_key) or 0)
            r.delete(counter_key)
            r.delete(f"{_leader_prefix()}{role}")

        assert observed_any_leader, "no process ever became leader"
        assert observed_max <= 1, f"active-leader counter exceeded 1 (saw {observed_max}) — double leadership"
        assert final == 0, f"counter didn't settle back to 0 after all processes stopped (was {final})"


class TestRedisBroadcasterCrossProcess:
    def test_publish_in_one_process_reaches_subscriber_in_another(self):
        ctx = multiprocessing.get_context("fork")
        topic = f"f5b-test-{uuid.uuid4().hex[:8]}"

        queue = ctx.Queue()
        ready_event = ctx.Event()
        stop_event = ctx.Event()
        sub_proc = ctx.Process(target=_broadcast_subscriber_worker,
                                args=(topic, REDIS_URL, queue, ready_event, stop_event))
        sub_proc.start()
        try:
            assert ready_event.wait(timeout=5), "subscriber never finished psubscribe"
            time.sleep(0.2)  # small buffer past the psubscribe ack

            async def do_publish():
                from src.libs.redis_coord import RedisBroadcaster
                broadcaster = RedisBroadcaster(redis_url=REDIS_URL)
                n = await broadcaster.publish(topic, {"hello": "world", "topic": topic})
                await broadcaster.stop()
                return n

            n_delivered = asyncio.run(do_publish())
            assert n_delivered >= 1

            recv_topic, payload = queue.get(timeout=5)
            assert recv_topic == topic
            assert payload == {"hello": "world", "topic": topic}
        finally:
            stop_event.set()
            sub_proc.join(timeout=5)
            if sub_proc.is_alive():
                sub_proc.kill()

    def test_publisher_also_gets_its_own_broadcast_via_relay(self):
        """Single delivery path: the publisher only sees its own message
        because its own relay is subscribed too — publish() never calls
        the handler directly."""
        topic = f"f5b-test-self-{uuid.uuid4().hex[:8]}"

        async def scenario():
            from src.libs.redis_coord import RedisBroadcaster
            broadcaster = RedisBroadcaster(redis_url=REDIS_URL)
            received = []

            async def handler(recv_topic, payload):
                received.append((recv_topic, payload))

            await broadcaster.start_relay(handler)
            await asyncio.sleep(0.2)  # let psubscribe settle
            await broadcaster.publish(topic, {"self": True})

            deadline = time.time() + 3.0
            while not received and time.time() < deadline:
                await asyncio.sleep(0.05)

            await broadcaster.stop()
            return received

        received = asyncio.run(scenario())
        assert received == [(topic, {"self": True})]


class TestRedisBroadcasterKeyIsWorkspaceScoped:
    def test_publish_uses_aw_ws_prefix(self):
        from src.libs.redis_coord import RedisBroadcaster, _bcast_prefix

        assert _bcast_prefix() == f"aw:ws:{WORKSPACE}:bcast:"

        topic = f"f5b-test-prefix-{uuid.uuid4().hex[:8]}"
        received_channels = []

        async def scenario():
            broadcaster = RedisBroadcaster(redis_url=REDIS_URL)
            client = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            pubsub.psubscribe(f"{_bcast_prefix()}*")
            pubsub.get_message(timeout=1)  # subscribe ack
            await broadcaster.publish(topic, {"x": 1})
            msg = pubsub.get_message(timeout=2)
            while msg is not None and msg["type"] != "pmessage":
                msg = pubsub.get_message(timeout=2)
            if msg is not None:
                received_channels.append(msg["channel"])
            pubsub.close()
            await broadcaster.stop()

        asyncio.run(scenario())
        assert received_channels == [f"{_bcast_prefix()}{topic}"]


class TestCooldownAcquire:
    def test_first_caller_wins_second_is_blocked_then_expires(self):
        from src.libs.redis_coord import cooldown_acquire, _cooldown_prefix

        key = f"f5b-test-cooldown-{uuid.uuid4().hex[:8]}"

        async def scenario():
            first = await cooldown_acquire(key, seconds=1, redis_url=REDIS_URL)
            second = await cooldown_acquire(key, seconds=1, redis_url=REDIS_URL)
            await asyncio.sleep(1.3)
            third = await cooldown_acquire(key, seconds=1, redis_url=REDIS_URL)
            return first, second, third

        first, second, third = asyncio.run(scenario())
        assert first is True
        assert second is False
        assert third is True

        sync_redis.Redis.from_url(REDIS_URL).delete(f"{_cooldown_prefix()}{key}")


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
