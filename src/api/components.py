"""Component compatibility routes for the cloud SPA.

The React workspace UI still talks in the legacy "component" vocabulary:
``/api/components``, lifecycle POSTs, ``/ws/logs/<key>``, and component rows in
the ``/ws/status`` init payload. In the workspace data-plane, Tier-2 apps are
owned by :class:`src.apps.containers.ContainerSupervisor`; this module exposes
those registered app containers through that legacy shape so managed-app
windows keep the same title-bar controls without depending on the cloud
control-plane backend.
"""
from __future__ import annotations

import asyncio
import inspect
import logging

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from src.api.identity import authorize_ws, require_identity
from src.apps.containers import ContainerError

log = logging.getLogger(__name__)


def _runtime(app: FastAPI):
    return getattr(app.state, "app_runtime", None)


def _component_key(app_id: str) -> str:
    return f"docker:aw-{app_id}"


def _app_id_from_key(key: str) -> str:
    if key.startswith("docker:aw-"):
        return key.removeprefix("docker:aw-")
    if key.startswith("docker:"):
        return key.removeprefix("docker:")
    return key


def _service_key(app_id: str, service_id: str) -> str:
    return f"service:{app_id}:{service_id}"


def _service_snapshot(app: FastAPI) -> list[dict]:
    """Return `ctx.services`-managed subprocesses (F4) in the legacy shape.

    Unlike Tier-2 containers, these are `tier: inprocess` apps' OWN managed
    subprocesses (ServiceSupervisor) — e.g. aw-app-proxy's CONNECT-tunnel
    server. There is no container engine backing them, so status/logs come
    straight from the supervisor's Popen + captured stdout/stderr instead of
    `docker inspect` / `docker logs`.
    """
    rt = _runtime(app)
    services = getattr(rt, "services", None)
    if rt is None or services is None:
        return []

    rows: list[dict] = []
    for app_id, service_id in services.registered():
        loaded = rt.get(app_id)
        manifest = getattr(loaded, "manifest", None)
        status = rt.services.status(app_id, service_id)
        running = bool(status.get("running"))
        rows.append({
            "key": _service_key(app_id, service_id),
            "component": f"{app_id}:{service_id}",
            "mode": "service",
            "description": getattr(manifest, "description", "") or f"{app_id} service",
            "category": getattr(manifest, "category", None) or "Apps",
            "standalone_app": bool(getattr(manifest, "standalone_app", False)),
            "port": None,
            "depends": [],
            "setup_required": False,
            "menu_display": True,
            "status": "running" if running else "off",
            "running": running,
            "pid": status.get("pid"),
            "last_exit_code": status.get("last_exit_code"),
            "last_error": status.get("last_error"),
        })
    return rows


def component_snapshot(app: FastAPI) -> list[dict]:
    """Return Tier-2 app containers + Tier-1 managed services in the legacy
    frontend component shape.

    **Blocking — never call this directly inside an ``async def``.** The loop
    below runs ``rt.containers.status(app_id)`` once per registered Tier-2
    container, and each of those is two serial round-trips to the podman
    socket (``containers.get()`` + ``reload()``). With dozens of apps
    installed that is seconds to minutes of a worker's ONE loop thread, which
    freezes every other in-flight request — including a ``/ws/status``
    handshake that never gets far enough to be accepted or closed (found live
    2026-09-04). ``src/apps/containers.py`` caps a single stuck socket call,
    but the only thing that keeps the loop turning is the thread hop: every
    call site does ``await asyncio.to_thread(component_snapshot, app)``.
    """
    rt = _runtime(app)
    if rt is None:
        return []

    rows: list[dict] = []
    for app_id, container in rt.containers.registered():
        loaded = rt.get(app_id)
        try:
            status = rt.containers.status(app_id)
        except ContainerError:
            status = {"running": False, "status": "off", "container": container.name}
        running = bool(status.get("running"))
        manifest = getattr(loaded, "manifest", None)
        rows.append({
            "key": _component_key(app_id),
            "component": app_id,
            "mode": "docker",
            "description": getattr(manifest, "description", "") or f"{app_id} app container",
            "category": getattr(manifest, "category", None) or "Apps",
            "standalone_app": bool(getattr(manifest, "standalone_app", True)),
            "port": getattr(container, "port", None),
            "depends": [],
            "setup_required": False,
            "menu_display": True,
            "status": "running" if running else "off",
            "running": running,
            "container": status.get("container", container.name),
            "image": getattr(container, "image", None),
            "url": status.get("url"),
        })
    rows.extend(_service_snapshot(app))
    return rows


def _component_for(app: FastAPI, key: str) -> tuple[str, dict] | tuple[None, JSONResponse]:
    """Blocking — it goes through ``component_snapshot``. Every caller is an
    ``async def``, so all of them thread it in one hop rather than threading
    inside; see ``component_snapshot``'s own note."""
    rt = _runtime(app)
    if rt is None:
        return None, JSONResponse({"error": f"Unknown component: {key}"}, status_code=404)
    if key.startswith("service:"):
        row = next((c for c in _service_snapshot(app) if c["key"] == key), None)
        if row is None:
            return None, JSONResponse({"error": f"Unknown component: {key}"}, status_code=404)
        return key, row
    app_id = _app_id_from_key(key)
    if app_id not in dict(rt.containers.registered()):
        return None, JSONResponse({"error": f"Unknown component: {key}"}, status_code=404)
    row = next((c for c in component_snapshot(app) if c["component"] == app_id), None)
    if row is None:
        return None, JSONResponse({"error": f"Unknown component: {key}"}, status_code=404)
    return app_id, row


class ComponentRoutes:
    def __init__(self, app: FastAPI):
        self.app = app
        app.get("/api/components")(self.list_components)
        app.get("/api/components/{key:path}/status")(self.get_status)
        app.post("/api/components/{key:path}/start")(self.start_component)
        app.post("/api/components/{key:path}/stop")(self.stop_component)
        app.post("/api/components/{key:path}/restart")(self.restart_component)
        app.websocket("/ws/logs/{key:path}")(self.log_stream)

    async def list_components(self, identity: dict = Depends(require_identity)):
        return await asyncio.to_thread(component_snapshot, self.app)

    async def get_status(self, key: str, identity: dict = Depends(require_identity)):
        app_id, row = await asyncio.to_thread(_component_for, self.app, key)
        if app_id is None:
            return row
        return row

    async def start_component(self, key: str, identity: dict = Depends(require_identity)):
        if key.startswith("service:"):
            return await self._mutate_service(
                key, "started", lambda rt, aid, sid: asyncio.to_thread(rt.services.start, aid, sid))
        return await self._mutate(
            key, "started", lambda rt, app_id: asyncio.to_thread(rt.containers.start, app_id))

    async def stop_component(self, key: str, identity: dict = Depends(require_identity)):
        if key.startswith("service:"):
            return await self._mutate_service(
                key, "stopped", lambda rt, aid, sid: asyncio.to_thread(rt.services.stop, aid, sid))
        return await self._mutate(
            key, "stopped", lambda rt, app_id: asyncio.to_thread(rt.containers.stop, app_id))

    async def restart_component(self, key: str, identity: dict = Depends(require_identity)):
        if key.startswith("service:"):
            async def restart_service(rt, aid, sid):
                await asyncio.to_thread(rt.services.stop, aid, sid)
                return await asyncio.to_thread(rt.services.start, aid, sid)
            return await self._mutate_service(key, "restarted", restart_service)

        async def restart(rt, app_id):
            await asyncio.to_thread(rt.containers.stop, app_id)
            return await asyncio.to_thread(rt.containers.start, app_id)
        return await self._mutate(key, "restarted", restart)

    async def _mutate(self, key: str, action: str, fn):
        app_id, row = await asyncio.to_thread(_component_for, self.app, key)
        if app_id is None:
            return row
        rt = _runtime(self.app)
        try:
            result = fn(rt, app_id)
            if inspect.isawaitable(result):
                result = await result
        except ContainerError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        rows = await asyncio.to_thread(component_snapshot, self.app)
        updated = next((c for c in rows if c["component"] == app_id), row)
        payload = {"key": updated["key"], "action": action, **updated, "result": result}
        hub = getattr(self.app.state, "status_hub", None)
        if hub is not None:
            hub.broadcast_soon(payload)
        return payload

    async def _mutate_service(self, key: str, action: str, fn):
        _, row = await asyncio.to_thread(_component_for, self.app, key)
        if row is None:
            return JSONResponse({"error": f"Unknown component: {key}"}, status_code=404)
        parts = key.split(":", 2)
        if len(parts) != 3:
            return JSONResponse({"error": f"Malformed service key: {key}"}, status_code=400)
        _, app_id, service_id = parts
        rt = _runtime(self.app)
        try:
            result = fn(rt, app_id, service_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # ServiceError or subprocess failure
            return JSONResponse({"error": str(e)}, status_code=400)

        updated = next((c for c in _service_snapshot(self.app) if c["key"] == key), row)
        payload = {"key": updated["key"], "action": action, **updated, "result": result}
        hub = getattr(self.app.state, "status_hub", None)
        if hub is not None:
            hub.broadcast_soon(payload)
        return payload

    async def log_stream(self, websocket: WebSocket, key: str):
        claims = await authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        app_id, row = await asyncio.to_thread(_component_for, self.app, key)
        if app_id is None:
            await websocket.accept()
            await websocket.close(code=4004, reason="Component not found")
            return

        await websocket.accept()
        rt = _runtime(self.app)

        if key.startswith("service:"):
            _, svc_app_id, service_id = key.split(":", 2)
            try:
                lines = await asyncio.to_thread(rt.services.logs, svc_app_id, service_id)
                if lines:
                    await websocket.send_text("\n".join(lines) + "\n")
                else:
                    await websocket.send_text(
                        "(no output captured yet for this managed service)\n")
            except Exception as e:
                await websocket.send_text(f"Unable to read service logs: {e}\n")

            try:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            return

        container = dict(rt.containers.registered())[app_id]
        try:
            obj = await asyncio.to_thread(rt.containers.docker().containers.get, container.name)
            logs = await asyncio.to_thread(obj.logs, tail=200)
            if logs:
                await websocket.send_text(logs.decode("utf-8", errors="replace"))
        except Exception as e:
            await websocket.send_text(f"Unable to read container logs: {e}\n")

        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass


def register_component_routes(app: FastAPI) -> None:
    ComponentRoutes(app)
