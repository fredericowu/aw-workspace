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

**It does not depend on Redis, and deliberately so.** It used to: a
``SET NX PX`` key, with the local ``asyncio.Lock`` as the stated fallback
"with Redis unreachable (the normal case today)". That fallback was not one.
``AW_WORKSPACE_WORKERS=10`` is ten OS *processes*, and an ``asyncio.Lock`` is
per-process state — so in the case the design called normal, there was no
cross-worker mutex at all, and ten boot reconciles ran ``apt-get`` against one
``/var/cache/apt`` concurrently. That is the reported corruption
(``Could not open file /var/cache/apt/archives/...deb`` mid-dpkg, across
different apps and workspaces), not a theoretical one.

The authoritative guard is now an ``fcntl.flock`` on
``<AW_WORKSPACE_HOME>/locks/apps-provision-<name>.lock``
(``src/apps/fs_lock.py``) — every worker is a process inside one container
sharing one filesystem, which is exactly the scope the lock needs to cover,
and the kernel releases it on crash or ``kill -9`` instead of after a 900s
TTL. The ``asyncio.Lock`` stays in front of it as the in-process layer
(cheaper, and it is what ``in_process_exclusive`` shares).

The Redis dependency that remains in this module is the ``apps:changed``
broadcast below — a fan-out, not a mutex, and harmless when it is missing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Awaitable, Callable, Optional

from src.apps import fs_lock

log = logging.getLogger(__name__)

#: Broadcast topic. One topic for the whole app set rather than one per app:
#: the payload is a hint for logging, and the handler always re-reads the full
#: mirror anyway, so a coalesced burst of these costs one convergence pass.
TOPIC_APPS_CHANGED = "apps:changed"

#: How long to wait for the provisioning lock before proceeding anyway.
#:
#: Much longer than the 120s the Redis lock used, because the number now means
#: something different. A Redis key could outlive its owner (900s TTL), so a
#: short give-up hedged against waiting on a lock nobody held. ``flock`` cannot
#: be stale — if we are still waiting, a real process is still provisioning —
#: so the ceiling has to clear a genuine cold pass instead: the boot reconcile
#: gets 1200s (``_BOOT_RECONCILE_TIMEOUT``, ``src/apps/routes.py``) and a cold
#: pass has been measured at 450s+.
PROVISION_LOCK_WAIT_S = 1800.0

ChangedHandler = Callable[[dict], Awaitable[None]]


class AppLifecycle:
    """Owns the ``apps:changed`` fan-out and the provisioning mutex.

    One instance per process, held on ``app.state.app_lifecycle``. Started
    from the lifespan (``start()``) and stopped with it; every method is a
    safe no-op before ``start()`` so a unit test that never wires Redis
    behaves exactly like a single-worker deployment.
    """

    def __init__(self, redis_url: Optional[str] = None,
                 lock_dir: Optional[str] = None) -> None:
        self._redis_url = redis_url
        self._broadcaster: Optional[object] = None
        self._handler: Optional[ChangedHandler] = None
        # Provisioning is serialized in-process too, not only across workers:
        # it is cheaper than the file lock for the common same-worker case,
        # and it is the lock ``in_process_exclusive`` shares to keep a
        # convergence pass from interleaving with a provisioning one.
        self._local_lock = asyncio.Lock()
        # Defaults to ``<AW_WORKSPACE_HOME>/locks`` (resolved lazily, at
        # acquire time). Injectable because the default is the LIVE
        # workspace's lock dir: a test taking that file could block behind,
        # or block, a real provisioning pass on this very container.
        self._lock_dir = lock_dir

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

    # ---- the provisioning mutex ------------------------------------------

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

        Deliberately NOT the cross-worker lock, and that is load-bearing: a
        converge does no shared work, so queueing every worker's convergence
        behind another worker's 450s+ cold provisioning pass would add
        cross-fleet latency to defend a purely local invariant.
        """
        async with self._local_lock:
            yield

    @contextlib.asynccontextmanager
    async def provision_lock(self, name: str = "apps"):
        """Hold the cross-worker provisioning lock for the duration of a block.

        Takes the in-process lock first, then an ``fcntl.flock`` on
        ``<lock dir>/apps-provision-<name>.lock`` — the real cross-process
        mutex (``src/apps/fs_lock.py`` for why a file lock and not Redis).

        Yields ``True`` when it holds the file lock, ``False`` when the
        :data:`PROVISION_LOCK_WAIT_S` ceiling elapsed first and it is
        proceeding on the in-process lock alone. The caller may log that, but
        must proceed either way: refusing an install outright is a worse
        failure than the race, so proceed-anyway survives as the last resort
        it was always described as — just no longer as the normal path.

        Not reentrant, on purpose: ``flock`` on a second fd in the same
        process blocks like any other contender. ``Reconciler._provisioning``'s
        depth counter is what keeps ``install``'s recursion and ``reconcile``'s
        per-app calls from deadlocking on themselves.
        """
        async with self._local_lock:
            path = fs_lock.lock_path(f"apps-provision-{name}", self._lock_dir)
            fd = await fs_lock.acquire_async(path, timeout=PROVISION_LOCK_WAIT_S)
            if fd is None:
                log.error(
                    "apps: waited %.0fs for the provisioning lock %s (held by %s) "
                    "and did not get it — proceeding ANYWAY, unserialized. "
                    "Concurrent apt/pip/podman against one filesystem can corrupt "
                    "a package install; this is the last-resort path, not a "
                    "normal one. Check for a wedged provisioning process.",
                    PROVISION_LOCK_WAIT_S, path, fs_lock.holder(path))
            try:
                yield fd is not None
            finally:
                fs_lock.release(fd)
