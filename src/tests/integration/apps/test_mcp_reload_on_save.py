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
