"""Unit tests for ``src/apps/knowledge.py`` — the bits not already covered by
the load/unload integration test (``test_knowledge.py`` under
``src/tests/integration/apps``): the path-escape guard and the reconciler's
roster-sweep backstop.
"""
from __future__ import annotations

import os

import pytest

from src.apps import knowledge
from src.apps.knowledge import KnowledgeError, KnowledgeRegistry


def test_resolve_knowledge_dir_rejects_escaping_path(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (tmp_path / "outside").mkdir()

    with pytest.raises(KnowledgeError, match="escapes"):
        knowledge.resolve_knowledge_dir(str(pkg), "../outside")


def test_resolve_knowledge_dir_rejects_missing_dir(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()

    with pytest.raises(KnowledgeError, match="not found"):
        knowledge.resolve_knowledge_dir(str(pkg), "docs")


def test_sweep_removes_only_orphaned_namespaces(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))

    apps_root = knowledge.paths.knowledge_apps_dir()
    for app_id in ("kept", "orphan"):
        d = os.path.join(apps_root, app_id, "docs")
        os.makedirs(d)
        with open(os.path.join(d, "x.md"), "w") as f:
            f.write("x")

    removed = knowledge.sweep({"kept"})

    assert removed == ["orphan"]
    assert os.path.isdir(os.path.join(apps_root, "kept"))
    assert not os.path.exists(os.path.join(apps_root, "orphan"))
    # the generated-marker README is not itself swept as an orphaned app id
    assert os.path.isfile(os.path.join(apps_root, knowledge.README_NAME))


def test_sweep_is_a_noop_when_nothing_is_orphaned(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))

    apps_root = knowledge.paths.knowledge_apps_dir()
    os.makedirs(os.path.join(apps_root, "kept", "docs"))

    assert knowledge.sweep({"kept"}) == []


def test_register_overwrites_on_content_change_without_unregister_having_run(tmp_path, monkeypatch):
    """The cross-worker journal gap this hash check exists for (design §C,
    same shape as ``skills.py``'s): a destination can already be on disk
    with stale content and ``unregister()`` never ran. ``register()`` must
    still overwrite once the source hash no longer matches."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = tmp_path / "pkg"
    docs = pkg / "docs"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("v1")

    registry = KnowledgeRegistry()
    dest = registry.register("notes", str(pkg), "docs")
    assert open(os.path.join(dest, "a.md")).read() == "v1"

    # Same content again — must not touch it (no-op reboot).
    with open(os.path.join(dest, "a.md"), "a") as f:
        f.write("-touched")
    touched = open(os.path.join(dest, "a.md")).read()
    registry.register("notes", str(pkg), "docs")
    assert open(os.path.join(dest, "a.md")).read() == touched

    # Real content change — must overwrite even with a dir already there.
    (docs / "a.md").write_text("v2")
    registry.register("notes", str(pkg), "docs")
    assert open(os.path.join(dest, "a.md")).read() == "v2"


def test_purge_app_removes_the_whole_namespace_not_just_docs(tmp_path, monkeypatch):
    """purge_app is the unconditional, journal-independent uninstall layer —
    it must remove the whole apps/<id>/ dir (e.g. a cli_reference/ capture
    from the same app too), not just the docs/ sub-tree register() wrote."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    apps_root = knowledge.paths.knowledge_apps_dir()
    os.makedirs(os.path.join(apps_root, "notes", "docs"))
    os.makedirs(os.path.join(apps_root, "notes", "cli_reference"))

    KnowledgeRegistry().purge_app("notes")

    assert not os.path.exists(os.path.join(apps_root, "notes"))
