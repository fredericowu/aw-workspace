"""Coordinates AW_WORKSPACE_WORKERS>1 workers so exactly ONE runs the
expensive boot-time app reconcile per fleet boot — even across a uvicorn
worker respawn within that same boot.

**The incident this replaces.** ``_is_boot_provisioner`` (``src/api/app.py``)
used to call ``redis_coord.cooldown_acquire`` — a one-shot ``SET NX EX``
with no renewal ("no release, it just expires"). Its 120s window was picked
to cover "how far apart the workers of ONE boot start", but a live boot
reconcile pass over the full app manifest measures 450s+
(``src/apps/routes.py``'s ``_BOOT_RECONCILE_TIMEOUT = 1200.0`` is sized with
margin over that). Any worker respawned by uvicorn AFTER the 120s window
elapsed — because it was SIGKILLed for missing a liveness ping under memory
pressure, say — re-called ``_is_boot_provisioner`` with the SAME inherited
``boot_id`` (see ``src/api/boot_info.py``), found the claim already expired,
and "won" it again: a SECOND full reconcile pass (47 GitHub fetches, pip
installs, podman starts) starting on top of a FIRST one that might still be
running. Live 2026-09-17 (workspace ``crispal``): up to 21 such passes over
2+ hours, each one's rename-swap window (``src/apps/fetch.py``) and
per-worker route-mounting race causing the installed-apps list to flap
between "added" and "removed" on every mcp-gateway reload, forever, and the
redundant concurrent installs themselves being a large share of what drove
the container to 99% memory / 88% of its PID ceiling.

**The fix.** Two Redis keys per ``boot_id``, both scoped under this
workspace's own coordination namespace (``redis_coord._key_prefix()``):

``boot-apps-reconcile-lease:<boot_id>``
    Held by the ACTIVE leader — a real lease (``SET NX PX`` + a Lua
    compare-and-swap renewal, the same primitive ``RedisLease`` uses for
    its own leader election), renewed every ``heartbeat_interval`` seconds
    for as long as the real reconcile pass runs. A leader that dies
    mid-pass (OOM kill, crash) stops renewing and the lease expires
    within ``lease_ttl`` seconds — bounding how long an abandoned pass can
    block everyone else, without ever letting a genuinely-still-working
    leader get preempted by a fixed clock the way ``cooldown_acquire`` did.

``boot-apps-reconcile-done:<boot_id>``
    Set ONCE, when the leader's pass ends — success, exception, or its own
    internal timeout give-up. A terminal fact for the rest of this boot's
    lifetime: every later check, including a worker respawned five
    separate times, sees it immediately and returns to the cheap
    ``attach_on_boot`` path without touching the lease at all.

Redis unreachable (the normal case in every environment before W3, and
still possible if the per-workspace companion is down) fails OPEN exactly
like ``cooldown_acquire`` did — ``coordinate()`` returns True (run it
myself) rather than raising, so a coordination outage degrades to "every
worker converges independently, wastefully but correctly", never to a
workspace that never provisions at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis

from src.libs.redis_coord import _key_prefix, get_workspace_redis_url

logger = logging.getLogger(__name__)

# Renews the lease only if we still hold it (our own token is still the
# value) — the same shape RedisLease._try_renew uses, so a lease we lost
# (another process's SET NX won after ours expired) is never accidentally
# stomped back to "ours" by a late renewal call racing behind it.
_RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class BootReconcileCoordinator:
    """One instance per worker process, constructed with this boot's
    ``boot_id``. Call :meth:`coordinate` once at boot; if it returns True,
    call :meth:`start_heartbeat` immediately and :meth:`stop_heartbeat` +
    :meth:`mark_done` in a ``finally`` once the real reconcile pass ends.
    """

    def __init__(
        self,
        boot_id: str,
        *,
        redis_url: Optional[str] = None,
        lease_ttl: float = 30.0,
        heartbeat_interval: float = 10.0,
        done_ttl: float = 3600.0,
        # Comfortably above _BOOT_RECONCILE_TIMEOUT (1200s): a waiter must
        # never give up and race for leadership itself BEFORE the leader's
        # own internal timeout would have made it give up first — that
        # ordering is what guarantees at most one "give up and just do it"
        # per boot instead of two workers deciding that within seconds of
        # each other.
        max_wait_s: float = 1260.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.boot_id = boot_id
        self.lease_ttl = lease_ttl
        self.heartbeat_interval = heartbeat_interval
        self.done_ttl = done_ttl
        self.max_wait_s = max_wait_s
        self.poll_interval = poll_interval
        self.token = uuid.uuid4().hex
        self._redis_url = redis_url or get_workspace_redis_url()
        self._lease_key = f"{_key_prefix()}boot-apps-reconcile-lease:{boot_id}"
        self._done_key = f"{_key_prefix()}boot-apps-reconcile-done:{boot_id}"
        self._client: Optional[aioredis.Redis] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def aclose(self) -> None:
        """Best-effort — call once this worker is done with the
        coordinator entirely (after mark_done()/stop_heartbeat(), or after
        a follower's coordinate() returned False). Never required for
        correctness, only to release the connection promptly."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def coordinate(self) -> bool:
        """Returns True if THIS call must run the full reconcile itself
        (it won leadership, or every other candidate leader it waited on
        died without finishing) — the caller should then call
        start_heartbeat() immediately and, in a finally once the pass
        ends, stop_heartbeat() + mark_done(). Returns False if another
        worker already finished (fast path) or finished WHILE this call
        was waiting — the caller should just attach_on_boot().
        """
        client = self._get_client()
        deadline = time.monotonic() + self.max_wait_s
        while True:
            if await client.get(self._done_key):
                return False
            won = await client.set(
                self._lease_key, self.token, nx=True, px=int(self.lease_ttl * 1000))
            if won:
                return True
            # Someone else holds the lease right now. Loop back: either the
            # DONE marker appears (they finished — next iteration's GET
            # catches it), or the lease itself disappears with no DONE
            # marker (they died mid-pass — next iteration's SET NX wins it
            # for us), or we've waited long enough that giving up and
            # running our own pass beats staying unattached forever.
            if time.monotonic() >= deadline:
                logger.error(
                    "apps: boot reconcile coordination timed out after %ss "
                    "waiting for another worker (boot_id=%s) — running our "
                    "own pass rather than never attaching",
                    self.max_wait_s, self.boot_id,
                )
                return True
            await asyncio.sleep(self.poll_interval)

    async def start_heartbeat(self) -> None:
        """Call once, immediately after coordinate() returns True.
        Renews the lease every heartbeat_interval seconds for as long as
        the real reconcile pass runs — this is what stops a genuinely
        long-running pass from ever losing its own claim, unlike the
        fixed-TTL cooldown_acquire this coordinator replaces."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def stop_heartbeat(self) -> None:
        """Call in a finally around the reconcile pass, whether it
        succeeded, raised, or hit its own internal timeout — leaving the
        heartbeat running after the pass ends would keep renewing a lease
        for work that is no longer happening."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        client = self._get_client()
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                try:
                    await client.eval(
                        _RENEW_LUA, 1, self._lease_key, self.token,
                        int(self.lease_ttl * 1000))
                except Exception:
                    logger.warning(
                        "apps: boot reconcile heartbeat renewal failed "
                        "(Redis blip?) — will retry next interval (boot_id=%s)",
                        self.boot_id, exc_info=True,
                    )
        except asyncio.CancelledError:
            raise

    async def mark_done(self) -> None:
        """Leader-only — call once the pass ends (success, exception, or
        internal timeout give-up), AFTER stop_heartbeat(). Sets the
        terminal marker every later worker of this boot checks first,
        before ever touching the lease again. Best-effort: a failure here
        just means a later respawn re-runs the pass, same as today."""
        client = self._get_client()
        try:
            await client.set(self._done_key, "1", ex=int(self.done_ttl))
        except Exception:
            logger.warning(
                "apps: could not record the boot reconcile done marker "
                "(Redis blip?) — a respawned worker may re-run the pass "
                "(boot_id=%s)", self.boot_id, exc_info=True,
            )
