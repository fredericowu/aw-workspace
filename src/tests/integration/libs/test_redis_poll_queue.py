"""Integration tests for RedisPollQueue (src/libs/redis_coord.py, F5b port
of aw-backend's F2 module — see that repo's own
src/tests/integration/libs/test_redis_poll_queue.py for the un-ported
original).

These exercise the real primitive against a real Redis (some via a second
OS process, since that's the whole point of moving off in-memory state) —
see test_redis_coord.py for the same pattern. Requires a reachable Redis at
AW_TEST_REDIS_URL (defaults to the same 127.0.0.1:6379 address the F5a
companion is deterministically reachable at) — skips cleanly if it isn't
there.
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

def _enqueue_after_delay_worker(session_id: str, redis_url: str, delay_s: float, queue) -> None:
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.libs.redis_coord import RedisPollQueue

    async def main():
        await asyncio.sleep(delay_s)
        q = RedisPollQueue(redis_url=redis_url)
        entry_id = await q.enqueue(session_id, {"type": "response", "text": "hi from another process"})
        queue.put(entry_id)
        await q.close()

    asyncio.run(main())


def _get_epoch_worker(redis_url: str, queue) -> None:
    os.environ["AW_WORKSPACE"] = WORKSPACE
    from src.libs.redis_coord import RedisPollQueue

    async def main():
        q = RedisPollQueue(redis_url=redis_url)
        epoch = await q.get_epoch()
        queue.put(epoch)
        await q.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRedisPollQueueCrossProcessDelivery:
    def test_xread_block_wakes_up_when_another_process_xadds(self):
        """The whole point of the migration: a poll() blocked in this
        process (XREAD BLOCK) must see an event XADDed by a DIFFERENT
        process — the in-process deque + asyncio.Event this replaced could
        never do that."""
        from src.libs.redis_coord import RedisPollQueue, _poll_stream_prefix

        session_id = f"f5b-test-{uuid.uuid4().hex[:8]}"
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        # Give the reader a head start so it's genuinely blocked (not just
        # racing to read something already there) when the writer fires.
        writer = ctx.Process(target=_enqueue_after_delay_worker,
                              args=(session_id, REDIS_URL, 0.5, queue))

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            writer.start()
            start = time.time()
            events = await q.read_new(session_id, "0", block_ms=5000)
            elapsed = time.time() - start
            await q.close()
            return events, elapsed

        try:
            events, elapsed = asyncio.run(scenario())
            assert len(events) == 1
            entry_id, payload = events[0]
            assert payload == {"type": "response", "text": "hi from another process"}
            # Woken by the XADD, not by the 5s block timeout.
            assert elapsed < 3.0, f"took {elapsed:.2f}s — looks like it waited for the block timeout"

            written_id = queue.get(timeout=5)
            assert written_id == entry_id
        finally:
            writer.join(timeout=5)
            if writer.is_alive():
                writer.kill()
            sync_redis.Redis.from_url(REDIS_URL).delete(f"{_poll_stream_prefix()}{session_id}")

    def test_read_new_returns_immediately_when_events_already_buffered(self):
        """No block at all when the cursor is already behind existing
        entries — matches the old "check buffer first" fast path."""
        from src.libs.redis_coord import RedisPollQueue, _poll_stream_prefix

        session_id = f"f5b-test-{uuid.uuid4().hex[:8]}"

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            await q.enqueue(session_id, {"type": "transcript", "text": "already here"})
            start = time.time()
            events = await q.read_new(session_id, "0", block_ms=5000)
            elapsed = time.time() - start
            await q.close()
            return events, elapsed

        events, elapsed = asyncio.run(scenario())
        assert len(events) == 1
        assert events[0][1] == {"type": "transcript", "text": "already here"}
        assert elapsed < 1.0, f"took {elapsed:.2f}s — should have returned immediately"

        sync_redis.Redis.from_url(REDIS_URL).delete(f"{_poll_stream_prefix()}{session_id}")

    def test_read_new_times_out_empty_when_nothing_arrives(self):
        from src.libs.redis_coord import RedisPollQueue

        session_id = f"f5b-test-{uuid.uuid4().hex[:8]}"

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            events = await q.read_new(session_id, "0", block_ms=300)
            await q.close()
            return events

        events = asyncio.run(scenario())
        assert events == []


class TestRedisPollQueueCursorMonotonic:
    def test_enqueue_ids_strictly_increase_and_read_returns_them_in_order(self):
        from src.libs.redis_coord import RedisPollQueue, _poll_stream_prefix

        session_id = f"f5b-test-{uuid.uuid4().hex[:8]}"

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            ids = []
            for i in range(5):
                entry_id = await q.enqueue(session_id, {"type": "transcript", "n": i})
                ids.append(entry_id)
            events = await q.read_new(session_id, "0", block_ms=300)
            await q.close()
            return ids, events

        ids, events = asyncio.run(scenario())

        def _parts(entry_id: str) -> tuple[int, int]:
            ms, seq = entry_id.split("-")
            return int(ms), int(seq)

        for prev, cur in zip(ids, ids[1:]):
            assert _parts(cur) > _parts(prev), f"{cur} did not increase past {prev}"

        assert [e[0] for e in events] == ids
        assert [e[1]["n"] for e in events] == [0, 1, 2, 3, 4]

        # A cursor set to the 3rd event only yields events after it.
        cursor = ids[2]

        async def read_from_cursor():
            q = RedisPollQueue(redis_url=REDIS_URL)
            evs = await q.read_new(session_id, cursor, block_ms=300)
            await q.close()
            return evs

        remaining = asyncio.run(read_from_cursor())
        assert [e[1]["n"] for e in remaining] == [3, 4]

        sync_redis.Redis.from_url(REDIS_URL).delete(f"{_poll_stream_prefix()}{session_id}")

    def test_last_id_matches_most_recent_enqueue(self):
        from src.libs.redis_coord import RedisPollQueue

        session_id = f"f5b-test-{uuid.uuid4().hex[:8]}"

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            assert await q.last_id(session_id) == "0"  # empty stream
            first = await q.enqueue(session_id, {"type": "transcript", "n": 1})
            second = await q.enqueue(session_id, {"type": "transcript", "n": 2})
            last = await q.last_id(session_id)
            await q.close()
            return first, second, last

        first, second, last = asyncio.run(scenario())
        assert last == second
        assert last != first


class TestRedisPollQueueEpochStable:
    def test_epoch_is_created_once_and_shared_across_instances(self):
        """SET NX: whichever RedisPollQueue instance calls get_epoch() first
        wins, every other instance (this process or another) reads the
        SAME value back — the epoch does not change again once created."""
        from src.libs.redis_coord import RedisPollQueue, _poll_epoch_key

        sync_redis.Redis.from_url(REDIS_URL).delete(_poll_epoch_key())

        async def scenario():
            q1 = RedisPollQueue(redis_url=REDIS_URL)
            q2 = RedisPollQueue(redis_url=REDIS_URL)
            e1 = await q1.get_epoch()
            e2 = await q2.get_epoch()
            e1_again = await q1.get_epoch()  # cached locally, still consistent
            await q1.close()
            await q2.close()
            return e1, e2, e1_again

        e1, e2, e1_again = asyncio.run(scenario())
        assert e1 == e2 == e1_again
        assert e1  # non-empty

        sync_redis.Redis.from_url(REDIS_URL).delete(_poll_epoch_key())

    def test_epoch_stable_across_processes(self):
        from src.libs.redis_coord import RedisPollQueue, _poll_epoch_key

        sync_redis.Redis.from_url(REDIS_URL).delete(_poll_epoch_key())
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()

        async def get_local_epoch():
            q = RedisPollQueue(redis_url=REDIS_URL)
            epoch = await q.get_epoch()
            await q.close()
            return epoch

        local_epoch = asyncio.run(get_local_epoch())

        proc = ctx.Process(target=_get_epoch_worker, args=(REDIS_URL, queue))
        proc.start()
        try:
            remote_epoch = queue.get(timeout=5)
        finally:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()

        assert remote_epoch == local_epoch

        sync_redis.Redis.from_url(REDIS_URL).delete(_poll_epoch_key())


class TestLegacyPollCursorDetection:
    """Pure-function checks — no Redis needed, but kept in this module since
    this is exactly the F2 "old int cursor means reset" contract."""

    def test_legacy_plain_int_cursors_are_detected(self):
        from src.libs.redis_coord import is_legacy_poll_cursor

        assert is_legacy_poll_cursor("-1") is True   # old default ("never polled")
        assert is_legacy_poll_cursor("0") is True
        assert is_legacy_poll_cursor("42") is True
        assert is_legacy_poll_cursor("1000000") is True

    def test_redis_stream_ids_are_not_legacy(self):
        from src.libs.redis_coord import is_legacy_poll_cursor

        assert is_legacy_poll_cursor("1690000000000-0") is False
        assert is_legacy_poll_cursor("1-1") is False

    def test_empty_cursor_is_not_legacy(self):
        from src.libs.redis_coord import is_legacy_poll_cursor

        assert is_legacy_poll_cursor("") is False


class TestRedisPollQueueKeyIsWorkspaceScoped:
    def test_stream_key_uses_aw_ws_prefix(self):
        from src.libs.redis_coord import RedisPollQueue, _poll_stream_prefix

        assert _poll_stream_prefix() == f"aw:ws:{WORKSPACE}:mdpoll:"

        session_id = f"f5b-test-prefix-{uuid.uuid4().hex[:8]}"

        async def scenario():
            q = RedisPollQueue(redis_url=REDIS_URL)
            await q.enqueue(session_id, {"n": 1})
            await q.close()

        asyncio.run(scenario())

        r = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            assert r.exists(f"{_poll_stream_prefix()}{session_id}") == 1
        finally:
            r.delete(f"{_poll_stream_prefix()}{session_id}")
