"""``ctx.state`` — a gated primitive for an app's own cross-worker state.

Design: ``docs/design/app-shared-state-facade.md`` (approved 2026-09-30,
§11 amendment 2026-10-09). Gated by the ``state:own`` capability
(:mod:`src.apps.capabilities`). Exactly three operations, no more:

* ``kv`` — TTL'd put/get/scan/delete, Redis-backed
  (:class:`~src.libs.redis_coord.RedisBroadcaster`'s module, same client).
* ``lease`` — named, app-owned mutual exclusion, **flock-backed**
  (:mod:`src.apps.fs_lock`), NOT Redis. The design originally cited
  ``redis_coord.RedisLease`` for this, but that class was deleted
  2026-10-03 (commit ``11b9109``) when core leadership became flock-only —
  see §11 of the design doc for the full ruling. kv/broadcast are
  unaffected: the line ``redis_coord.py``'s own docstring draws is
  "messaging degrades to staleness; ownership degrades to two processes
  both thinking they own it", and only the latter half moved off Redis.
* ``broadcast`` — publish/subscribe, Redis-backed
  (:class:`~src.libs.redis_coord.RedisBroadcaster`).

Deliberately NOT included: raw client access, cross-app/core-key reads,
SCAN outside the app's own prefix, arbitrary Lua, ``RedisPollQueue``.

Key/topic construction, not a startswith-check. Unlike
:func:`src.apps.db_tables._validate` (which CHECKS a caller-supplied full
table name already carries ``app__<slug>__``, because the app spells that
name into raw SQL it owns), every kv/broadcast method here takes a bare
local name and CONSTRUCTS the full Redis key/topic by concatenating a
prefix the caller never supplies with a validated identifier. ``prefix +
x`` always starts with ``prefix`` regardless of what ``x`` contains, so
there is no argument shape that can reach a key outside
``aw:ws:<ws>:app:<app_id>:`` — a stronger boundary than a check that could
itself have a bug, appropriate here since callers never need to spell the
prefix themselves the way ``ctx.db`` callers spell table names into SQL.
Lease lock paths are built the same way, under their own per-app
subdirectory of ``fs_lock.lock_dir()``.

Redis-down semantics (design §6, kv/broadcast only): every operation
degrades OPEN behind a process-wide circuit breaker (``_breaker`` — the
underlying Redis connection is one shared resource, not per-app, so
"degraded" is a single fact per worker process, not per facade instance).
kv reads miss, kv writes drop, broadcast publishes drop with no
local-loopback fallback (a loopback would make WORKERS=1 dev look correct
while N>1 production silently isn't — the exact bug class this whole
facade exists to stop). The transition into degraded logs at WARNING once
per cooldown window; recovery logs at INFO; individual failures inside an
already-degraded window stay at DEBUG. See ``breaker_status()`` for what
``aw-workspace-cli doctor`` reports.

``lease`` has NO degrade mode (design §11): ``fcntl.flock`` has no
reachability failure — ``EAGAIN`` IS the fact that someone else holds it,
same as ``fs_lock``'s own leadership primitives. A broken lock directory
(read-only, full) raises, same ruling as ``fs_lock._try_acquire``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from src.apps import fs_lock

if TYPE_CHECKING:
    from src.apps.base import AppContext

log = logging.getLogger(__name__)

#: Bare local name/topic an app passes in — never a full key. 1-128 chars,
#: lowercase/digits/._- only. Deliberately restrictive (no ':', no '/', no
#: whitespace) so keys stay legible and nothing in the name can be mistaken
#: for a structural separator by anything that later greps the keyspace.
_IDENT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")

#: Same shape as architecture_app/jobs.py's _BREAKER_COOLDOWN_S — long enough
#: that a dead Redis doesn't get hammered every call, short enough that a
#: real recovery is noticed within a minute. kv/broadcast only — lease has
#: no breaker (see module docstring).
_BREAKER_COOLDOWN_S = 30.0


class StateKeyError(ValueError):
    """A key, topic, lease name, or TTL failed validation."""


# --- validation --------------------------------------------------------------

def _validate_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise StateKeyError(
            f"{kind} name {name!r} must match {_IDENT_RE.pattern!r} "
            f"(lowercase letters/digits/._- , 1-128 chars)")
    return name


def kv_key(app_id: str, name: str) -> str:
    """``aw:ws:<ws>:app:<app_id>:kv:<name>`` — the only key shape this
    module ever writes to or reads from for the kv namespace."""
    from src.libs.redis_coord import _key_prefix
    _validate_name("kv", name)
    return f"{_key_prefix()}app:{app_id}:kv:{name}"


def kv_prefix(app_id: str) -> str:
    from src.libs.redis_coord import _key_prefix
    return f"{_key_prefix()}app:{app_id}:kv:"


def broadcast_topic(app_id: str, topic: str) -> str:
    """``app:<app_id>:<topic>`` — forced under the app's own namespace,
    within the ``bcast:`` prefix :class:`~src.libs.redis_coord.RedisBroadcaster`
    already adds (redis_coord.py's ``_bcast_prefix()``)."""
    _validate_name("broadcast topic", topic)
    return f"app:{app_id}:{topic}"


def lease_path(app_id: str, name: str, lock_dir: Optional[str] = None) -> str:
    """``<lock_dir or fs_lock.lock_dir()>/apps/<app_id>/<name>.lock`` — its
    own subdirectory per app so an app can never collide with a core lock
    name (``locks/<name>.lock``, flat) or another app's. ``lock_dir``
    overrides the base directory — same test-isolation shape as
    ``FlockLease(lock_dir=...)``, never ``AW_APPS_LOCK_DIR`` mutation."""
    _validate_name("lease", name)
    app_dir = os.path.join(lock_dir or fs_lock.lock_dir(), "apps", app_id)
    return fs_lock.lock_path(name, app_dir)


# --- circuit breaker (process-wide, shared by every app's kv/broadcast) ----

class _StateBreaker:
    """One breaker per worker process, not per app — the Redis connection
    underneath every ``ctx.state`` instance in this process is the same
    shared resource, so "degraded" is a single fact, the same way
    architecture_app/jobs.py's ``_breaker_until`` was one fact for every job
    in that process. Edge-triggered: a WARNING fires on the transition INTO
    degraded, not on every failed op (log flood) and not never (the
    all-DEBUG silence this replaces, jobs.py:178,189,218). kv/broadcast
    only — lease (flock) has no degrade mode."""

    def __init__(self) -> None:
        self._until = 0.0
        self._since: Optional[float] = None

    @property
    def degraded(self) -> bool:
        return time.time() < self._until

    @property
    def degraded_since(self) -> Optional[float]:
        return self._since if self.degraded else None

    def trip(self, exc: Exception, op: str) -> None:
        was_degraded = self.degraded
        self._until = time.time() + _BREAKER_COOLDOWN_S
        if not was_degraded:
            self._since = time.time()
            log.warning("ctx.state: Redis %s failed — degrading for %.0fs: %s",
                        op, _BREAKER_COOLDOWN_S, exc)
        else:
            log.debug("ctx.state: Redis %s failed during an already-degraded "
                      "window: %s", op, exc)

    def on_success(self) -> None:
        if self._since is not None:
            log.info("ctx.state: Redis reachable again (was degraded since %.0f)",
                     self._since)
        self._until = 0.0
        self._since = None


_breaker = _StateBreaker()
_client: Any = None


def breaker_status() -> dict[str, Any]:
    """Snapshot for ``doctor`` — not a live probe (``_redis_coord_status``
    in ``src/apps/routes.py`` already does the live PING for the generic
    ``redis`` doctor section; this reports whether THIS facade's own
    breaker is presently open in this worker). kv/broadcast only."""
    return {"degraded": _breaker.degraded, "since": _breaker.degraded_since}


def _get_client():
    """Lazy, cached — connect-on-first-use so a Redis outage at app load
    time cannot turn into an app-load failure (design §9: "Facade
    activation must not require Redis")."""
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        from src.libs.redis_coord import get_workspace_redis_url
        _client = aioredis.from_url(get_workspace_redis_url(), decode_responses=True)
    return _client


# --- kv namespace ------------------------------------------------------------

class _KvNamespace:
    """``ctx.state.kv`` — TTL'd put/get/scan/delete under the app's own
    ``kv:`` namespace. TTL is required on every put: every real call site
    this facade replaces treats its state as transient (a finished job is a
    UI concern, not a durable record — install_jobs.py's rule), so there is
    no way to write an immortal key through this facade."""

    def __init__(self, ctx: "AppContext") -> None:
        self._ctx = ctx

    async def put(self, name: str, value: Any, ttl: float) -> None:
        self._ctx._enforce("state:own")
        if not ttl or ttl <= 0:
            raise StateKeyError("ttl is required and must be > 0 — ctx.state.kv "
                                "has no immortal keys")
        key = kv_key(self._ctx.app_id, name)
        try:
            client = _get_client()
            await client.set(key, json.dumps(value), ex=int(ttl))
        except Exception as exc:  # noqa: BLE001 — degrade open, see module docstring
            _breaker.trip(exc, "kv.put")
            return
        _breaker.on_success()

    async def get(self, name: str) -> Any | None:
        self._ctx._enforce("state:own")
        key = kv_key(self._ctx.app_id, name)
        try:
            client = _get_client()
            raw = await client.get(key)
        except Exception as exc:  # noqa: BLE001
            _breaker.trip(exc, "kv.get")
            return None
        _breaker.on_success()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            log.debug("ctx.state: malformed kv entry at %s", key)
            return None

    async def delete(self, name: str) -> None:
        self._ctx._enforce("state:own")
        key = kv_key(self._ctx.app_id, name)
        try:
            client = _get_client()
            await client.delete(key)
        except Exception as exc:  # noqa: BLE001
            _breaker.trip(exc, "kv.delete")
            return
        _breaker.on_success()

    async def scan(self, prefix: str = "") -> list[str]:
        """Bare names (not full keys) under ``prefix`` — bounded to this
        app's own ``kv:`` namespace by construction; there is no ``prefix``
        value that reaches outside it, because ``base`` below is fixed."""
        self._ctx._enforce("state:own")
        if prefix:
            _validate_name("kv", prefix)
        base = kv_prefix(self._ctx.app_id)
        pattern = f"{base}{prefix}*"
        names: list[str] = []
        try:
            client = _get_client()
            async for key in client.scan_iter(match=pattern):
                names.append(key[len(base):])
        except Exception as exc:  # noqa: BLE001
            _breaker.trip(exc, "kv.scan")
            return []
        _breaker.on_success()
        return names


# --- broadcast namespace -----------------------------------------------------

class _BroadcastNamespace:
    """``ctx.state.broadcast`` — publish/subscribe over
    :class:`~src.libs.redis_coord.RedisBroadcaster`, topics forced under the
    app's own namespace. Best-effort: a publish during a Redis outage is
    dropped, with NO local-loopback fallback (design §6) — a local-only
    delivery path would make single-worker dev look correct while
    multi-worker production silently isn't, which is this entire bug class
    wearing a new hat.
    """

    def __init__(self, ctx: "AppContext") -> None:
        self._ctx = ctx
        self._broadcaster: Any = None
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

    async def publish(self, topic: str, payload: dict) -> None:
        self._ctx._enforce("state:own")
        full_topic = broadcast_topic(self._ctx.app_id, topic)
        try:
            from src.libs.redis_coord import RedisBroadcaster
            if self._broadcaster is None:
                self._broadcaster = RedisBroadcaster()
            await self._broadcaster.publish(full_topic, payload)
        except Exception as exc:  # noqa: BLE001
            _breaker.trip(exc, "broadcast.publish")
            return
        _breaker.on_success()

    async def subscribe(self, topic: str,
                        handler: Callable[[dict], Awaitable[None]]) -> None:
        self._ctx._enforce("state:own")
        full_topic = broadcast_topic(self._ctx.app_id, topic)
        self._handlers[full_topic] = handler
        from src.libs.redis_coord import RedisBroadcaster
        if self._broadcaster is None:
            self._broadcaster = RedisBroadcaster()
        if getattr(self._broadcaster, "_relay_task", None) is None:
            try:
                await self._broadcaster.start_relay(self._on_relay_message)
            except Exception as exc:  # noqa: BLE001
                _breaker.trip(exc, "broadcast.subscribe")
                return
            _breaker.on_success()

    async def _on_relay_message(self, topic: str, payload: dict) -> None:
        handler = self._handlers.get(topic)
        if handler is not None:
            await handler(payload)

    async def aclose(self) -> None:
        if self._broadcaster is not None:
            try:
                await self._broadcaster.stop()
            except Exception:  # noqa: BLE001 — shutdown path
                log.debug("ctx.state: broadcast teardown raised", exc_info=True)


# --- lease namespace (flock-backed, design §11 amendment) -------------------

class _LeaseNamespace:
    """``ctx.state.lease`` — named, app-owned mutual exclusion, flock-backed
    (design §11, 2026-10-09 amendment — NOT Redis; see module docstring).

    One-shot, non-blocking claim: a worker that loses simply stands by, the
    same contract as ``fs_lock.try_acquire`` — leadership is decided by one
    try, not by waiting. No ``ttl``, no ``renew``: the held
    open-file-description IS the liveness proof, dropped by the kernel on
    exit/crash/kill. These are plain synchronous syscalls (open + flock),
    O(1) — deliberately NOT wrapped in ``asyncio.to_thread``, same as every
    other ``fs_lock`` caller.
    """

    def __init__(self, ctx: "AppContext", lock_dir: Optional[str] = None) -> None:
        self._ctx = ctx
        self._lock_dir = lock_dir
        self._held: dict[str, int] = {}

    def _path(self, name: str) -> str:
        return lease_path(self._ctx.app_id, name, self._lock_dir)

    def claim(self, name: str) -> bool:
        """Non-blocking. ``True`` if this worker now holds (or already
        held) ``name``; ``False`` if another worker holds it right now."""
        self._ctx._enforce("state:own")
        if name in self._held:
            return True
        fd = fs_lock.try_acquire(self._path(name))
        if fd is None:
            return False
        self._held[name] = fd
        return True

    def release(self, name: str) -> None:
        self._ctx._enforce("state:own")
        fd = self._held.pop(name, None)
        fs_lock.release(fd)

    def is_held(self, name: str) -> bool:
        """True if SOME worker (this one or another) currently holds
        ``name``. An acquire-probe when this worker isn't the holder
        itself: wins it, immediately releases it, and reports False — the
        same microsecond-hold limitation ``service_lease.py`` already
        documents for the identical shape."""
        self._ctx._enforce("state:own")
        if name in self._held:
            return True
        fd = fs_lock.try_acquire(self._path(name))
        if fd is None:
            return True
        fs_lock.release(fd)
        return False

    def release_all(self) -> None:
        """Called from the app's deactivate hook — a held fd surviving a
        reload would strand the lease in a process whose app is no longer
        active."""
        for fd in self._held.values():
            fs_lock.release(fd)
        self._held.clear()


# --- doctor §4.3: facade-bypass detection ------------------------------------

#: Apps with a ruled exemption (design §5) — checked BEFORE scanning, so
#: they are never even walked, let alone flagged. Every entry needs its
#: reason kept right here, next to the exemption it justifies — the
#: allowlist is itself a drift risk the design calls out (§8).
BYPASS_ALLOWLIST: dict[str, str] = {
    "agents-platform-runners": (
        "warm_pool.py/execute.py/execution_index.py talk to agents-platform's "
        "OWN Redis via the shared_redis_url secret — a different instance, "
        "different keyspace, cross-SERVICE coordination with an external "
        "system, not workspace cross-worker state. See design doc "
        "app-shared-state-facade.md §5."
    ),
}

_BYPASS_PATTERN = re.compile(
    r"^\s*(?:from\s+src\.libs\.redis_coord\s+import\b"
    r"|from\s+src\.libs\s+import\s+redis_coord\b"
    r"|import\s+src\.libs\.redis_coord\b)",
    re.MULTILINE,
)

_SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def bypass_report(runtime) -> list[dict[str, str]]:
    """Every installed Tier-1 (``inprocess``) app file that imports
    ``src.libs.redis_coord`` directly instead of going through ``ctx.state``
    — path-based, cheap, same attribution philosophy as the architecture
    app's path-based test attribution (design §4.3). Apps on
    :data:`BYPASS_ALLOWLIST` are skipped entirely, not merely excluded from
    the count.
    """
    hits: list[dict[str, str]] = []
    for slug in runtime.loaded_slugs():
        if slug in BYPASS_ALLOWLIST:
            continue
        loaded = runtime.get(slug)
        if loaded is None or loaded.manifest.tier != "inprocess":
            continue
        package_dir = loaded.package_dir
        for root, dirs, files in os.walk(package_dir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    continue
                if _BYPASS_PATTERN.search(text):
                    hits.append({
                        "app": slug,
                        "file": os.path.relpath(path, package_dir),
                    })
    return hits
