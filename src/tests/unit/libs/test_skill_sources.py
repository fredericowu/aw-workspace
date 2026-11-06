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

import logging
import shutil

import pytest

from src.apps import skill_sources
from src.apps.skills import SkillsRegistry
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


def test_sourced_pass_without_takeover_never_touches_native_or_pushed_skills(sourced_workspace):
    """Scoped by marker in both directions, so the three halves stay disjoint —
    *as long as no source claims a pushed id with ``takeover``*. This fixture's
    ``maintenance-agents`` source has no takeover rights and no overlapping
    slug, so this is the ordinary (no-collision) case; the takeover exception
    itself — a source explicitly allowed to claim a pushed id — is covered by
    ``test_takeover_source_adopts_pushed_skill_silently_when_hash_matches`` and
    its siblings below."""
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


def test_record_persists_the_takeover_flag(tmp_path, monkeypatch):
    """Dropped silently before this change (``record`` only ever wrote
    ``{"dirs": [...]}`` back) — the pushed->sourced transition below reads
    it straight off the registry, so a lossy round-trip would make every
    source look like it has no takeover rights regardless of what it
    reported."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))
    skill_sources.record("skills", {"ok": True, "dirs": ["/some/dir"], "takeover": True})

    assert skill_sources.read_registry()["skills"]["takeover"] is True

    skill_sources.record("maintenance-agents", {"ok": True, "dirs": ["/other/dir"]})
    assert skill_sources.read_registry()["maintenance-agents"]["takeover"] is False


@pytest.fixture()
def takeover_workspace(tmp_path, monkeypatch):
    """A skill pushed by one app, plus a takeover-flagged registry source
    claiming the SAME id — the §17.7 pushed->sourced transition this module
    gates. ``native-skills/`` must exist (even empty) or ``materialize()``
    returns before ever reaching the sourced pass."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))
    (tmp_path / "native-skills").mkdir(parents=True)

    pushed = tmp_path / "skills" / "shared-skill"
    pushed.mkdir(parents=True)
    (pushed / "SKILL.md").write_text("---\nname: shared-skill\n---\npushed body\n")
    (pushed / skills_sync.OWNER_MARKER).write_text("mini-browser")
    # A real push (src/apps/skills.py's register()) always leaves this too —
    # present here so the hash comparison below is tested against the real
    # on-disk shape, not an idealized one missing a marker.
    (pushed / skills_sync.HASH_MARKER).write_text("irrelevant-pushed-hash")

    registry_copy = tmp_path / "registry-cache" / "shared-skill"
    registry_copy.mkdir(parents=True)
    (registry_copy / "SKILL.md").write_text("---\nname: shared-skill\n---\npushed body\n")

    skill_sources.record("skills", {
        "ok": True, "dirs": [str(registry_copy.parent)], "takeover": True,
    })
    return tmp_path, pushed, registry_copy


def test_pushed_skill_with_no_takeover_source_is_left_alone(takeover_workspace, caplog):
    """Without the takeover flag, a source naming an id some other app
    already pushed is reported and skipped — same posture as a genuine
    sourced×sourced collision, never a silent steal."""
    root, pushed, _ = takeover_workspace
    skill_sources.record("maintenance-agents", {
        "ok": True, "dirs": [str((root / "registry-cache"))],
    })
    skill_sources.forget("skills")  # only the no-takeover source claims it now

    with caplog.at_level(logging.WARNING):
        skills_sync.materialize()

    entry = root / "skills" / "shared-skill"
    assert entry.joinpath(skills_sync.OWNER_MARKER).read_text() == "mini-browser"
    assert not entry.joinpath(skill_sources.SOURCE_MARKER).exists()
    assert any("takeover" in r.message for r in caplog.records)


def test_takeover_source_adopts_pushed_skill_silently_when_hash_matches(takeover_workspace, caplog):
    """Byte-identical content: the markers change, nothing else does, and
    there is nothing worth a WARNING about."""
    root, pushed, _ = takeover_workspace

    with caplog.at_level(logging.WARNING):
        skills_sync.materialize()

    entry = root / "skills" / "shared-skill"
    assert entry.joinpath(skill_sources.SOURCE_MARKER).read_text() == "skills"
    # provenance kept — the pull pass only stamps OWNER when absent.
    assert entry.joinpath(skills_sync.OWNER_MARKER).read_text() == "mini-browser"
    assert entry.joinpath("SKILL.md").read_text().endswith("pushed body\n")
    assert not any("takes over" in r.message for r in caplog.records)


def test_takeover_source_overwrites_with_warning_when_hash_differs(takeover_workspace, caplog):
    """A real content mismatch is the registry winning anyway, but logged —
    the opposite of the silent §17.7 steal this module replaces."""
    root, pushed, registry_copy = takeover_workspace
    (registry_copy / "SKILL.md").write_text("---\nname: shared-skill\n---\nregistry body v2\n")

    with caplog.at_level(logging.WARNING):
        skills_sync.materialize()

    entry = root / "skills" / "shared-skill"
    assert entry.joinpath("SKILL.md").read_text().endswith("registry body v2\n")
    assert entry.joinpath(skill_sources.SOURCE_MARKER).read_text() == "skills"
    assert any("takes over" in r.message for r in caplog.records)


def test_pull_then_push_and_push_then_pull_converge_on_the_same_state(tmp_path, monkeypatch):
    """register() (the push half, src/apps/skills.py) and materialize()'s
    pull pass are two independent writers on one shared tree with no third
    pass arbitrating order between them (design §16 risk 5) — whichever
    writer runs first must land in the same state as whichever runs
    second."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))
    (tmp_path / "native-skills").mkdir(parents=True)

    app_pkg = tmp_path / "notes-app"
    skill_src = app_pkg / "skills" / "how-to"
    skill_src.mkdir(parents=True)
    (skill_src / "SKILL.md").write_text("# How to\n")

    registry_src = tmp_path / "registry-cache" / "how-to"
    registry_src.mkdir(parents=True)
    (registry_src / "SKILL.md").write_text("# How to\n")  # byte-identical
    skill_sources.record("skills", {
        "ok": True, "dirs": [str(registry_src.parent)], "takeover": True,
    })

    def _state():
        entry = tmp_path / "skills" / "how-to"
        return {
            "content": entry.joinpath("SKILL.md").read_text(),
            "owner": entry.joinpath(skills_sync.OWNER_MARKER).read_text(),
            "source": entry.joinpath(skill_sources.SOURCE_MARKER).read_text(),
        }

    # Order A: push first, then pull.
    SkillsRegistry().register("notes", "how-to", str(app_pkg), "skills/how-to/SKILL.md")
    skills_sync.materialize()
    state_a = _state()

    shutil.rmtree(tmp_path / "skills")

    # Order B: pull first, then push.
    skills_sync.materialize()
    SkillsRegistry().register("notes", "how-to", str(app_pkg), "skills/how-to/SKILL.md")
    state_b = _state()

    assert state_a == state_b == {"content": "# How to\n", "owner": "notes", "source": "skills"}
