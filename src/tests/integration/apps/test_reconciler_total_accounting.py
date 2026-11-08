"""§A.1 of the activation-audit design
(``docs/design/service-supervision-activation-audit.md``) — total accounting
in the reconcile summary.

Before this, an app partitioned as ``present`` with no version/trust change
was touched by nothing in a reconcile pass and appeared in NO summary
bucket at all (silent skip path #1 of the design). This pins the fix: it
now lands in ``present_unchanged``, and the pass asserts the union of every
bucket equals ``desired_active`` — a violation becomes its own ``errors``
entry instead of silently vanishing.

Uses the same harness as ``test_reconciler.py``: a fake fetch (local dir
copy) and an in-memory fake cloud registry, so no network/Postgres needed.
"""
from __future__ import annotations

import asyncio
import shutil
import textwrap

from fastapi import FastAPI

from src.apps.reconciler import AppSpec, Reconciler
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _fake_fetch(repo: str, ref: str = "HEAD", *, slug: str, token=None, dest=None) -> str:
    from src.apps import fetch as fetch_mod
    dest = dest or fetch_mod.package_dir_for(slug)
    if dest != repo:
        if shutil.os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(repo, dest)
    return dest


def _make_app_repo(tmp_path, slug) -> str:
    src = tmp_path / f"src_{slug}"
    src.mkdir()
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
    (src / "plugin.py").write_text(textwrap.dedent("""
        from fastapi import FastAPI
        class AppPlugin:
            async def activate(self, ctx):
                ctx.routes.register(FastAPI())
            async def deactivate(self):
                return None
    """))
    return str(src)


class FakeCloud:
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


def _reconciler(tmp_path, monkeypatch, cloud):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=_fake_fetch)
    return rt, rc


def test_an_already_loaded_app_with_no_change_lands_in_present_unchanged(tmp_path, monkeypatch):
    repo = _make_app_repo(tmp_path, "ta-widget")
    cloud = FakeCloud([{"app_id": "ta-widget", "repo": repo, "ref": "main",
                        "version": "1.0.0", "state": "installed"}])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        await rc.install(AppSpec(app_id="ta-widget", repo=repo, ref="main"))
        return await rc.reconcile(sync_skill_mirrors=False)

    result = _async(run())

    assert result["installed"] == [] and result["upgraded"] == []
    assert result["present_unchanged"] == ["ta-widget"]
    assert result["skipped_already_loaded"] == []
    assert result["errors"] == []


def test_every_desired_app_lands_in_exactly_one_bucket(tmp_path, monkeypatch):
    """The §A.1 invariant itself: the union of installed / upgraded /
    present_unchanged / skipped_already_loaded / errors covers every app in
    ``desired``, for a pass that does a bit of everything (one fresh
    install, one already-loaded-and-unchanged)."""
    widget_repo = _make_app_repo(tmp_path, "ta-widget")
    gadget_repo = _make_app_repo(tmp_path, "ta-gadget")
    cloud = FakeCloud([
        {"app_id": "ta-widget", "repo": widget_repo, "ref": "main",
         "version": "1.0.0", "state": "installed"},
        {"app_id": "ta-gadget", "repo": gadget_repo, "ref": "main",
         "version": "1.0.0", "state": "installed"},
    ])
    rt, rc = _reconciler(tmp_path, monkeypatch, cloud)

    async def run():
        # widget is already loaded going into the pass; gadget is not.
        await rc.install(AppSpec(app_id="ta-widget", repo=widget_repo, ref="main"))
        return await rc.reconcile(sync_skill_mirrors=False)

    result = _async(run())

    accounted = (set(result["installed"]) | set(result["upgraded"])
                | set(result["present_unchanged"])
                | {e["app_id"] for e in result["skipped_already_loaded"]}
                | {e["app_id"] for e in result["errors"] if e.get("app_id")})
    assert accounted == {"ta-widget", "ta-gadget"}
    assert not any(e.get("action") == "accounting" for e in result["errors"])
