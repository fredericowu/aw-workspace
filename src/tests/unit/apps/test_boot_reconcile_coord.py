"""Unit tests for BootReconcileCoordinator (src/apps/boot_reconcile_coord.py)
— no live Redis needed. Exercises the real class against an in-memory fake
implementing the tiny subset of redis-py's async API it uses (GET, SET
NX/PX/EX, EVAL of the one renew script), with real wall-clock TTLs so the
lease-expiry arithmetic under test is the same arithmetic production runs.

Written from the W4 incident (workspace ``crispal``, 2026-09-17 — see the
module docstring in ``boot_reconcile_coord.py``): the one-shot
``cooldown_acquire`` claim this coordinator replaces had NO test proving it
survives a pass longer than its own TTL, which is exactly the gap that
produced up to 21 redundant concurrent reconcile passes on one boot.
Cross-process behaviour (real workers, real Redis) is covered separately by
``src/tests/integration/apps/test_multiworker_app_lifecycle.py``.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.apps.boot_reconcile_coord import BootReconcileCoordinator, _RENEW_LUA


class FakeRedis:
    """In-memory stand-in for the tiny subset of redis-py's async API this
    coordinator uses. Real wall-clock TTLs — no simulated clock — so this
    exercises the exact same expiry arithmetic the real thing would."""

    def __init__(self):
        self._store: dict[str, tuple[str, float | None]] = {}

    def _expire_if_due(self, key: str) -> None:
        if key in self._store:
            _, expires_at = self._store[key]
            if expires_at is not None and time.monotonic() >= expires_at:
                del self._store[key]

    async def get(self, key: str):
        self._expire_if_due(key)
        entry = self._store.get(key)
        return entry[0] if entry else None

    async def set(self, key: str, value: str, nx: bool = False,
                  px: int | None = None, ex: int | None = None):
        self._expire_if_due(key)
        if nx and key in self._store:
            return None
        expires_at = None
        if px is not None:
            expires_at = time.monotonic() + px / 1000.0
        elif ex is not None:
            expires_at = time.monotonic() + ex
        self._store[key] = (value, expires_at)
        return True

    async def eval(self, script, numkeys, *args):
        assert script is _RENEW_LUA
        key, token, px = args
        self._expire_if_due(key)
        entry = self._store.get(key)
        if entry is None or entry[0] != token:
            return 0
        self._store[key] = (token, time.monotonic() + int(px) / 1000.0)
        return 1

    async def aclose(self):
        pass


def _coordinator(boot_id: str, client: FakeRedis, **kw) -> BootReconcileCoordinator:
    c = BootReconcileCoordinator(boot_id, redis_url="redis://fake", **kw)
    c._client = client  # inject the fake, bypass real aioredis.from_url
    return c


@pytest.fixture
def client() -> FakeRedis:
    return FakeRedis()


async def _run(coro):
    return await asyncio.wait_for(coro, timeout=10.0)


def test_first_caller_leads(client):
    leader = _coordinator("boot-1", client)
    assert asyncio.run(_run(leader.coordinate())) is True


def test_follower_behind_a_live_lease_eventually_gives_up_and_leads(client):
    """No done marker ever appears (the leader never finishes) — the
    follower must not hang forever; it gives up at max_wait_s and runs the
    pass itself, matching cooldown_acquire's old degrade-open contract."""
    leader = _coordinator("boot-1", client)
    follower = _coordinator("boot-1", client, max_wait_s=0.15, poll_interval=0.02,
                            lease_ttl=10.0)

    assert asyncio.run(_run(leader.coordinate())) is True

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is True
    assert 0.12 <= elapsed <= 0.6, f"gave up too fast or too slow: {elapsed}s"


def test_done_marker_short_circuits_a_follower_immediately(client):
    leader = _coordinator("boot-2", client)
    follower = _coordinator("boot-2", client, max_wait_s=5.0, poll_interval=0.01)

    assert asyncio.run(_run(leader.coordinate())) is True
    asyncio.run(_run(leader.mark_done()))

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is False
    assert elapsed < 1.0, "should have short-circuited on the done marker, not polled"


def test_heartbeat_keeps_a_working_leader_from_losing_its_claim(client):
    """The whole point of this coordinator over the cooldown_acquire it
    replaces: a lease renewed while genuinely still working must survive
    many multiples of its own TTL, not just the original fixed window."""
    leader = _coordinator("boot-3", client, lease_ttl=0.15, heartbeat_interval=0.05)

    async def scenario():
        assert await leader.coordinate() is True
        await leader.start_heartbeat()
        try:
            await asyncio.sleep(0.6)  # ~4x the lease TTL
            return await client.get(leader._lease_key)
        finally:
            await leader.stop_heartbeat()

    still_held = asyncio.run(_run(scenario()))
    assert still_held == leader.token


def test_a_leader_that_dies_without_finishing_is_reclaimed_promptly(client):
    """No heartbeat, no mark_done — simulates an OOM kill mid-pass. Only the
    lease's own un-renewed TTL can let a follower take over, and it must do
    so on the TTL's timescale, not by exhausting the follower's own
    (much longer) max_wait_s patience."""
    leader = _coordinator("boot-4", client, lease_ttl=0.1)
    follower = _coordinator("boot-4", client, max_wait_s=3.0, poll_interval=0.03)

    assert asyncio.run(_run(leader.coordinate())) is True

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 1.0, f"reclaim took {elapsed}s — looks like it waited out max_wait_s instead"


def test_exactly_one_of_many_concurrent_racers_leads(client):
    """6 workers race for the same boot_id at once. The winner does a
    (simulated, fast) pass and marks done — every other racer must see
    that before its own max_wait_s runs out, and stay a follower."""
    boot_id = "boot-5"

    async def race(coordinator):
        leading = await coordinator.coordinate()
        if leading:
            await asyncio.sleep(0.02)  # stand-in for a fast real pass
            await coordinator.mark_done()
        return leading

    coordinators = [_coordinator(boot_id, client, max_wait_s=0.5, poll_interval=0.02)
                     for _ in range(6)]

    async def scenario():
        return await asyncio.gather(*(race(c) for c in coordinators))

    results = asyncio.run(_run(scenario()))
    assert sum(results) == 1, f"expected exactly one leader, got {sum(results)}"


def test_different_boot_ids_never_interfere(client):
    """A stale claim from a PREVIOUS boot must never block a fresh one —
    the whole reason this (like cooldown_acquire before it) is keyed on
    boot_info.boot_id() rather than something that could repeat, e.g. a
    reused low pid after an in-place container restart."""
    a_leader = _coordinator("boot-a", client)
    b_leader = _coordinator("boot-b", client)

    assert asyncio.run(_run(a_leader.coordinate())) is True
    assert asyncio.run(_run(b_leader.coordinate())) is True


def test_coordinate_degrades_open_when_redis_is_unreachable(client, monkeypatch):
    """Mirrors cooldown_acquire's old contract at the call-site level
    (app.py's except-Exception branch): if Redis itself is unreachable,
    the caller must fail open (run the pass itself) rather than deadlock a
    workspace that can never provision. This coordinator raises from
    coordinate() in that case — app.py's _is_boot_provisioner is what
    catches it and falls back to (True, None); this test proves the raise,
    the fallback is covered in src/tests/unit/apps/test_lifecycle_split.py.
    """
    class _Boom:
        async def get(self, *a, **k):
            raise ConnectionError("redis is down")

    coordinator = _coordinator("boot-6", client)
    monkeypatch.setattr(coordinator, "_get_client", lambda: _Boom())

    with pytest.raises(ConnectionError):
        asyncio.run(_run(coordinator.coordinate()))
