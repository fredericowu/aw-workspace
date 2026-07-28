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
    from fastapi import FastAPI, Request, WebSocket

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.get("/")
            async def root():
                return {"ok": True}

            @api.get("/whoami")
            async def whoami(request: Request):
                return {"aw_identity": request.scope.get("aw_identity")}

            @api.post("/eval")
            async def eval_(request: Request):
                return {"aw_identity": request.scope.get("aw_identity")}

            @api.websocket("/ws")
            async def ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.send_text("hello")
                await websocket.close()

            ctx.routes.register(api)

        async def deactivate(self):
            return None
"""


def _write_app(tmp_path, *, local_paths=None):
    pkg = tmp_path / "guarded"
    pkg.mkdir()
    routes = '{"prefix": "/api/apps/guarded"' + (
        f', "local_paths": {local_paths!r}'.replace("'", '"') if local_paths else ""
    ) + "}"
    permissions = '["routes:register", "routes:local"]' if local_paths else '["routes:register"]'
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "guarded",
      "name": "guarded",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {permissions},
      "contributes": {{"routes": [{routes}]}}
    }}
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


def test_verified_claims_land_at_scope_aw_identity(guarded_app):
    # ADR "Apps Own Their Front + Back Routes" Decision 2 — IdentityGuard
    # passes the decoded claims through so app handlers can read who's
    # calling (websocket.scope.get("aw_identity")), instead of discarding
    # them once verified.
    client = TestClient(guarded_app)
    r = client.get("/api/apps/guarded/whoami", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"aw_identity": {"sub": "u"}}


@pytest.fixture()
def local_bypass_app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        identity, "decode_identity_jwt",
        lambda tok: {"sub": "u"} if tok == "good" else None)
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(
        _write_app(tmp_path, local_paths=["/eval"]),
        granted_permissions=["routes:register", "routes:local"],
    ))
    return app


def test_local_paths_bypass_auth_from_loopback(local_bypass_app):
    # No Authorization header, but the caller is loopback and /eval is a
    # declared local_path — the request goes through with no aw_identity
    # (never verified), instead of a 401.
    client = TestClient(local_bypass_app, client=("127.0.0.1", 12345))
    r = client.post("/api/apps/guarded/eval")
    assert r.status_code == 200
    assert r.json() == {"aw_identity": None}


def test_local_paths_bypass_does_not_widen_other_routes(local_bypass_app):
    # "/" was never declared as a local_path — a loopback caller still needs
    # a valid token there.
    client = TestClient(local_bypass_app, client=("127.0.0.1", 12345))
    assert client.get("/api/apps/guarded/").status_code == 401


def test_local_paths_bypass_does_not_apply_to_non_loopback_callers(local_bypass_app):
    # A non-loopback caller hitting the SAME local_path still needs a token
    # — the escape hatch is for in-workspace callers only.
    client = TestClient(local_bypass_app, client=("10.0.0.5", 12345))
    assert client.post("/api/apps/guarded/eval").status_code == 401
