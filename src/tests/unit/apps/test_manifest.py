"""Unit tests for the aw-app.json v1 manifest validator."""
from __future__ import annotations

import copy

import pytest

from src.apps.manifest import ManifestError, validate_manifest

VALID = {
    "manifest_version": 1,
    "id": "notes",
    "name": "Notes",
    "version": "1.2.0",
    "tier": "inprocess",
    "runtime": {"entrypoint": "notes_app.plugin:NotesPlugin"},
    "permissions": ["routes:register", "config:extend:kanban", "ui:slots:core.toolbar"],
    "contributes": {
        "routes": [{"prefix": "/api/apps/notes"}],
        "windows": [{"id": "notes.main", "title": "Notes"}],
    },
    "config_schema": {"type": "object"},
}


def _m(**over):
    d = copy.deepcopy(VALID)
    d.update(over)
    return d


def test_valid_manifest_parses():
    m = validate_manifest(VALID)
    assert m.id == "notes"
    assert m.version == "1.2.0"
    assert m.tier == "inprocess"
    assert m.entrypoint == "notes_app.plugin:NotesPlugin"
    assert m.windows[0]["id"] == "notes.main"


@pytest.mark.parametrize("mv", [0, 2, "1", None])
def test_bad_manifest_version(mv):
    with pytest.raises(ManifestError, match="manifest_version"):
        validate_manifest(_m(manifest_version=mv))


@pytest.mark.parametrize("slug", ["Notes", "1notes", "a", "with space", "toolong" * 10, ""])
def test_bad_slug(slug):
    with pytest.raises(ManifestError, match="id must match"):
        validate_manifest(_m(id=slug))


@pytest.mark.parametrize("ver", ["1.2", "v1.2.0", "1", "abc", ""])
def test_bad_version(ver):
    with pytest.raises(ManifestError, match="version must be semver"):
        validate_manifest(_m(version=ver))


def test_bad_tier():
    with pytest.raises(ManifestError, match="tier must be one of"):
        validate_manifest(_m(tier="subprocess"))


def test_inprocess_requires_entrypoint():
    with pytest.raises(ManifestError, match="entrypoint"):
        validate_manifest(_m(runtime={}))


def test_entrypoint_must_be_module_class():
    with pytest.raises(ManifestError, match="module:ClassName"):
        validate_manifest(_m(runtime={"entrypoint": "notes_app.plugin"}))


def test_unknown_permission_rejected():
    with pytest.raises(ManifestError, match="unknown permission"):
        validate_manifest(_m(permissions=["routes:register", "do:anything"]))


def test_routes_contribution_requires_permission():
    with pytest.raises(ManifestError, match="routes:register"):
        validate_manifest(_m(permissions=[]))


def test_window_id_must_be_namespaced():
    bad = _m()
    bad["contributes"]["windows"] = [{"id": "main", "title": "x"}]
    with pytest.raises(ManifestError, match="namespaced"):
        validate_manifest(bad)


def test_container_tier_needs_no_entrypoint():
    m = validate_manifest(_m(tier="container", runtime={"image": "ghcr.io/x", "port": 8080},
                             permissions=["containers:manage"], contributes={}))
    assert m.tier == "container"


def test_container_tier_requires_image_port_and_permission():
    # missing image
    with pytest.raises(ManifestError):
        validate_manifest(_m(tier="container", runtime={"port": 8080},
                             permissions=["containers:manage"], contributes={}))
    # missing/invalid port
    with pytest.raises(ManifestError):
        validate_manifest(_m(tier="container", runtime={"image": "ghcr.io/x"},
                             permissions=["containers:manage"], contributes={}))
    # missing containers:manage
    with pytest.raises(ManifestError):
        validate_manifest(_m(tier="container", runtime={"image": "ghcr.io/x", "port": 8080},
                             permissions=[], contributes={}))


def test_publisher_defaults_to_tekflox():
    m = validate_manifest(VALID)
    assert m.publisher == "TekFlox"


def test_publisher_can_be_overridden():
    m = validate_manifest(_m(publisher="Acme"))
    assert m.publisher == "Acme"


def test_bad_publisher_rejected():
    with pytest.raises(ManifestError, match="publisher"):
        validate_manifest(_m(publisher=""))


def test_requires_ui_refresh_defaults_false():
    m = validate_manifest(VALID)
    assert m.requires_ui_refresh is False


def test_requires_ui_refresh_can_be_declared_true():
    # e.g. an app contributing a top-nav entry — the SPA only mounts a fresh
    # nav/slot contribution on a real page load, not from a live refetch.
    m = validate_manifest(_m(requires_ui_refresh=True))
    assert m.requires_ui_refresh is True


def test_resource_estimate_defaults_cpu_low_mem_disk_dash():
    # CPU is a traffic-light level; memory/disk default to "-" (not resident).
    m = validate_manifest(VALID)
    assert m.resource_estimate == {"cpu": "low", "memory": "-", "disk": "-"}


def test_resource_estimate_partial_override_fills_defaults():
    m = validate_manifest(_m(resource_estimate={"cpu": "medium", "memory": "~450 MB"}))
    assert m.resource_estimate == {"cpu": "medium", "memory": "~450 MB", "disk": "-"}


@pytest.mark.parametrize("bad", [{"cpu": "extreme"}, {"cpu": 1}])
def test_bad_resource_estimate_cpu_level_rejected(bad):
    with pytest.raises(ManifestError, match="resource_estimate.cpu"):
        validate_manifest(_m(resource_estimate=bad))


@pytest.mark.parametrize("bad", [{"memory": ""}, {"disk": 512}, {"memory": None}])
def test_bad_resource_estimate_mem_disk_must_be_nonempty_string(bad):
    with pytest.raises(ManifestError, match="resource_estimate.(memory|disk)"):
        validate_manifest(_m(resource_estimate=bad))


def test_launchable_windows_excludes_settings_only_window():
    # aw-app-git's shape: its one window only hosts the settings form (gh
    # login) — nothing to "launch" from the Installed grid, so it should be
    # categorized as a Runnable (CLI), not a UI app.
    m = validate_manifest(_m(contributes={
        "windows": [{"id": "notes.main", "title": "Git & GitHub CLI"}],
        "settings_panels": [{"id": "notes.settings", "window": "notes.main"}],
        "system_clis": [{"name": "git", "installer": "install.sh"}],
    }, permissions=[]))
    assert m.windows == [{"id": "notes.main", "title": "Git & GitHub CLI"}]
    assert m.launchable_windows == []


def test_launchable_windows_keeps_windows_not_used_as_settings_panels():
    m = validate_manifest(_m(contributes={
        "windows": [
            {"id": "notes.main", "title": "Notes"},
            {"id": "notes.settings", "title": "Notes settings"},
        ],
        "settings_panels": [{"id": "notes.cfg", "window": "notes.settings"}],
    }, permissions=[]))
    assert [w["id"] for w in m.launchable_windows] == ["notes.main"]


def test_what_you_get_derives_ui_screens_and_commands():
    m = validate_manifest(_m(contributes={
        "windows": [{"id": "notes.main", "title": "Notes"}],
        "nav": [{"id": "notes.nav", "label": "Notes", "opens": "notes.main"}],
        "system_clis": [{"name": "git", "installer": "install.sh"}],
        "mcp": {"provides": [{"name": "notes_search"}, "notes_create"]},
        "skills": [{"id": "notes-howto", "path": "skills/notes-howto/SKILL.md"}],
    }, permissions=[]))
    assert m.what_you_get == {
        "mcp_tools": ["notes_search", "notes_create"],
        "ui_screens": ["Notes"],
        "commands": ["git"],
        "skills": ["notes-howto"],
    }


def test_what_you_get_empty_when_nothing_declared():
    m = validate_manifest(_m(contributes={}, permissions=[]))
    assert m.what_you_get == {"mcp_tools": [], "ui_screens": [], "commands": [], "skills": []}


def test_skills_property_reads_contributes_skills():
    m = validate_manifest(_m(contributes={
        "skills": [{"id": "notes-howto", "path": "skills/notes-howto/SKILL.md",
                    "description": "How to use Notes."}],
    }, permissions=[]))
    assert m.skills == [{"id": "notes-howto", "path": "skills/notes-howto/SKILL.md",
                          "description": "How to use Notes."}]


def test_skills_property_empty_when_not_declared():
    m = validate_manifest(_m(contributes={}, permissions=[]))
    assert m.skills == []


def test_skills_entry_requires_id_and_path():
    bad = _m(contributes={"skills": [{"path": "skills/x/SKILL.md"}]}, permissions=[])
    with pytest.raises(ManifestError, match="contributes.skills"):
        validate_manifest(bad)

    bad2 = _m(contributes={"skills": [{"id": "x"}]}, permissions=[])
    with pytest.raises(ManifestError, match="contributes.skills"):
        validate_manifest(bad2)
