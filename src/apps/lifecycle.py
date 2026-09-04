"""W3: the one seam every app-lifecycle side effect has to pick a side of.

An app's **desired** state is shared (the ``AppInstall`` rows in Postgres —
``src/apps/reconciler.py``'s ``LocalMirror``). Its **loaded** state is not:
``AppRuntime._apps``, the imported plugin module and — the one that actually
breaks — the ``Mount``/``Host`` routes appended to *this* worker's FastAPI
router (``AppRuntime._attach_mount``). At ``AW_WORKSPACE_WORKERS=1`` those two
facts are indistinguishable. At >1 they diverge permanently: ``POST
/api/apps/install`` lands on whichever worker the load balancer picked, that
worker writes the DB row and mounts the routes, and the other N-1 answer 404
for that app until the next full restart.

So every step of a load/unload belongs to exactly one of two halves, and this
module is where that choice is named rather than left to convention:

**PROVISION** — the side-effecting half. Touches the shared filesystem, the
shared Postgres, or podman: the GitHub fetch, ``pip install``, DB migrations,
container pull/start, managed-service ``Popen``, skills materialization,
system-CLI shims, ``mcp.json`` rendering, the mirror/cloud registry rows.
**Must happen exactly once per change**, no matter how many workers are
running. Ten workers running ``pip install`` into one venv is the same
corruption class W1 fixed for the CLI healer.

**ATTACH** — the in-process half. Pure to this process: import the plugin
module, run ``activate()``, append the mount, invalidate the OpenAPI cache —
and their inverses on unload. **Must happen in every worker**, or that worker
serves 404 for an app the rest of the fleet has.

The bridge between them is this module's broadcast. After the provisioning
worker finishes, it publishes ``apps:changed`` over
:class:`~src.libs.redis_coord.RedisBroadcaster`; every worker — *including the
publisher*, since ``redis_coord`` deliberately has a single delivery path with
no local shortcut — receives it and re-converges its own in-process view from
``LocalMirror``. The broadcast carries only "the shared state changed,
re-attach". It never carries work: a convergence pass must not be able to
reach ``_install_pip_requires``, ``fetch_app_repo`` or ``containers.start``,
which is exactly what ``provision=False`` enforces all the way down.

### Which half am I in?

New app-runtime code has to answer that. The rule:

* Would running it in ten processes at once corrupt something, cost ten times
  as much, or produce ten of a thing there should be one of? → PROVISION.
  Guard it with ``if provision:`` and let the attach path skip it.
* Is it state this process needs in order to serve a request correctly? →
  ATTACH. Run it unconditionally.

Getting it wrong is silent at ``workers=1`` — which is what ships — and only
shows up under load. When in doubt, look at what ``AppRuntime.load`` already
does with the flag and follow the nearest case.

### Serialization

``provision_lock()`` is a cross-worker mutex, not a leader lease. Leadership
(W1's ``RedisLease("core")``) answers "who runs the periodic tasks"; this
answers "only one worker is inside the side-effecting half at a time", which
is what ``pip``/``podman``/``git`` into one shared tree actually needs. Two
*different* apps installed simultaneously through two different workers is a
real request pattern and it would otherwise put two ``pip install`` processes
in the same venv — a race the single event loop used to prevent for free.

Degrades on purpose: with Redis unreachable (the normal case today — see
``src/libs/redis_coord.py``'s note on the F5a companion) the local
``asyncio.Lock`` alone still holds, which at ``workers=1`` is exactly the
serialization the event loop already gave. Behaviour identical to what ships.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)

#: Broadcast topic. One topic for the whole app set rather than one per app:
#: the payload is a hint for logging, and the handler always re-reads the full
#: mirror anyway, so a coalesced burst of these costs one convergence pass.
TOPIC_APPS_CHANGED = "apps:changed"

#: How long a provisioning worker may hold the shared lock before it is
#: considered dead and the lock expires. A cold install can genuinely take
#: minutes (a GitHub fetch with retries + a pip install + an image pull), so
#: this is deliberately generous; it is a crash backstop, not a timeout.
PROVISION_LOCK_TTL_S = 900.0

#: How long to wait for the lock before giving up and provisioning anyway.
#: Giving up is the right call: refusing the install outright would turn a
#: Redis hiccup into a user-visible failure, whereas proceeding is exactly
#: today's single-worker behaviour.
PROVISION_LOCK_WAIT_S = 120.0

_LOCK_POLL_S = 0.5

# Release-time CAS, same shape as redis_coord's — only delete the key if we
# still own it, so a slow provisioner that outran its TTL can't drop the lock
# a different worker has since acquired.
_UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

ChangedHandler = Callable[[dict], Awaitable[None]]


def _lock_key(name: str) -> str:
    from src.libs.redis_coord import _key_prefix

    return f"{_key_prefix()}applock:{name}"


class AppLifecycle:
    """Owns the ``apps:changed`` fan-out and the provisioning mutex.

    One instance per process, held on ``app.state.app_lifecycle``. Started
    from the lifespan (``start()``) and stopped with it; every method is a
    safe no-op before ``start()`` so a unit test that never wires Redis
    behaves exactly like a single-worker deployment.
    """

    def __init__(self, redis_url: Optional[str] = None) -> None:
        self._redis_url = redis_url
        self._broadcaster: Optional[object] = None
        self._handler: Optional[ChangedHandler] = None
        # Provisioning is serialized in-process too, not only across workers:
        # at workers=1 with Redis down this lock is the ONLY thing left, and
        # it is what reproduces today's "the single event loop serializes
        # everything" behaviour for two concurrent installs.
        self._local_lock = asyncio.Lock()
        self._client: Optional[object] = None

    # ---- fan-out ---------------------------------------------------------

    async def start(self, handler: ChangedHandler) -> bool:
        """Subscribe this worker to ``apps:changed``. True if the relay is up.

        Never raises: a workspace whose Redis is unreachable must still boot
        and serve, it just gets no cross-worker convergence — which is
        precisely today's behaviour, and correct at ``workers=1`` where there
        is no other worker to converge with.
        """
        if self._broadcaster is not None:
            return True
        from src.libs.redis_coord import RedisBroadcaster

        self._handler = handler
        broadcaster = RedisBroadcaster(self._redis_url)
        try:
            await broadcaster.start_relay(self._on_message)
        except Exception:
            log.warning(
                "apps: could not subscribe to %r — this worker will not be told "
                "when another worker installs or removes an app. Harmless at "
                "AW_WORKSPACE_WORKERS=1 (there is no other worker); at >1 it "
                "means app routes diverge between workers until a restart. See "
                "doctor's `redis` check.",
                TOPIC_APPS_CHANGED, exc_info=True,
            )
            with contextlib.suppress(Exception):
                await broadcaster.stop()
            return False
        self._broadcaster = broadcaster
        log.info("apps: subscribed to %r for cross-worker convergence", TOPIC_APPS_CHANGED)
        return True

    async def _on_message(self, topic: str, payload: dict) -> None:
        # The relay is a PSUBSCRIBE over every topic in this workspace's
        # namespace (redis_coord starts one relay per broadcaster), so filter.
        if topic != TOPIC_APPS_CHANGED or self._handler is None:
            return
        log.info("apps: %r received (%s) — re-converging this worker's loaded set",
                 TOPIC_APPS_CHANGED, payload)
        await self._handler(payload)

    async def publish(self, reason: str, app_id: str | None = None) -> None:
        """Tell every worker (this one included) that the shared state moved.

        Fire-and-forget by contract: the caller has already finished the
        provisioning half and persisted it, so a failed publish costs
        convergence latency on the *other* workers, not correctness on this
        one. Never raises into an install/uninstall path.
        """
        if self._broadcaster is None:
            return
        payload = {"reason": reason, "app_id": app_id, "pid": os.getpid()}
        try:
            delivered = await self._broadcaster.publish(TOPIC_APPS_CHANGED, payload)
            log.info("apps: published %r (%s) to %s subscriber(s)",
                     TOPIC_APPS_CHANGED, payload, delivered)
        except Exception:
            log.exception("apps: could not publish %r after %s of %s — other "
                          "workers will not converge until their next restart",
                          TOPIC_APPS_CHANGED, reason, app_id)

    async def stop(self) -> None:
        if self._broadcaster is not None:
            with contextlib.suppress(Exception):
                await self._broadcaster.stop()
            self._broadcaster = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    # ---- the provisioning mutex ------------------------------------------

    def _get_client(self):
        if self._client is None:
            import redis.asyncio as aioredis

            from src.libs.redis_coord import get_workspace_redis_url

            self._client = aioredis.from_url(
                self._redis_url or get_workspace_redis_url(), decode_responses=True)
        return self._client

    @contextlib.asynccontextmanager
    async def in_process_exclusive(self):
        """The LOCAL half of :meth:`provision_lock`, without the shared claim.

        Held by a convergence pass so it cannot interleave with a provisioning
        pass in the same process. That is not tidiness — it is correctness:
        ``converge_in_process`` detaches anything this worker has loaded but
        the mirror does not list, and ``install`` necessarily has a window
        between ``runtime.load`` (loaded) and ``local.upsert`` (listed). An
        ``apps:changed`` from ANOTHER worker landing in that window would find
        the app in exactly that state and unmount an install that was
        succeeding.

        Deliberately not the shared lock: a converge does no shared work, so
        making every worker's convergence queue behind every other worker's
        install would add cross-fleet latency to defend a purely local
        invariant.
        """
        async with self._local_lock:
            yield

    @contextlib.asynccontextmanager
    async def provision_lock(self, name: str = "apps"):
        """Hold the cross-worker provisioning lock for the duration of a block.

        Always acquires the in-process lock; additionally takes a Redis
        ``SET NX PX`` lock when Redis is reachable. Yields ``True`` when it
        actually holds the shared lock, ``False`` when it is running on the
        local lock alone (Redis down, or the wait timed out) — the caller may
        log that, but must proceed either way: blocking an install because a
        coordination Redis is unavailable would be a worse failure than the
        race it prevents.
        """
        async with self._local_lock:
            token = uuid.uuid4().hex
            key = _lock_key(name)
            held = await self._acquire_shared(key, token)
            try:
                yield held
            finally:
                if held:
                    await self._release_shared(key, token)

    async def _acquire_shared(self, key: str, token: str) -> bool:
        deadline = None
        try:
            client = self._get_client()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PROVISION_LOCK_WAIT_S
            while True:
                won = await client.set(key, token, nx=True,
                                       px=int(PROVISION_LOCK_TTL_S * 1000))
                if won:
                    return True
                if loop.time() >= deadline:
                    log.warning(
                        "apps: waited %ss for the shared provisioning lock %r and "
                        "did not get it — proceeding anyway (single-worker "
                        "behaviour). If this repeats, a worker died holding it "
                        "and the key clears after %ss.",
                        PROVISION_LOCK_WAIT_S, key, PROVISION_LOCK_TTL_S)
                    return False
                await asyncio.sleep(_LOCK_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("apps: shared provisioning lock unavailable (%r) — using the "
                      "in-process lock only", key, exc_info=True)
            return False

    async def _release_shared(self, key: str, token: str) -> None:
        try:
            client = self._get_client()
            await client.eval(_UNLOCK_LUA, 1, key, token)
        except Exception:
            log.debug("apps: could not release the shared provisioning lock %r — it "
                      "expires on its own after %ss", key, PROVISION_LOCK_TTL_S,
                      exc_info=True)
