"""Multi-process integration tests for src/libs/redis_coord.py (F5b port of
aw-backend's F0 module — see that repo's own
src/tests/integration/libs/test_redis_coord.py for the un-ported original).

The ``RedisLease`` and ``cooldown_acquire`` halves of this file are gone with
the primitives themselves (2026-10-03): leadership is decided by ``flock``
only now, and its cross-process proofs live in
``src/tests/integration/apps/test_watchdog_flock_lease_multiworker.py`` and
``test_boot_reconcile_flock_multiprocess.py`` — which, unlike anything here,
do not skip when Redis is unreachable (i.e. always, in this environment).

These spawn REAL OS processes (multiprocessing, fork) racing against each
other over a shared Redis instance — the whole point of RedisBroadcaster is
cross-process coordination, so a single-process / single-event-loop test
wouldn't actually exercise the guarantee. Requires
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

def _redis_url() -> str:
    """The Redis this environment actually has, most explicit first.

    It defaulted straight to redis://127.0.0.1:6379/0, which is right for local
    dev and wrong for a managed workspace, where the companion listens as its
    own host (AW_REDIS_URL=redis://aw-remote-host-redis:6379/0 here, alive and
    answering PING). So this suite had been skipping on every run: green,
    reporting nothing, testing nothing.
    """
    for var in ("AW_TEST_REDIS_URL", "AW_WORKSPACE_REDIS_URL", "AW_REDIS_URL"):
        url = os.environ.get(var)
        if url:
            return url
    return "redis://127.0.0.1:6379/0"


REDIS_URL = _redis_url()
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


class TestRedisCoordUsesRealFallbackResolution:
    """W0: proves the PRODUCTION resolution path — no explicit redis_url
    passed anywhere here — actually reaches a live Redis in this
    environment, not just that an explicit REDIS_URL works. Before W0,
    get_workspace_redis_url() had no AW_REDIS_URL fallback and would
    resolve to the dead hardcoded 127.0.0.1:6379 default whenever
    AW_WORKSPACE_REDIS_URL was unset (the normal case — F5a, which would
    provision that var, is blocked in Need Human)."""

    def test_publish_and_receive_via_default_resolution_no_explicit_url(self, monkeypatch):
        monkeypatch.delenv("AW_WORKSPACE_REDIS_URL", raising=False)
        monkeypatch.setenv("AW_REDIS_URL", REDIS_URL)

        import importlib
        import src.libs.redis_coord as redis_coord
        importlib.reload(redis_coord)
        assert redis_coord.get_workspace_redis_url() == REDIS_URL

        topic = f"f5b-test-realpath-{uuid.uuid4().hex[:8]}"

        async def scenario():
            broadcaster = redis_coord.RedisBroadcaster()  # no redis_url — must resolve on its own
            received = []

            async def handler(recv_topic, payload):
                received.append((recv_topic, payload))

            await broadcaster.start_relay(handler)
            await asyncio.sleep(0.2)
            await broadcaster.publish(topic, {"via": "default_resolution"})

            deadline = time.time() + 3.0
            while not received and time.time() < deadline:
                await asyncio.sleep(0.05)

            await broadcaster.stop()
            return received

        received = asyncio.run(scenario())
        importlib.reload(redis_coord)
        assert received == [(topic, {"via": "default_resolution"})]


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
