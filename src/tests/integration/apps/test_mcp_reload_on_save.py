"""POST /api/apps/{slug}/config's two new hooks (see routes.save_app_config):

1. ``loaded.plugin.on_config_saved(ctx)`` — called for EVERY save, right
   after ``ctx.config`` is updated, so an app can react to the change
   (e.g. rewrite a generated file on disk). Duck-typed, so plugins written
   before this hook existed (no ``Plugin`` subclass) don't need updating.
2. ``_reload_mcp_gateway(runtime)`` — called ONLY when the saved app's
   manifest declares ``contributes.mcp.reload_on_save: true``, and skipped
   entirely (not even attempted) otherwise.
"""
from __future__ import annotations

import asyncio
import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps import routes as routes_mod
from src.apps.routes import register_apps_routes


def _async(coro):
    return asyncio.run(coro)


class _FakeLocalReconciler:
    """Stands in for reconciler.local — save_app_config only needs
    update_config(app_id, config) to exist and be callable; the real one
    hits a real Postgres session (AW_WORKSPACE_SCHEMA), which none of these
    tests need or want."""

    def __init__(self):
        self.saved: list[tuple[str, dict]] = []

    def update_config(self, app_id, config):
        self.saved.append((app_id, dict(config)))


def _client():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.state.app_reconciler.local = _FakeLocalReconciler()
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app, runtime, TestClient(app)


def _write_app(tmp_path, slug, *, reload_on_save: bool, plugin_src: str):
    pkg = tmp_path / slug
    pkg.mkdir()
    contributes = '{"mcp": {"reload_on_save": true}}' if reload_on_save else "{}"
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": [],
      "contributes": {contributes},
      "config_schema": {{
        "type": "object",
        "properties": {{"enabled": {{"type": "boolean", "default": true}}}}
      }}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


PLUGIN_RECORDING_SAVES = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.saved_configs = []
        async def deactivate(self):
            return None
        async def on_config_saved(self, ctx):
            ctx.saved_configs.append(dict(ctx.config))
"""

PLUGIN_NO_HOOK = """
    class AppPlugin:
        async def activate(self, ctx):
            return None
        async def deactivate(self):
            return None
"""


def test_on_config_saved_hook_runs_with_the_updated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "hookapp", reload_on_save=False, plugin_src=PLUGIN_RECORDING_SAVES)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    res = client.post("/api/apps/hookapp/config", json={"config": {"enabled": False}})
    assert res.status_code == 200

    loaded = runtime.get("hookapp")
    assert loaded.ctx.saved_configs == [{"enabled": False}]


def test_plugin_without_the_hook_is_not_a_hard_error(tmp_path, monkeypatch):
    """Duck-typed getattr — a plugin predating on_config_saved (no Plugin
    subclass, no such method) must not break the save."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "nohookapp", reload_on_save=False, plugin_src=PLUGIN_NO_HOOK)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    res = client.post("/api/apps/nohookapp/config", json={"config": {"enabled": False}})
    assert res.status_code == 200
    assert res.json()["config"]["enabled"] is False


PLUGIN_RECORDING_BOTH_HOOKS = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.order = []
            # Captured once, at activate — see Reconciler._refresh_config.
            ctx.captured = ctx.config
        async def deactivate(self):
            return None
        async def on_config_reloaded(self, ctx):
            ctx.order.append(("reloaded", ctx.config["enabled"]))
        async def on_config_saved(self, ctx):
            ctx.order.append(("saved", ctx.config["enabled"]))
"""


def test_on_config_reloaded_runs_before_on_config_saved(tmp_path, monkeypatch):
    """The request worker must run the SAME in-process refresh hook the other
    nine workers get off apps:changed — otherwise it is the one process with
    stale derived state, and with Redis down nobody refreshes at all. Ordered
    first so an app regenerating its mcp.json in on_config_saved sees
    already-refreshed state."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "bothhooks", reload_on_save=False,
                     plugin_src=PLUGIN_RECORDING_BOTH_HOOKS)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    res = client.post("/api/apps/bothhooks/config", json={"config": {"enabled": False}})
    assert res.status_code == 200
    assert runtime.get("bothhooks").ctx.order == [("reloaded", False), ("saved", False)]


def test_save_mutates_ctx_config_in_place(tmp_path, monkeypatch):
    """A plugin that captured ctx.config at activate time must read the new
    values from that same object. Rebinding ctx.config leaves it holding a dead
    dict forever — the bug that made one app invent its own _live_config
    cache — and the converge path mutates in place, so the request worker has
    to as well or the two diverge."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "identityapp", reload_on_save=False,
                     plugin_src=PLUGIN_RECORDING_BOTH_HOOKS)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))
    loaded = runtime.get("identityapp")
    captured = loaded.ctx.captured
    # load() hands the app its stored config as-is (schema defaults are filled
    # in by the save path, not by load) — so this starts out empty.
    assert "enabled" not in captured

    client.post("/api/apps/identityapp/config", json={"config": {"enabled": False}})

    assert loaded.ctx.config is captured
    assert captured["enabled"] is False


def test_save_publishes_apps_changed_for_the_other_workers(tmp_path, monkeypatch):
    """The publish is the whole cross-worker half: without it the other nine
    workers keep answering GET /config — and running their watchdogs — from the
    config this save replaced, until the next restart."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "pubapp", reload_on_save=False, plugin_src=PLUGIN_NO_HOOK)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    published: list[tuple[str, str]] = []

    async def _publish(reason, app_id=None):
        published.append((reason, app_id))

    # The real AppLifecycle.publish is already a no-op here (no lifespan ran,
    # so no broadcaster) — which is also what keeps this test off the LIVE
    # Redis this workspace's own workers are subscribed to.
    monkeypatch.setattr(app.state.app_lifecycle, "publish", _publish)

    assert client.post("/api/apps/pubapp/config",
                       json={"config": {"enabled": False}}).status_code == 200
    assert published == [("config", "pubapp")]


def test_gateway_reload_skipped_when_app_does_not_contribute_mcp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", lambda runtime: calls.append(1))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "plainapp", reload_on_save=False, plugin_src=PLUGIN_NO_HOOK)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    client.post("/api/apps/plainapp/config", json={"config": {"enabled": False}})
    assert calls == []


def test_gateway_reload_triggered_when_app_contributes_mcp(tmp_path, monkeypatch):
    calls = []

    async def fake_reload(runtime):
        calls.append(1)

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "mcpapp", reload_on_save=True, plugin_src=PLUGIN_RECORDING_SAVES)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    res = client.post("/api/apps/mcpapp/config", json={"config": {"enabled": False}})
    assert res.status_code == 200
    assert calls == [1]


PLUGIN_WATCHING_WORKSPACE_MCP = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.mcp_changed = []
        async def deactivate(self):
            return None
        async def on_workspace_mcp_changed(self, ctx):
            ctx.mcp_changed.append(1)
"""


def test_another_apps_config_save_notifies_workspace_mcp_changed(tmp_path, monkeypatch):
    """The third _app_touches_mcp-gated site (install and uninstall are covered
    in test_reconciler.py). A config save can rewrite the saving app's own
    mcp.json, which moves the shared surface for everyone — so the OTHER app,
    which nobody touched, is the one that has to hear about it.

    This route now goes through Reconciler._trigger_gateway_reload rather than
    calling _reload_mcp_gateway directly. Outside a reconcile pass those are
    the same call, so the assertion on `calls` below is the behaviour-
    preserving half; ctx.mcp_changed is the new half."""
    calls = []

    async def fake_reload(runtime, **kwargs):
        calls.append(1)

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    watcher = _write_app(tmp_path, "watcher", reload_on_save=False,
                         plugin_src=PLUGIN_WATCHING_WORKSPACE_MCP)
    saver = _write_app(tmp_path, "mcpsaver", reload_on_save=True,
                       plugin_src=PLUGIN_NO_HOOK)
    app, runtime, client = _client()
    _async(runtime.load(watcher, granted_permissions=[]))
    _async(runtime.load(saver, granted_permissions=[]))

    assert client.post("/api/apps/mcpsaver/config",
                       json={"config": {"enabled": False}}).status_code == 200

    assert calls == [1]
    assert runtime.get("watcher").ctx.mcp_changed == [1]


def test_a_plain_apps_config_save_notifies_nobody(tmp_path, monkeypatch):
    """Same gate as the reload it rides on: a save that cannot have moved the
    MCP surface must not condemn anybody's long-lived clients."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    watcher = _write_app(tmp_path, "watcher2", reload_on_save=False,
                         plugin_src=PLUGIN_WATCHING_WORKSPACE_MCP)
    saver = _write_app(tmp_path, "plainsaver", reload_on_save=False,
                       plugin_src=PLUGIN_NO_HOOK)
    app, runtime, client = _client()
    _async(runtime.load(watcher, granted_permissions=[]))
    _async(runtime.load(saver, granted_permissions=[]))

    assert client.post("/api/apps/plainsaver/config",
                       json={"config": {"enabled": False}}).status_code == 200

    assert runtime.get("watcher2").ctx.mcp_changed == []


def test_on_config_saved_runs_before_gateway_reload_is_triggered(tmp_path, monkeypatch):
    """The ordering the whole feature depends on: the app must have already
    rewritten its own mcp.json (via on_config_saved) BEFORE the gateway is
    told to reload — otherwise the gateway would scan a stale file."""
    order = []

    async def fake_reload(runtime):
        order.append("gateway_reload")

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    plugin_src = """
        class AppPlugin:
            async def activate(self, ctx):
                ctx.order = []
            async def deactivate(self):
                return None
            async def on_config_saved(self, ctx):
                ctx.order.append("on_config_saved")
    """
    pkg = _write_app(tmp_path, "orderapp", reload_on_save=True, plugin_src=plugin_src)
    app, runtime, client = _client()
    _async(runtime.load(pkg, granted_permissions=[]))

    client.post("/api/apps/orderapp/config", json={"config": {"enabled": False}})

    assert runtime.get("orderapp").ctx.order == ["on_config_saved"]
    assert order == ["gateway_reload"]


def test_reload_mcp_gateway_skips_when_gateway_app_not_installed(tmp_path, monkeypatch):
    """_reload_mcp_gateway itself (not the route-level trigger) — no
    installed mcp-gateway app means nothing to call, so it's a silent no-op
    rather than an error."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime, client = _client()
    assert runtime.is_loaded("mcp-gateway") is False
    _async(routes_mod._reload_mcp_gateway(runtime))  # must not raise


class _FakeLoadedRuntime:
    """Minimal stand-in exposing exactly what _reload_mcp_gateway touches:
    is_loaded("mcp-gateway") -> True, containers.base_url("mcp-gateway")."""

    def is_loaded(self, slug):
        return slug == "mcp-gateway"

    class containers:
        @staticmethod
        def base_url(slug):
            return "http://fake-gateway:9200"


def test_reload_mcp_gateway_retries_a_just_created_container_not_ready_yet(monkeypatch):
    # is_loaded("mcp-gateway") only means containers.start() returned — the
    # gateway's own FastAPI app may not be listening yet. Confirmed live:
    # the very first reload attempt right after installing it in the SAME
    # reconcile pass connection-refused.
    monkeypatch.setattr("asyncio.sleep", lambda *_: _async_noop())

    calls = {"n": 0}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"upstreams": ["whiteboard"]}

    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionRefusedError("gateway not listening yet")
            return _FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", _FakeAsyncClient)

    _async(routes_mod._reload_mcp_gateway(_FakeLoadedRuntime()))

    assert calls["n"] == 3  # 2 failures + 1 success, no exception raised


async def _async_noop():
    return None
