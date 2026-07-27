"""Reconciler tests (F3 acceptance core) — desired-vs-actual convergence.

Uses a fresh ``AppRuntime`` on a bare FastAPI host, an in-memory fake cloud
registry + local mirror (so no PG/network), and the REAL git fetch against a
``file://`` repo. Proves:

* install → repo fetched, plugin hot-loaded (route answers), registry rows written;
* workspace recreation (fresh runtime, empty loaded-set, registry populated) →
  the reconciler auto-reinstalls with no manual step;
* uninstall → plugin gone, journal residue-free, repo removed, rows deleted;
* reconcile removes an app that is loaded but no longer desired.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI

from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd)})


def _make_app_repo(tmp_path, slug="widget"):
    src = tmp_path / f"src_{slug}"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["routes:register"],
      "contributes": {{"routes": [{{"prefix": "/api/apps/{slug}"}}]}}
    }}
    """))
    (src / "plugin.py").write_text(textwrap.dedent(f"""
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                api = FastAPI()
                @api.get("/")
                async def root():
                    return {{"app": "{slug}", "ok": True}}
                ctx.routes.register(api)
            async def deactivate(self):
                return None
    """))
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "v1")
    return f"file://{src}"


class FakeCloud:
    """In-memory stand-in for the aw-backend cloud registry."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.configured = True

    def list_desired(self):
        return [dict(r) for r in self.rows]

    def put_desired(self, app_id, *, version, repo=None, ref="HEAD",
                    granted_permissions=None, config=None, instance_id="", signed=False):
        self.delete_desired(app_id)
        self.rows.append({"app_id": app_id, "version": version, "repo": repo, "ref": ref,
                          "granted_permissions": granted_permissions or [],
                          "config": config or {}, "state": "installed"})

    def delete_desired(self, app_id, instance_id=""):
        self.rows = [r for r in self.rows if r["app_id"] != app_id]


class FakeMirror:
    def __init__(self):
        self.rows = {}

    def list(self):
        return [dict(r) for r in self.rows.values()]

    def upsert(self, spec, package_dir):
        self.rows[spec.app_id] = {
            "app_id": spec.app_id, "version": spec.version, "package_dir": package_dir,
            "repo": spec.repo, "ref": spec.ref,
            "granted_permissions": spec.granted_permissions, "config": spec.config,
            "state": "installed"}

    def forget(self, app_id):
        self.rows.pop(app_id, None)


async def _get(host, path):
    transport = httpx.ASGITransport(app=host)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def _reconciler(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    host = FastAPI()
    rt = AppRuntime(host)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror())
    return host, rt, rc


def test_install_fetches_hot_loads_and_writes_registry(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert rt.is_loaded("widget")
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 200 and r.json() == {"app": "widget", "ok": True}
        # registry + mirror both recorded
        assert [row["app_id"] for row in cloud.list_desired()] == ["widget"]
        assert "widget" in rc.local.rows
        assert (tmp_path / "apps" / "widget" / "aw-app.json").exists()

    _async(run())


def test_recreation_auto_reinstalls_from_registry(tmp_path, monkeypatch):
    """Acceptance (b): a fresh runtime with an EMPTY loaded-set but a populated
    cloud registry reinstalls the app with no manual step."""
    repo = _make_app_repo(tmp_path)
    # registry already has the app (as if a prior install / another worker wrote it)
    cloud = FakeCloud([{"app_id": "widget", "version": "1.0.0", "repo": repo,
                        "ref": "main", "granted_permissions": ["routes:register"],
                        "config": {}, "state": "installed"}])
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        assert rt.loaded_slugs() == []          # fresh runtime — nothing loaded
        result = await rc.reconcile()           # boot-equivalent converge
        assert result["source"] == "cloud"
        assert result["installed"] == ["widget"]
        assert rt.is_loaded("widget")
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 200

    _async(run())


def test_uninstall_is_residue_free(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path)
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))
        assert (tmp_path / "apps" / "widget").exists()

        res = await rc.uninstall("widget")
        assert res["uninstalled"] is True and res["repo_removed"] is True
        assert not rt.is_loaded("widget")
        assert rt.journal.entries_for("widget") == []          # journal clean
        assert not any(n.startswith("aw_apps.widget") for n in sys.modules)
        assert cloud.list_desired() == []                       # registry row gone
        assert "widget" not in rc.local.rows                    # mirror row gone
        assert not (tmp_path / "apps" / "widget").exists()      # repo removed
        r = await _get(host, "/api/apps/widget/")
        assert r.status_code == 404

    _async(run())


def test_reconcile_removes_undesired_and_installs_missing(tmp_path, monkeypatch):
    repo_a = _make_app_repo(tmp_path, "aaa")
    repo_b = _make_app_repo(tmp_path, "bbb")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        # start with aaa loaded + in the registry
        await rc.install(AppSpec(app_id="aaa", repo=repo_a, ref="main"))
        # now the desired set is {bbb} only — aaa must be removed, bbb installed
        cloud.rows = [{"app_id": "bbb", "version": "1.0.0", "repo": repo_b, "ref": "main",
                       "granted_permissions": ["routes:register"], "config": {},
                       "state": "installed"}]
        result = await rc.reconcile()
        assert result["installed"] == ["bbb"]
        assert result["removed"] == ["aaa"]
        assert rt.is_loaded("bbb") and not rt.is_loaded("aaa")
        assert not result["errors"]

    _async(run())


def test_reconcile_one_bad_app_does_not_block_others(tmp_path, monkeypatch):
    repo_ok = _make_app_repo(tmp_path, "good")
    cloud = FakeCloud([
        {"app_id": "good", "version": "1.0.0", "repo": repo_ok, "ref": "main",
         "granted_permissions": ["routes:register"], "config": {}, "state": "installed"},
        {"app_id": "broken", "version": "1.0.0", "repo": "file:///no/such/repo",
         "ref": "main", "granted_permissions": [], "config": {}, "state": "installed"},
    ])
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        result = await rc.reconcile()
        assert "good" in result["installed"]
        assert rt.is_loaded("good")
        assert any(e["app_id"] == "broken" for e in result["errors"])

    _async(run())
