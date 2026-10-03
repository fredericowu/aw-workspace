"""Unit tests for ``FlockLease`` (Lock D, ``src/apps/fs_lock.py``) — the
in-process half of the contract the lifespan depends on. The cross-PROCESS
proof (three real workers, a SIGKILLed leader, per-pid tick counters) is
``src/tests/integration/apps/test_watchdog_flock_lease_multiworker.py``.

Two contenders in one process is a valid shape here and would not be for a
Redis lease: ``fcntl.flock`` is per-open-file-description, so two
``FlockLease`` instances in this process exclude each other exactly as two
worker processes do.

The callback-edge properties are what this file is really for. They are the
ones with a live incident behind them:

* a loser MUST get ``on_release`` — ``WatchdogSupervisor`` defaults to
  leader=True, so a worker that is never told it lost keeps running every
  periodic task (W1: "leader-election never pauses a worker that loses the
  race", confirmed under real 10-worker load);
* and it must get it EXACTLY ONCE, not on every poll — a standby
  re-contends forever, and firing per-poll would call ``pause()`` twelve
  times a minute on every non-leader worker.
"""
from __future__ import annotations

import asyncio

import pytest

from src.apps import fs_lock
from src.apps.fs_lock import FlockLease


@pytest.fixture
def lock_dir(tmp_path) -> str:
    return str(tmp_path / "locks")


def _lease(lock_dir: str, events: list, **kw) -> FlockLease:
    async def on_acquire():
        events.append("acquire")

    async def on_release():
        events.append("release")

    return FlockLease("unit-test", lock_dir=lock_dir, poll_interval=0.02,
                      on_acquire=on_acquire, on_release=on_release, **kw)


def test_first_lease_leads_and_fires_on_acquire(lock_dir):
    events: list = []
    lease = _lease(lock_dir, events)

    async def scenario():
        await lease.start()
        try:
            assert lease.is_leader is True
            assert events == ["acquire"]
        finally:
            await lease.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_a_loser_is_told_it_lost_before_start_returns(lock_dir):
    """The reason ``start()`` contends INLINE rather than only in the poll
    loop: the lifespan continues the moment it returns, and until a loser has
    been paused it is still running every periodic task."""
    leader_events: list = []
    loser_events: list = []
    leader = _lease(lock_dir, leader_events)
    loser = _lease(lock_dir, loser_events)

    async def scenario():
        await leader.start()
        try:
            await loser.start()
            try:
                assert loser.is_leader is False
                assert loser_events == ["release"], (
                    "a worker that lost the race was not paused — this is the "
                    "W1 regression that left 9/10 workers running every task")
            finally:
                await loser.stop()
        finally:
            await leader.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_on_release_fires_once_not_on_every_poll(lock_dir):
    leader_events: list = []
    loser_events: list = []
    leader = _lease(lock_dir, leader_events)
    loser = _lease(lock_dir, loser_events)

    async def scenario():
        await leader.start()
        try:
            await loser.start()
            try:
                await asyncio.sleep(0.2)  # ~10 poll intervals
                assert loser_events == ["release"], (
                    f"on_release fired {len(loser_events)}x — edge-triggering "
                    "is gone, and watchdog.pause() is now being called on a "
                    "loop")
            finally:
                await loser.stop()
        finally:
            await leader.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_a_standby_takes_over_when_the_leader_releases(lock_dir):
    """Failover, bounded by one poll rather than by a TTL: the kernel drops
    the flock the instant the holder's fd closes."""
    leader_events: list = []
    standby_events: list = []
    leader = _lease(lock_dir, leader_events)
    standby = _lease(lock_dir, standby_events)

    async def scenario():
        await leader.start()
        await standby.start()
        try:
            assert standby.is_leader is False
            await leader.stop()  # == the leader process going away
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                if standby.is_leader:
                    break
                await asyncio.sleep(0.02)
            assert standby.is_leader is True, "the standby never took over"
            assert standby_events == ["release", "acquire"]
        finally:
            await standby.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_stop_releases_so_another_process_can_win_immediately(lock_dir):
    events: list = []
    lease = _lease(lock_dir, events)

    async def scenario():
        await lease.start()
        assert lease.is_leader is True
        await lease.stop()
        assert lease.is_leader is False
        fd = fs_lock.try_acquire(lease.path)
        assert fd is not None, (
            "stop() did not release — a surviving worker would have to wait "
            "for this process to actually die")
        fs_lock.release(fd)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_the_poll_loop_survives_a_raised_attempt(lock_dir, monkeypatch):
    """Carried over from ``RedisLease``, whose ``_run`` once died permanently
    on any raised poll (an unretrieved asyncio task exception) — darkening a
    paused worker forever, because it never polled again to notice things had
    recovered. Only a broken lock directory can do that here, but the loop
    must still survive it."""
    events: list = []
    leader = _lease(lock_dir, events)
    standby = _lease(lock_dir, [])

    async def scenario():
        await leader.start()
        try:
            await standby.start()
            calls = {"n": 0}
            real = fs_lock.try_acquire

            def _flaky(path):
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise OSError("read-only file system")
                return real(path)

            monkeypatch.setattr(fs_lock, "try_acquire", _flaky)
            await asyncio.sleep(0.15)
            assert standby._task is not None and not standby._task.done(), (
                "the poll loop died on a raised attempt")
            assert standby.is_leader is False
            assert calls["n"] > 3, "the loop stopped retrying after the errors"
        finally:
            await standby.stop()
            await leader.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_start_twice_is_a_programming_error(lock_dir):
    lease = _lease(lock_dir, [])

    async def scenario():
        await lease.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                await lease.start()
        finally:
            await lease.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
