"""Per-service cross-worker ownership lease for :class:`~src.apps.services.ServiceSupervisor`.

**The gap this closes.** ``ServicesFacade.register()`` (``src/apps/base.py``)
already gates AUTOSTART on ``ctx.provision`` — the per-boot leader elected by
``BootReconcileCoordinator`` — so only one of ``AW_WORKSPACE_WORKERS`` workers
spawns a service's subprocess at boot. Manual start/restart
(``ComponentRoutes.start_component``/``restart_component``,
``src/api/components.py``) call ``rt.services.start()``/``stop()`` directly on
the *local* worker with no such check, and ``status()`` only ever reports the
local worker's ``Popen`` state. A request that happens to land on a
non-owning worker therefore sees ``running=False`` and, if it then starts or
restarts, spawns a REAL second subprocess that collides with the one already
running elsewhere on the service's port (confirmed live: the proxy app's
``OSError: Address already in use``, which used to be masked by a separate
bug in ``apps/proxy/proxy_app/proxy_server.py`` — that mask is gone since
``ThreadPoolHTTPServer.__init__`` builds its pool before ``TCPServer.__init__``,
so a bind collision now surfaces as the real ``OSError`` on the child's
stdout. :class:`~src.apps.services.ServiceSupervisor` reads it back out of
that captured stdout and converts it into an immediate ``gave_up`` with
``reason=orphan-suspected`` instead of a backoff loop that can never win —
see ``docs/standards/core-restart-blast-radius.md`` §2).

This module is the ownership record that closes that gap for every
``ctx.services``-backed app, not just proxy — reused by
:meth:`~src.apps.services.ServiceSupervisor.start`,
:meth:`~src.apps.services.ServiceSupervisor.stop` and
:meth:`~src.apps.services.ServiceSupervisor.status` so autostart, manual
start/restart and status queries all agree on the same fact.

Same ``SET NX PX`` + Lua CAS renew/release shape as
``src/apps/boot_reconcile_coord.py``'s boot-reconcile lease and
``src/libs/redis_coord.py``'s ``RedisLease``, scoped per ``(app_id,
service_id)`` instead of per ``boot_id``/role. Deliberately **synchronous**,
not ``asyncio``, because :class:`~src.apps.services.ServiceSupervisor` is: its
``subprocess.Popen`` + reader-thread model predates this workspace's async
coordination infra and is called both from inside an app's ``activate()``
coroutine (via ``ServicesFacade``, never awaited — see ``base.py``) and from
FastAPI route handlers via ``asyncio.to_thread`` (``src/api/components.py``).
A sync API here needs no ripple into either caller's signature.

Degrades open like every other coordination primitive in this family: with
Redis unreachable, :meth:`ServiceLease.acquire` reports success (proceed
exactly like today's single-worker behaviour) and
:func:`is_lease_held_anywhere` reports ``False`` (fall back to the
local-only answer) rather than ever blocking a service that has nowhere
else to check ownership against.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Optional

import redis as sync_redis

from src.libs.redis_coord import _key_prefix, get_workspace_redis_url

log = logging.getLogger(__name__)

# Same shape as redis_coord._RENEW_LUA / boot_reconcile_coord._RENEW_LUA:
# only refresh the TTL while we're still the recorded owner, so a delayed
# renew can never resurrect a lease someone else has since legitimately won.
_RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""

# Only delete the key if we're still the owner, so release() can never
# clobber a lease another worker has since acquired.
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_DEFAULT_TTL_S = 30.0
_DEFAULT_HEARTBEAT_S = 10.0

_client_cache: dict[str, "sync_redis.Redis"] = {}


def _get_shared_client(redis_url: Optional[str] = None) -> "sync_redis.Redis":
    """Process-wide cached sync client, one per URL — mirrors
    ``redis_coord.get_redis_pool()``'s "shared, never closed by the caller"
    contract, just synchronous. redis-py pools connections internally, so
    sharing this across every service's lease is cheaper than one
    connection per (app_id, service_id) in a fleet with many services."""
    url = redis_url or get_workspace_redis_url()
    client = _client_cache.get(url)
    if client is None:
        client = sync_redis.Redis.from_url(url, decode_responses=True)
        _client_cache[url] = client
    return client


def _lease_key(app_id: str, service_id: str) -> str:
    return f"{_key_prefix()}service-lease:{app_id}:{service_id}"


def _gave_up_key(app_id: str, service_id: str) -> str:
    return f"{_key_prefix()}svc:gaveup:{app_id}:{service_id}"


# ---------------------------------------------------------------------------
# Give-up markers — the DURABLE half of the restart loop's state
# ---------------------------------------------------------------------------
# The lease above answers "who is running this right now" and is deliberately
# short-lived (30s TTL, heartbeat-renewed) so a dead worker's claim expires.
# A give-up decision is the opposite kind of fact: the supervisor has stopped
# trying, and that must stay true until a human (or an install) says
# otherwise. It is therefore written WITHOUT a TTL and read by every worker,
# `doctor` and `/api/health` — the backoff counters themselves stay per-worker
# memory (see the design doc's "What this makes harder later").
#
# Same degrade-open contract as the lease: Redis away means the marker cannot
# be written or read, which costs visibility, never correctness — the owning
# worker's in-memory `state == "gave_up"` is what actually stops respawning.


def set_gave_up(app_id: str, service_id: str, *, reason: str,
                exit_code: Optional[int] = None, detail: str = "",
                redis_url: Optional[str] = None) -> bool:
    """Record that the supervisor has stopped respawning this service."""
    payload = json.dumps({
        "app": app_id, "service": service_id, "reason": reason,
        "exit_code": exit_code, "detail": detail, "at": time.time(),
    })
    try:
        _get_shared_client(redis_url).set(_gave_up_key(app_id, service_id), payload)
        return True
    except Exception:
        log.warning(
            "service_lease: could not record the give-up marker for %s/%s — "
            "this worker still stops respawning it, but no other worker, doctor "
            "or /api/health can see why", app_id, service_id, exc_info=True)
        return False


def clear_gave_up(app_id: str, service_id: str, *,
                  redis_url: Optional[str] = None) -> bool:
    """Drop the marker — a manual start/restart, an install/update, or a
    config save re-arms the restart loop (see ``ServiceSupervisor``)."""
    try:
        _get_shared_client(redis_url).delete(_gave_up_key(app_id, service_id))
        return True
    except Exception:
        log.warning("service_lease: could not clear the give-up marker for %s/%s",
                    app_id, service_id, exc_info=True)
        return False


def gave_up_markers(pairs: list[tuple[str, str]], *,
                    redis_url: Optional[str] = None,
                    ) -> tuple[dict[tuple[str, str], dict], bool]:
    """``(markers, redis_reachable)`` for many services in ONE round-trip.

    One ``MGET`` rather than a key-per-service lookup: this is on
    ``/api/health``'s path, which uptime monitors and container healthchecks
    poll every few seconds. The boolean is the only Redis-reachability signal
    that path has, and it decides whether health reports its answer as
    fleet-wide or ``health_scope: local-worker``.
    """
    if not pairs:
        return {}, True
    try:
        raw = _get_shared_client(redis_url).mget(
            [_gave_up_key(a, s) for (a, s) in pairs])
    except Exception:
        log.warning("service_lease: could not read give-up markers — falling back "
                    "to the local-only view", exc_info=True)
        return {}, False
    out: dict[tuple[str, str], dict] = {}
    for pair, value in zip(pairs, raw):
        if not value:
            continue
        try:
            out[pair] = json.loads(value)
        except ValueError:
            out[pair] = {"reason": "unknown"}
    return out, True


def is_lease_held_anywhere(app_id: str, service_id: str, *, redis_url: Optional[str] = None) -> bool:
    """True if SOME worker's lease for this service is currently live —
    used by ``status()`` when the local ``Popen`` isn't running, to report
    the true cross-worker state instead of a false "off" for a service
    actually running on another worker."""
    try:
        client = _get_shared_client(redis_url)
        return client.exists(_lease_key(app_id, service_id)) > 0
    except Exception:
        log.warning(
            "service_lease: could not reach Redis to check %s/%s — "
            "falling back to the local-only answer", app_id, service_id, exc_info=True)
        return False


class ServiceLease:
    """One instance per locally-spawned service process.

    :meth:`acquire` is a single ``SET NX PX`` attempt: ``True`` means this
    worker now owns the service and may spawn it; ``False`` means another
    worker's lease is already live and this worker must not spawn a
    duplicate. A won lease is kept alive by a background heartbeat thread
    for as long as the local process runs; :meth:`release` (explicit stop,
    or the reader thread noticing the process exited on its own) deletes it
    so the next starter — this worker or another — can acquire cleanly.
    """

    def __init__(self, app_id: str, service_id: str, *,
                 redis_url: Optional[str] = None,
                 ttl: float = _DEFAULT_TTL_S,
                 heartbeat_interval: float = _DEFAULT_HEARTBEAT_S) -> None:
        self.app_id = app_id
        self.service_id = service_id
        self.ttl = ttl
        self.heartbeat_interval = heartbeat_interval
        self.token = uuid.uuid4().hex
        self._redis_url = redis_url
        self._key = _lease_key(app_id, service_id)
        self._owned = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    @property
    def owned(self) -> bool:
        """Whether this worker still holds the claim — ``acquire()`` won it and
        no heartbeat has since found it stolen or expired. Read by
        :meth:`~src.apps.services.ServiceSupervisor.owns_locally` to decide
        whether this process may act on a broadcast service command."""
        return self._owned

    def _client(self) -> "sync_redis.Redis":
        return _get_shared_client(self._redis_url)

    def acquire(self) -> bool:
        try:
            won = self._client().set(self._key, self.token, nx=True, px=int(self.ttl * 1000))
        except Exception:
            log.warning(
                "service_lease: could not reach Redis to claim %s/%s — "
                "proceeding unguarded (single-worker fallback)",
                self.app_id, self.service_id, exc_info=True)
            return True
        self._owned = bool(won)
        return self._owned

    def start_heartbeat(self) -> None:
        if not self._owned or self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True,
            name=f"service-lease-hb-{self.app_id}-{self.service_id}")
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self.heartbeat_interval):
            try:
                renewed = self._client().eval(
                    _RENEW_LUA, 1, self._key, self.token, int(self.ttl * 1000))
                if not renewed:
                    log.warning("service_lease: lost lease for %s/%s (stolen or expired)",
                                self.app_id, self.service_id)
                    self._owned = False
                    return
            except Exception:
                log.warning("service_lease: heartbeat renew failed for %s/%s (Redis blip?)",
                            self.app_id, self.service_id, exc_info=True)

    def release(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            try:
                self._heartbeat_thread.join(timeout=2.0)
            except RuntimeError:
                # "cannot join thread before it is started": release() raced
                # start_heartbeat() between constructing the thread and
                # starting it. ServiceSupervisor.start() now arms the
                # heartbeat before the reader thread that calls this, which
                # closes that window — but release() must not be able to
                # raise INTO a caller's finally block whatever the ordering,
                # because the one that matters is the pump's, and an
                # exception there loses the process's exit entirely.
                pass
            self._heartbeat_thread = None
        if self._owned:
            try:
                self._client().eval(_RELEASE_LUA, 1, self._key, self.token)
            except Exception:
                log.warning(
                    "service_lease: release failed for %s/%s (Redis blip? lease will "
                    "simply expire in %.0fs)", self.app_id, self.service_id, self.ttl,
                    exc_info=True)
            self._owned = False
