"""The reconciler publishes an app's effective public_paths to the registry.

aw-backend's tunnel edge reads them from the registry row, never from the
request, so every load has to (re)publish them — including the reconciler's
own ``write_cloud=False`` installs, which is how an update lands.
"""
from __future__ import annotations

import asyncio
import json
import textwrap

from src.apps.reconciler import AppSpec
from src.tests.integration.apps.test_reconciler import FakeCloud, _reconciler


class PathsCloud(FakeCloud):
    def __init__(self, rows=None):
        super().__init__(rows)
        self.public_paths: list[tuple[str, list[str]]] = []

    def put_public_paths(self, app_id, public_paths, instance_id=""):
        self.public_paths.append((app_id, list(public_paths)))
        return {}


def _repo(tmp_path, *, slug, permissions, public_paths):
    src = tmp_path / f"src_{slug}"
    src.mkdir()
    route = {"prefix": f"/api/apps/{slug}"}
    if public_paths:
        route["public_paths"] = public_paths
    (src / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1, "id": slug, "name": slug, "version": "1.0.0",
        "tier": "inprocess", "runtime": {"entrypoint": f"{slug}_public_paths_plugin:P"},
        "permissions": permissions, "contributes": {"routes": [route]},
    }))
    (src / f"{slug}_public_paths_plugin.py").write_text(textwrap.dedent("""
        from fastapi import FastAPI
        class P:
            async def activate(self, ctx):
                ctx.routes.register(FastAPI())
            async def deactivate(self):
                return None
    """))
    return str(src)


def test_granted_public_paths_are_published_even_without_write_cloud(tmp_path, monkeypatch):
    cloud = PathsCloud()
    _, _, rc = _reconciler(tmp_path, monkeypatch, cloud)
    repo = _repo(tmp_path, slug="hooky",
                 permissions=["routes:register", "routes:public"],
                 public_paths=["/api/apps/hooky/webhooks"])
    asyncio.run(rc.install(AppSpec(app_id="hooky", repo=repo), write_cloud=False))
    assert cloud.public_paths == [("hooky", ["/api/apps/hooky/webhooks"])]


def test_refused_capability_publishes_an_empty_set(tmp_path, monkeypatch):
    # routes:public declared but filtered out of the grant → the edge must
    # be told "nothing", so a previously opened path closes there too.
    cloud = PathsCloud()
    _, rt, rc = _reconciler(tmp_path, monkeypatch, cloud)
    repo = _repo(tmp_path, slug="hooky",
                 permissions=["routes:register", "routes:public"],
                 public_paths=["/api/apps/hooky/webhooks"])
    monkeypatch.setattr("src.apps.runtime.filter_grants",
                        lambda req, signed: ([c for c in req if c != "routes:public"],
                                             ["routes:public"]))
    asyncio.run(rc.install(AppSpec(app_id="hooky", repo=repo), write_cloud=False))
    assert "routes:public" not in rt.get("hooky").granted_permissions
    assert cloud.public_paths == [("hooky", [])]


def test_apps_without_public_paths_make_no_extra_call(tmp_path, monkeypatch):
    cloud = PathsCloud()
    _, _, rc = _reconciler(tmp_path, monkeypatch, cloud)
    repo = _repo(tmp_path, slug="plain", permissions=["routes:register"],
                 public_paths=None)
    asyncio.run(rc.install(AppSpec(app_id="plain", repo=repo)))
    assert cloud.public_paths == []


def test_fresh_install_publishes_after_the_registry_row_exists(tmp_path, monkeypatch):
    # The backend 404s a publish for a row put_desired has not created yet.
    calls: list[str] = []

    class OrderCloud(PathsCloud):
        def put_desired(self, app_id, **kw):
            calls.append("put_desired")
            return super().put_desired(app_id, **kw)

        def put_public_paths(self, app_id, public_paths, instance_id=""):
            calls.append("put_public_paths")
            return super().put_public_paths(app_id, public_paths, instance_id)

    cloud = OrderCloud()
    _, _, rc = _reconciler(tmp_path, monkeypatch, cloud)
    repo = _repo(tmp_path, slug="hooky",
                 permissions=["routes:register", "routes:public"],
                 public_paths=["/api/apps/hooky/webhooks"])
    asyncio.run(rc.install(AppSpec(app_id="hooky", repo=repo)))
    assert calls == ["put_desired", "put_public_paths"]


def test_upgrade_that_drops_public_paths_clears_the_registry(tmp_path, monkeypatch):
    cloud = PathsCloud()
    _, _, rc = _reconciler(tmp_path, monkeypatch, cloud)
    repo = _repo(tmp_path, slug="plain", permissions=["routes:register"],
                 public_paths=None)
    spec = AppSpec.from_row({"app_id": "plain", "repo": repo,
                             "public_paths": ["/api/apps/plain/webhooks"]})
    asyncio.run(rc.install(spec, write_cloud=False))
    assert cloud.public_paths == [("plain", [])]
