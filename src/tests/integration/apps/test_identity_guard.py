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


def test_auth_required_false_bypasses_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "decode_identity_jwt", lambda tok: None)
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(
        _write_app(tmp_path),
        granted_permissions=["routes:register"],
        config={"auth_required": False},
    ))

    client = TestClient(app)
    r = client.get("/api/apps/guarded/")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_required_false_still_forwards_identity_when_present(tmp_path, monkeypatch):
    # "App decides" is not "no auth": a caller who DOES present a valid
    # identity still gets it forwarded at scope["aw_identity"] even with
    # auth_required off — only a MISSING/invalid one is tolerated instead of
    # 401ing. Lets one route serve both a cookie-based dashboard caller and
    # a bearer-token-only external caller under the same relaxed setting.
    monkeypatch.setattr(
        identity, "decode_identity_jwt",
        lambda tok: {"sub": "u"} if tok == "good" else None)
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(
        _write_app(tmp_path),
        granted_permissions=["routes:register"],
        config={"auth_required": False},
    ))

    client = TestClient(app)
    # No identity presented at all — still let through (app decides).
    r = client.get("/api/apps/guarded/whoami")
    assert r.status_code == 200
    assert r.json() == {"aw_identity": None}
    # A valid identity IS forwarded, not discarded, when present.
    r = client.get("/api/apps/guarded/whoami", headers={"Authorization": "Bearer good"})
    assert r.status_code == 200
    assert r.json() == {"aw_identity": {"sub": "u"}}
    # An INVALID identity doesn't 401 either (app decides) — just isn't forwarded.
    r = client.get("/api/apps/guarded/whoami", headers={"Authorization": "Bearer bad"})
    assert r.status_code == 200
    assert r.json() == {"aw_identity": None}


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


_SHARE_PLUGIN = """
    from fastapi import FastAPI, Request

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.get("/presentations/{pres_id}/html")
            async def html(pres_id: str, request: Request):
                # Stands in for the real route's validate_share_token: the APP
                # is what judges the token, the guard only has to let it in.
                if request.query_params.get("token") != "good-share-token":
                    return JSONResponse({"detail": "forbidden"}, status_code=403)
                return HTMLResponse("<html>slides</html>")

            @api.get("/presentations")
            async def index():
                return {"presentations": []}

            ctx.routes.register(api)

        async def deactivate(self):
            return None
"""


@pytest.fixture()
def presentations_app(tmp_path, monkeypatch):
    # No valid identity exists at all here — a share-link recipient is often
    # not a workspace user, which is the whole point of the carve-out.
    monkeypatch.setattr(identity, "decode_identity_jwt", lambda tok: None)
    pkg = tmp_path / "presentations"
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent("""
    {
      "manifest_version": 1,
      "id": "presentations",
      "name": "presentations",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {"entrypoint": "plugin:AppPlugin"},
      "permissions": ["routes:register"],
      "contributes": {"routes": [{"prefix": "/api/apps/presentations"}]}
    }
    """))
    (pkg / "plugin.py").write_text(
        "from fastapi.responses import HTMLResponse, JSONResponse\n"
        + textwrap.dedent(_SHARE_PLUGIN)
    )
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    asyncio.run(rt.load(str(pkg), granted_permissions=["routes:register"]))
    return app


def test_share_link_reaches_the_app_without_any_identity(presentations_app):
    # The bug this exists for: a share link's credential is an opaque UUID in
    # the query string, not an identity JWT, so the guard could only ever 401
    # it — and did, for every link share_presentation ever handed out.
    client = TestClient(presentations_app)
    r = client.get(
        "/api/apps/presentations/presentations/deck-1/html?token=good-share-token")
    assert r.status_code == 200
    assert r.text == "<html>slides</html>"


def test_bad_share_token_is_403ed_by_the_app_not_401ed_by_the_guard(presentations_app):
    # The guard delegates; it does not validate. A token it lets through that
    # the app rejects must surface the APP's 403, never a guard 401 — that
    # distinction is what keeps the carve-out from being a hole.
    client = TestClient(presentations_app)
    r = client.get(
        "/api/apps/presentations/presentations/deck-1/html?token=made-up")
    assert r.status_code == 403


def test_share_link_carve_out_needs_a_non_empty_token(presentations_app):
    client = TestClient(presentations_app)
    assert client.get(
        "/api/apps/presentations/presentations/deck-1/html").status_code == 401
    assert client.get(
        "/api/apps/presentations/presentations/deck-1/html?token=").status_code == 401


def test_share_link_carve_out_does_not_widen_the_rest_of_the_app(presentations_app):
    # A token on any OTHER route of the same app buys nothing.
    client = TestClient(presentations_app)
    assert client.get(
        "/api/apps/presentations/presentations?token=good-share-token").status_code == 401


def test_valid_workspace_api_key_authenticates_app_routes(guarded_app, monkeypatch):
    # A workspace API key lets an external app/MCP call ANY installed app's
    # routes without a browser-issued JWT — same guard, second credential.
    import src.api.workspace_api_key as api_key_mod
    monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "the-real-key")

    client = TestClient(guarded_app)
    r = client.get("/api/apps/guarded/whoami", headers={api_key_mod.HEADER_NAME: "the-real-key"})
    assert r.status_code == 200
    assert r.json() == {"aw_identity": {"sub": "workspace-api-key", "api_key": True}}


def test_wrong_workspace_api_key_is_401ed(guarded_app, monkeypatch):
    import src.api.workspace_api_key as api_key_mod
    monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "the-real-key")

    client = TestClient(guarded_app)
    r = client.get("/api/apps/guarded/", headers={api_key_mod.HEADER_NAME: "wrong-key"})
    assert r.status_code == 401


def test_ws_with_workspace_api_key_header_is_accepted(guarded_app, monkeypatch):
    # Mirrors the HTTP workspace-api-key path above — a caller that can set
    # WS upgrade-request headers (this workspace's own CLI, an external
    # MCP, a CDP-driven automation tool) authenticates the same way it
    # already does for HTTP, instead of needing a browser-issued JWT it has
    # no way to hold. Confirmed missing live 2026-08-08: aw-app-code-server's
    # WebSocket-dependent UI hung under a Playwright session authenticated
    # only via X-Api-Key — every WS handshake 4401'd while HTTP calls in the
    # same session succeeded.
    import src.api.workspace_api_key as api_key_mod
    monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "the-real-key")

    client = TestClient(guarded_app)
    with client.websocket_connect(
        "/api/apps/guarded/ws", headers={api_key_mod.HEADER_NAME: "the-real-key"}
    ) as ws:
        assert ws.receive_text() == "hello"


def test_ws_with_wrong_workspace_api_key_header_closes_4401(guarded_app, monkeypatch):
    import src.api.workspace_api_key as api_key_mod
    monkeypatch.setattr(api_key_mod, "verify_workspace_api_key", lambda presented: presented == "the-real-key")

    client = TestClient(guarded_app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
            "/api/apps/guarded/ws", headers={api_key_mod.HEADER_NAME: "wrong-key"}
        ) as ws:
            ws.receive_text()
    assert ei.value.code == 4401
