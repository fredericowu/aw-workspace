"""What ``contributions().frontend`` is allowed to announce to the SPA.

Component-mode apps ship a PREBUILT bundle inside their package (there is no
build step at install time — see aw-app-diff-tool's .gitignore, which spells
out why the compiled file is committed). So a bundle declared in the manifest
but absent from the package is a packaging bug in that app, and announcing it
anyway costs a failed dynamic import() plus a console error on every single
page load, forever.
"""
from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps.routes import register_apps_routes


def _client():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    return app, runtime, TestClient(app)


def _write_frontend_app(tmp_path, slug, bundle, ship_bundle):
    """Minimal component-mode app declaring ``bundle``, which the package
    either ships under ui/dist or (the bug) doesn't."""
    pkg = tmp_path / slug
    (pkg / "ui" / "dist").mkdir(parents=True)
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": slug,
        "version": "1.0.0",
        "tier": "inprocess",
        "runtime": {"entrypoint": "plugin:AppPlugin"},
        "permissions": ["ui:code"],
        "contributes": {"frontend": {"mode": "component", "bundle": bundle}},
    }))
    (pkg / "plugin.py").write_text(
        "class AppPlugin:\n"
        "    async def activate(self, ctx):\n        pass\n"
        "    async def deactivate(self):\n        pass\n"
    )
    if ship_bundle:
        (pkg / "ui" / "dist" / os.path.basename(bundle)).write_text("export default {};\n")
    return str(pkg)


def test_bundle_is_announced_only_when_the_package_ships_it(tmp_path, monkeypatch):
    """Found live 2026-08-12: the App Template app declares
    ui/dist/template.js, which its repo gitignores and its release never
    builds — so every dashboard load fetched
    /api/apps/aw-app-template/ui/template.js
    and got a 404. The rest of the app's contribution must survive; only the
    unservable URL is withheld."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime, client = _client()

    shipped = _write_frontend_app(tmp_path, "shipped", "ui/dist/shipped.js", True)
    phantom = _write_frontend_app(tmp_path, "phantom", "ui/dist/phantom.js", False)
    asyncio.run(runtime.load(shipped, granted_permissions=["ui:code"]))
    asyncio.run(runtime.load(phantom, granted_permissions=["ui:code"]))

    frontend = {f["app"]: f for f in client.get("/api/apps/-/contributions").json()["frontend"]}
    assert frontend["shipped"]["bundle_url"] == "/api/apps/shipped/ui/shipped.js"
    assert frontend["phantom"]["bundle_url"] is None
    # still listed, still component mode — only the unservable URL is withheld
    assert frontend["phantom"]["mode"] == "component"
    # and what it withheld is exactly the request that would have 404'd
    assert client.get("/api/apps/phantom/ui/phantom.js").status_code == 404
    assert client.get("/api/apps/shipped/ui/shipped.js").status_code == 200
