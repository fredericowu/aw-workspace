"""In-memory install-job tracker (async install — the BYOD-tunnel fix).

``POST /api/apps/install`` used to run the fetch + system-CLI ``apt install``
synchronously (30-60s) and hold the HTTP response open for the whole thing.
The BYOD tunnel (browser → aw-backend WorkspaceTunnelProxy → aw-workspace)
drops long-lived requests before the 200 lands, so the UI saw "Failed to
fetch" even though the install completed fine server-side. The install now
runs in a background task and this module tracks its progress per app id, so
the route can return immediately and the UI can poll
``GET /api/apps/{slug}/install-status``.

Per-process, in-memory only — a workspace restart mid-install loses the job,
same as the install itself (the ``AppInstall`` mirror row isn't written until
it finishes, so there's nothing further to lose).

W3 (multi-worker): the job STATE is additionally mirrored into Redis, because
at ``AW_WORKSPACE_WORKERS>1`` the install runs on whichever worker got
``POST /api/apps/install`` while the UI's ``GET /api/apps/{slug}/install-status``
poll lands on any of them — so a poll on a different worker used to 404 an
install that was running perfectly well next door, and the Marketplace panel
read that as a failure.

The decision the W3 card asks for, stated explicitly: **jobs stay owned by the
worker that runs the install, and are PUBLISHED to Redis so any worker can
read them.** They are not moved to Redis wholesale and they are not
leader-owned. The reasons are that the ``asyncio.Task`` and the WebSocket
listener set are inherently process-local (a task cannot be handed to another
process, and W4 owns the WS fan-out), and that an install must not become
impossible when Redis is down — the Redis mirror is strictly additive, and
with it unreachable every behaviour here is exactly what ships today.

Keys are ``aw:ws:<ws>:appjob:<app_id>``, TTL'd (``JOB_TTL_S``) because a
finished job is a transient UI concern, not state anything converges to — the
``AppInstall`` row is the durable record.

W4 (multi-worker): the STATE mirror above (W3) only fixed the poll fallback.
The live WS push (``/ws/apps/install-status``) was still worker-local on both
ends — ``_broadcast`` only reached ``self._listeners`` in THIS process, and a
client connecting mid-install replayed only ``self._jobs`` on THIS process —
so a user watching the Marketplace panel from a worker other than the one
running the install saw an empty panel that never ticked. This module now
also runs a ``RedisBroadcaster`` relay (``_WS_TOPIC``) for the live pushes,
and ``all_active_shared`` folds the Redis-mirrored STATE in for the replay,
same split as everywhere else in this file: jobs stay owned by the worker
running the install, everything else reads them from Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket

from src.libs.redis_coord import RedisBroadcaster

log = logging.getLogger(__name__)

#: A finished install's status only has to outlive the UI's poll interval;
#: an in-flight one is refreshed on every transition. Long enough that a
#: user watching the Marketplace panel always sees the terminal state.
JOB_TTL_S = 900

#: W4 relay topic for the live WS push (separate from the W3 STATE mirror's
#: per-app-id keys above).
_WS_TOPIC = "install-status"


@dataclass
class InstallJob:
    app_id: str
    status: str = "installing"  # installing | installed | failed
    error: Optional[str] = None
    summary: Optional[dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    task: Optional["asyncio.Task"] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "status": self.status,
            "error": self.error,
            "summary": self.summary,
        }


class InstallJobs:
    """Tracks in-flight/finished background installs, keyed by app id.

    Also broadcasts every status transition over WebSocket (``/ws/apps/install
    -status``, see ``routes.py``) — same pattern as ``NotificationManager`` —
    so the Marketplace panel can show live "Installing…" progress that
    survives a page refresh instead of relying on 2s polling alone. Polling
    is kept as a fallback (see ``AppsMarketplace.jsx``); this just makes the
    common case near-instant and cheaper.
    """

    def __init__(self, share: bool = True) -> None:
        self._jobs: dict[str, InstallJob] = {}
        self._listeners: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Off for unit tests that build an InstallJobs directly and must not
        # reach for a Redis client.
        self._share = share
        self._client: Any = None
        self._broadcaster = RedisBroadcaster()
        self._relay_up = False

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start_relay(self) -> None:
        """W4: the live half of the cross-worker fix (see module docstring).
        Awaited from the lifespan, not fire-and-forget — the same reasoning
        as ``NotificationManager.start_relay``: a subscribe that hasn't
        landed yet would silently drop this process's own first broadcasts.
        Never raises: with no reachable Redis, the Marketplace panel falls
        back to its own 2s poll against ``get_shared`` (still cross-worker
        via the W3 STATE mirror), same posture as every other relay in this
        codebase."""
        if not self._share:
            return
        try:
            await self._broadcaster.start_relay(self._on_relay_message)
            self._relay_up = True
        except Exception:
            log.warning(
                "apps: could not start the install-status Redis relay — this "
                "worker will only push live install progress to its own "
                "local listeners until restarted (harmless at "
                "AW_WORKSPACE_WORKERS=1)", exc_info=True)

    async def _on_relay_message(self, topic: str, payload: dict) -> None:
        if topic != _WS_TOPIC:
            return
        await self._send_all(json.dumps(payload))

    # ---- cross-worker mirror (W3) ---------------------------------------

    @staticmethod
    def _key(app_id: str) -> str:
        from src.libs.redis_coord import _key_prefix

        return f"{_key_prefix()}appjob:{app_id}"

    def _redis(self):
        if not self._share:
            return None
        if self._client is None:
            import redis.asyncio as aioredis

            from src.libs.redis_coord import get_workspace_redis_url

            self._client = aioredis.from_url(
                get_workspace_redis_url(), decode_responses=True)
        return self._client

    def _publish_shared(self, job: InstallJob) -> None:
        """Mirror one transition into Redis, fire-and-forget.

        Sync (called from the same sync transition methods the WS broadcast
        already uses) and scheduled onto the loop rather than awaited, so a
        slow or dead Redis can never add latency to — let alone fail — an
        install.
        """
        if not self._share or self._loop is None:
            return
        payload = json.dumps(job.as_dict())

        async def _write() -> None:
            try:
                client = self._redis()
                if client is not None:
                    await client.set(self._key(job.app_id), payload, ex=JOB_TTL_S)
            except Exception:
                log.debug("apps: could not mirror install job %s to Redis — the "
                          "status poll only answers on this worker",
                          job.app_id, exc_info=True)

        self._loop.call_soon_threadsafe(asyncio.ensure_future, _write())

    async def get_shared(self, app_id: str) -> Optional[dict[str, Any]]:
        """This worker's job for ``app_id``, else whatever worker owns it.

        The local job always wins — it is the live one, with the task handle
        behind it — and Redis is consulted only when this process has never
        seen the app. Returns ``None`` when neither has it, which is what the
        route turns into its 404.
        """
        job = self._jobs.get(app_id)
        if job is not None:
            return job.as_dict()
        try:
            client = self._redis()
            if client is None:
                return None
            raw = await client.get(self._key(app_id))
        except Exception:
            log.debug("apps: could not read the shared install job for %s",
                      app_id, exc_info=True)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def is_installing_shared(self, app_id: str) -> bool:
        """Cross-worker version of :meth:`is_installing` — the double-click /
        two-tabs guard has to see an install running on ANOTHER worker too,
        or two workers fetch and pip-install the same app at once."""
        shared = await self.get_shared(app_id)
        return bool(shared and shared.get("status") == "installing")

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — shutdown path
                pass
            self._client = None
        try:
            await self._broadcaster.stop()
        except Exception:  # noqa: BLE001 — shutdown path
            pass

    def add_listener(self, ws: WebSocket) -> None:
        self._listeners.add(ws)

    def remove_listener(self, ws: WebSocket) -> None:
        self._listeners.discard(ws)

    def get(self, app_id: str) -> Optional[InstallJob]:
        return self._jobs.get(app_id)

    def clear(self, app_id: str) -> None:
        self._jobs.pop(app_id, None)
        # Drop the shared copy too, or an uninstall leaves a stale
        # "installed" job that the status route on ANY worker would keep
        # answering with after the app is gone.
        if self._share and self._loop is not None:
            async def _del() -> None:
                try:
                    client = self._redis()
                    if client is not None:
                        await client.delete(self._key(app_id))
                except Exception:
                    log.debug("apps: could not clear the shared install job for %s",
                              app_id, exc_info=True)

            self._loop.call_soon_threadsafe(asyncio.ensure_future, _del())

    def all_active(self) -> list[dict[str, Any]]:
        """This worker's own tracked jobs — local-only snapshot. Kept for
        callers that only care about this process; the ``/ws/apps/
        install-status`` route uses :meth:`all_active_shared` (W4)."""
        return [job.as_dict() for job in self._jobs.values()]

    async def all_active_shared(self) -> list[dict[str, Any]]:
        """Cross-worker snapshot for a WS client's on-connect replay (W4).

        This worker's own jobs (authoritative, same precedence as
        :meth:`get_shared`), plus anything mirrored into Redis by another
        worker that this process has never seen — without this, a client
        connecting to a worker that isn't running any install saw an empty
        panel even mid-install, the other half of the bug W3 didn't cover
        (W3 only fixed ``GET .../install-status``, not this replay)."""
        jobs = {job.app_id: job.as_dict() for job in self._jobs.values()}
        if self._share:
            try:
                client = self._redis()
                if client is not None:
                    prefix = self._key("")
                    async for key in client.scan_iter(match=f"{prefix}*"):
                        app_id = key[len(prefix):]
                        if app_id in jobs:
                            continue
                        raw = await client.get(key)
                        if not raw:
                            continue
                        try:
                            jobs[app_id] = json.loads(raw)
                        except ValueError:
                            continue
            except Exception:
                log.debug("apps: could not read the shared install-job "
                         "snapshot for the WS replay — falling back to this "
                         "worker's own jobs only", exc_info=True)
        return list(jobs.values())

    def is_installing(self, app_id: str) -> bool:
        job = self._jobs.get(app_id)
        return bool(job and job.status == "installing")

    def start(self, app_id: str) -> InstallJob:
        job = InstallJob(app_id=app_id)
        self._jobs[app_id] = job
        self._broadcast(job)
        return job

    def mark_installed(self, app_id: str, summary: dict[str, Any]) -> None:
        job = self._jobs.setdefault(app_id, InstallJob(app_id=app_id))
        job.status = "installed"
        job.summary = summary
        job.error = None
        job.finished_at = time.time()
        self._broadcast(job)

    def mark_failed(self, app_id: str, error: str) -> None:
        job = self._jobs.setdefault(app_id, InstallJob(app_id=app_id))
        job.status = "failed"
        job.error = error
        job.finished_at = time.time()
        self._broadcast(job)

    def _broadcast(self, job: InstallJob) -> None:
        # Mirror to Redis on every transition (W3 STATE half), independently
        # of whether this worker has any WS listener — the point is the
        # OTHER workers' HTTP status polls, not this one's sockets.
        self._publish_shared(job)
        # W4 LIVE half: publish for the WS push too, independently of local
        # listeners — the point there is the OTHER workers' open sockets.
        if not self._loop:
            return
        payload = {"type": "app_install_status", "job": job.as_dict()}
        self._loop.call_soon_threadsafe(asyncio.ensure_future, self._publish_ws(payload))

    async def _publish_ws(self, payload: dict) -> None:
        """Single delivery path: publish to Redis, which this worker's own
        relay subscription (``_on_relay_message``) fans back out to
        ``_listeners`` locally too. Degrades to local-only delivery, loudly,
        if Redis is unreachable or the relay never came up."""
        if self._share and self._relay_up:
            try:
                await self._broadcaster.publish(_WS_TOPIC, payload)
                return
            except Exception:
                log.warning("apps: install-status Redis publish failed — "
                           "falling back to local-only delivery for this "
                           "transition", exc_info=True)
        await self._send_all(json.dumps(payload))

    async def _send_all(self, msg: str) -> None:
        dead = []
        for ws in self._listeners:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._listeners.discard(ws)
