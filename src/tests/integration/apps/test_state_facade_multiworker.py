"""Cross-worker proof for ``ctx.state``'s kv + broadcast (design doc
app-shared-state-facade.md §10 criterion 1) — two independent ``StateFacade``
instances sharing one real Redis, mirroring aw-app-devctl's
``test_relay_multiworker.py`` / aw-backend's ``test_f6_tunnel_relay_multiworker.py``.

``lease``'s cross-worker proof is NOT here: ``fcntl.flock`` is
per-open-file-description, so two instances in ONE process already prove
mutual exclusion exactly like a real second worker would — see ``TestLease``
in ``src/tests/unit/apps/test_state_facade.py``, same precedent as
``test_flock_lease.py``'s for ``FlockLease``. A real Redis is needed for
kv/broadcast only.

Needs a reachable Redis (``AW_TEST_REDIS_URL`` / ``AW_WORKSPACE_REDIS_URL`` /
``AW_REDIS_URL`` — same resolution order ``redis_coord.py`` itself uses).
Skips cleanly without one. Never asserts a literal ``aw:ws:default:`` /
``aw:ws:aw:`` prefix — every expected key is computed through
``state_facade``'s own helpers — and uses a per-run-unique ``AW_WORKSPACE``
so this can never touch a real workspace's keys on the live, shared Redis
this environment's tests run against.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest


def _redis_url() -> str:
    for var in ("AW_TEST_REDIS_URL", "AW_WORKSPACE_REDIS_URL", "AW_REDIS_URL"):
        url = os.environ.get(var)
        if url:
            return url
    return "redis://127.0.0.1:6379/0"


def _redis_available() -> bool:
    try:
        import redis as sync_redis
        client = sync_redis.Redis.from_url(_redis_url(), socket_connect_timeout=2)
        return bool(client.ping())
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _redis_available(), reason="Redis not reachable"),
]


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE", f"test-ctx-state-{uuid.uuid4().hex[:8]}")
    monkeypatch.delenv("AW_WORKSPACE_REDIS_URL", raising=False)
    monkeypatch.setenv("AW_REDIS_URL", _redis_url())
    # The facade caches one client + one breaker at module scope; both are
    # keyed on env resolved at first use, so a fresh test needs a fresh one.
    import src.apps.state_facade as sf
    sf._client = None
    sf._breaker = sf._StateBreaker()
    yield


def _state(app_id: str = "myapp"):
    from src.apps.base import AppContext

    class _Journal:
        def record(self, *a, **kw):
            pass

    class _Runtime:
        journal = _Journal()

    ctx = AppContext(runtime=_Runtime(), app_id=app_id, version="1.0.0",
                     granted_permissions=["state:own"], config={},
                     package_dir="/tmp/" + app_id)
    return ctx.state


class TestKvCrossWorker:
    def test_put_on_worker_a_is_visible_from_worker_b(self):
        async def scenario():
            worker_a = _state()
            worker_b = _state()
            try:
                await worker_a.kv.put("shared-key", {"hello": "world"}, ttl=30)
                value = await worker_b.kv.get("shared-key")
                assert value == {"hello": "world"}, (
                    "a key written by worker A must be readable from "
                    "worker B through the same Redis-backed facade — this "
                    "is the exact cross-worker gap the facade exists to close")
            finally:
                await worker_a.kv.delete("shared-key")

        asyncio.run(scenario())

    def test_scan_is_bounded_to_the_apps_own_namespace(self):
        async def scenario():
            app_a = _state(app_id="app-a")
            app_b = _state(app_id="app-b")
            try:
                await app_a.kv.put("x", 1, ttl=30)
                await app_b.kv.put("y", 2, ttl=30)
                names_a = await app_a.kv.scan()
                assert "x" in names_a
                assert "y" not in names_a, "app A's scan must never see app B's keys"
            finally:
                await app_a.kv.delete("x")
                await app_b.kv.delete("y")

        asyncio.run(scenario())


class TestBroadcastCrossWorker:
    def test_publish_on_worker_a_is_delivered_to_worker_b(self):
        async def scenario():
            worker_a = _state()
            worker_b = _state()
            received: list = []

            async def handler(payload):
                received.append(payload)

            try:
                await worker_b.broadcast.subscribe("job-done", handler)
                await asyncio.sleep(0.3)  # let the PSUBSCRIBE land
                await worker_a.broadcast.publish("job-done", {"id": 42})

                loop = asyncio.get_event_loop()
                deadline = loop.time() + 5
                while not received and loop.time() < deadline:
                    await asyncio.sleep(0.1)
                assert received == [{"id": 42}], (
                    "a broadcast published by worker A must be delivered "
                    "to a handler subscribed on worker B")
            finally:
                await worker_a.broadcast.aclose()
                await worker_b.broadcast.aclose()

        asyncio.run(scenario())
