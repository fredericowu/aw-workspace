"""``aw-workspace-cli repo-sync`` — the refusals, and that a refusal changes
nothing.

These tests are mostly about the REFUSE paths, because that is the whole
point of the command: a stale shared ``repos/`` checkout already caused one
incident where a diff silently reverted a shipped commit (KB
``ap-mt-c1-working-tree-diff-reverts-reconcile-commit``), and the obvious
"fix the tree so the sync can proceed" reflex — reset/stash/checkout — would
discard whichever agent's in-progress work is sitting there. So each refusal
test asserts the tree afterwards, not just the exit code: "it printed a
warning and then did it anyway" has to be a failing test, not a code review
note.

Real git repos in tmp_path, driven through the module's ``run()`` exactly
like the CLI entrypoint does, with ``AW_WORKSPACE_CONTAINER_DIR`` pointed at
the fixture (which is what ``src.apps.paths.repos_dir`` reads).
"""
from __future__ import annotations

import subprocess

import pytest

from src.cli.commands import repo_sync


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture()
def shared_tree(tmp_path, monkeypatch):
    """A bare origin, a `repos/demo` clone of it, and one upstream commit the
    clone does not have yet — i.e. exactly the post-merge stale-tree state."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repos = tmp_path / "repos"
    repos.mkdir()

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "a.txt").write_text("one\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "c1")
    _git(seed, "branch", "-M", "master")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "master")

    checkout = repos / "demo"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "t")

    # upstream moves on — the commit a real merge would have produced
    (seed / "b.txt").write_text("two\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "c2 upstream")
    _git(seed, "push", "-q", "origin", "master")

    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    return checkout


def _state(checkout):
    return (_git(checkout, "branch", "--show-current").stdout.strip(),
            _git(checkout, "status", "--porcelain").stdout,
            _git(checkout, "rev-parse", "HEAD").stdout.strip())


# ---- the happy path, which is the narrow one -------------------------

def test_clean_master_fast_forwards(shared_tree, capsys):
    before = _git(shared_tree, "rev-parse", "HEAD").stdout.strip()

    assert repo_sync.run(["demo"]) == repo_sync.EXIT_OK

    after = _git(shared_tree, "rev-parse", "HEAD").stdout.strip()
    assert after != before
    assert (shared_tree / "b.txt").exists()      # the upstream commit landed
    assert "fast-forwarded" in capsys.readouterr().out


def test_already_up_to_date_is_a_success(shared_tree):
    assert repo_sync.run(["demo"]) == repo_sync.EXIT_OK
    assert repo_sync.run(["demo"]) == repo_sync.EXIT_OK


# ---- refusals --------------------------------------------------------

def test_refuses_on_another_branch_and_changes_nothing(shared_tree, capsys):
    _git(shared_tree, "checkout", "-q", "-b", "probe/someone-elses-work")
    before = _state(shared_tree)

    assert repo_sync.run(["demo"]) == repo_sync.EXIT_WRONG_BRANCH

    assert _state(shared_tree) == before
    out = capsys.readouterr().out
    assert "REFUSING" in out
    # Names the branch, so the report a coder relays identifies the work.
    assert "probe/someone-elses-work" in out


def test_refuses_on_a_dirty_master_and_keeps_the_dirt(shared_tree, capsys):
    (shared_tree / "a.txt").write_text("locally edited\n")
    (shared_tree / "untracked.txt").write_text("someone's scratch\n")
    before = _state(shared_tree)

    assert repo_sync.run(["demo"]) == repo_sync.EXIT_DIRTY

    assert _state(shared_tree) == before
    assert (shared_tree / "a.txt").read_text() == "locally edited\n"
    assert (shared_tree / "untracked.txt").exists()
    out = capsys.readouterr().out
    assert "REFUSING" in out
    assert "a.txt" in out and "untracked.txt" in out   # lists what is dirty


def test_refuses_on_a_detached_head(shared_tree):
    sha = _git(shared_tree, "rev-parse", "HEAD").stdout.strip()
    _git(shared_tree, "checkout", "-q", sha)
    before = _state(shared_tree)

    assert repo_sync.run(["demo"]) == repo_sync.EXIT_WRONG_BRANCH
    assert _state(shared_tree) == before


def test_diverged_history_errors_without_discarding_the_local_commit(shared_tree, capsys):
    """Clean and on master, but carrying a commit that is not upstream.
    --ff-only must refuse rather than make a merge commit, and the local
    commit must still be there afterwards."""
    (shared_tree / "local.txt").write_text("local only\n")
    _git(shared_tree, "add", "-A")
    _git(shared_tree, "commit", "-qm", "local-only commit")
    local_head = _git(shared_tree, "rev-parse", "HEAD").stdout.strip()

    assert repo_sync.run(["demo"]) == repo_sync.EXIT_ERROR

    assert _git(shared_tree, "rev-parse", "HEAD").stdout.strip() == local_head
    assert (shared_tree / "local.txt").exists()
    assert "diverged" in capsys.readouterr().out


# ---- argument handling ----------------------------------------------

def test_unknown_repo_errors(shared_tree, capsys):
    assert repo_sync.run(["not-a-repo"]) == repo_sync.EXIT_ERROR
    assert "not a git checkout" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["../etc", "a/b", ".hidden"])
def test_rejects_anything_that_is_not_a_plain_repo_name(shared_tree, name, capsys):
    assert repo_sync.run([name]) == repo_sync.EXIT_ERROR
    assert "plain repo name" in capsys.readouterr().out


def test_dry_run_touches_nothing(shared_tree, capsys):
    before = _state(shared_tree)
    assert repo_sync.run(["demo", "--dry-run"]) == repo_sync.EXIT_OK
    assert _state(shared_tree) == before
    assert "would" in capsys.readouterr().out.lower()
