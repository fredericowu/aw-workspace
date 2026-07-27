"""App-repo fetch tests (F3) — real git, no network.

Builds a throwaway git repo on disk and fetches it via a ``file://`` URL, so we
exercise the real clone/pull + ref-pin path without hitting GitHub. Proves: a
fresh clone lands the package (with its ``aw-app.json``), a pinned tag/sha
checks out that exact content, and a re-fetch updates an existing clone in place.
"""
from __future__ import annotations

import subprocess
import textwrap

from src.apps import fetch as fetch_mod


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(cwd)})


def _make_repo(tmp_path, slug="widget"):
    src = tmp_path / "src_repo"
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
      "permissions": []
    }}
    """))
    (src / "plugin.py").write_text("class AppPlugin:\n    async def activate(self, ctx):\n        pass\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "v1")
    _git(src, "tag", "v1.0.0")
    return src


def test_clone_pins_tag_and_lands_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    src = _make_repo(tmp_path)

    dest = fetch_mod.fetch_app_repo(f"file://{src}", "v1.0.0", slug="widget")
    assert dest == str(tmp_path / "apps" / "widget")
    manifest = (tmp_path / "apps" / "widget" / "aw-app.json").read_text()
    assert '"id": "widget"' in manifest


def test_refetch_updates_existing_clone(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    src = _make_repo(tmp_path)
    fetch_mod.fetch_app_repo(f"file://{src}", "main", slug="widget")

    # advance the source repo, then re-fetch main
    (src / "marker.txt").write_text("v2")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "v2")

    dest = fetch_mod.fetch_app_repo(f"file://{src}", "main", slug="widget")
    assert (tmp_path / "apps" / "widget" / "marker.txt").read_text() == "v2"

    # remove wipes the clone
    assert fetch_mod.remove_app_repo("widget") is True
    assert fetch_mod.remove_app_repo("widget") is False
