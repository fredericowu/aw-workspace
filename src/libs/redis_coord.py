"""Redis-based coordination primitives for the aw-workspace runtime.

F5b: ported from `aw-backend/src/libs/redis_coord.py` (F0/F2/F3) onto the
per-workspace Redis companion provisioned by aw-backend's docker placement
driver (F5a) — one companion container per workspace, joined into the
workspace container's own netns and injected as `AW_WORKSPACE_REDIS_URL`
(deterministically `redis://127.0.0.1:6379` from inside the workspace
container; see `docker_driver.py`'s "Redis companion (F5a)" note in the
aw-backend repo).

F5a (the card that would provision that companion) is blocked in Need
Human, so no environment running this module today actually has
`AW_WORKSPACE_REDIS_URL` set, and the hardcoded `127.0.0.1:6379` default
is dead everywhere except a local dev box sharing aw-sandbox's netns —
`get_workspace_redis_url()` would otherwise resolve to an address nothing
is listening on, silently, since this module had (and has) zero
consumers to notice. W0 adds `AW_REDIS_URL` as a second fallback: the
shared Redis this environment actually has reachable (e.g.
`redis://172.18.0.1:6379/1`, the docker bridge gateway — see
`get_workspace_redis_url()` for the full resolution order). Not
workspace-isolated the way a real per-workspace companion would be (see
the key-scoping note below), but real and alive, unlike the hardcoded
default.

Unlike aw-backend's `aw:` namespace, every key here is additionally scoped
under `aw:ws:<AW_WORKSPACE>:` — the companion Redis is already isolated
per-workspace (F5a), so this isn't required for correctness, but it keeps
keys legible/greppable if a companion instance is ever inspected directly
or shared. This module provides two of the four primitives in the source:

- `RedisBroadcaster` — pub/sub fan-out across workers/processes.
- `RedisPollQueue` — Redis Streams-backed event log (long-poll delivery).

**Leadership deliberately does NOT live here any more.** `RedisLease`
(single-leader election) and `cooldown_acquire` (one-shot "claim this
window") were deleted on 2026-10-03 — every leader decision in this
codebase is now an `fcntl.flock`, and only an flock: see
`src/apps/fs_lock.py`'s `FlockLease` (watchdog tasks),
`src/apps/boot_reconcile_coord.py` (boot reconcile), and the one-shot claim
in `src/api/app.py`'s lifespan (marketplace sources). The reason is the
paragraph above about F5a: with this Redis unreachable by default, a
`SET NX` that raised carried no fact about who held the key, so every
caller needed a degrade-open branch — and that branch was the live
behaviour, confirmed by 45/45 samples of `GET /api/apps/-/watchdog`
reporting no leader at all. What is left here is messaging and fan-out,
which is genuinely Redis-shaped: a flock cannot deliver a message to
another process, and a broadcast that silently doesn't arrive degrades to
"converge on the next restart" rather than to "two processes both think
they own this".

(`src/apps/service_lease.py` is the one remaining Redis-primary ownership
decision in core. It is NOT an oversight — its `is_lease_held_anywhere`
needs a remote READ of "does anyone hold this", which flock only answers
via a momentary acquire-probe. Tracked as its own follow-up card.)

Plus `get_redis_pool()` — a shared, per-URL-cached async client (bootstrap
helper) meant to be reused by upcoming sub-cards (F5d proxy, F5g terminal)
instead of each opening its own connection.

Key layout (all under `aw:ws:<workspace>:`):
    aw:ws:<ws>:bcast:<topic>    — pub/sub channels (RedisBroadcaster)
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
SHARED_REDIS_URL_ENV = "AW_REDIS_URL"
WORKSPACE_ENV = "AW_WORKSPACE"

# Same address the F5a docker placement driver injects as
# AW_WORKSPACE_REDIS_URL — the workspace container always reaches its own
# Redis companion at 127.0.0.1:6379 inside its own netns. Last-resort
# fallback only: dead in any environment with no companion and no
# AW_REDIS_URL set either (see get_workspace_redis_url()).
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_WORKSPACE = "default"

POLL_STREAM_MAXLEN = 200

#: Relay reconnect backoff — doubling from the first to the second, so a
#: momentary blip (a Redis restart, the 2026-10-12 auth rollout) costs one
#: second of missed fan-out rather than the rest of the process's life, while a
#: genuinely dead Redis settles into one attempt every half minute instead of a
#: hot loop. Deliberately NOT an attempt ceiling: a relay that gives up is the
#: exact silent death this backoff exists to end.
RELAY_BACKOFF_START_S = 1.0
RELAY_BACKOFF_MAX_S = 30.0

BroadcastHandler = Callable[[str, dict], Awaitable[None]]


def get_workspace_redis_url() -> str:
    """Resolution order, most specific (and most isolated) first:

    1. `AW_WORKSPACE_REDIS_URL` — the per-workspace companion F5a's docker
       placement driver injects when it exists. Not provisioned in any
       environment today (F5a is blocked in Need Human).
    2. `AW_REDIS_URL` — the shared Redis this environment actually has
       reachable. Not workspace-isolated on its own (see the module
       docstring), which is why every key this module writes is still
       scoped under `aw:ws:<AW_WORKSPACE>:`.
    3. `DEFAULT_REDIS_URL` — hardcoded guess at the companion's address,
       only correct for local dev sharing aw-sandbox's netns.
    """
    return (
        os.environ.get(REDIS_URL_ENV)
        or os.environ.get(SHARED_REDIS_URL_ENV)
        or DEFAULT_REDIS_URL
    )


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


def _poll_stream_prefix() -> str:
    return f"{_key_prefix()}mdpoll:"


def _poll_epoch_key() -> str:
    return f"{_poll_stream_prefix()}epoch"


_pool_cache: dict[str, aioredis.Redis] = {}


def get_redis_pool(redis_url: Optional[str] = None) -> aioredis.Redis:
    """Shared, pool-backed `aioredis.Redis` client for `redis_url`
    (defaults to `get_workspace_redis_url()`), cached per URL so repeated
    calls across this process reuse the same underlying connection pool
    instead of each opening a new one — the bootstrap helper F5d (proxy)
    and F5g (terminal) are meant to call directly rather than hand-rolling
    their own `aioredis.from_url(...)`.

    Not used internally by `RedisBroadcaster`/`RedisPollQueue` below —
    those still own (and close) their own client, matching the
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

    That single path is also a single point of silent failure, which is why
    `_relay_loop` reconnects: until 2026-10-17 it caught only
    `asyncio.CancelledError`, so any `ConnectionError` out of
    `pubsub.listen()` (a Redis restart, an auth change) killed the relay task
    outright — and with it every cross-worker effect that rides this
    broadcaster, for the rest of that process's life, with nothing in any log
    but an unretrieved task exception. See `relay_alive`/`relay_failures` and
    `src/apps/degradations.py` for the other half of that fix: a dead relay is
    a reported degradation now, not an invisible one.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._redis_url = redis_url or get_workspace_redis_url()
        self._client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._relay_task: Optional[asyncio.Task] = None
        self._relay_failures = 0

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
        """Start the PSUBSCRIBE relay that delivers every broadcast to `handler(topic, payload)`.

        Still raises if the FIRST subscribe cannot be made — the caller
        (`AppLifecycle.start`) reports that honestly as "this worker has no
        cross-worker convergence" rather than pretending to be subscribed.
        Once the relay is up, every LATER failure is the loop's to retry.
        """
        if self._relay_task is not None:
            raise RuntimeError("relay already started")
        client = self._get_client()
        self._pubsub = client.pubsub()
        await self._pubsub.psubscribe(f"{_bcast_prefix()}*")
        self._relay_task = asyncio.create_task(self._relay_loop(handler))

    @property
    def relay_alive(self) -> bool:
        """True while this process's relay task exists and has not finished.

        The one fact a health check needs and could not previously get: a
        broadcaster whose relay has died looks exactly like a healthy one from
        the outside — `publish()` keeps working (it only writes), it is
        *delivery* that stopped."""
        return self._relay_task is not None and not self._relay_task.done()

    @property
    def relay_failures(self) -> int:
        """How many times the relay has had to reconnect. Non-zero is not
        itself a problem (one blip, recovered); a number that keeps climbing
        is a Redis the reconnect loop is politely hiding — see
        ``src/apps/degradations.py``."""
        return self._relay_failures

    async def _relay_loop(self, handler: BroadcastHandler) -> None:
        pattern = f"{_bcast_prefix()}*"
        backoff = RELAY_BACKOFF_START_S
        try:
            while True:
                try:
                    if self._pubsub is None:
                        client = self._get_client()
                        self._pubsub = client.pubsub()
                        await self._pubsub.psubscribe(pattern)
                        logger.warning("redis_coord: relay re-subscribed to %s after "
                                       "%d failure(s)", pattern, self._relay_failures)
                    prefix = _bcast_prefix()
                    async for message in self._pubsub.listen():
                        # A delivered message is proof the connection works, so
                        # the next blip starts its backoff from the bottom again
                        # rather than inheriting a long-dead incident's delay.
                        backoff = RELAY_BACKOFF_START_S
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
                    # listen() is an infinite generator over a live
                    # subscription; it returning at all means the subscription
                    # is gone (connection closed under us). Treated as the
                    # failure it is, not as a clean exit — a `return` here is
                    # how the relay used to die silently.
                    raise ConnectionError("pubsub.listen() ended — subscription lost")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._relay_failures += 1
                    logger.exception(
                        "redis_coord: relay on %s dropped (failure #%d) — "
                        "reconnecting in %.0fs. Every broadcast is MISSED until it "
                        "is back (no replay): this worker will not be told about "
                        "app installs or config saves served elsewhere.",
                        pattern, self._relay_failures, backoff)
                    pubsub, self._pubsub = self._pubsub, None
                    if pubsub is not None:
                        try:
                            await pubsub.aclose()
                        except Exception:  # noqa: BLE001 — it is already broken
                            pass
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RELAY_BACKOFF_MAX_S)
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
