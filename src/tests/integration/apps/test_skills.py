"""contributes.skills registration (framework: consume app-contributed skills).

Proves an app declaring ``contributes.skills`` gets its ``SKILL.md`` dir
symlinked into the shared skills index on load — no content duplication,
discoverable via ``AppRuntime.skills_index()`` / ``GET /api/apps/-/skills`` —
and that uninstall removes the symlink (journal reverse-replay). An app with
no ``contributes.skills`` is a no-op: nothing added to the index.
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

        link_path = os.path.join(paths.skills_dir(), "notes__how-to")
        assert os.path.islink(link_path)
        assert os.path.isfile(os.path.join(link_path, "SKILL.md"))

        index = rt.skills_index()
        assert index == [{
            "app": "notes", "id": "how-to", "description": "How to use this app.",
            "skill_md_path": os.path.join(link_path, "SKILL.md"), "registered": True,
        }]

        # journaled so uninstall reverts it
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("notes")]
        assert ("skill:register", "how-to") in kinds

        await rt.unload("notes")
        assert not os.path.islink(link_path)
        assert rt.journal.entries_for("notes") == []

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
