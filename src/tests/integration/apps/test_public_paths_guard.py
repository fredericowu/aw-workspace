"""IdentityGuard honors manifest ``public_paths`` — and nothing wider.

A path opens only when ALL of: ``routes:public`` was granted, the manifest
names that exact full path, and the owner set ``public_paths_approved``. Every
other route of the same app keeps the identity requirement.
"""
from __future__ import annotations

import asyncio
import json
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.identity as identity
from src.apps.runtime import AppRuntime

_PLUGIN = """
    from fastapi import FastAPI, Request, WebSocket

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.post("/webhooks")
            async def webhooks(request: Request):
                return {"reached": "app", "aw_identity": request.scope.get("aw_identity")}

            @api.post("/webhooks/extra")
            async def webhooks_extra():
                return {"reached": "app"}

            @api.get("/settings")
            async def settings():
                return {"secret": "settings"}

            @api.websocket("/webhooks")
            async def ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.close()

            ctx.routes.register(api)

        async def deactivate(self):
            return None
"""


def _write_app(tmp_path):
    pkg = tmp_path / "hooky"
    pkg.mkdir()
    manifest = {
        "manifest_version": 1,
        "id": "hooky",
        "name": "hooky",
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "hooky_public_paths_guard:AppPlugin"},
        "permissions": ["routes:register", "routes:public"],
        "contributes": {"routes": [{
            "prefix": "/api/apps/hooky",
            "public_paths": ["/api/apps/hooky/webhooks"],
        }]},
    }
    (pkg / "aw-app.json").write_text(json.dumps(manifest))
    (pkg / "hooky_public_paths_guard.py").write_text(textwrap.dedent(_PLUGIN))
    return str(pkg)


def _load(tmp_path, monkeypatch, *, granted, config):
    monkeypatch.setattr(
        identity, "decode_identity_jwt",
        lambda tok: {"sub": "u"} if tok == "good" else None)
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(_write_app(tmp_path), granted_permissions=granted,
                        config=config))
    return app, rt


ALL = ["routes:register", "routes:public"]


def test_approved_public_path_reaches_the_app_with_no_identity(tmp_path, monkeypatch):
    app, _ = _load(tmp_path, monkeypatch, granted=ALL,
                   config={"public_paths_approved": True})
    r = TestClient(app).post("/api/apps/hooky/webhooks")
    assert r.status_code == 200
    assert r.json() == {"reached": "app", "aw_identity": None}


def test_other_routes_of_the_same_app_stay_gated(tmp_path, monkeypatch):
    app, _ = _load(tmp_path, monkeypatch, granted=ALL,
                   config={"public_paths_approved": True})
    client = TestClient(app)
    assert client.get("/api/apps/hooky/settings").status_code == 401
    # exact match only: a deeper path is not covered by the declared one
    assert client.post("/api/apps/hooky/webhooks/extra").status_code == 401
    assert client.post("/api/apps/hooky/webhooks/").status_code == 401
    # still reachable with a real identity
    assert client.get("/api/apps/hooky/settings",
                      headers={"Authorization": "Bearer good"}).status_code == 200


def test_without_owner_approval_the_path_stays_gated(tmp_path, monkeypatch):
    app, _ = _load(tmp_path, monkeypatch, granted=ALL, config={})
    assert TestClient(app).post("/api/apps/hooky/webhooks").status_code == 401


def test_approval_must_be_literally_true(tmp_path, monkeypatch):
    app, _ = _load(tmp_path, monkeypatch, granted=ALL,
                   config={"public_paths_approved": "yes"})
    assert TestClient(app).post("/api/apps/hooky/webhooks").status_code == 401


def test_without_the_granted_capability_the_path_stays_gated(tmp_path, monkeypatch):
    # Declared + approved, but routes:public not granted (e.g. refused by the
    # trust filter) — nothing opens.
    app, _ = _load(tmp_path, monkeypatch, granted=["routes:register"],
                   config={"public_paths_approved": True})
    assert TestClient(app).post("/api/apps/hooky/webhooks").status_code == 401


def test_revoking_approval_closes_the_path_without_a_remount(tmp_path, monkeypatch):
    app, rt = _load(tmp_path, monkeypatch, granted=ALL,
                    config={"public_paths_approved": True})
    client = TestClient(app)
    assert client.post("/api/apps/hooky/webhooks").status_code == 200
    rt.get("hooky").config = {"public_paths_approved": False}
    assert client.post("/api/apps/hooky/webhooks").status_code == 401


def test_websocket_on_a_public_path_is_not_opened(tmp_path, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    app, _ = _load(tmp_path, monkeypatch, granted=ALL,
                   config={"public_paths_approved": True})
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/api/apps/hooky/webhooks") as ws:
            ws.receive_text()
    assert exc.value.code == 4401
