"""``GET /api/apps/{slug}/versions`` + ``POST /api/apps/{slug}/version`` —
pin/rollback, replacing "which version to install" as a config field
(Frederico's feedback: the Marketplace must never force a config prompt on
open/install, and version is a stable/reversible operation, not a setting).

Same fake-fetch/fake-cloud/fake-mirror harness as ``test_update_endpoint.py``,
plus a stubbed ``list_tags`` (no real GitHub API call) — no PG/network needed.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from src.api.identity import require_identity
from src.apps import routes as routes_mod
from src.apps.reconciler import AppSpec
from src.apps.routes import register_apps_routes

from .test_reconciler import FakeCloud, FakeMirror, _fake_fetch, _make_app_repo


def _async(coro):
    return asyncio.run(coro)


def _app(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    reconciler = app.state.app_reconciler
    reconciler.cloud = cloud
    reconciler.local = FakeMirror()
    reconciler._fetch = _fake_fetch
    reconciler._remove = lambda app_id: True
    return app, runtime, reconciler


def _set_catalog(monkeypatch, apps):
    monkeypatch.setattr(routes_mod, "get_catalog", lambda force=False: {"apps": apps})


def _set_tags(monkeypatch, tags):
    monkeypatch.setattr(routes_mod, "list_tags", lambda repo: tags)


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


async def _post(app, path, json=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path, json=json or {})


def test_versions_lists_tags_against_installed(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        _set_catalog(monkeypatch, [{"id": "widget", "version": "1.0.0", "repo": repo, "ref": "main"}])
        tags = [{"ref": "v1.0.0", "version": "1.0.0"}, {"ref": "v0.9.0", "version": "0.9.0"}]
        _set_tags(monkeypatch, tags)

        r = await _get(app, "/api/apps/widget/versions")
        assert r.status_code == 200
        body = r.json()
        assert body["installed_version"] == "1.0.0"
        assert body["catalog_version"] == "1.0.0"
        assert body["versions"] == tags

        r404 = await _get(app, "/api/apps/not-installed/versions")
        assert r404.status_code == 404

    _async(run())


def test_version_rollback_reinstalls_at_older_ref(tmp_path, monkeypatch):
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        repo_v2 = _make_app_repo(tmp_path, "widget", "2.0.0")
        await reconciler.install(AppSpec(app_id="widget", repo=repo_v2, ref="main"))
        repo_v1 = _make_app_repo(tmp_path, "widget", "1.0.0")
        _set_catalog(monkeypatch, [{"id": "widget", "version": "2.0.0", "repo": repo_v1, "ref": "main"}])

        r = await _post(app, "/api/apps/widget/version", {"ref": "v1.0.0", "version": "1.0.0"})
        assert r.status_code == 202
        assert r.json() == {"app_id": "widget", "status": "installing"}

        # desired row written immediately, pinned to the OLDER ref/version —
        # same mechanics as an update, just going backwards.
        assert cloud.rows[0]["version"] == "1.0.0"
        assert cloud.rows[0]["ref"] == "v1.0.0"

        job = app.state.app_install_jobs.get("widget")
        assert job is not None and job.task is not None
        await job.task

        s = await _get(app, "/api/apps/widget/install-status")
        assert s.json()["status"] == "installed"
        assert runtime.get("widget").manifest.version == "1.0.0"

    _async(run())


def test_version_requires_ref(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        r = await _post(app, "/api/apps/widget/version", {})
        assert r.status_code == 400

    _async(run())


def test_version_not_installed_is_404(tmp_path, monkeypatch):
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        r = await _post(app, "/api/apps/widget/version", {"ref": "v1.0.0"})
        assert r.status_code == 404
        r2 = await _get(app, "/api/apps/widget/versions")
        assert r2.status_code == 404

    _async(run())


def test_version_not_in_catalog_is_404(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        _set_catalog(monkeypatch, [])  # widget not in the catalog

        r = await _post(app, "/api/apps/widget/version", {"ref": "v1.0.0"})
        assert r.status_code == 404

    _async(run())
