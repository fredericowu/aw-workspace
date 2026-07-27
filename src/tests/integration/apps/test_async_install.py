"""Async install (BYOD-tunnel "Failed to fetch" fix).

``POST /api/apps/install`` must return immediately (202, status "installing")
instead of blocking on the fetch + system-CLI install, and
``GET /api/apps/{slug}/install-status`` must reflect installing → installed /
failed as the background task progresses. Uses the real ``aw-app-essentials``
package (manifest + import path) with the installer scripts stubbed out, so
no apt/root is needed — same approach as ``test_real_apps.py``.
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi import FastAPI

from src.api.identity import require_identity
from src.apps.routes import register_apps_routes

REPOS = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", ".."))
ESSENTIALS = os.path.join(REPOS, "aw-app-essentials")

pytestmark = pytest.mark.skipif(
    not os.path.isdir(ESSENTIALS), reason="aw-app-essentials repo not checked out alongside")


def _async(coro):
    return asyncio.run(coro)


def _app():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "test"}
    runtime.commands.run_installer = lambda package_dir, script: f"ran {script}"
    runtime.commands.run_revert = lambda package_dir, script: f"reverted {script}"
    # local mirror writes to the real Postgres AppInstall table — not what
    # these tests exercise (routes.py's async job-tracking), so no-op it.
    reconciler = app.state.app_reconciler
    reconciler.local.upsert = lambda spec, package_dir: None
    reconciler.local.forget = lambda app_id: None
    return app, runtime


def test_install_returns_202_immediately_then_transitions_to_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime = _app()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/apps/install", json={"package_dir": ESSENTIALS})
            assert r.status_code == 202
            assert r.json() == {"app_id": "essentials", "status": "installing"}

            # the background task may already have run to completion by the
            # time we poll (stubbed installer is fast) — either state proves
            # the POST itself didn't block on it.
            s = await c.get("/api/apps/essentials/install-status")
            assert s.status_code == 200
            assert s.json()["status"] in ("installing", "installed")

            job = app.state.app_install_jobs.get("essentials")
            assert job is not None and job.task is not None
            await job.task  # let the background install actually run to completion

            s = await c.get("/api/apps/essentials/install-status")
            assert s.json()["status"] == "installed"
            assert runtime.is_loaded("essentials")

            # already installed → 409, not another background job
            r2 = await c.post("/api/apps/install", json={"package_dir": ESSENTIALS})
            assert r2.status_code == 409

    _async(run())


def test_duplicate_install_while_in_progress_does_not_start_a_second_job(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime = _app()
    reconciler = app.state.app_reconciler

    gate = asyncio.Event()
    real_install = reconciler.install
    calls: list[str] = []

    async def slow_install(spec, **kw):
        calls.append(spec.app_id)
        await gate.wait()
        return await real_install(spec, **kw)

    reconciler.install = slow_install

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r1 = await c.post("/api/apps/install", json={"package_dir": ESSENTIALS})
            assert r1.status_code == 202
            r2 = await c.post("/api/apps/install", json={"package_dir": ESSENTIALS})
            assert r2.status_code == 202
            assert calls == ["essentials"]  # second POST did not kick off a new install

            gate.set()
            job = app.state.app_install_jobs.get("essentials")
            await job.task

            s = await c.get("/api/apps/essentials/install-status")
            assert s.json()["status"] == "installed"

    _async(run())


def test_install_failure_is_reported_via_status_not_a_dropped_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, runtime = _app()
    reconciler = app.state.app_reconciler

    async def boom(spec, **kw):
        raise RuntimeError("apt install exploded")

    reconciler.install = boom

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/api/apps/install", json={"package_dir": ESSENTIALS})
            assert r.status_code == 202

            job = app.state.app_install_jobs.get("essentials")
            await job.task

            s = await c.get("/api/apps/essentials/install-status")
            body = s.json()
            assert body["status"] == "failed"
            assert "apt install exploded" in body["error"]
            assert not runtime.is_loaded("essentials")

    _async(run())


def test_install_status_for_unknown_slug_is_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    app, _runtime = _app()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/api/apps/never-installed/install-status")
            assert r.status_code == 404

    _async(run())
