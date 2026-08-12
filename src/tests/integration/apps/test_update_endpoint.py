"""``POST /api/apps/{slug}/update`` (ADR app-update-mechanism.md, Metade B (2)).

Resolves the app's catalog entry, writes a new desired row (version/ref from
the catalog) and reconciles in the background via the same job tracker as
``POST /api/apps/install`` — same status contract, polled the same way.
Reuses the fake-fetch/fake-cloud/fake-mirror harness from ``test_reconciler.py``
so no PG/network is needed.
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


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


async def _post(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post(path)


def test_update_writes_desired_and_enqueues_job(tmp_path, monkeypatch):
    repo_v1 = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo_v1, ref="main"))
        repo_v2 = _make_app_repo(tmp_path, "widget", "2.0.0")
        _set_catalog(monkeypatch, [{"id": "widget", "version": "2.0.0",
                                    "repo": repo_v2, "ref": "main"}])

        r = await _post(app, "/api/apps/widget/update")
        assert r.status_code == 202
        assert r.json() == {"app_id": "widget", "status": "installing"}

        job = app.state.app_install_jobs.get("widget")
        assert job is not None and job.task is not None
        await job.task

        # desired row written by the background job (version/ref from catalog)
        assert cloud.rows[0]["version"] == "2.0.0"
        assert cloud.rows[0]["repo"] == repo_v2
        assert "widget" in reconciler.local.rows
        assert reconciler.local.rows["widget"]["version"] == "2.0.0"

        s = await _get(app, "/api/apps/widget/install-status")
        assert s.json()["status"] == "installed"
        assert runtime.get("widget").manifest.version == "2.0.0"

    _async(run())


def test_update_already_at_catalog_version_is_a_noop(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        _set_catalog(monkeypatch, [{"id": "widget", "version": "1.0.0",
                                    "repo": repo, "ref": "main"}])

        r = await _post(app, "/api/apps/widget/update")
        assert r.status_code == 202
        assert r.json() == {"app_id": "widget", "status": "installing"}

        job = app.state.app_install_jobs.get("widget")
        assert job is not None and job.task is not None
        await job.task

        s = await _get(app, "/api/apps/widget/install-status")
        assert s.json()["status"] == "installed"
        assert s.json()["summary"] == {"app_id": "widget", "status": "no-op", "version": "1.0.0"}

    _async(run())


def test_update_not_installed_is_404(tmp_path, monkeypatch):
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)
    _set_catalog(monkeypatch, [{"id": "widget", "version": "2.0.0", "repo": "x", "ref": "main"}])

    async def run():
        r = await _post(app, "/api/apps/widget/update")
        assert r.status_code == 404

    _async(run())


def test_update_not_in_catalog_fails_via_status(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        await reconciler.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        _set_catalog(monkeypatch, [])  # widget not in the catalog

        r = await _post(app, "/api/apps/widget/update")
        assert r.status_code == 202
        job = app.state.app_install_jobs.get("widget")
        assert job is not None and job.task is not None
        await job.task

        s = await _get(app, "/api/apps/widget/install-status")
        assert s.json()["status"] == "failed"
        assert "not found in catalog" in s.json()["error"]

    _async(run())


async def _delete(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.delete(path)


def test_uninstall_can_remove_an_app_that_never_loaded(tmp_path, monkeypatch):
    """A failed install still leaves a desired row, and reconcile retries it on
    every pass forever. Guarding uninstall on `is_loaded` made exactly that
    state unremovable — seen live 2026-08-12 as "app 'remote-screen' has
    neither a repo to fetch nor an on-disk package_dir" on every boot.
    """
    cloud = FakeCloud([{"app_id": "ghost", "version": "1.0.0", "repo": None, "ref": "main",
                        "granted_permissions": [], "config": {}, "state": "installed"}])
    app, runtime, reconciler = _app(tmp_path, monkeypatch, cloud)

    async def run():
        assert not runtime.is_loaded("ghost")

        r = await _delete(app, "/api/apps/ghost")
        assert r.status_code == 200
        assert r.json()["uninstalled"] is True
        # The row is gone, so the next reconcile has nothing left to retry.
        assert [row["app_id"] for row in cloud.list_desired()] == []

    _async(run())


def test_uninstall_of_a_genuinely_unknown_app_is_still_404(tmp_path, monkeypatch):
    """Not loaded AND no row anywhere — nothing to remove, so don't pretend."""
    app, runtime, reconciler = _app(tmp_path, monkeypatch, FakeCloud())

    async def run():
        r = await _delete(app, "/api/apps/nope")
        assert r.status_code == 404

    _async(run())
