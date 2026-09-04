"""Terminal session endpoints — aw-workspace (BYOD data-plane) port.

REST for lifecycle + a PTY WebSocket for I/O, plus a slim ``/ws/status`` push
socket so the SPA's terminal list stays live. Strangler-fig port of the
monolith's ``src/api/routes/terminal.py`` — the API + WS byte/JSON contract is
preserved EXACTLY so the cloud SPA (aw-workspace-ui) talks to this unchanged
via its ``apiBase`` shim (``api.<ws>.workspace`` + the apex ``aw_id_jwt``
cookie).

Identity gate: every ``/api/terminals*`` route requires a valid identity JWT
(``require_identity``); both WebSockets validate the JWT before doing
anything (``authorize_ws`` — cookie or ``?token=``).

Agent-session history (``/api/v2/agent-sessions*``) used to live here as an
always-empty stub ("no agent CLIs on the BYOD image yet"). Per the
2026-08-03 decision to decouple Terminals+AgentsNav's session-history piece
out of core (see docs/knowledge_base/docs/architecture/
monolith-migration-roadmap.md), it moved to the app that actually installs
those CLIs — ``aw-app-code-agent-clis``'s ``/api/apps/code-agent-clis/
agent-sessions`` route. The plain PTY terminal shell below stays core.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import tempfile

from fastapi import (
    Body, Depends, FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from src.api.components import component_snapshot
from src.api.identity import authorize_ws, require_identity
from src.api.terminal_manager import (
    TerminalManager, kill_proc_tree,
)
from src.libs.redis_coord import RedisBroadcaster

log = logging.getLogger(__name__)

_DROPS_DIR = os.path.join(tempfile.gettempdir(), "aw-drops")

#: W4 relay topic for the /ws/status push socket.
_STATUS_TOPIC = "terminal-status"


class StatusHub:
    """Fan-out for the ``/ws/status`` push socket.

    W4: cross-worker via ``RedisBroadcaster`` — a ``terminal_update`` fired
    by a REST call that landed on one worker must reach a client whose
    ``/ws/status`` socket is open on a different one.
    """

    def __init__(self):
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broadcaster = RedisBroadcaster()
        self._relay_up = False

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start_relay(self) -> None:
        """Awaited from the lifespan, not fire-and-forget — see
        ``NotificationManager.start_relay`` for why. Never raises: a
        workspace with no reachable Redis must still push terminal updates
        to whichever worker produced them, which is exactly
        ``AW_WORKSPACE_WORKERS=1`` behaviour."""
        try:
            await self._broadcaster.start_relay(self._on_relay_message)
            self._relay_up = True
        except Exception:
            log.warning(
                "terminal: could not start the /ws/status Redis relay — this "
                "worker will only deliver terminal updates to its own local "
                "listeners until restarted (harmless at "
                "AW_WORKSPACE_WORKERS=1)", exc_info=True)

    async def _on_relay_message(self, topic: str, payload: dict) -> None:
        if topic != _STATUS_TOPIC:
            return
        await self._send_to_all(payload)

    def add(self, ws: WebSocket):
        self._clients.add(ws)

    def discard(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, message: dict):
        """Push ``message`` to every ``/ws/status`` listener across every
        worker via the Redis relay (single delivery path — this worker's own
        listeners are reached through ``_on_relay_message`` too, never
        directly from here). Degrades to local-only delivery, loudly, if
        Redis is unreachable or the relay never came up."""
        if self._relay_up:
            try:
                await self._broadcaster.publish(_STATUS_TOPIC, message)
                return
            except Exception:
                log.warning("terminal: /ws/status Redis publish failed — "
                           "falling back to local-only delivery for this "
                           "message", exc_info=True)
        await self._send_to_all(message)

    async def _send_to_all(self, message: dict):
        payload = json.dumps(message)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def broadcast_soon(self, message: dict):
        """Schedule a broadcast from a sync context on the running loop."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        loop.create_task(self.broadcast(message))

    async def aclose(self) -> None:
        try:
            await self._broadcaster.stop()
        except Exception:  # noqa: BLE001 — shutdown path
            pass


class TerminalRoutes:
    """Terminal CRUD + WebSocket streaming, registered onto a FastAPI app."""

    def __init__(self, app: FastAPI, terminal_mgr: TerminalManager, status_hub: StatusHub):
        self.app = app
        self.mgr = terminal_mgr
        self.hub = status_hub

        app.get("/api/terminals")(self.list_terminals)
        app.post("/api/terminals")(self.create_terminal)
        app.post("/api/terminals/upload")(self.upload_file)
        app.put("/api/terminals/{session_id}")(self.rename_terminal)
        app.post("/api/terminals/{session_id}/restart")(self.restart_terminal)
        app.delete("/api/terminals/{session_id}")(self.delete_terminal)
        app.post("/api/terminals/{session_id}/write")(self.write_terminal)
        app.get("/api/terminals/{session_id}/scrollback")(self.get_scrollback)
        app.get("/api/terminals/{session_id}/procs")(self.list_procs)
        app.post("/api/terminals/{session_id}/procs/{pid}/kill")(self.kill_proc)

        app.websocket("/ws/terminal/{session_id}")(self.terminal_stream)
        app.websocket("/ws/status")(self.status_stream)

    def _terminal_payload(self, session):
        return {
            "id": session.id, "name": session.name, "type": session.type,
            "alive": session.alive, "insecure": session.insecure,
            "agent_session_id": session.agent_session_id,
        }

    async def _broadcast_terminals(self, session_id=None, action=None,
                                   session=None):
        """Push the fleet's terminal list to every /ws/status listener.

        ``list_sessions`` shells out to ``screen -ls`` on a screen-backed
        workspace, so it goes through ``asyncio.to_thread`` like every other
        blocking call in this file — inside an ``async def`` it would freeze
        every in-flight request on this worker for the subprocess's duration
        (the 2026-09-02 event-loop freeze, whose regression guard cannot see
        through ``self.mgr.<method>`` attribute access).
        """
        terminals = await asyncio.to_thread(self.mgr.list_sessions)
        await self.hub.broadcast({
            "type": "terminal_update",
            "action": action or "update",
            "session_id": session_id,
            "terminal": self._terminal_payload(session) if session else None,
            "terminals": terminals,
        })

    # ---- REST -----------------------------------------------------------

    async def list_terminals(self, include_hidden: bool = False,
                             identity: dict = Depends(require_identity)):
        return await asyncio.to_thread(self.mgr.list_sessions, include_hidden)

    async def create_terminal(self, data: dict = Body(default={}),
                              identity: dict = Depends(require_identity)):
        # to_thread: a screen-backed create runs `screen -dmS`, polls
        # `screen -ls` until the server is up, then forks the attach PTY —
        # up to a couple of seconds of blocking work that must not happen on
        # the event-loop thread.
        session = await asyncio.to_thread(
            functools.partial(
                self.mgr.create,
                name=data.get("name"),
                rows=data.get("rows", 24),
                cols=data.get("cols", 80),
                command=data.get("command"),
                session_type=data.get("type", "terminal"),
                initial_prompt=data.get("initial_prompt"),
                cwd=data.get("cwd"),
            )
        )
        session.start_reader(asyncio.get_running_loop())
        await self._broadcast_terminals(session.id, "create", session)
        return {"id": session.id, "name": session.name, "type": session.type}

    async def rename_terminal(self, session_id: str, data: dict = Body(...),
                             identity: dict = Depends(require_identity)):
        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        # Through the manager, not `session.name = ...`: the name has to reach
        # Redis or the other workers keep serving the old one (W5 guarantee 2).
        await asyncio.to_thread(self.mgr.set_name, session_id,
                                data.get("name", session.name))
        await self._broadcast_terminals(session_id, "rename", session)
        return {"id": session.id, "name": session.name, "success": True}

    async def restart_terminal(self, session_id: str, data: dict = Body(default={}),
                              identity: dict = Depends(require_identity)):
        loop = asyncio.get_running_loop()
        # The loop is passed in explicitly because this now runs on a worker
        # thread, where asyncio.get_event_loop() would fail and leave the old
        # session's fd reader installed on a closed fd.
        session = await asyncio.to_thread(
            functools.partial(
                self.mgr.restart,
                session_id,
                command=data.get("command"),
                name=data.get("name"),
                new_session=data.get("new_session", False),
                is_insecure=data.get("is_insecure"),
                loop=loop,
            )
        )
        if not session:
            return {"error": "Session not found", "success": False}
        session.start_reader(loop)
        await self._broadcast_terminals(session_id, "restart", session)
        return {"id": session.id, "name": session.name, "success": True}

    async def delete_terminal(self, session_id: str,
                             identity: dict = Depends(require_identity)):
        await asyncio.to_thread(self.mgr.remove, session_id,
                                asyncio.get_running_loop())
        await self._broadcast_terminals(session_id, "delete")
        return {"success": True}

    async def write_terminal(self, session_id: str, data: dict = Body(...),
                            identity: dict = Depends(require_identity)):
        """HTTP fallback for keystrokes (the open WS is preferred).

        Supports ``{text}`` and ``{text, send_enter}`` — both are in use by
        the SPA (voice input embeds ``\\r`` directly; prompt/plan actions pass
        ``send_enter``).
        """
        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        text = data.get("text", "")
        if text:
            session.write(text.encode())
        if data.get("send_enter", False):
            await asyncio.sleep(0.1)
            session.write(b"\r")
        return {"success": True}

    async def get_scrollback(self, session_id: str,
                            identity: dict = Depends(require_identity)):
        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        raw = session.get_scrollback()
        if not raw:
            return {"content": ""}
        import re
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
        text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
        text = re.sub(r"\r", "", text)
        return {"content": text}

    async def list_procs(self, session_id: str,
                        identity: dict = Depends(require_identity)):
        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            return {"error": "Session not found", "success": False, "procs": []}
        # session.child_procs(), not session_child_procs(session.pid): on a
        # screen-backed session the shell hangs off the screen SERVER, not off
        # this worker's `screen -x` attach — see TerminalSession.proc_root_pid.
        procs = await asyncio.to_thread(session.child_procs)
        return {"procs": procs, "count": len(procs)}

    async def kill_proc(self, session_id: str, pid: int,
                       identity: dict = Depends(require_identity)):
        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        procs = await asyncio.to_thread(session.child_procs)
        if not any(p["pid"] == pid for p in procs):
            return {"error": "PID does not belong to this session", "success": False}
        # kill_proc_tree, not a bare os.kill: `pid` can be a mid-tree node
        # (e.g. an `apt-get`/`dpkg`/`bash` inside this session), and its own
        # live children (dpkg-preconfigure, git, gh, ...) would otherwise be
        # orphaned onto this container's PID 1 and never reaped — see
        # terminal_manager.kill_proc_tree's docstring.
        try:
            await asyncio.to_thread(kill_proc_tree, pid)
        except OSError as e:
            return {"error": str(e), "success": False}
        return {"success": True}

    async def upload_file(self, file: UploadFile = File(...),
                        identity: dict = Depends(require_identity)):
        os.makedirs(_DROPS_DIR, exist_ok=True)
        dest = os.path.join(_DROPS_DIR, os.path.basename(file.filename or "upload"))
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return {"path": dest, "filename": file.filename}

    # ---- WebSockets -----------------------------------------------------

    async def terminal_stream(self, websocket: WebSocket, session_id: str):
        """Bidirectional PTY socket: browser <-> shell, fan-out to N clients.

        Contract (unchanged from the monolith): inbound binary frames are raw
        keystrokes written to the PTY; an inbound TEXT frame that parses to
        ``{"type":"resize","rows","cols"}`` resizes the PTY; everything the
        server sends is raw PTY output bytes.
        """
        # Accept then close for the reject paths: closing a WS *before*
        # accept() is delivered as a bare HTTP rejection that some clients
        # (incl. Starlette's TestClient) never surface as a disconnect. Accept
        # first, then close with a code — no PTY is spawned and no input is
        # read, so nothing is exposed.
        claims = authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return

        session = await asyncio.to_thread(self.mgr.get, session_id)
        if not session:
            await websocket.accept()
            await websocket.close(code=4004, reason="Session not found")
            return

        await websocket.accept()
        loop = asyncio.get_running_loop()
        session.start_reader(loop)

        scrollback = session.get_scrollback()
        if scrollback:
            await websocket.send_bytes(scrollback)

        queue = session.subscribe()

        async def pty_to_ws():
            try:
                while session.alive:
                    data = await queue.get()
                    if not data:
                        break
                    buf = bytearray(data)
                    while not queue.empty():
                        chunk = queue.get_nowait()
                        if not chunk:
                            break
                        buf.extend(chunk)
                    try:
                        await websocket.send_bytes(bytes(buf))
                    except Exception:
                        break
            except asyncio.CancelledError:
                pass

        reader_task = asyncio.create_task(pty_to_ws())
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg and msg["bytes"]:
                    session.write(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    text = msg["text"]
                    try:
                        parsed = json.loads(text)
                        if parsed.get("type") == "resize":
                            session.resize(parsed.get("rows", 24), parsed.get("cols", 80))
                            continue
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    session.write(text.encode())
        except WebSocketDisconnect:
            pass
        finally:
            session.unsubscribe(queue)
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    async def status_stream(self, websocket: WebSocket):
        """Slim control-plane push socket the SPA subscribes to for live state.

        Sends an ``init`` snapshot on connect, then relays ``terminal_update``
        broadcasts. Send-nothing from the client (pure server push), matching
        the monolith's ``/ws/status`` shape for the terminal-relevant subset.
        """
        claims = authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        await websocket.accept()
        self.hub.add(websocket)
        try:
            await websocket.send_text(json.dumps({
                "type": "init",
                "components": component_snapshot(self.app),
                "terminals": await asyncio.to_thread(self.mgr.list_sessions),
            }))
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            self.hub.discard(websocket)


def register_terminal_routes(app: FastAPI) -> TerminalManager:
    """Wire the terminal subsystem onto ``app``. Returns the manager."""
    mgr = TerminalManager()
    hub = StatusHub()
    app.state.status_hub = hub
    TerminalRoutes(app, mgr, hub)
    return mgr
