"""contributes.skills registration (framework: consume app-contributed skills).

Proves an app declaring ``contributes.skills`` gets its ``SKILL.md`` dir
copied into the shared skills index on load (the app's own package dir is
immutable/overwritten wholesale on update, so this is a real copy, not a
symlink — see ``src/apps/skills.py``), discoverable via
``AppRuntime.skills_index()`` / ``GET /api/apps/-/skills`` — and that
uninstall removes the copy (journal reverse-replay). An app with no
``contributes.skills`` is a no-op: nothing added to the index.
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


def _write_app(tmp_path, slug, *, with_skill: bool):
    pkg = tmp_path / slug
    pkg.mkdir(parents=True)
    contributes = {}
    if with_skill:
        skill_dir = pkg / "skills" / "how-to"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: how-to\ndescription: How to use this app.\n---\n\n# How to\n"
        )
        contributes = {
            "skills": [{"id": "how-to", "path": "skills/how-to/SKILL.md",
                        "description": "How to use this app."}]
        }
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


def test_app_with_skill_gets_registered_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "notes", with_skill=True)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg)

        dest_path = os.path.join(paths.skills_dir(), "notes__how-to")
        assert os.path.isdir(dest_path)
        assert not os.path.islink(dest_path)
        assert os.path.isfile(os.path.join(dest_path, "SKILL.md"))

        index = rt.skills_index()
        assert index == [{
            "app": "notes", "id": "how-to", "description": "How to use this app.",
            "skill_md_path": os.path.join(dest_path, "SKILL.md"), "registered": True,
        }]

        # journaled so uninstall reverts it
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("notes")]
        assert ("skill:register", "how-to") in kinds

        await rt.unload("notes")
        assert not os.path.exists(dest_path)
        assert rt.journal.entries_for("notes") == []

    _async(run())


def test_reregister_does_not_clobber_a_users_live_edit(tmp_path, monkeypatch):
    """The app's package dir is immutable/overwritten wholesale on update, so
    the workspace's own copy under skills/ is the user's to edit. Every
    process boot re-activates every already-installed app from scratch
    (reconcile-on-boot: a fresh runtime, empty in-memory state, same on-disk
    skills dir) — that re-``register()`` must leave an existing copy alone
    rather than re-copying over it and silently discarding the edit."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "notes", with_skill=True)

    async def run():
        rt1 = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt1.load(pkg)

        dest_path = os.path.join(paths.skills_dir(), "notes__how-to")
        skill_md = os.path.join(dest_path, "SKILL.md")
        with open(skill_md, "a") as f:
            f.write("\nUser's own note, added after install.\n")
        edited = open(skill_md).read()

        # A brand-new runtime (simulating a fresh process boot) reconciling
        # the same already-installed app against the same on-disk skills dir.
        rt2 = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt2.load(pkg)

        assert open(skill_md).read() == edited

    _async(run())


def test_app_without_skills_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_app(tmp_path, "plain", with_skill=False)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg)

        assert rt.skills_index() == []
        assert not os.path.isdir(paths.skills_dir()) or os.listdir(paths.skills_dir()) == []
        assert [e for e in rt.journal.entries_for("plain") if e.kind == "skill:register"] == []

        await rt.unload("plain")

    _async(run())
