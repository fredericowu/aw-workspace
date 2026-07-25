"""Redis-based coordination primitives for the aw-workspace runtime.

F5b: ported from `aw-backend/src/libs/redis_coord.py` (F0/F2/F3) onto the
per-workspace Redis companion provisioned by aw-backend's docker placement
driver (F5a) — one companion container per workspace, joined into the
workspace container's own netns and injected as `AW_WORKSPACE_REDIS_URL`
(deterministically `redis://127.0.0.1:6379` from inside the workspace
container; see `docker_driver.py`'s "Redis companion (F5a)" note in the
aw-backend repo). `get_workspace_redis_url()` falls back to that same
address when the env var isn't set (e.g. local dev against the repo's own
docker-compose, which currently shares aw-sandbox's netns and therefore the
*shared* `aw-redis` — fine for local exercising of this module, just not
workspace-isolated).

Unlike aw-backend's `aw:` namespace, every key here is additionally scoped
under `aw:ws:<AW_WORKSPACE>:` — the companion Redis is already isolated
per-workspace (F5a), so this isn't required for correctness, but it keeps
keys legible/greppable if a companion instance is ever inspected directly
or shared. This module provides the same four primitives as the source:

- `RedisBroadcaster` — pub/sub fan-out across workers/processes.
- `RedisLease` — single-leader election with automatic failover.
- `cooldown_acquire` — one-shot "claim this window" helper.
- `RedisPollQueue` — Redis Streams-backed event log (long-poll delivery).

Plus `get_redis_pool()` — a shared, per-URL-cached async client (bootstrap
helper) meant to be reused by upcoming sub-cards (F5d proxy, F5g terminal)
instead of each opening its own connection.

Key layout (all under `aw:ws:<workspace>:`):
    aw:ws:<ws>:bcast:<topic>    — pub/sub channels (RedisBroadcaster)
    aw:ws:<ws>:leader:<role>    — leader-election keys (RedisLease)
    aw:ws:<ws>:cooldown:<key>   — cooldown_acquire keys
    aw:ws:<ws>:mdpoll:<session> — long-poll event streams (RedisPollQueue)
    aw:ws:<ws>:mdpoll:epoch     — long-poll shared epoch (RedisPollQueue)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Awaitable, Callable, Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL_ENV = "AW_WORKSPACE_REDIS_URL"
WORKSPACE_ENV = "AW_WORKSPACE"

# Same address the F5a docker placement driver injects as
# AW_WORKSPACE_REDIS_URL — the workspace container always reaches its own
# Redis companion at 127.0.0.1:6379 inside its own netns.
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_WORKSPACE = "default"

POLL_STREAM_MAXLEN = 200

BroadcastHandler = Callable[[str, dict], Awaitable[None]]


def get_workspace_redis_url() -> str:
    """`AW_WORKSPACE_REDIS_URL` (set by F5a for every managed workspace
    container), falling back to `DEFAULT_REDIS_URL` — same 127.0.0.1:6379
    address the companion is deterministically reachable at, useful when
    running this module outside the managed placement driver (local dev,
    unit tests)."""
    return os.environ.get(REDIS_URL_ENV) or DEFAULT_REDIS_URL


def get_workspace_slug() -> str:
    """`AW_WORKSPACE` — the slug this process's Redis keys are scoped
    under. Falls back to `DEFAULT_WORKSPACE` rather than raising, since
    key-prefixing is a legibility aid here (the companion Redis is already
    isolated per-workspace by F5a), not a correctness boundary."""
    return os.environ.get(WORKSPACE_ENV) or DEFAULT_WORKSPACE


def _key_prefix() -> str:
    return f"aw:ws:{get_workspace_slug()}:"


def _bcast_prefix() -> str:
    return f"{_key_prefix()}bcast:"


def _leader_prefix() -> str:
    return f"{_key_prefix()}leader:"


def _cooldown_prefix() -> str:
    return f"{_key_prefix()}cooldown:"


def _poll_stream_prefix() -> str:
    return f"{_key_prefix()}mdpoll:"


def _poll_epoch_key() -> str:
    return f"{_poll_stream_prefix()}epoch"


# Renew-time CAS: only refresh the TTL if we're still the recorded owner.
# Prevents a straggler renew (e.g. delayed by GC/scheduling) from
# resurrecting a lease another process has since legitimately acquired.
_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Release-time CAS: only delete the key if we're still the owner, so a
# graceful stop() can never delete a lease someone else already holds.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


_pool_cache: dict[str, aioredis.Redis] = {}


def get_redis_pool(redis_url: Optional[str] = None) -> aioredis.Redis:
    """Shared, pool-backed `aioredis.Redis` client for `redis_url`
    (defaults to `get_workspace_redis_url()`), cached per URL so repeated
    calls across this process reuse the same underlying connection pool
    instead of each opening a new one — the bootstrap helper F5d (proxy)
    and F5g (terminal) are meant to call directly rather than hand-rolling
    their own `aioredis.from_url(...)`.

    Not used internally by `RedisBroadcaster`/`RedisLease`/`RedisPollQueue`
    below — those still own (and close) their own client, matching the
    ported aw-backend behavior 1:1. A shared, never-closed client here
    would break their `stop()`/`close()` semantics for every other caller.
    """
    url = redis_url or get_workspace_redis_url()
    client = _pool_cache.get(url)
    if client is None:
        client = aioredis.from_url(url, decode_responses=True)
        _pool_cache[url] = client
    return client


class RedisBroadcaster:
    """Fan-out pub/sub across aw-workspace worker processes.

    Single delivery path, no dedup: `publish()` only PUBLISHes to Redis —
    it never invokes the local handler directly. Every worker (including
    the one that published) receives the message exclusively through its
    own PSUBSCRIBE relay task started by `start_relay()`. That's the only
    way a handler ever runs, so there's no separate "local" vs "remote"
    branch to keep in sync.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url or get_workspace_redis_url()
        self._client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._relay_task: Optional[asyncio.Task] = None

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def publish(self, topic: str, payload: dict) -> int:
        """PUBLISH `payload` (JSON-encoded) to `aw:ws:<ws>:bcast:<topic>`.

        Returns the number of subscribers Redis delivered to (0 if none —
        including this same process, if its relay isn't running yet).
        """
        client = self._get_client()
        return await client.publish(f"{_bcast_prefix()}{topic}", json.dumps(payload))

    async def start_relay(self, handler: BroadcastHandler) -> None:
        """Start the PSUBSCRIBE relay that delivers every broadcast to `handler(topic, payload)`."""
        if self._relay_task is not None:
            raise RuntimeError("relay already started")
        client = self._get_client()
        self._pubsub = client.pubsub()
        await self._pubsub.psubscribe(f"{_bcast_prefix()}*")
        self._relay_task = asyncio.create_task(self._relay_loop(handler))

    async def _relay_loop(self, handler: BroadcastHandler) -> None:
        assert self._pubsub is not None
        prefix = _bcast_prefix()
        try:
            async for message in self._pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                topic = message["channel"][len(prefix):]
                try:
                    payload = json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("redis_coord: dropping malformed broadcast on %s", message["channel"])
                    continue
                try:
                    await handler(topic, payload)
                except Exception:
                    logger.exception("redis_coord: broadcast handler raised for topic=%s", topic)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._relay_task is not None:
            self._relay_task.cancel()
            try:
                await self._relay_task
            except asyncio.CancelledError:
                pass
            self._relay_task = None
        if self._pubsub is not None:
            await self._pubsub.punsubscribe(f"{_bcast_prefix()}*")
            await self._pubsub.aclose()
            self._pubsub = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class RedisLease:
    """Single-leader election for one `role` name, backed by
    `aw:ws:<ws>:leader:<role>`.

    - Acquire: `SET aw:ws:<ws>:leader:<role> <token> NX PX <ttl*1000>` —
      only one process ever wins this for a given role at a time (Redis
      key exclusivity), win propagates via `on_acquire`.
    - Hold: every `renew` seconds, a Lua CAS refreshes the TTL only if the
      key still holds our own `token` — a process that lost the lease
      (key expired and someone else grabbed it) detects that on its next
      renew attempt and fires `on_release`.
    - Failover: if the leader dies without releasing (crash, kill -9), the
      key simply expires after `ttl` seconds with no renewal, and the next
      process's acquire attempt succeeds — bounded failover time of `ttl`.
    """

    def __init__(
        self,
        role: str,
        redis_url: Optional[str] = None,
        ttl: float = 15.0,
        renew: float = 5.0,
        on_acquire: Optional[Callable[[], Awaitable[None]]] = None,
        on_release: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.role = role
        self.ttl = ttl
        self.renew_interval = renew
        self.token = uuid.uuid4().hex
        self._redis_url = redis_url or get_workspace_redis_url()
        self._key = f"{_leader_prefix()}{role}"
        self._on_acquire = on_acquire
        self._on_release = on_release
        self._client: Optional[aioredis.Redis] = None
        self._task: Optional[asyncio.Task] = None
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("lease already started")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._is_leader:
            await self._release()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        try:
            while True:
                if not self._is_leader:
                    await self._try_acquire()
                else:
                    await self._try_renew()
                await asyncio.sleep(self.renew_interval)
        except asyncio.CancelledError:
            raise

    async def _try_acquire(self) -> None:
        client = self._get_client()
        won = await client.set(self._key, self.token, nx=True, px=int(self.ttl * 1000))
        if won:
            self._is_leader = True
            logger.info("redis_coord: role=%s acquired by token=%s", self.role, self.token)
            if self._on_acquire is not None:
                await self._on_acquire()

    async def _try_renew(self) -> None:
        client = self._get_client()
        renewed = await client.eval(_RENEW_LUA, 1, self._key, self.token, int(self.ttl * 1000))
        if not renewed:
            self._is_leader = False
            logger.warning("redis_coord: role=%s lost by token=%s", self.role, self.token)
            if self._on_release is not None:
                await self._on_release()

    async def _release(self) -> None:
        client = self._get_client()
        await client.eval(_RELEASE_LUA, 1, self._key, self.token)
        self._is_leader = False
        if self._on_release is not None:
            await self._on_release()


async def cooldown_acquire(key: str, seconds: float, redis_url: Optional[str] = None) -> bool:
    """Atomically claim a cooldown window for `key`.

    Returns True if this call won it (the key didn't already exist — it's
    now set with a `seconds`-long expiry), False if someone already holds
    the cooldown. One-shot: there's no release, it just expires.
    """
    client = aioredis.from_url(redis_url or get_workspace_redis_url(), decode_responses=True)
    try:
        won = await client.set(f"{_cooldown_prefix()}{key}", "1", nx=True, ex=max(1, int(seconds)))
        return bool(won)
    finally:
        await client.aclose()


_LEGACY_INT_CURSOR_RE = re.compile(r"^-?\d+$")


def is_legacy_poll_cursor(cursor: str) -> bool:
    """True for a pre-migration long-poll cursor — a plain integer rather
    than a Redis stream id ("<ms>-<seq>", e.g. "1690000000000-0"). A
    cursor in this shape can't be resolved against `RedisPollQueue`'s
    streams, so the caller must treat it as a reset."""
    return bool(cursor) and bool(_LEGACY_INT_CURSOR_RE.match(cursor))


class RedisPollQueue:
    """Redis Streams-backed event log for a session-scoped long-poll sync
    (the aw-workspace analog of aw-backend's `/api/meta/poll` — consumers
    land in F5d/F5g).

    - `aw:ws:<ws>:mdpoll:<session_id>` — one Redis Stream per session,
      `XADD`-fed, trimmed to ~`maxlen` entries (`MAXLEN ~ maxlen`,
      approximate — exact trimming isn't needed, this is a rolling event
      log, not durable history).
    - Cursor = the Redis stream id of the last event delivered (opaque
      string from the client's perspective, round-tripped as-is — see
      `is_legacy_poll_cursor` for detecting a pre-migration int cursor).
    - `XREAD BLOCK` does both jobs the old code needed two code paths for:
      returns already-buffered entries immediately if any exist past the
      cursor, otherwise blocks up to `block_ms` and returns whatever
      arrived (or an empty read on timeout) — no separate "check buffer,
      then register a waiter" dance.
    """

    def __init__(self, redis_url: Optional[str] = None, maxlen: int = POLL_STREAM_MAXLEN):
        self._redis_url = redis_url or get_workspace_redis_url()
        self._maxlen = maxlen
        self._client: Optional[aioredis.Redis] = None
        self._epoch: Optional[str] = None

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    @staticmethod
    def _stream_key(session_id: str) -> str:
        return f"{_poll_stream_prefix()}{session_id}"

    async def get_epoch(self) -> str:
        """Shared epoch across every worker/restart, `SET aw:ws:<ws>:mdpoll:epoch
        NX` on first-ever call anywhere (any process) — after that every
        process just reads the same value back. A client whose `epoch`
        doesn't match must treat its cursor as unusable (see `read_new`
        callers). Cached locally after the first lookup — the key is
        effectively immutable once created, so there's no point
        re-fetching it every poll."""
        if self._epoch is not None:
            return self._epoch
        client = self._get_client()
        await client.set(_poll_epoch_key(), uuid.uuid4().hex[:8], nx=True)
        self._epoch = await client.get(_poll_epoch_key())
        return self._epoch

    async def enqueue(self, session_id: str, payload: dict) -> str:
        """`XADD` payload onto the session's stream (`MAXLEN ~ self._maxlen`).
        Returns the new entry's Redis stream id."""
        client = self._get_client()
        return await client.xadd(
            self._stream_key(session_id),
            {"payload": json.dumps(payload)},
            maxlen=self._maxlen,
            approximate=True,
        )

    async def last_id(self, session_id: str) -> str:
        """Id of the most recent entry in the session's stream, or "0" if
        the stream is empty/doesn't exist yet — the "start from now, skip
        the backlog" cursor used on reset."""
        client = self._get_client()
        entries = await client.xrevrange(self._stream_key(session_id), count=1)
        return entries[0][0] if entries else "0"

    async def read_new(self, session_id: str, after_id: str, block_ms: int) -> list[tuple[str, dict]]:
        """`XREAD BLOCK <block_ms> STREAMS <stream> <after_id>` — entries
        with an id greater than `after_id`, immediately if any are already
        there, else held open up to `block_ms` (returns `[]` on timeout)."""
        client = self._get_client()
        result = await client.xread(
            {self._stream_key(session_id): after_id or "0"},
            count=self._maxlen,
            block=max(1, block_ms),
        )
        if not result:
            return []
        _stream_name, entries = result[0]
        return [(entry_id, json.loads(fields["payload"])) for entry_id, fields in entries]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
