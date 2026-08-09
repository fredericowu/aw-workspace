"""Mapped-folder validation + the Tier-2 bind expansion it feeds.

The point of the feature is that a mapped folder is JUST A PATH — no git repo,
no ``repos/`` prefix — so these tests pin exactly that: arbitrary absolute
paths are accepted, names are derived/validated, and
``$AW_WORKSPACE_FOLDERS`` expands into one bind per folder.
"""
from __future__ import annotations

import pytest

from src.api import folders


# --- validation --------------------------------------------------------------


def test_maps_an_arbitrary_path_with_no_repo_involved(tmp_path):
    target = tmp_path / "just-a-folder"
    target.mkdir()

    entry = folders.validate(str(target))

    assert entry == {"name": "just-a-folder", "path": str(target), "mode": "ro"}


def test_nested_non_repo_subdirectory_is_mappable(tmp_path):
    nested = tmp_path / "repo" / "docs" / "adr"
    nested.mkdir(parents=True)

    entry = folders.validate(str(nested), name="adr", mode="rw")

    assert entry["path"] == str(nested)
    assert entry["mode"] == "rw"


def test_trailing_slash_is_normalised_away(tmp_path):
    target = tmp_path / "docs"
    target.mkdir()

    assert folders.validate(f"{target}/")["path"] == str(target)
    assert folders.validate(f"{target}/")["name"] == "docs"


def test_relative_path_is_rejected():
    with pytest.raises(folders.FolderError, match="absolute"):
        folders.validate("docs")


def test_empty_path_is_rejected():
    with pytest.raises(folders.FolderError, match="required"):
        folders.validate("")


@pytest.mark.parametrize("name", ["with/slash", "..", ".", "-leading-dash", "sp ace", ""])
def test_invalid_names_are_rejected(name, tmp_path):
    # An empty explicit name falls back to the basename, so use a path whose
    # basename is itself invalid to keep that case meaningful.
    path = "/" if name == "" else str(tmp_path)
    with pytest.raises(folders.FolderError, match="invalid name|path is required"):
        folders.validate(path, name=name or "..")


def test_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(folders.FolderError, match="mode must be"):
        folders.validate(str(tmp_path), mode="rwx")


def test_describe_reports_liveness_without_enforcing_it(tmp_path):
    real = folders.describe({"name": "a", "path": str(tmp_path), "mode": "ro"})
    ghost = folders.describe({"name": "b", "path": "/not/on/this/fs", "mode": "ro"})

    assert real["exists"] is True
    # A host-only path still describes cleanly — it's a valid Tier-2 bind
    # source even though this process can't see it.
    assert ghost["exists"] is False


# --- browse ------------------------------------------------------------------


def test_browse_lists_only_subdirectories(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a-file.txt").write_text("x")

    result = folders.browse(str(tmp_path))

    assert [e["name"] for e in result["entries"]] == ["alpha", "beta"]
    assert result["path"] == str(tmp_path)


def test_browse_rejects_a_non_directory(tmp_path):
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    with pytest.raises(folders.FolderError, match="not a directory"):
        folders.browse(str(a_file))


# --- Tier-2 bind expansion ---------------------------------------------------


def _runtime_with_folders(monkeypatch, mapped):
    from src.apps.runtime import AppRuntime

    monkeypatch.setattr(AppRuntime, "_mapped_folders", staticmethod(lambda: mapped))
    return AppRuntime.__new__(AppRuntime)


def _manifest(volumes, permissions=("fs:workspace-data",)):
    from src.apps.manifest import Manifest

    m = Manifest.__new__(Manifest)
    m.id = "kb"
    m.runtime = {"volumes": volumes}
    m.permissions = list(permissions)
    return m


def test_workspace_folders_expands_to_one_bind_per_folder(monkeypatch, tmp_path):
    docs = tmp_path / "docs"
    data = tmp_path / "data"
    docs.mkdir()
    data.mkdir()
    rt = _runtime_with_folders(monkeypatch, [
        {"name": "docs", "path": str(docs), "mode": "ro"},
        {"name": "data", "path": str(data), "mode": "rw"},
    ])
    manifest = _manifest([
        {"source": "$AW_WORKSPACE_FOLDERS", "target": "/workspace-folders", "mode": "rw"},
    ])

    binds = rt._container_volumes(manifest, str(tmp_path))

    assert binds[str(docs)] == {"bind": "/workspace-folders/docs", "mode": "ro"}
    assert binds[str(data)] == {"bind": "/workspace-folders/data", "mode": "rw"}


def test_declared_ro_clamps_a_rw_folder(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    rt = _runtime_with_folders(monkeypatch, [
        {"name": "data", "path": str(data), "mode": "rw"},
    ])
    manifest = _manifest([
        {"source": "$AW_WORKSPACE_FOLDERS", "target": "/workspace-folders", "mode": "ro"},
    ])

    binds = rt._container_volumes(manifest, str(tmp_path))

    assert binds[str(data)]["mode"] == "ro"


def test_no_mapped_folders_yields_no_binds(monkeypatch, tmp_path):
    rt = _runtime_with_folders(monkeypatch, [])
    manifest = _manifest([
        {"source": "$AW_WORKSPACE_FOLDERS", "target": "/workspace-folders", "mode": "rw"},
    ])

    assert rt._container_volumes(manifest, str(tmp_path)) == {}


def test_workspace_folders_requires_the_fs_permission(monkeypatch, tmp_path):
    from src.apps.runtime import ContainerError

    rt = _runtime_with_folders(monkeypatch, [
        {"name": "docs", "path": str(tmp_path), "mode": "ro"},
    ])
    manifest = _manifest(
        [{"source": "$AW_WORKSPACE_FOLDERS", "target": "/workspace-folders", "mode": "rw"}],
        permissions=("containers:manage",),
    )

    with pytest.raises(ContainerError, match="fs:workspace-data"):
        rt._container_volumes(manifest, str(tmp_path))


def test_mapped_folders_degrades_to_empty_without_a_database(monkeypatch):
    """No DB (unit tests, offline tooling) must mean "no folders", never a
    Tier-2 load failure."""
    from src.apps.runtime import AppRuntime

    def boom():
        raise RuntimeError("no database configured")

    monkeypatch.setattr("src.api.folders.list_folders", boom)

    assert AppRuntime._mapped_folders() == []


# --- remap_folders -----------------------------------------------------------
#
# Regression: the first cut called containers.register() here, which refuses to
# overwrite an existing registration ("container already registered for 'kb'").
# Every folder mapped after an app was loaded silently failed to reach it — the
# route still returned 200 because a failed remap must not fail the mapping, so
# nothing surfaced except an empty `remapped_apps`. Exercise the real method.


class _FakeContainers:
    def __init__(self) -> None:
        self.volumes: dict[str, dict] = {}
        self.started: list[str] = []
        self.registered_calls = 0

    def register(self, *args, **kwargs):
        self.registered_calls += 1
        raise AssertionError("remap must not re-register an already-registered app")

    def set_volumes(self, app_id, volumes):
        self.volumes[app_id] = volumes

    def start(self, app_id):
        self.started.append(app_id)


def _loaded_app(tmp_path, volumes, tier="container"):
    from types import SimpleNamespace

    manifest = SimpleNamespace(
        id="kb", tier=tier, runtime={"volumes": volumes, "image": "img", "port": 1},
        permissions=["fs:workspace-data", "containers:manage"],
    )
    return SimpleNamespace(manifest=manifest, package_dir=str(tmp_path))


async def _remap(rt):
    return await rt.remap_folders()


def test_remap_updates_volumes_and_restarts_the_app(monkeypatch, tmp_path):
    import asyncio

    from src.apps.runtime import AppRuntime

    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(
        AppRuntime, "_mapped_folders",
        staticmethod(lambda: [{"name": "docs", "path": str(docs), "mode": "ro"}]))

    rt = AppRuntime.__new__(AppRuntime)
    rt.containers = _FakeContainers()
    rt._apps = {"kb": _loaded_app(tmp_path, [
        {"source": "$AW_WORKSPACE_FOLDERS", "target": "/workspace-folders", "mode": "ro"},
    ])}

    assert asyncio.run(_remap(rt)) == ["kb"]
    assert rt.containers.started == ["kb"]
    assert rt.containers.volumes["kb"][str(docs)] == {
        "bind": "/workspace-folders/docs", "mode": "ro"}
    assert rt.containers.registered_calls == 0


def test_remap_skips_apps_that_never_asked_for_folders(monkeypatch, tmp_path):
    """Mapping a folder must not bounce unrelated containers."""
    import asyncio

    from src.apps.runtime import AppRuntime

    monkeypatch.setattr(AppRuntime, "_mapped_folders", staticmethod(lambda: []))

    rt = AppRuntime.__new__(AppRuntime)
    rt.containers = _FakeContainers()
    rt._apps = {"browser": _loaded_app(tmp_path, [
        {"source": "$AW_APP_DATA", "target": "/data", "mode": "rw"},
    ])}

    assert asyncio.run(_remap(rt)) == []
    assert rt.containers.started == []


def test_remap_skips_tier1_apps(monkeypatch, tmp_path):
    import asyncio

    from src.apps.runtime import AppRuntime

    monkeypatch.setattr(AppRuntime, "_mapped_folders", staticmethod(lambda: []))

    rt = AppRuntime.__new__(AppRuntime)
    rt.containers = _FakeContainers()
    rt._apps = {"proxy": _loaded_app(tmp_path, [
        {"source": "$AW_WORKSPACE_FOLDERS", "target": "/f", "mode": "ro"},
    ], tier="inprocess")}

    assert asyncio.run(_remap(rt)) == []


def test_one_failing_app_does_not_block_the_others(monkeypatch, tmp_path):
    import asyncio

    from src.apps.runtime import AppRuntime

    monkeypatch.setattr(AppRuntime, "_mapped_folders", staticmethod(lambda: []))

    class _HalfBroken(_FakeContainers):
        def start(self, app_id):
            if app_id == "broken":
                raise RuntimeError("podman is down")
            super().start(app_id)

    vols = [{"source": "$AW_WORKSPACE_FOLDERS", "target": "/f", "mode": "ro"}]
    rt = AppRuntime.__new__(AppRuntime)
    rt.containers = _HalfBroken()
    rt._apps = {"broken": _loaded_app(tmp_path, vols), "kb": _loaded_app(tmp_path, vols)}

    assert asyncio.run(_remap(rt)) == ["kb"]
