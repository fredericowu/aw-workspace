"""IdentityGuard enforces scoped API keys on app routes (scoped-API-keys design).

The acceptance target: a scope covering only app ``xiaomi``'s ``/tv/*``, a
never-expiring key minted for it, and then a request carrying ONLY that key
must 200 on ``/api/apps/xiaomi/tv/power``, 403 (not 401) on every other
route of that app, 403 on another app's routes, and 403 on framework routes
(that last one lives in ``src/tests/integration/api/test_scoped_api_keys.py``
— it's ``require_identity``'s half, not the guard's).

``resolve_scoped_key`` is monkeypatched here, exactly like the existing
``verify_workspace_api_key`` tests in ``test_identity_guard.py`` do for the
master key: this file is about what the guard DOES with claims, not about
how they're read out of Postgres (that's the api/ module's job).

Both entry points are exercised deliberately. The path ``Mount`` and the
per-app-subdomain ``Host`` mount wrap the SAME guarded ASGI app, so they
cannot diverge by construction — but ``get_route_path`` derives the
mount-relative path differently for each (the Host mount has no prefix to
strip), and that derivation is what the scope match is fed.
"""
from __future__ import annotations

import asyncio
import textwrap

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import src.api.identity as identity
import src.api.scoped_api_keys as scoped_mod
import src.api.workspace_api_key as api_key_mod
from src.apps.runtime import AppRuntime

_PLUGIN = """
    from fastapi import FastAPI, Request, WebSocket

    class AppPlugin:
        async def activate(self, ctx):
            api = FastAPI()

            @api.post("/tv/power")
            async def power(request: Request):
                return {"route": "tv/power",
                        "aw_identity": request.scope.get("aw_identity")}

            @api.get("/tv/power")
            async def power_get(request: Request):
                return {"route": "tv/power", "method": "GET"}

            @api.get("/settings")
            async def settings(request: Request):
                return {"route": "settings"}

            @api.post("/eval")
            async def eval_(request: Request):
                return {"route": "eval",
                        "aw_identity": request.scope.get("aw_identity")}

            @api.websocket("/tv/ws")
            async def tv_ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.send_text("hello")
                await websocket.close()

            @api.websocket("/other/ws")
            async def other_ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.send_text("hello")
                await websocket.close()

            ctx.routes.register(api)

        async def deactivate(self):
            return None
"""

# The scope the whole acceptance target is written against: one app, one
# path prefix, nothing else.
_XIAOMI_TV_RULES = [{"app": "xiaomi", "paths": ["/tv/*"]}]
_SCOPED_TOKEN = "awsk_" + "a" * 64
_MASTER_KEY = "the-real-master-key"


def _write_app(tmp_path, app_id: str, *, local_paths=None):
    pkg = tmp_path / app_id
    pkg.mkdir()
    routes = f'{{"prefix": "/api/apps/{app_id}"' + (
        f', "local_paths": {local_paths!r}'.replace("'", '"') if local_paths else ""
    ) + "}"
    permissions = ('["routes:register", "routes:local"]' if local_paths
                   else '["routes:register"]')
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{app_id}",
      "name": "{app_id}",
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
def creds(monkeypatch):
    """A valid scoped key, a valid master key, and nothing else working."""
    monkeypatch.setattr(
        identity, "decode_identity_jwt",
        lambda tok: {"sub": "u"} if tok == "good" else None)
    monkeypatch.setattr(
        api_key_mod, "verify_workspace_api_key",
        lambda presented: presented == _MASTER_KEY)
    monkeypatch.setattr(
        scoped_mod, "resolve_scoped_key",
        lambda presented: ({"sub": "scoped-api-key", "scoped": True,
                            "key_id": "k1", "key_name": "alexa",
                            "scope_id": "s1", "scope_name": "xiaomi-tv",
                            "rules": _XIAOMI_TV_RULES}
                           if presented == _SCOPED_TOKEN else None))


def _runtime(app_ids, tmp_path, *, configs=None, local_paths=None, want_rt=False):
    app = FastAPI()
    rt = AppRuntime(app, guard_identity=True)
    for app_id in app_ids:
        lp = (local_paths or {}).get(app_id)
        perms = ["routes:register"] + (["routes:local"] if lp else [])
        asyncio.run(rt.load(
            _write_app(tmp_path, app_id, local_paths=lp),
            granted_permissions=perms,
            config=(configs or {}).get(app_id, {}),
        ))
    return (app, rt) if want_rt else app


@pytest.fixture()
def two_apps(tmp_path, creds):
    """``xiaomi`` (the scoped key's target) plus an unrelated second app.

    The second slug is deliberately not a bare generic one: AppRuntime caches
    a loaded app's modules in ``sys.modules`` under ``aw_apps.<slug>`` and
    only drops them on unload, so a slug reused by another test FILE in the
    same pytest process silently runs this file's plugin.py there (it surfaces
    as a bogus PermissionError in the other test).
    """
    return _runtime(["xiaomi", "otherapp-scopedkeys"], tmp_path)


def _scoped():
    return {api_key_mod.HEADER_NAME: _SCOPED_TOKEN}


def _master():
    return {api_key_mod.HEADER_NAME: _MASTER_KEY}


# ---- the acceptance target, both entry points --------------------------


def test_scoped_key_200_on_a_covered_route_via_the_path_mount(two_apps):
    client = TestClient(two_apps)
    r = client.post("/api/apps/xiaomi/tv/power", headers=_scoped())
    assert r.status_code == 200
    assert r.json()["route"] == "tv/power"
    # The claims reach the app, same as any other credential class.
    assert r.json()["aw_identity"]["scoped"] is True
    assert r.json()["aw_identity"]["key_name"] == "alexa"


def test_scoped_key_200_on_a_covered_route_via_the_host_subdomain_mount(two_apps):
    # Second entry point: <app>.app.<anything>, path taken as-is with no
    # /api/apps/<slug> prefix. Same guarded app, but get_route_path derives
    # the mount-relative path a different way — which is the whole reason
    # this is tested separately rather than assumed from the Mount above.
    client = TestClient(two_apps)
    r = client.post("/tv/power", headers={
        **_scoped(), "host": "xiaomi.app.aw.workspace.example.com"})
    assert r.status_code == 200
    assert r.json()["route"] == "tv/power"


def test_scoped_key_403_on_another_route_of_the_same_app(two_apps):
    client = TestClient(two_apps)
    r = client.get("/api/apps/xiaomi/settings", headers=_scoped())
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden: key scope does not cover this route"


def test_scoped_key_403_on_another_apps_routes(two_apps):
    # The scope names app "xiaomi"; the same token on the other app is a valid
    # credential used where it does not apply — 403, never 200 and never 401.
    client = TestClient(two_apps)
    r = client.post("/api/apps/otherapp-scopedkeys/tv/power", headers=_scoped())
    assert r.status_code == 403
    assert r.json()["detail"] == "forbidden: key scope does not cover this route"


def test_scoped_key_403_on_another_apps_routes_via_the_host_mount(two_apps):
    client = TestClient(two_apps)
    r = client.post("/tv/power", headers={
        **_scoped(), "host": "otherapp-scopedkeys.app.aw.workspace.example.com"})
    assert r.status_code == 403


def test_an_unresolvable_scoped_key_is_401_not_403(two_apps):
    # 401 = not a credential at all. The distinction matters: 403 would tell
    # the caller its key is fine and only the route is wrong.
    client = TestClient(two_apps)
    r = client.post("/api/apps/xiaomi/tv/power",
                    headers={api_key_mod.HEADER_NAME: "awsk_" + "b" * 64})
    assert r.status_code == 401


def test_an_awsk_value_is_never_retried_against_the_master_key(tmp_path, creds):
    # The master key here IS valid, but it is presented under the awsk_
    # prefix. If the prefix check fell through on failure, a mistyped scoped
    # key would silently escalate to a workspace-wide credential.
    app = _runtime(["xiaomi"], tmp_path)
    client = TestClient(app)
    r = client.post("/api/apps/xiaomi/tv/power",
                    headers={api_key_mod.HEADER_NAME: "awsk_" + _MASTER_KEY})
    assert r.status_code == 401


def test_methods_list_narrows_a_rule(tmp_path, creds, monkeypatch):
    monkeypatch.setattr(
        scoped_mod, "resolve_scoped_key",
        lambda presented: ({"sub": "scoped-api-key", "scoped": True,
                            "rules": [{"app": "xiaomi", "paths": ["/tv/*"],
                                       "methods": ["POST"]}]}
                           if presented == _SCOPED_TOKEN else None))
    app = _runtime(["xiaomi"], tmp_path)
    client = TestClient(app)
    assert client.post("/api/apps/xiaomi/tv/power",
                       headers=_scoped()).status_code == 200
    assert client.get("/api/apps/xiaomi/tv/power",
                      headers=_scoped()).status_code == 403


def test_exact_path_rule_does_not_cover_siblings(tmp_path, creds, monkeypatch):
    monkeypatch.setattr(
        scoped_mod, "resolve_scoped_key",
        lambda presented: ({"sub": "scoped-api-key", "scoped": True,
                            "rules": [{"app": "xiaomi", "paths": ["/tv/power"]}]}
                           if presented == _SCOPED_TOKEN else None))
    app = _runtime(["xiaomi"], tmp_path)
    client = TestClient(app)
    assert client.post("/api/apps/xiaomi/tv/power",
                       headers=_scoped()).status_code == 200
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_scoped()).status_code == 403


# ---- auth_type: the master key, and ONLY the master key ----------------


def test_master_key_403s_on_an_auth_type_scoped_app(tmp_path, creds):
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_type": "scoped"}})
    client = TestClient(app)
    r = client.post("/api/apps/xiaomi/tv/power", headers=_master())
    assert r.status_code == 403
    assert r.json()["detail"] == \
        "forbidden: workspace key not accepted (auth_type=scoped)"


def test_master_key_200s_on_a_default_app(two_apps):
    # No auth_type set anywhere ⇒ "workspace" ⇒ today's behavior, unchanged.
    client = TestClient(two_apps)
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_master()).status_code == 200


def test_auth_type_scoped_does_not_gate_the_scoped_key_itself(tmp_path, creds):
    # auth_type controls ONE thing: whether the MASTER key is accepted. The
    # scope is the single authority on a scoped key (design, rejected
    # alternative 3) — so a scoped key works identically either way.
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_type": "scoped"}})
    client = TestClient(app)
    assert client.post("/api/apps/xiaomi/tv/power",
                       headers=_scoped()).status_code == 200
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_scoped()).status_code == 403


def test_auth_type_is_read_live_not_pinned_at_mount_time(tmp_path, creds):
    # The ops step in the design flips auth_type with POST
    # /api/apps/<slug>/config, which REASSIGNS loaded.config
    # (src/apps/routes.py:1018) on an already-mounted app. The guard
    # therefore has to read through `loaded` on every request, exactly like
    # auth_required does — a value captured at mount time would make the
    # flip a silent no-op until the next restart.
    app, rt = _runtime(["xiaomi"], tmp_path, want_rt=True)
    client = TestClient(app)
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_master()).status_code == 200

    loaded = rt._apps["xiaomi"]
    loaded.config = {**loaded.config, "auth_type": "scoped"}
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_master()).status_code == 403
    # ...and back again, with no remount.
    loaded.config = {**loaded.config, "auth_type": "workspace"}
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_master()).status_code == 200


def test_auth_type_garbage_is_treated_as_workspace(tmp_path, creds):
    # POST /api/apps/<slug>/config passes unknown keys through unvalidated
    # (_coerce_config), so the guard has to tolerate anything here rather
    # than fail closed on a typo and lock the master key out by accident.
    app = _runtime(["xiaomi"], tmp_path,
                   configs={"xiaomi": {"auth_type": "SCOPED-ish nonsense"}})
    client = TestClient(app)
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_master()).status_code == 200


def test_jwt_is_unaffected_by_auth_type_scoped(tmp_path, creds):
    # The Settings UI keeps working on an auth_type: scoped app.
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_type": "scoped"}})
    client = TestClient(app)
    assert client.get("/api/apps/xiaomi/settings",
                      headers={"Authorization": "Bearer good"}).status_code == 200
    assert client.get("/api/apps/xiaomi/settings",
                      headers={"Authorization": "Bearer bad"}).status_code == 401


# ---- the two carve-outs that run BEFORE any of this --------------------


def test_local_paths_loopback_bypass_is_unaffected(tmp_path, creds):
    # A loopback local_paths call presents no credential at all, so there is
    # nothing to scope — and xiaomi's own HA/agent callers depend on this
    # path staying open on an auth_type: scoped app.
    app = _runtime(["xiaomi"], tmp_path,
                   configs={"xiaomi": {"auth_type": "scoped"}},
                   local_paths={"xiaomi": ["/eval"]})
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.post("/api/apps/xiaomi/eval")
    assert r.status_code == 200
    assert r.json() == {"route": "eval", "aw_identity": None}


def test_local_bypass_wins_over_a_scope_miss(tmp_path, creds):
    # Ordering, pinned: the bypass runs FIRST, so a declared local path from
    # loopback is served even when the presented scoped key doesn't cover it.
    app = _runtime(["xiaomi"], tmp_path, local_paths={"xiaomi": ["/eval"]})
    client = TestClient(app, client=("127.0.0.1", 12345))
    r = client.post("/api/apps/xiaomi/eval", headers=_scoped())
    assert r.status_code == 200
    assert r.json()["aw_identity"] is None


# ---- the relaxed (auth_required: false) branch -------------------------


def test_scope_miss_403s_even_with_auth_required_false(tmp_path, creds):
    # An explicitly presented scoped credential never grants, nor
    # masquerades as, more than its scope. Downgrading it to anonymous here
    # would hide the misconfiguration instead of reporting it.
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_required": False}})
    client = TestClient(app)
    assert client.get("/api/apps/xiaomi/settings",
                      headers=_scoped()).status_code == 403
    # ...while a COVERED route still goes through, with claims forwarded.
    r = client.post("/api/apps/xiaomi/tv/power", headers=_scoped())
    assert r.status_code == 200
    assert r.json()["aw_identity"]["scoped"] is True


def test_auth_required_false_still_serves_anonymous_callers(tmp_path, creds):
    # No regression on the existing relaxed behavior: no credential at all
    # is still let through.
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_required": False}})
    client = TestClient(app)
    assert client.get("/api/apps/xiaomi/settings").status_code == 200


def test_master_key_403s_with_auth_required_false_and_auth_type_scoped(tmp_path, creds):
    app = _runtime(["xiaomi"], tmp_path,
                   configs={"xiaomi": {"auth_required": False, "auth_type": "scoped"}})
    client = TestClient(app)
    assert client.post("/api/apps/xiaomi/tv/power",
                       headers=_master()).status_code == 403


# ---- WebSocket: 4401 vs 4403 ------------------------------------------


def test_ws_on_a_covered_route_is_accepted(two_apps):
    client = TestClient(two_apps)
    with client.websocket_connect("/api/apps/xiaomi/tv/ws", headers=_scoped()) as ws:
        assert ws.receive_text() == "hello"


def test_ws_outside_the_scope_closes_4403_not_4401(two_apps):
    # 4403 is the whole point: a caller must be able to tell "your key is
    # not valid" (4401) from "your key is valid but not for this socket".
    client = TestClient(two_apps)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
                "/api/apps/xiaomi/other/ws", headers=_scoped()) as ws:
            ws.receive_text()
    assert ei.value.code == 4403


def test_ws_with_an_unresolvable_scoped_key_closes_4401(two_apps):
    client = TestClient(two_apps)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
                "/api/apps/xiaomi/tv/ws",
                headers={api_key_mod.HEADER_NAME: "awsk_" + "c" * 64}) as ws:
            ws.receive_text()
    assert ei.value.code == 4401


def test_ws_master_key_closes_4403_on_an_auth_type_scoped_app(tmp_path, creds):
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_type": "scoped"}})
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
                "/api/apps/xiaomi/tv/ws", headers=_master()) as ws:
            ws.receive_text()
    assert ei.value.code == 4403


def test_ws_scope_miss_closes_4403_with_auth_required_false(tmp_path, creds):
    app = _runtime(["xiaomi"], tmp_path, configs={"xiaomi": {"auth_required": False}})
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(
                "/api/apps/xiaomi/other/ws", headers=_scoped()) as ws:
            ws.receive_text()
    assert ei.value.code == 4403
