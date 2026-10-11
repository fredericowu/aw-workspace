"""The reconciler's knowledge roster sweep (design §B/§C, third uninstall
layer) — proves ``Reconciler.reconcile()`` itself calls
``knowledge.sweep()`` against the pass's final loaded set, not merely that
the primitive works in isolation (see ``src/tests/unit/apps/test_knowledge.py``
for that). An orphaned ``knowledge_base/apps/<id>/`` namespace — left behind
by, say, a missed uninstall on another worker — must be gone after the next
reconcile even though nothing explicitly uninstalled it just now.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import textwrap
from pathlib import Path

from fastapi import FastAPI

from src.apps import paths
from src.apps.reconciler import AppSpec, Reconciler


def _async(coro):
    return asyncio.run(coro)


def _fake_fetch(repo: str, ref: str = "HEAD", *, slug: str, token=None, dest=None) -> str:
    from src.apps import fetch as fetch_mod
    dest = dest or fetch_mod.package_dir_for(slug)
    if dest != repo:
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(repo, dest)
    return dest


def _make_app_repo_with_knowledge(tmp_path, slug):
    src = tmp_path / f"src_{slug}"
    docs = src / "docs"
    docs.mkdir(parents=True)
    (docs / "intro.md").write_text(f"# {slug}\n")
    (src / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": [],
      "contributes": {{"knowledge": {{"path": "docs"}}}}
    }}
    """))
    (src / "plugin.py").write_text(textwrap.dedent("""
        class AppPlugin:
            async def activate(self, ctx):
                return None
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
        self.rows = [r for r in self.rows if r["app_id"] != app_id]
        self.rows.append({"app_id": app_id, "version": version, "repo": repo, "ref": ref,
                          "granted_permissions": granted_permissions or [], "config": config or {},
                          "state": "installed"})

    def delete_desired(self, app_id):
        self.rows = [r for r in self.rows if r["app_id"] != app_id]


class FakeMirror:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def list(self):
        return list(self.rows.values())

    def upsert(self, spec, package_dir):
        self.rows[spec.app_id] = {"app_id": spec.app_id, "version": spec.version,
                                  "config": spec.config, "package_dir": package_dir}

    def forget(self, app_id):
        self.rows.pop(app_id, None)

    def update_config(self, app_id, config):
        if app_id in self.rows:
            self.rows[app_id]["config"] = config


def test_reconcile_sweeps_an_orphaned_knowledge_namespace(tmp_path, monkeypatch):
    from src.apps.runtime import AppRuntime

    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    repo = _make_app_repo_with_knowledge(tmp_path, "notes")
    cloud = FakeCloud()
    host = FastAPI()
    rt = AppRuntime(host, guard_identity=False)
    rc = Reconciler(rt, cloud=cloud, local=FakeMirror(), fetch=_fake_fetch)

    async def run():
        await rc.install(AppSpec(app_id="notes", repo=repo, ref="main"))
        notes_docs = os.path.join(paths.knowledge_apps_dir(), "notes", "docs")
        assert os.path.isdir(notes_docs)

        # An orphan left behind by, say, a missed uninstall on another
        # worker (the AW_WORKSPACE_WORKERS>1 gap the sweep exists for) —
        # not journaled on THIS worker, not reachable via unload().
        ghost_dir = os.path.join(paths.knowledge_apps_dir(), "ghost-app", "docs")
        os.makedirs(ghost_dir)

        result = await rc.reconcile([{
            "app_id": "notes", "version": "1.0.0", "repo": repo, "ref": "main",
            "granted_permissions": [], "config": {}, "state": "installed",
        }])

        assert result["removed"] == []  # "notes" stays desired and loaded
        assert os.path.isdir(notes_docs), "the still-installed app's own mirror must survive"
        assert not os.path.exists(os.path.join(paths.knowledge_apps_dir(), "ghost-app")), (
            "the orphaned namespace must be swept by the reconcile pass")

    _async(run())
