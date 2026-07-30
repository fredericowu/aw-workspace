"""Reconciler tests (F3 acceptance core) — desired-vs-actual convergence.

Uses a fresh ``AppRuntime`` on a bare FastAPI host, an in-memory fake cloud
registry + local mirror (so no PG/network), and a fake ``fetch`` (a plain
directory copy) injected into the ``Reconciler`` in place of the real HTTP
tarball fetch (``src/apps/fetch.py``) — the reconciler's own convergence
logic is what's under test here, not the fetch transport (see
``test_fetch.py`` for tarball-download/extraction coverage). Proves:

* install → repo fetched, plugin hot-loaded (route answers), registry rows written;
* workspace recreation (fresh runtime, empty loaded-set, registry populated) →
  the reconciler auto-reinstalls with no manual step;
* uninstall → plugin gone, journal residue-free, repo removed, rows deleted;
* reconcile removes an app that is loaded but no longer desired.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI

from src.apps import fetch as fetch_mod
from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _fake_fetch(repo: str, ref: str = "HEAD", *, slug: str, token=None, dest=None) -> str:
    """Stand-in for the real tarball fetch: ``repo`` is a source dir on disk."""
    dest = dest or fetch_mod.package_dir_for(slug)
    if dest != repo:
        if shutil.os.path.exists(dest):  # noqa: SIM108 - mirrors fetch.py's swap
            shutil.rmtree(dest)
        shutil.copytree(repo, dest)
    return dest


def _make_app_repo(tmp_path, slug="widget", version="1.0.0", dependencies=None):
    src = tmp_path / f"src_{slug}_{version}"
    src.mkdir()
    deps_json = ""
    if dependencies:
        import json
        deps_json = ',\n      "dependencies": ' + json.dumps({"apps": dependencies})
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "{version}",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": ["routes:register"],
      "contributes": {{"routes": [{{"prefix": "/api/apps/{slug}"}}]}}
      {deps_json}
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
    return str(src)


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
                          "config": config or {}, "signed": signed, "state": "installed"})

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
            "signed": spec.signed, "state": "installed"}

    def forget(self, app_id):
        self.rows.pop(app_id, None)


async def _get(host, path):
    transport = httpx.ASGITransport(app=host)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def _reconciler(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=_fake_fetch)
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


def test_install_loads_required_app_dependencies_first(tmp_path, monkeypatch):
    dep_repo = _make_app_repo(tmp_path, "proxy")
    browser_repo = _make_app_repo(
        tmp_path,
        "browser",
        dependencies=[{"id": "proxy", "package_dir": dep_repo}],
    )
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        summary = await rc.install(AppSpec(app_id="browser", repo=browser_repo, ref="main"))
        assert summary["dependencies_installed"] == ["proxy"]
        assert rt.loaded_slugs() == ["proxy", "browser"]

        desired_browser_only = [{
            "app_id": "browser",
            "version": "1.0.0",
            "repo": browser_repo,
            "ref": "main",
            "granted_permissions": ["routes:register"],
            "config": {},
            "state": "installed",
        }]
        result = await rc.reconcile(desired_browser_only)
        assert result["removed"] == []
        assert rt.is_loaded("proxy")
        assert rt.is_loaded("browser")

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


def test_local_fallback_preserves_signed_component_grants(tmp_path, monkeypatch):
    """A signed component app must keep ``ui:code`` when boot reconcile falls
    back to the local mirror; otherwise the SPA downgrades it and never imports
    the bundle."""
    repo = _make_app_repo(tmp_path, slug="devctl")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(
            app_id="devctl",
            repo=repo,
            ref="main",
            granted_permissions=["routes:register", "ui:code"],
            signed=True,
        ))

        fallback_rows = rc.local.list()
        assert fallback_rows[0]["signed"] is True
        assert "ui:code" in fallback_rows[0]["granted_permissions"]

        fresh_host = FastAPI()
        fresh_rt = AppRuntime(fresh_host, guard_identity=False)
        fallback = Reconciler(
            fresh_rt,
            cloud=FakeCloud(),
            local=rc.local,
            fetch=_fake_fetch,
        )
        fallback.cloud.configured = False

        result = await fallback.reconcile()
        assert result["source"] == "local"
        loaded = fresh_rt.get("devctl")
        assert loaded is not None
        assert loaded.signed is True
        assert "ui:code" in loaded.granted_permissions

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


def test_reconcile_version_drift_triggers_upgrade_uninstall_then_install(tmp_path, monkeypatch):
    """ADR app-update-mechanism.md, Metade B (1): a desired.version bump for an
    app already loaded is an upgrade — uninstall the old version, then install
    the new one, in that order."""
    repo_v1 = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo_v1, ref="main"))
        assert rt.get("widget").manifest.version == "1.0.0"

        repo_v2 = _make_app_repo(tmp_path, "widget", "2.0.0")
        cloud.rows = [{"app_id": "widget", "version": "2.0.0", "repo": repo_v2,
                       "ref": "main", "granted_permissions": ["routes:register"],
                       "config": {}, "state": "installed"}]

        calls: list[tuple[str, str]] = []
        real_uninstall = rc.uninstall
        real_install = rc.install

        async def spy_uninstall(app_id, **kw):
            calls.append(("uninstall", app_id))
            return await real_uninstall(app_id, **kw)

        async def spy_install(spec, **kw):
            calls.append(("install", spec.app_id))
            return await real_install(spec, **kw)

        rc.uninstall = spy_uninstall
        rc.install = spy_install

        result = await rc.reconcile()
        assert result["upgraded"] == ["widget"]
        assert result["installed"] == [] and result["removed"] == []
        assert not result["errors"]
        assert calls == [("uninstall", "widget"), ("install", "widget")]
        assert rt.get("widget").manifest.version == "2.0.0"

    _async(run())


def test_reconcile_no_version_drift_is_a_noop(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "widget", "1.0.0")
    cloud = FakeCloud()
    host, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="widget", repo=repo, ref="main"))

        result = await rc.reconcile()
        assert result["upgraded"] == []
        assert result["installed"] == [] and result["removed"] == []
        assert rt.get("widget").manifest.version == "1.0.0"

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
