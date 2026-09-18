"""Cross-worker forwarding of manual service start/stop/restart to the worker
that actually owns the process.

**The gap this closes.** ``src/apps/service_lease.py`` made a manual
start/restart landing on a non-owning worker *safe* — it refuses to spawn a
duplicate and reports the true fleet-wide state. It did not make it *work*: at
``AW_WORKSPACE_WORKERS=10`` a human clicking "Restart" (or
``aw-workspace-cli restart <app>``, which POSTs the same route) has ~9/10 odds
of landing on a worker that holds no ``Popen`` for that service, and the
request then does nothing but confirm the service is already running — when
what the person wanted was the real process restarted, e.g. to pick up new
code.

**The mechanism: broadcast, don't address.** The command is PUBLISHed to every
worker over :class:`~src.libs.redis_coord.RedisBroadcaster` and each receiver
decides *for itself* whether it is the owner. Nothing ever resolves "which
worker owns this" into an address and then sends there, which is what makes
the lease-handover race structurally impossible: ownership is re-evaluated by
each receiver at the instant it processes the message, against the fact that
actually matters — "do I have a live ``Popen`` plus a lease I still own"
(:meth:`~src.apps.services.ServiceSupervisor.owns_locally`) — rather than
against a snapshot the requester took milliseconds earlier. It is also the
only shape ``RedisBroadcaster`` supports: it PSUBSCRIBEs the whole workspace
namespace once at startup and never subscribes dynamically, so per-owner or
per-``corr_id`` channels would buy nothing. Same shape aw-backend already
ships for its BYOD-tunnel relay (``host_link_relay.py``: *ownership is decided
per-message, non-owners simply ignore what isn't theirs*).

Two topics, both on the existing ``aw:ws:<ws>:bcast:*`` namespace:

``service:cmd``
    requester → all workers. ``{corr_id, app_id, service_id, action,
    origin_pid}``, ``action`` one of ``start`` / ``stop`` / ``restart``.
``service:reply``
    owner → all workers. ``{corr_id, ok, status|error, owner_pid}``. A single
    shared topic rather than one channel per ``corr_id``; every worker
    receives every topic anyway, so the requester just filters on ``corr_id``
    against its own in-process :attr:`ServiceCommandRelay._pending`.

**Dispatch rules** (:meth:`ServiceCommandRelay.dispatch`):

1. *I own it locally* → act locally. Today's path, untouched, and zero Redis
   round-trips at ``AW_WORKSPACE_WORKERS=1``.
2. *Someone else owns it* → publish and await the owner's reply, bounded by
   :data:`TIMEOUT_ENV` (default :data:`DEFAULT_TIMEOUT_S`).
3. *Nobody owns it* → act locally; the local ``start()`` acquires the free
   lease, which is today's behaviour and correct.
4. *Timeout in (2)* → log loudly, then fall back to acting locally exactly
   once. Safe **because the lease gate is still in place**: if the owner was
   merely slow, the local ``start()`` finds the lease held and degrades to
   today's honest no-op; if the owner really died, its lease has expired (or
   is about to) and the local start legitimately takes over.
5. *Redis unreachable* → no relay, and ``is_lease_held_anywhere`` already
   fails open to ``False``, so every worker acts locally. That is precisely
   ``workers=1`` behaviour, the standing rule for every coordination
   primitive in this repo.

The timeout's hard ceiling is the tunnel edge, which cuts requests at 30s — a
forwarded restart has to come back with a real answer before that, not a 504
the user cannot tell apart from a hang.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Optional

from src.apps.service_lease import is_lease_held_anywhere
from src.apps.services import ServiceError

log = logging.getLogger(__name__)

#: Requester → all workers.
TOPIC_SERVICE_CMD = "service:cmd"
#: Owner → all workers (filtered by ``corr_id`` on arrival).
TOPIC_SERVICE_REPLY = "service:reply"

#: The only actions a broadcast command may name. Anything else is dropped
#: rather than reflected into ``getattr(supervisor, action)`` — this is a
#: control channel now, not only a notification one.
ACTIONS = ("start", "stop", "restart")

#: Deliberately under the tunnel edge's 30s cut (see the module docstring).
DEFAULT_TIMEOUT_S = 20.0
TIMEOUT_ENV = "AW_SERVICE_RELAY_TIMEOUT_S"


def resolve_timeout_s() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("service_relay: ignoring unparseable %s=%r, using %ss",
                    TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_S)
        return DEFAULT_TIMEOUT_S
    if value <= 0:
        log.warning("service_relay: ignoring non-positive %s=%r, using %ss",
                    TIMEOUT_ENV, raw, DEFAULT_TIMEOUT_S)
        return DEFAULT_TIMEOUT_S
    return value


class ServiceCommandRelay:
    """One instance per worker process, held on ``app.state.service_relay``.

    Constructed in ``src/apps/routes.py`` next to :class:`AppLifecycle` and
    started/stopped from ``src/api/app.py``'s lifespan alongside the other
    relays. Every method is a safe no-op before :meth:`start`, so a unit test
    that never wires Redis behaves exactly like a single-worker deployment.
    """

    def __init__(self, runtime, redis_url: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self._runtime = runtime
        self._redis_url = redis_url
        self._timeout = timeout
        self._broadcaster: Optional[object] = None
        self._pending: dict[str, asyncio.Queue] = {}

    @property
    def timeout(self) -> float:
        return self._timeout if self._timeout is not None else resolve_timeout_s()

    def _services(self):
        return getattr(self._runtime, "services", None)

    # ---- lifespan --------------------------------------------------------

    async def start(self) -> bool:
        """Subscribe this worker to the command/reply topics. True if up.

        Never raises: a workspace whose Redis is unreachable must still boot
        and serve. It just gets no forwarding, which is exactly today's
        behaviour and correct at ``AW_WORKSPACE_WORKERS=1``.
        """
        if self._broadcaster is not None:
            return True
        from src.libs.redis_coord import RedisBroadcaster

        broadcaster = RedisBroadcaster(self._redis_url)
        try:
            await broadcaster.start_relay(self._on_message)
        except Exception:
            log.warning(
                "apps: could not subscribe to %r — a manual service "
                "start/restart landing on this worker cannot be forwarded to "
                "the worker that owns the process, and falls back to acting "
                "locally (the lease gate still prevents a duplicate spawn). "
                "Harmless at AW_WORKSPACE_WORKERS=1. See doctor's `redis` check.",
                TOPIC_SERVICE_CMD, exc_info=True,
            )
            with contextlib.suppress(Exception):
                await broadcaster.stop()
            return False
        self._broadcaster = broadcaster
        log.info("apps: subscribed to %r/%r for cross-worker service commands",
                 TOPIC_SERVICE_CMD, TOPIC_SERVICE_REPLY)
        return True

    async def stop(self) -> None:
        if self._broadcaster is not None:
            with contextlib.suppress(Exception):
                await self._broadcaster.stop()
            self._broadcaster = None
        # Anyone still awaiting a reply simply times out and falls back to
        # acting locally — the same path a dead owner already produces.
        self._pending.clear()

    # ---- receiving -------------------------------------------------------

    async def _on_message(self, topic: str, payload: dict) -> None:
        # The relay is a PSUBSCRIBE over every topic in this workspace's
        # namespace, so filter — same as AppLifecycle._on_message.
        if topic == TOPIC_SERVICE_REPLY:
            queue = self._pending.get(payload.get("corr_id"))
            if queue is not None:
                queue.put_nowait(payload)
            return
        if topic == TOPIC_SERVICE_CMD:
            await self._handle_command(payload)

    async def _handle_command(self, payload: dict) -> None:
        corr_id = payload.get("corr_id")
        app_id = payload.get("app_id")
        service_id = payload.get("service_id")
        action = payload.get("action")
        services = self._services()
        if services is None or not corr_id or action not in ACTIONS:
            return
        # Ownership is decided HERE, per message, not by the requester — see
        # the module docstring. A worker whose lease expired or was stolen
        # still has a live Popen and must not answer for a service it no
        # longer owns, which is why owns_locally checks both.
        if not services.owns_locally(app_id, service_id):
            return

        log.info("apps: executing forwarded %r for service %s/%s (corr_id=%s, from pid=%s)",
                 action, app_id, service_id, corr_id, payload.get("origin_pid"))
        try:
            # ServiceSupervisor is blocking (Popen, proc.wait, killpg) and this
            # runs on the worker's event loop thread — a 5s proc.wait here
            # would freeze every other request on this worker.
            result = await asyncio.to_thread(getattr(services, action), app_id, service_id)
            reply = {"corr_id": corr_id, "ok": True, "status": result,
                     "owner_pid": os.getpid()}
        except Exception as e:  # noqa: BLE001 — must ALWAYS reply
            # Staying silent here costs the requester the full timeout and
            # then a misleading local fallback.
            log.exception("apps: forwarded %r for service %s/%s failed",
                          action, app_id, service_id)
            reply = {"corr_id": corr_id, "ok": False, "error": str(e),
                     "owner_pid": os.getpid()}
        try:
            await self._broadcaster.publish(TOPIC_SERVICE_REPLY, reply)
        except Exception:
            log.exception("apps: could not publish the reply for corr_id=%s — the "
                          "requester will time out and fall back to acting locally",
                          corr_id)

    # ---- dispatching -----------------------------------------------------

    async def dispatch(self, app_id: str, service_id: str, action: str) -> dict:
        """Run ``action`` on whichever worker owns this service, and return
        that worker's own ``status()`` dict (annotated ``forwarded``/
        ``owner_pid`` when it was not this one)."""
        if action not in ACTIONS:
            raise ServiceError(f"unknown service action {action!r}")
        services = self._services()
        if services is None:
            raise ServiceError("this worker has no service supervisor")

        # (1) I own it — today's path, no Redis round-trip.
        if services.owns_locally(app_id, service_id):
            return await self._act_locally(services, app_id, service_id, action)

        # (3)/(5) Nobody owns it, or we have no relay to forward over — act
        # locally. is_lease_held_anywhere is a blocking sync Redis call.
        held = await asyncio.to_thread(is_lease_held_anywhere, app_id, service_id)
        if not held or self._broadcaster is None:
            return await self._act_locally(services, app_id, service_id, action)

        # (2) Someone else owns it — broadcast and wait for whoever that is.
        corr_id = uuid.uuid4().hex
        queue: asyncio.Queue = asyncio.Queue()
        self._pending[corr_id] = queue
        try:
            await self._broadcaster.publish(TOPIC_SERVICE_CMD, {
                "corr_id": corr_id,
                "app_id": app_id,
                "service_id": service_id,
                "action": action,
                "origin_pid": os.getpid(),
            })
            reply = await asyncio.wait_for(queue.get(), timeout=self.timeout)
        except asyncio.TimeoutError:
            # (4) Fall back exactly once. Safe because of the lease gate —
            # see the module docstring.
            log.warning(
                "apps: no worker answered %r for service %s/%s within %ss "
                "(corr_id=%s) — the owner may be mid-restart, wedged or gone. "
                "Falling back to acting locally; the lease gate still prevents "
                "a duplicate spawn if it is merely slow.",
                action, app_id, service_id, self.timeout, corr_id)
            return await self._act_locally(services, app_id, service_id, action)
        except Exception:
            log.exception(
                "apps: could not forward %r for service %s/%s — acting locally",
                action, app_id, service_id)
            return await self._act_locally(services, app_id, service_id, action)
        finally:
            self._pending.pop(corr_id, None)

        if not reply.get("ok"):
            # Fail loudly rather than fall back: the owner was reached and
            # said no, which is a real answer the user needs to see.
            raise ServiceError(
                reply.get("error") or f"the owning worker failed to {action} this service")
        status = dict(reply.get("status") or {})
        status["forwarded"] = True
        status["owner_pid"] = reply.get("owner_pid")
        return status

    async def _act_locally(self, services, app_id: str, service_id: str, action: str) -> dict:
        return await asyncio.to_thread(getattr(services, action), app_id, service_id)
