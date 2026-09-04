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
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)

#: A finished install's status only has to outlive the UI's poll interval;
#: an in-flight one is refreshed on every transition. Long enough that a
#: user watching the Marketplace panel always sees the terminal state.
JOB_TTL_S = 900


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

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

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
        """Snapshot of every tracked job — sent to a WS client on connect so
        it catches up on installs that started before it connected."""
        return [job.as_dict() for job in self._jobs.values()]

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
        # Mirror to Redis on every transition, independently of whether this
        # worker has any WS listener — the point is the OTHER workers' HTTP
        # status polls, not this one's sockets.
        self._publish_shared(job)
        if not self._loop or not self._listeners:
            return
        msg = json.dumps({"type": "app_install_status", "job": job.as_dict()})
        self._loop.call_soon_threadsafe(asyncio.ensure_future, self._send_all(msg))

    async def _send_all(self, msg: str) -> None:
        dead = []
        for ws in self._listeners:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._listeners.discard(ws)
