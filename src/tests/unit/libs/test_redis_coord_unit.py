"""Unit tests for the F5b redis_coord bootstrap helpers — no live Redis
needed, these only check env-var resolution, key-prefix scoping, and the
`get_redis_pool()` per-URL cache. Cross-process behavior of the two
remaining coordination primitives is covered by
src/tests/integration/libs/test_redis_coord.py and
test_redis_poll_queue.py (require a real Redis, skip cleanly otherwise).

The `RedisLease` tests that used to live here went with the class
(2026-10-03, leadership is flock-only now). Their one non-obvious property
— a poll that raises must not kill the loop, or a paused worker never polls
again and goes dark forever — carried over to `FlockLease` and is tested in
src/tests/unit/apps/test_flock_lease.py.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_REDIS_URL", raising=False)
    monkeypatch.delenv("AW_REDIS_URL", raising=False)
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    import src.libs.redis_coord as redis_coord
    importlib.reload(redis_coord)
    yield redis_coord
    importlib.reload(redis_coord)


class TestGetWorkspaceRedisUrl:
    def test_falls_back_to_default_when_unset(self, _clean_env):
        assert _clean_env.get_workspace_redis_url() == _clean_env.DEFAULT_REDIS_URL

    def test_uses_env_var_when_set(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://10.0.0.5:6379/2")
        assert _clean_env.get_workspace_redis_url() == "redis://10.0.0.5:6379/2"

    def test_falls_back_to_shared_aw_redis_url_when_workspace_url_unset(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_REDIS_URL", "redis://172.18.0.1:6379/1")
        assert _clean_env.get_workspace_redis_url() == "redis://172.18.0.1:6379/1"

    def test_workspace_redis_url_wins_over_shared_aw_redis_url(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://10.0.0.5:6379/2")
        monkeypatch.setenv("AW_REDIS_URL", "redis://172.18.0.1:6379/1")
        assert _clean_env.get_workspace_redis_url() == "redis://10.0.0.5:6379/2"


class TestGetWorkspaceSlug:
    def test_falls_back_to_default_when_unset(self, _clean_env):
        assert _clean_env.get_workspace_slug() == _clean_env.DEFAULT_WORKSPACE

    def test_uses_env_var_when_set(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        assert _clean_env.get_workspace_slug() == "acme"


class TestKeyPrefixesAreWorkspaceScoped:
    def test_prefixes_embed_the_workspace_slug(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        assert _clean_env._bcast_prefix() == "aw:ws:acme:bcast:"
        assert _clean_env._poll_stream_prefix() == "aw:ws:acme:mdpoll:"
        assert _clean_env._poll_epoch_key() == "aw:ws:acme:mdpoll:epoch"

    def test_different_workspaces_never_share_a_prefix(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE", "acme")
        acme_prefix = _clean_env._bcast_prefix()
        monkeypatch.setenv("AW_WORKSPACE", "widgets-inc")
        widgets_prefix = _clean_env._bcast_prefix()
        assert acme_prefix != widgets_prefix


class _FakePubSub:
    """One subscription's worth of ``listen()``, scripted.

    ``scripts`` holds one script per expected (re)subscribe. A script item is
    a pmessage dict to yield, an exception INSTANCE to raise, or a number to
    sleep for — the last standing in for a healthy idle subscription, which
    matters because ``listen()`` RETURNING is itself one of the failures under
    test. No Redis anywhere: the suite shares this container's live Redis
    keyspace, so a broadcaster test that connected for real would publish into
    production keys.
    """

    def __init__(self, scripts: list):
        self.scripts = scripts
        self.subscribes: list[str] = []
        self.closed = 0

    async def psubscribe(self, pattern: str) -> None:
        self.subscribes.append(pattern)

    async def listen(self):
        script = self.scripts.pop(0) if self.scripts else [30.0]
        for item in script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, (int, float)):
                await asyncio.sleep(item)  # a live, idle subscription
                continue
            yield item

    async def aclose(self) -> None:
        self.closed += 1

    async def punsubscribe(self, pattern: str) -> None:
        pass


def _pmessage(redis_coord, topic: str, payload: str):
    return {"type": "pmessage",
            "channel": f"{redis_coord._bcast_prefix()}{topic}",
            "data": payload}


class TestRelaySelfHeals:
    """Regression tests for the relay that died permanently on any Redis
    hiccup (card 3fc5bf3b, defect B): ``_relay_loop`` caught only
    ``CancelledError``, so one ``ConnectionError`` out of ``listen()`` ended
    cross-worker convergence for the life of that process — app installs,
    removals and config saves served on other workers never arrived again, and
    the only trace was an unretrieved task exception."""

    def _broadcaster(self, redis_coord, monkeypatch, scripts):
        b = redis_coord.RedisBroadcaster("redis://127.0.0.1:1/0")
        pubsub = _FakePubSub(scripts)
        monkeypatch.setattr(b, "_get_client", lambda: _FakeClient(pubsub))
        monkeypatch.setattr(redis_coord, "RELAY_BACKOFF_START_S", 0.01)
        monkeypatch.setattr(redis_coord, "RELAY_BACKOFF_MAX_S", 0.02)
        return b, pubsub

    def test_it_resubscribes_and_keeps_delivering_after_a_dropped_connection(
            self, _clean_env, monkeypatch):
        redis_coord = _clean_env
        got: list[tuple[str, dict]] = []
        delivered_twice = asyncio.Event()

        async def handler(topic, payload):
            got.append((topic, payload))
            if len(got) == 2:
                delivered_twice.set()

        b, pubsub = self._broadcaster(redis_coord, monkeypatch, [
            [_pmessage(redis_coord, "apps:changed", '{"reason": "before"}'),
             ConnectionError("connection reset by peer")],
            [_pmessage(redis_coord, "apps:changed", '{"reason": "after"}'), 30.0],
        ])

        async def run():
            await b.start_relay(handler)
            await asyncio.wait_for(delivered_twice.wait(), timeout=5)
            assert b.relay_alive is True
            await b.stop()

        asyncio.run(run())

        assert got == [("apps:changed", {"reason": "before"}),
                       ("apps:changed", {"reason": "after"})]
        assert b.relay_failures >= 1
        # It really re-subscribed rather than silently reusing a dead pubsub.
        assert len(pubsub.subscribes) >= 2
        assert pubsub.closed >= 1

    def test_listen_simply_ending_counts_as_a_failure_not_a_clean_exit(
            self, _clean_env, monkeypatch):
        """``listen()`` is an infinite generator over a live subscription, so
        it returning at all means the subscription is gone. Returning used to
        end the relay as if that were a normal shutdown."""
        redis_coord = _clean_env
        resubscribed = asyncio.Event()

        async def handler(topic, payload):
            resubscribed.set()

        b, pubsub = self._broadcaster(redis_coord, monkeypatch, [
            [],  # listen() returns immediately — no error, no messages
            [_pmessage(redis_coord, "apps:changed", "{}"), 30.0],
        ])

        async def run():
            await b.start_relay(handler)
            await asyncio.wait_for(resubscribed.wait(), timeout=5)
            await b.stop()

        asyncio.run(run())
        assert b.relay_failures >= 1
        assert len(pubsub.subscribes) >= 2

    def test_relay_alive_is_false_before_a_start_and_after_a_stop(
            self, _clean_env, monkeypatch):
        """The accessor ``src/apps/degradations.py`` reports on. A broadcaster
        whose relay has died looks healthy from the outside — ``publish()``
        still works, it only writes; it is DELIVERY that stopped."""
        redis_coord = _clean_env
        b, _ = self._broadcaster(redis_coord, monkeypatch, [[30.0]])
        assert b.relay_alive is False
        assert b.relay_failures == 0

        async def run():
            await b.start_relay(lambda topic, payload: asyncio.sleep(0))
            assert b.relay_alive is True
            await b.stop()
            assert b.relay_alive is False

        asyncio.run(run())

    def test_a_handler_that_raises_does_not_kill_the_relay(
            self, _clean_env, monkeypatch):
        """Unchanged behaviour, asserted because the reconnect loop is now
        wrapped around the same try: a bad handler must still be a logged
        per-message failure, not a reconnect and not a death."""
        redis_coord = _clean_env
        seen: list[str] = []
        both = asyncio.Event()

        async def handler(topic, payload):
            seen.append(payload["n"])
            if len(seen) == 2:
                both.set()
            raise RuntimeError("handler blew up")

        b, pubsub = self._broadcaster(redis_coord, monkeypatch, [
            [_pmessage(redis_coord, "apps:changed", '{"n": "a"}'),
             _pmessage(redis_coord, "apps:changed", '{"n": "b"}'), 30.0],
        ])

        async def run():
            await b.start_relay(handler)
            await asyncio.wait_for(both.wait(), timeout=5)
            await b.stop()

        asyncio.run(run())
        assert seen == ["a", "b"]
        assert len(pubsub.subscribes) == 1  # no reconnect over a handler error


class _FakeClient:
    def __init__(self, pubsub):
        self._pubsub = pubsub

    def pubsub(self):
        return self._pubsub

    async def aclose(self):
        pass


class TestGetRedisPool:
    def test_same_url_returns_the_same_cached_client(self, _clean_env):
        a = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        b = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        assert a is b

    def test_different_urls_return_different_clients(self, _clean_env):
        a = _clean_env.get_redis_pool("redis://127.0.0.1:6379/0")
        b = _clean_env.get_redis_pool("redis://127.0.0.1:6379/1")
        assert a is not b

    def test_defaults_to_the_workspace_redis_url(self, _clean_env, monkeypatch):
        monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://127.0.0.1:6379/3")
        pool = _clean_env.get_redis_pool()
        assert pool is _clean_env.get_redis_pool("redis://127.0.0.1:6379/3")
