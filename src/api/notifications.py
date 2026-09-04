"""Notification engine — receive messages via REST, stream to UI via WebSocket.

Strangler-fig port of the monolith's ``src/api/routes/notifications.py``: same
queue/broadcast mechanics and API/WS contract (so the cloud SPA's
``TopNotifications``/``App.jsx`` panels work unchanged), rebound to this
workspace's own schema-isolated Postgres (``NotificationDB``) and gated by the
identity JWT like every other aw-workspace route (``terminal.py``'s pattern).

Any Tier-1 app can also fire a notification through this same engine via
``AppContext.notifications`` (``src/apps/base.py``) — this module owns the
``NotificationManager`` singleton that facade calls into.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import Body, Depends, FastAPI, WebSocket, WebSocketDisconnect

from src.api.identity import authorize_ws, require_identity
from src.api.notification_db import NotificationDB
from src.libs.redis_coord import RedisBroadcaster

log = logging.getLogger("notifications")

# W4: cross-worker fan-out topic. Notifications themselves are already
# persisted in Postgres (`get_pending`/`get_recent`, already cross-worker) —
# this relay carries only the live nudge to whichever workers have a
# /ws/notifications listener open right now.
_RELAY_TOPIC = "notifications"


class NotificationManager:
    """Manages the notification queue, Postgres persistence, and WS broadcast."""

    def __init__(self):
        self._db = NotificationDB()
        self._listeners: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broadcaster = RedisBroadcaster()
        self._relay_up = False

    def set_loop(self, loop):
        self._loop = loop

    async def start_relay(self) -> None:
        """Subscribe this worker to the ``notifications`` relay so a message
        published from ANY worker (including this one — redis_coord's single
        delivery path, no local shortcut) reaches this worker's
        ``_listeners``. Awaited from the lifespan (not fire-and-forget): the
        underlying ``psubscribe`` must be confirmed before this process
        starts serving requests, or a broadcast published in the gap between
        ``start()`` returning and the subscribe landing is simply never
        delivered (redis_coord's relay has no replay). Never raises: a
        workspace with no reachable Redis must still boot and deliver
        notifications to whichever worker produced them, which is exactly
        ``AW_WORKSPACE_WORKERS=1`` behaviour."""
        try:
            await self._broadcaster.start_relay(self._on_relay_message)
            self._relay_up = True
            log.info("notifications: subscribed to the %r relay for cross-worker fan-out",
                     _RELAY_TOPIC)
        except Exception:
            log.warning(
                "notifications: could not start the Redis relay — this worker "
                "will only deliver notifications to its own local listeners "
                "until restarted (harmless at AW_WORKSPACE_WORKERS=1; see "
                "doctor's `redis` check)", exc_info=True)

    async def _on_relay_message(self, topic: str, payload: dict) -> None:
        if topic != _RELAY_TOPIC:
            return
        await self._broadcast(json.dumps(payload))

    async def _publish(self, payload: dict) -> None:
        """Single delivery path: publish to Redis, which this worker's own
        relay subscription (``_on_relay_message``) fans back out locally —
        never call ``_broadcast`` directly from the producer side. Degrades
        to local-only delivery, loudly, if Redis is unreachable or the relay
        never came up, so a notification is never silently dropped."""
        if self._relay_up:
            try:
                await self._broadcaster.publish(_RELAY_TOPIC, payload)
                return
            except Exception:
                log.warning("notifications: Redis publish failed — falling "
                           "back to local-only delivery for this notification",
                           exc_info=True)
        await self._broadcast(json.dumps(payload))

    async def aclose(self) -> None:
        try:
            await self._broadcaster.stop()
        except Exception:
            log.debug("notifications: relay teardown raised", exc_info=True)

    def add_notification(self, message: str, level: str = "info", title: str = "",
                         source: str = "", url: str = "",
                         external_id: str = "", external_status: str = "",
                         supersedes: bool = True):
        """Add a notification, persist to DB, broadcast to WebSocket clients.

        Deduplicates: if (source, external_id, external_status) already exists,
        skip. If supersedes=True, older pending notifications for the same
        (source, external_id) are marked as superseded (e.g. PR new → CI
        passed → merged, only latest shows). Set supersedes=False for events
        like comments where all are relevant.
        """
        if external_id and self._db.has_notification(source, external_id, external_status):
            return None

        notif = self._db.add(
            message=message,
            level=level,
            title=title,
            source=source,
            url=url,
            external_id=external_id,
            external_status=external_status,
            supersedes=supersedes,
        )
        superseded_ids = notif.pop("superseded_ids", [])
        log.info("Notification #%d: [%s] %s (superseded: %s)", notif["id"], level, message[:80], superseded_ids or "none")

        if self._loop:
            payload = {
                "type": "ninja_notification",
                "notification": notif,
                "superseded_ids": superseded_ids,
            }
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._publish(payload),
            )
        return notif

    def dismiss(self, notif_id: int):
        """Mark notification as read."""
        self._db.mark_read(notif_id)

    def mark_delivered(self, notif_id: int):
        """Mark notification as delivered to the UI."""
        self._db.mark_delivered(notif_id)

    def get_pending(self):
        """Get all pending (new + delivered) notifications."""
        return self._db.get_pending()

    def get_recent(self, limit=50):
        """Get recent notifications (all statuses)."""
        return self._db.get_recent(limit)

    async def _broadcast(self, msg: str):
        dead = []
        for ws in self._listeners:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._listeners.discard(ws)

    def add_listener(self, ws: WebSocket):
        self._listeners.add(ws)

    def remove_listener(self, ws: WebSocket):
        self._listeners.discard(ws)


class NotificationRoutes:
    """Notification REST + WebSocket, registered onto the host ``FastAPI`` app."""

    def __init__(self, app: FastAPI, manager: NotificationManager):
        self.manager = manager

        app.post("/api/notify")(self.send_notification)
        app.delete("/api/notify/{notif_id}")(self.dismiss_notification)
        app.get("/api/notify/pending")(self.get_pending)
        app.get("/api/notify/recent")(self.get_recent)
        app.websocket("/ws/notifications")(self.notification_stream)

    # ---- REST -------------------------------------------------------------

    async def send_notification(self, data: dict = Body(...),
                               identity: dict = Depends(require_identity)):
        """Send a notification to the ninja.

        Body: {
            "message": "...",
            "level": "info|warning|error|success",
            "title": "...",
            "source": "...",
            "url": "...",
            "external_id": "...",
            "external_status": "..."
        }
        """
        message = data.get("message", "")
        if not message:
            return {"error": "message is required"}
        # to_thread on every manager call in this class: NotificationDB works
        # through the SYNCHRONOUS get_session (sync psycopg), and this process
        # runs a single uvicorn worker — one event-loop thread for every
        # request. An inline call blocks all of them for the DB round-trip.
        notif = await asyncio.to_thread(
            self.manager.add_notification,
            message=message,
            level=data.get("level", "info"),
            title=data.get("title", ""),
            source=data.get("source", ""),
            url=data.get("url", ""),
            external_id=data.get("external_id", ""),
            external_status=data.get("external_status", ""),
            supersedes=data.get("supersedes", True),
        )
        if notif is None:
            return {"success": False, "reason": "duplicate"}
        return {"success": True, "notification": notif}

    async def dismiss_notification(self, notif_id: int,
                                  identity: dict = Depends(require_identity)):
        """Dismiss (mark as read) a notification."""
        await asyncio.to_thread(self.manager.dismiss, notif_id)
        return {"success": True}

    async def get_pending(self, identity: dict = Depends(require_identity)):
        """Get all pending notifications."""
        return {"notifications": await asyncio.to_thread(self.manager.get_pending)}

    async def get_recent(self, limit: int = 50,
                        identity: dict = Depends(require_identity)):
        """Get recent notifications (all statuses)."""
        return {"notifications": await asyncio.to_thread(self.manager.get_recent, limit)}

    # ---- WebSocket ----------------------------------------------------------

    async def notification_stream(self, websocket: WebSocket):
        """WebSocket — sends pending notifications on connect, then live updates."""
        claims = authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return

        await websocket.accept()

        def _drain_pending() -> list[dict]:
            """One thread hop for the read AND every mark_delivered write:
            done one-call-at-a-time this was N serial DB round-trips on the
            event-loop thread, so a client connecting with a backlog stalled
            every other in-flight request for the whole backlog."""
            rows = self.manager.get_pending()
            for n in rows:
                self.manager.mark_delivered(n["id"])
            return rows

        pending = await asyncio.to_thread(_drain_pending)
        if pending:
            await websocket.send_text(json.dumps({
                "type": "ninja_init",
                "notifications": pending,
            }))
        self.manager.add_listener(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass  # viewer closed the connection
        finally:
            self.manager.remove_listener(websocket)


def register_notification_routes(app: FastAPI) -> NotificationManager:
    """Wire the notification engine onto ``app``. Returns the manager.

    The manager is also stashed on ``app.state.notification_mgr`` so the apps
    runtime's ``AppContext.notifications`` facade (``src/apps/base.py``) can
    reach the same singleton without importing this module directly.
    """
    manager = NotificationManager()
    NotificationRoutes(app, manager)
    app.state.notification_mgr = manager
    return manager
