"""The third skills upstream: directories an app registers at runtime.

These pin the two properties that make the pull half safe to run on every
boot. Both are about the delete pass, because that is the only thing here
that can destroy a user's data:

* an app that cannot answer must not lose its skills — "I failed" and "I have
  none" arrive as different states on purpose;
* the sourced pass must not be able to reach a native or a pushed skill, and
  the native pass must not be able to reach a sourced one.

Getting either wrong is silent: skills vanish from every agent mirror and the
next session simply runs without them.
"""
from __future__ import annotations

import shutil

import pytest

from src.apps import skill_sources
from src.libs import skills_sync


@pytest.fixture()
def sourced_workspace(tmp_path, monkeypatch):
    """One skill of each kind: native, app-pushed, and app-sourced."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))

    native = tmp_path / "native-skills" / "aw-native"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text("---\nname: aw-native\n---\nnative body\n")

    pushed = tmp_path / "skills" / "aw-pushed"
    pushed.mkdir(parents=True)
    (pushed / "SKILL.md").write_text("---\nname: aw-pushed\n---\npushed body\n")
    (pushed / skills_sync.OWNER_MARKER).write_text("some-app")

    store = tmp_path / ".aw-workspace" / "data" / "aw-autoskill" / "skills"
    generated = store / "aw-autoskill-demo"
    generated.mkdir(parents=True)
    (generated / "SKILL.md").write_text("---\nname: aw-autoskill-demo\n---\ngenerated body\n")

    skill_sources.record("maintenance-agents", {"ok": True, "dirs": [str(store)]})
    return tmp_path, store


def test_sourced_skill_reaches_skills_and_every_mirror(sourced_workspace):
    root, _ = sourced_workspace
    skills_sync.sync_all()

    assert (root / "skills" / "aw-autoskill-demo" / "SKILL.md").exists()
    for target in skills_sync.targets():
        assert (target / "aw-autoskill-demo" / "SKILL.md").read_text().endswith("generated body\n")


def test_sourced_skill_carries_both_markers(sourced_workspace):
    """``.aw-app-id`` so every existing owner check treats it as app-owned;
    ``.aw-skill-source`` so the delete pass can tell it from a pushed skill."""
    root, _ = sourced_workspace
    skills_sync.materialize()

    entry = root / "skills" / "aw-autoskill-demo"
    assert (entry / skills_sync.OWNER_MARKER).read_text() == "maintenance-agents"
    assert (entry / skill_sources.SOURCE_MARKER).read_text() == "maintenance-agents"


def test_deleting_from_the_store_removes_it_everywhere(sourced_workspace):
    """The point of re-scanning: the tenant edits the store, not the repo."""
    root, store = sourced_workspace
    skills_sync.sync_all()
    shutil.rmtree(store / "aw-autoskill-demo")

    skills_sync.sync_all()

    assert not (root / "skills" / "aw-autoskill-demo").exists()
    for target in skills_sync.targets():
        assert not (target / "aw-autoskill-demo").exists()


def test_an_app_that_cannot_answer_keeps_its_skills(sourced_workspace):
    """The regression that would make this unsafe to run on every boot.

    A hook behind a network call fails transiently. If ``ok: False`` were
    recorded as "no dirs", the very next sync would read that as an empty
    source and delete every skill the tenant has accumulated, out of skills/
    and all four agent mirrors at once.
    """
    root, _ = sourced_workspace
    skills_sync.sync_all()

    skill_sources.record("maintenance-agents", {"ok": False})
    skills_sync.sync_all()

    assert (root / "skills" / "aw-autoskill-demo" / "SKILL.md").exists()
    for target in skills_sync.targets():
        assert (target / "aw-autoskill-demo" / "SKILL.md").exists()


def test_uninstall_removes_the_sourced_skills(sourced_workspace):
    """The other side of the same coin: a *real* uninstall must take them.

    Left behind, they would keep teaching agents to use an app that is gone —
    the exact-mirror rule this module exists to preserve.
    """
    root, _ = sourced_workspace
    skills_sync.sync_all()

    skill_sources.forget("maintenance-agents")
    skills_sync.sync_all()

    assert not (root / "skills" / "aw-autoskill-demo").exists()
    for target in skills_sync.targets():
        assert not (target / "aw-autoskill-demo").exists()


def test_sourced_pass_never_touches_native_or_pushed_skills(sourced_workspace):
    """Scoped by marker in both directions, so the three halves stay disjoint."""
    root, _ = sourced_workspace
    skills_sync.sync_all()
    skill_sources.forget("maintenance-agents")
    skills_sync.materialize()

    assert (root / "skills" / "aw-native" / "SKILL.md").exists()
    assert (root / "skills" / "aw-pushed" / "SKILL.md").exists()


def test_native_pass_never_deletes_a_sourced_skill(sourced_workspace):
    """skills/ is an exact mirror of native-skills/ for native entries only.

    A sourced skill has no counterpart under native-skills/, so without the
    ownership split the native delete pass would take it on the first sync.
    """
    root, _ = sourced_workspace
    skills_sync.materialize()
    result = skills_sync.materialize()

    assert (root / "skills" / "aw-autoskill-demo" / "SKILL.md").exists()
    assert result.deleted == []


def test_a_missing_source_dir_is_reported_not_fatal(sourced_workspace):
    """An app naming a dir that isn't there yet must not break the sync.

    It also must not read as "the app has no skills": that is the ok/failure
    distinction again, one layer down.
    """
    root, store = sourced_workspace
    skills_sync.sync_all()
    shutil.rmtree(store)

    skills_sync.sync_all()

    assert (root / "skills" / "aw-native" / "SKILL.md").exists()
    assert (root / "skills" / "aw-pushed" / "SKILL.md").exists()


def test_a_directory_without_a_skill_md_is_skipped(sourced_workspace):
    root, store = sourced_workspace
    (store / "not-a-skill").mkdir()

    skills_sync.materialize()

    assert not (root / "skills" / "not-a-skill").exists()


def test_a_corrupt_registry_reads_as_empty(sourced_workspace):
    """Consulted from the sync path, so it degrades rather than raising."""
    root, _ = sourced_workspace
    skills_sync.sync_all()
    with open(skill_sources.registry_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")

    skills_sync.materialize()

    assert (root / "skills" / "aw-native" / "SKILL.md").exists()
