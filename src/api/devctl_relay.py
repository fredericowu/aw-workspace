"""DevCtl relay — remote JS eval into the USER's own browser tab.

The SPA ships `src/lib/devctlClient.js` (force-enabled) which opens a WebSocket
to ``/ws/devctl`` and executes ``{cmd:'eval', id, code}`` messages in the live
page, replying ``{id, result, error, ms}``. This is the server side that was
missing: it authenticates the tab (so we know WHICH user connected — the
"identify you accessing" part), registers it, and relays eval commands from an
authenticated caller (or in-workspace localhost, e.g. the agent) into that tab.

Endpoints registered on the host FastAPI app:
- WS   ``/ws/devctl``          — a user browser tab connects here (auth via aw_id_jwt)
- GET  ``/api/devctl/tabs``    — list currently-connected tabs (who's here)
- POST ``/api/devctl/eval``    — {code, user?} → run JS in a connected tab, return its result
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket

from src.api.identity import authorize_ws, require_identity

log = logging.getLogger("aw.devctl_relay")


class DevctlRelay:
    def __init__(self) -> None:
        self.tabs: dict[int, dict] = {}      # conn_id -> {ws, user, ua}
        self._pending: dict[int, asyncio.Future] = {}
        self._req_ids = itertools.count(1)
        self._conn_ids = itertools.count(1)

    def list_tabs(self) -> list[dict]:
        return [{"conn_id": cid, "user": t["user"], "ua": t.get("ua", "")}
                for cid, t in self.tabs.items()]

    async def eval(self, code: str, user: str | None = None, timeout: float = 15.0) -> dict:
        targets = [(cid, t) for cid, t in self.tabs.items()
                   if user is None or t["user"] == user]
        if not targets:
            raise RuntimeError(f"no connected tab" + (f" for user {user}" if user else ""))
        cid, t = targets[-1]  # most recently connected
        req_id = next(self._req_ids)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await t["ws"].send_text(json.dumps({"cmd": "eval", "id": req_id, "code": code}))
        try:
            res = await asyncio.wait_for(fut, timeout)
            return {"conn_id": cid, "user": t["user"], **res}
        finally:
            self._pending.pop(req_id, None)

    def _resolve(self, msg: dict) -> None:
        fut = self._pending.get(msg.get("id"))
        if fut and not fut.done():
            fut.set_result({k: msg.get(k) for k in ("result", "error", "ms")})


relay = DevctlRelay()


def register_devctl_relay(app: FastAPI) -> None:
    @app.websocket("/ws/devctl")
    async def devctl_ws(websocket: WebSocket):  # noqa: ANN202
        claims = authorize_ws(websocket)
        if not claims:
            await websocket.accept()
            await websocket.close(code=4401, reason="unauthorized")
            return
        await websocket.accept()
        user = claims.get("sub") or claims.get("email") or "unknown"
        ua = websocket.headers.get("user-agent", "")
        cid = next(relay._conn_ids)
        relay.tabs[cid] = {"ws": websocket, "user": user, "ua": ua}
        log.info("devctl tab connected: conn=%s user=%s", cid, user)
        try:
            await websocket.send_text(json.dumps({"cmd": "hello"}))
            while True:
                raw = await websocket.receive_text()
                try:
                    relay._resolve(json.loads(raw))
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            relay.tabs.pop(cid, None)
            log.info("devctl tab gone: conn=%s user=%s", cid, user)

    @app.get("/api/devctl/tabs")
    async def devctl_tabs(identity: dict = Depends(require_identity)):  # noqa: ANN202
        return {"tabs": relay.list_tabs()}

    async def _eval_impl(data: dict) -> dict:
        code = data.get("code")
        if not code:
            return {"ok": False, "error": "code is required"}
        try:
            res = await relay.eval(code, user=data.get("user"),
                                   timeout=float(data.get("timeout") or 15.0))
            return {"ok": True, **res}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/devctl/eval")
    async def devctl_eval(request: Request, data: dict = Body(...)):  # noqa: ANN202
        # In-workspace localhost only (the agent drives it via 127.0.0.1 with no
        # cookie). External callers are refused — pushing JS into a user's tab is
        # privileged and only the workspace's own processes may do it.
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="devctl eval is localhost-only")
        return await _eval_impl(data)
