"""``contributes.repos`` — clone-once checkouts (src/apps/repos.py)."""
from __future__ import annotations

import os
import subprocess

import pytest

from src.apps import paths
from src.apps.manifest import ManifestError, validate_manifest
from src.apps.repos import RepoError, ReposRegistry, resolve_dest


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway workspace root, so repos_dir() is tmp and not the real one."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / ".aw-workspace"))
    return tmp_path


def _origin(tmp_path):
    """A real local git repo to clone from — no network in unit tests."""
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "README.md").write_text("hello\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True, env=env)
    subprocess.run(["git", "add", "."], cwd=origin, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=origin, check=True, env=env)
    return origin


# ---- name resolution --------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", ".", "..", "../etc", "a/b", "a\\b"])
def test_a_name_that_is_a_path_is_refused(workspace, bad):
    """An app picking WHERE core writes is the thing this surface must not allow."""
    with pytest.raises(RepoError):
        resolve_dest(bad)


def test_a_plain_name_lands_under_the_repos_dir(workspace):
    assert resolve_dest("crispal") == os.path.join(
        os.path.realpath(paths.repos_dir()), "crispal")


# ---- clone-once semantics ---------------------------------------------------

def test_clones_into_repos_dir(workspace, tmp_path):
    origin = _origin(tmp_path)
    dest = ReposRegistry().clone("crispal", {"name": "crispal", "url": str(origin)})
    assert dest is not None
    assert os.path.isfile(os.path.join(dest, "README.md"))


def test_an_existing_checkout_is_left_completely_alone(workspace, tmp_path):
    """The user's working tree wins: no fetch, no pull, no clobber."""
    origin = _origin(tmp_path)
    reg = ReposRegistry()
    dest = reg.clone("crispal", {"name": "crispal", "url": str(origin)})
    # Local edits + an untracked file, exactly what a real checkout accumulates.
    with open(os.path.join(dest, "README.md"), "w") as f:
        f.write("edited by the user\n")
    open(os.path.join(dest, "uploads.bin"), "w").close()

    assert reg.clone("crispal", {"name": "crispal", "url": str(origin)}) is None

    with open(os.path.join(dest, "README.md")) as f:
        assert f.read() == "edited by the user\n"
    assert os.path.exists(os.path.join(dest, "uploads.bin"))


def test_a_failed_clone_leaves_no_partial_directory(workspace, tmp_path):
    """Otherwise the half-clone reads as a real checkout and is never retried."""
    reg = ReposRegistry()
    assert reg.clone("x", {"name": "nope", "url": str(tmp_path / "does-not-exist")}) is None
    assert not os.path.exists(resolve_dest("nope"))


def test_register_reports_only_what_it_created(workspace, tmp_path):
    origin = _origin(tmp_path)
    reg = ReposRegistry()
    specs = [{"name": "a", "url": str(origin)}, {"name": "b", "url": str(origin)}]
    assert len(reg.register("app", specs)) == 2
    assert reg.register("app", specs) == []


def test_one_bad_entry_does_not_stop_the_others(workspace, tmp_path):
    origin = _origin(tmp_path)
    created = ReposRegistry().register("app", [
        {"name": "../escape", "url": str(origin)},
        {"name": "good", "url": str(origin)},
    ])
    assert [os.path.basename(p) for p in created] == ["good"]


# ---- manifest validation ----------------------------------------------------

def _manifest(**over):
    data = {
        "manifest_version": 1, "id": "demo", "name": "Demo", "version": "1.0.0",
        "tier": "inprocess", "runtime": {"entrypoint": "mod:Cls"},
        "permissions": ["repos:contribute"],
        "contributes": {"repos": [{"name": "crispal", "url": "https://x/y.git"}]},
    }
    data.update(over)
    return data


def test_repos_are_parsed_off_the_manifest():
    assert validate_manifest(_manifest()).repos == [
        {"name": "crispal", "url": "https://x/y.git"}]


def test_repos_require_the_permission():
    with pytest.raises(ManifestError, match="repos:contribute"):
        validate_manifest(_manifest(permissions=[]))


@pytest.mark.parametrize("entry", [
    {"name": "crispal"},                      # no url
    {"url": "https://x/y.git"},               # no name
    {"name": "a/b", "url": "https://x/y"},    # a path, not a name
    {"name": "x", "url": "https://x/y", "depth": 0},
    {"name": "x", "url": "https://x/y", "depth": "1"},
])
def test_a_malformed_entry_is_rejected_at_validation(entry):
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(contributes={"repos": [entry]}))


def test_no_repos_declared_is_an_empty_list():
    assert validate_manifest(_manifest(contributes={}, permissions=[])).repos == []
