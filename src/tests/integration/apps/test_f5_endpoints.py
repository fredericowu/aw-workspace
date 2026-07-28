"""F5 backend surface: enriched ``/api/apps`` + contributions + marketplace catalog.

Proves the SPA's Apps view + Marketplace panel get exactly the fields they
render from: installed rows carry ``has_config``/``config_schema``/``icon``/
``frontend``; the contributions payload separates declarative (windows/nav/
settings) from the ``frontend`` code-plugin block (Decision 3b); and the catalog
endpoint serves the marketplace ``apps.json`` (git fetch mocked).
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps import catalog as catalog_mod
from src.apps.routes import register_apps_routes

REPOS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
GIT = os.path.join(REPOS, "aw-app-git")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(GIT), reason="aw-app-git repo not checked out alongside")


def _client():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app, runtime, TestClient(app)


def test_installed_list_carries_config_and_frontend_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime, client = _client()
    # git is a real has_config app (config_schema + settings_panels), unsigned.
    import asyncio
    asyncio.run(runtime.load(GIT, granted_permissions=[
        "commands:install", "routes:register", "secrets:own",
        "fs:workspace-data", "net:outbound"]))

    row = next(a for a in client.get("/api/apps").json() if a["slug"] == "git")
    assert row["has_config"] is True
    assert "github_token" in row["config_schema"]["properties"]
    assert row["icon"] == "git-branch"
    assert row["signed"] is False
    assert row["frontend"] == {}  # git ships no code bundle

    contrib = client.get("/api/apps/-/contributions").json()
    assert any(w["app"] == "git" for w in contrib["windows"])
    assert any(s["app"] == "git" for s in contrib["settings"])
    assert contrib["frontend"] == []  # no component/iframe bundle app loaded


def test_declarative_window_spec_is_inlined(tmp_path, monkeypatch):
    """F6 Cap 2: contributions() inlines a declarative window's spec file into
    body.spec_data so the SPA's AppWindow can render it (the git 'Sign in with
    GitHub' window was unreachable — its spec file was never served)."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime, client = _client()
    import asyncio
    asyncio.run(runtime.load(GIT, granted_permissions=[
        "commands:install", "routes:register", "secrets:own",
        "fs:workspace-data", "net:outbound"]))

    win = next(w for w in client.get("/api/apps/-/contributions").json()["windows"]
               if w["id"] == "git.main")
    spec = win["body"]["spec_data"]
    assert spec and "regions" in spec
    labels = [wg.get("label") for r in spec["regions"] for wg in r.get("widgets", [])]
    assert "Sign in with GitHub" in labels


def test_frontend_bundle_url_is_absolute_api_origin(tmp_path, monkeypatch):
    """The cloud SPA runs on ``<ws>.workspace`` while app bundles are served by
    ``api.<ws>.workspace``. Dynamic import() resolves relative URLs against the
    SPA origin, so bundle_url must leave the API already absolute."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    package = tmp_path / "component-app"
    (package / "ui" / "dist").mkdir(parents=True)
    (package / "demo_plugin.py").write_text("""class DemoPlugin:
    async def activate(self, ctx):
        return None

    async def deactivate(self):
        return None
""", encoding="utf-8")
    (package / "ui" / "dist" / "plugin.js").write_text(
        "export function register() {}\n", encoding="utf-8")
    (package / "aw-app.json").write_text("""{
  "manifest_version": 1,
  "id": "demo",
  "name": "Demo",
  "version": "0.1.0",
  "tier": "inprocess",
  "runtime": {"python": ">=3.11", "entrypoint": "demo_plugin:DemoPlugin"},
  "permissions": ["ui:code", "ui:slots:core.nav"],
  "contributes": {
    "frontend": {"mode": "component", "bundle": "ui/dist/plugin.js"}
  }
}
""", encoding="utf-8")

    app, runtime, _client_unused = _client()
    import asyncio
    asyncio.run(runtime.load(str(package), signed=True, granted_permissions=[
        "ui:code", "ui:slots:core.nav"]))

    client = TestClient(app, base_url="https://api.demo.workspace.aw.tekflox.com")
    contrib = client.get("/api/apps/-/contributions").json()
    assert contrib["frontend"][0]["bundle_url"] == (
        "https://api.demo.workspace.aw.tekflox.com/api/apps/demo/ui/plugin.js"
    )


def test_catalog_serves_marketplace_apps_json(monkeypatch):
    catalog_mod.clear_cache()
    payload = {
        "manifest_version": 1,
        "apps": [
            {"id": "git", "name": "Git", "has_config": True, "repo": "tekflox/aw-app-git", "ref": "master"},
            {"id": "essentials", "name": "Essentials", "has_config": False, "repo": "tekflox/aw-app-essentials", "ref": "master"},
        ],
    }
    monkeypatch.setattr(catalog_mod, "_fetch_source", lambda source, timeout=15.0: payload["apps"])

    app, _rt, client = _client()
    body = client.get("/api/apps/-/catalog").json()
    assert [a["id"] for a in body["apps"]] == ["git", "essentials"]
    assert body["sources"] == [f"{catalog_mod.MARKETPLACE_REPO}@{catalog_mod.MARKETPLACE_REF}"]
    catalog_mod.clear_cache()


def test_catalog_degrades_to_empty_on_cold_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    catalog_mod.clear_cache()

    def boom(source, timeout=15.0):
        raise RuntimeError("git host unreachable")

    monkeypatch.setattr(catalog_mod, "_fetch_source", boom)
    app, _rt, client = _client()
    body = client.get("/api/apps/-/catalog").json()
    assert body["apps"] == []
    assert "error" in body
    catalog_mod.clear_cache()
