"""IdentityGuard (F6 Capability 1): mounted app sub-apps are no longer open.

HTTP without a valid identity JWT → 401 (was 200 in F1); with a bearer token →
200. WS handshake without a token → close 4401; with ``?token=`` → accepted.
The guard verifiers are monkeypatched so no real EdDSA key is needed.
"""
from __future__ import annotations

import asyncio
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import src.api.identity as identity
from src.apps.runtime import AppRuntime

_PLUGIN = """
    from fastapi import FastAPI, WebSocket

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.get("/")
            async def root():
                return {"ok": True}

            @api.websocket("/ws")
            async def ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.send_text("hello")
                await websocket.close()

            ctx.routes.register(api)

        async def deactivate(self):
            return None
"""


def _write_app(tmp_path):
    pkg = tmp_path / "guarded"
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent("""
    {
      "manifest_version": 1,
      "id": "guarded",
      "name": "guarded",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {"entrypoint": "plugin:AppPlugin"},
      "permissions": ["routes:register"],
      "contributes": {"routes": [{"prefix": "/api/apps/guarded"}]}
    }
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(_PLUGIN))
    return str(pkg)


@pytest.fixture()
def guarded_app(tmp_path, monkeypatch):
    # A token of "good" verifies; anything else is rejected.
    monkeypatch.setattr(
        identity, "decode_identity_jwt",
        lambda tok: {"sub": "u"} if tok == "good" else None)
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(_write_app(tmp_path), granted_permissions=["routes:register"]))
    return app


def test_http_requires_identity(guarded_app):
    client = TestClient(guarded_app)
    # no auth → 401, app never invoked
    assert client.get("/api/apps/guarded/").status_code == 401
    # valid bearer token → 200
    r = client.get("/api/apps/guarded/", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    # invalid token → 401
    assert client.get(
        "/api/apps/guarded/", headers={"Authorization": "Bearer bad"}).status_code == 401


def test_ws_without_token_closes_4401(guarded_app):
    client = TestClient(guarded_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/api/apps/guarded/ws") as ws:
            ws.receive_text()
    assert ei.value.code == 4401


def test_ws_with_token_query_is_accepted(guarded_app):
    client = TestClient(guarded_app)
    with client.websocket_connect("/api/apps/guarded/ws?token=good") as ws:
        assert ws.receive_text() == "hello"
