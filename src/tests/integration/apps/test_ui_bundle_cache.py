"""``GET /api/apps/{slug}/ui/{path}`` must never be browser-cacheable.

Found live 2026-08-04: the route had no ``Cache-Control`` header, so browsers
fell back to RFC 7234 heuristic caching and kept serving a stale app bundle
(the old nav-pill JS) after the app was updated on the server — confirmed by
comparing a plain ``fetch()`` (stale, browser cache) against a
``cache: 'no-store'`` fetch (fresh) against the exact same URL. ``no-cache``
forces revalidation on every load via the ETag/Last-Modified FileResponse
already sets, so an unchanged bundle still gets a cheap 304.
"""
from __future__ import annotations

import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps.routes import register_apps_routes


def _write_ui_app(tmp_path):
    slug = "uiapp"
    pkg = tmp_path / slug
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "contributes": {{"frontend": {{"mode": "component", "bundle": "ui/dist/uiapp.js"}}}}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent("""
        class AppPlugin:
            async def activate(self, ctx):
                return None
            async def deactivate(self):
                return None
    """))
    ui_dist = pkg / "ui" / "dist"
    ui_dist.mkdir(parents=True)
    (ui_dist / "uiapp.js").write_text("export default function register() {}\n")
    return str(pkg)


def test_ui_bundle_route_sets_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    client = TestClient(app)

    import asyncio
    pkg = _write_ui_app(tmp_path)
    asyncio.run(runtime.load(pkg))

    res = client.get("/api/apps/uiapp/ui/uiapp.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache"
