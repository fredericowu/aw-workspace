"""contributes.knowledge registration (framework: consume app-contributed docs).

Proves an app declaring ``contributes.knowledge`` gets its docs dir copied
into the shared KB tree at ``knowledge_base/apps/<app-id>/docs/`` on load
(the app's own package dir is immutable/overwritten wholesale on update, so
this is a real copy, not a symlink — see ``src/apps/knowledge.py``), and
that uninstall removes the WHOLE ``apps/<app-id>/`` namespace
unconditionally — not merely via journal reverse-replay, which is only one
of the design's three uninstall layers (see
``docs/design/contributes-knowledge-and-cli-reference-split.md`` §C). An
app with no ``contributes.knowledge`` is a no-op: nothing mirrored.
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap

from fastapi import FastAPI

from src.apps import paths
from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


def _write_app(tmp_path, slug, *, with_knowledge: bool):
    pkg = tmp_path / slug
    pkg.mkdir(parents=True)
    contributes = {}
    if with_knowledge:
        docs_dir = pkg / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "intro.md").write_text("# Intro\n\nHello from the app's own docs.\n")
        contributes = {"knowledge": {"path": "docs"}}
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": [],
      "contributes": {json.dumps(contributes)}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent("""
        class AppPlugin:
            async def activate(self, ctx):
                return None
            async def deactivate(self):
                return None
    """))
    return str(pkg)


def test_app_with_knowledge_gets_mirrored_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    pkg = _write_app(tmp_path, "notes", with_knowledge=True)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg)

        dest_path = os.path.join(paths.knowledge_apps_dir(), "notes", "docs")
        assert os.path.isdir(dest_path)
        assert not os.path.islink(dest_path)
        assert os.path.isfile(os.path.join(dest_path, "intro.md"))
        assert "Hello from the app" in open(os.path.join(dest_path, "intro.md")).read()

        # journaled so uninstall reverts it
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("notes")]
        assert ("knowledge:register", "docs") in kinds

        await rt.unload("notes")
        # unconditional namespace purge (design §C) — the WHOLE apps/notes/
        # dir is gone, not just docs/.
        assert not os.path.exists(os.path.join(paths.knowledge_apps_dir(), "notes"))
        assert rt.journal.entries_for("notes") == []

    _async(run())


def test_app_without_knowledge_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    pkg = _write_app(tmp_path, "plain", with_knowledge=False)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg)

        assert not os.path.isdir(os.path.join(paths.knowledge_apps_dir(), "plain"))
        assert [e for e in rt.journal.entries_for("plain") if e.kind == "knowledge:register"] == []

        await rt.unload("plain")

    _async(run())


def test_reload_leaves_unchanged_mirror_alone_but_overwrites_on_version_bump(tmp_path, monkeypatch):
    """Every boot re-activates every installed app from scratch (reconcile-on-
    boot) — a no-op re-register (same source content) must not touch the
    mirror, but a real content change (new app version shipping new docs)
    must overwrite it even though a dir already exists at the destination."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    pkg = _write_app(tmp_path, "notes", with_knowledge=True)

    async def run():
        rt1 = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt1.load(pkg)
        dest_path = os.path.join(paths.knowledge_apps_dir(), "notes", "docs", "intro.md")
        first_mtime = os.path.getmtime(dest_path)

        # A brand-new runtime (simulating a fresh process boot) reconciling
        # the same already-installed app against the same on-disk mirror —
        # same source content, so this must be a no-op (no I/O).
        rt2 = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt2.load(pkg)
        assert os.path.getmtime(dest_path) == first_mtime

        # Now the app ships new content — a real version bump. This must
        # overwrite the live mirror even though a dir already exists there
        # and ``unload()`` never ran in between (the cross-worker gap the
        # hash check exists for — see ``src/apps/knowledge.py``).
        with open(os.path.join(pkg, "docs", "intro.md"), "w") as f:
            f.write("# Intro v2\n")
        rt3 = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt3.load(pkg)
        assert "v2" in open(dest_path).read()

    _async(run())
