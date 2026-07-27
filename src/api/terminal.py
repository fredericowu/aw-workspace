"""Terminal session endpoints — aw-workspace (BYOD data-plane) port.

REST for lifecycle + a PTY WebSocket for I/O, plus a slim ``/ws/status`` push
socket so the SPA's terminal list stays live. Strangler-fig port of the
monolith's ``src/api/routes/terminal.py`` — the API + WS byte/JSON contract is
preserved EXACTLY so the cloud SPA (aw-frontend) talks to this unchanged via
its ``apiBase`` shim (``api.<ws>.workspace`` + the apex ``aw_id_jwt`` cookie).

Identity gate: every ``/api/terminals*`` and ``/api/v2/agent-sessions*`` route
requires a valid identity JWT (``require_identity``); both WebSockets validate
the JWT before doing anything (``authorize_ws`` — cookie or ``?token=``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile

from fastapi import (
    Body, Depends, FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from src.api.identity import authorize_ws, require_identity
from src.api.terminal_manager import TerminalManager, session_child_procs

log = logging.getLogger(__name__)

_DROPS_DIR = os.path.join(tempfile.gettempdir(), "aw-drops")


class StatusHub:
    """Fan-out for the ``/ws/status`` push socket (single-worker, in-memory)."""

    def __init__(self):
        self._clients: set[WebSocket] = set()

    def add(self, ws: WebSocket):
        self._clients.add(ws)

    def discard(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, message: dict):
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

        # Agent-session history: no agent CLIs on the BYOD image (yet), so the
        # picker is always empty — but the routes exist so the SPA never 404s.
        app.get("/api/v2/agent-sessions")(self.list_agent_sessions_v2)
        app.delete("/api/v2/agent-sessions/{session_id}")(self.hide_agent_session_v2)

        app.websocket("/ws/terminal/{session_id}")(self.terminal_stream)
        app.websocket("/ws/status")(self.status_stream)

    def _terminal_payload(self, session):
        return {
            "id": session.id, "name": session.name, "type": session.type,
            "alive": session.alive, "insecure": False, "agent_session_id": None,
        }

    def _broadcast_terminals(self, session_id=None, action=None):
        session = self.mgr.get(session_id) if session_id else None
        self.hub.broadcast_soon({
            "type": "terminal_update",
            "action": action or "update",
            "session_id": session_id,
            "terminal": self._terminal_payload(session) if session else None,
            "terminals": self.mgr.list_sessions(),
        })

    # ---- REST -----------------------------------------------------------

    async def list_terminals(self, include_hidden: bool = False,
                             identity: dict = Depends(require_identity)):
        return self.mgr.list_sessions(include_hidden=include_hidden)

    async def create_terminal(self, data: dict = Body(default={}),
                              identity: dict = Depends(require_identity)):
        session = self.mgr.create(
            name=data.get("name"),
            rows=data.get("rows", 24),
            cols=data.get("cols", 80),
            command=data.get("command"),
            session_type=data.get("type", "terminal"),
            initial_prompt=data.get("initial_prompt"),
            cwd=data.get("cwd"),
        )
        session.start_reader(asyncio.get_running_loop())
        self._broadcast_terminals(session.id, "create")
        return {"id": session.id, "name": session.name, "type": session.type}

    async def rename_terminal(self, session_id: str, data: dict = Body(...),
                             identity: dict = Depends(require_identity)):
        session = self.mgr.get(session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        session.name = data.get("name", session.name)
        self._broadcast_terminals(session_id, "rename")
        return {"id": session.id, "name": session.name, "success": True}

    async def restart_terminal(self, session_id: str, data: dict = Body(default={}),
                              identity: dict = Depends(require_identity)):
        session = self.mgr.restart(
            session_id,
            command=data.get("command"),
            name=data.get("name"),
            new_session=data.get("new_session", False),
        )
        if not session:
            return {"error": "Session not found", "success": False}
        session.start_reader(asyncio.get_running_loop())
        self._broadcast_terminals(session_id, "restart")
        return {"id": session.id, "name": session.name, "success": True}

    async def delete_terminal(self, session_id: str,
                             identity: dict = Depends(require_identity)):
        self.mgr.remove(session_id)
        self._broadcast_terminals(session_id, "delete")
        return {"success": True}

    async def write_terminal(self, session_id: str, data: dict = Body(...),
                            identity: dict = Depends(require_identity)):
        """HTTP fallback for keystrokes (the open WS is preferred).

        Supports ``{text}`` and ``{text, send_enter}`` — both are in use by
        the SPA (voice input embeds ``\\r`` directly; prompt/plan actions pass
        ``send_enter``).
        """
        session = self.mgr.get(session_id)
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
        session = self.mgr.get(session_id)
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
        session = self.mgr.get(session_id)
        if not session:
            return {"error": "Session not found", "success": False, "procs": []}
        procs = await asyncio.to_thread(session_child_procs, session.pid)
        return {"procs": procs, "count": len(procs)}

    async def kill_proc(self, session_id: str, pid: int,
                       identity: dict = Depends(require_identity)):
        import signal as _signal
        session = self.mgr.get(session_id)
        if not session:
            return {"error": "Session not found", "success": False}
        procs = await asyncio.to_thread(session_child_procs, session.pid)
        if not any(p["pid"] == pid for p in procs):
            return {"error": "PID does not belong to this session", "success": False}
        try:
            os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
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

    async def list_agent_sessions_v2(self, type: str = None,
                                    identity: dict = Depends(require_identity)):
        # No agent CLIs on the slim BYOD image yet — empty by design.
        return []

    async def hide_agent_session_v2(self, session_id: str, restore: int = 0,
                                   type: str = None,
                                   identity: dict = Depends(require_identity)):
        return {"success": True, "id": session_id, "visible": bool(restore)}

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

        session = self.mgr.get(session_id)
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
                "components": {},
                "terminals": self.mgr.list_sessions(),
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
    TerminalRoutes(app, mgr, hub)
    return mgr
