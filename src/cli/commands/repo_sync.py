"""``aw-workspace-cli repo-sync <repo-name>`` — fast-forward a shared checkout
under ``repos/``, and refuse to do anything else.

Why this exists: ``repos/`` is a SHARED tree. Every agent dispatched against
this workspace gets the same working copy, so after a PR merges, the next
agent sees master-minus-that-merge until somebody pulls. In the Dev Team
PR-first flow (Kanban "Apply PR-first flow to aw-app-template", §7) the coder
woken by the `merged` event runs this as its first instruction.

**The whole value of this command is what it REFUSES to do.** A stale shared
tree already caused one real incident: a working-tree diff taken against an
out-of-date checkout silently reverted a shipped reconcile commit, with zero
git conflict to warn anyone (KB lesson
``ap-mt-c1-working-tree-diff-reverts-reconcile-commit``). The reflex fix for
"the tree is in the way" — reset, stash, checkout master — would make that
incident class WORSE, because the thing standing in the way is usually
another agent's in-progress work, and discarding it looks exactly like
success. There is also a second trap: ``git reset`` on a bind-mounted
checkout orphans the inode the mount points at (KB lesson
``deploy-git-reset-orphans-bind-mounted-inodes``), so even the "harmless"
version is not.

So the contract is deliberately narrow, and deliberately brittle:

* Act ONLY if the checkout is on ``master`` AND ``git status --porcelain`` is
  empty.
* Then exactly ``git fetch origin master`` + ``git merge --ff-only
  FETCH_HEAD``. A non-fast-forward is an error, not a merge commit.
* Anything else: exit non-zero, naming the branch and listing what is dirty,
  and change nothing.

A refusing sync is the command working. A sync that "fixed" the tree to get
past a refusal is the incident. If you are reading this because a refusal is
in your way: report it (branch + dirt) and let a human park the work. Do not
add a ``--force``.
"""
from __future__ import annotations

import argparse
import os
import subprocess

from src.apps.paths import repos_dir

COMMAND = "repo-sync"
DESCRIPTION = "Fast-forward a shared repos/<name> checkout (master + clean only; refuses otherwise)"

REQUIRED_BRANCH = "master"

#: Exit codes, distinct so a caller (or a workflow step) can tell a refusal
#: from a breakage without parsing text. A refusal is 2/3, never 1.
EXIT_OK = 0
EXIT_ERROR = 1          # no such repo, git/network failure, non-fast-forward
EXIT_WRONG_BRANCH = 2
EXIT_DIRTY = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli repo-sync", description=DESCRIPTION,
        epilog="Refuses (non-zero, changes nothing) unless the checkout is on "
               f"{REQUIRED_BRANCH} with a clean working tree. That refusal is "
               "correct behaviour — never reset/stash/checkout to get past it.",
    )
    parser.add_argument("repo", help="Directory name under repos/, e.g. aw-app-template")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the branch/cleanliness verdict and the commits that "
                             "WOULD be fast-forwarded, without fetching or merging")
    return parser


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args],
                          capture_output=True, text=True, check=False)


def run(args: list[str]) -> int:
    ns = _build_parser().parse_args(args)
    name = ns.repo.strip().strip("/")
    if not name or "/" in name or name.startswith("."):
        print(f"error: '{ns.repo}' is not a plain repo name under repos/")
        return EXIT_ERROR

    path = os.path.join(repos_dir(), name)
    if not os.path.isdir(os.path.join(path, ".git")):
        print(f"error: {path} is not a git checkout")
        return EXIT_ERROR

    # ---- the two guards, both BEFORE anything touches the tree ----
    branch = _git(path, "branch", "--show-current")
    if branch.returncode != 0:
        print(f"error: could not read the current branch of {path}:\n{branch.stderr.strip()}")
        return EXIT_ERROR
    current = branch.stdout.strip()

    status = _git(path, "status", "--porcelain")
    if status.returncode != 0:
        print(f"error: could not read the status of {path}:\n{status.stderr.strip()}")
        return EXIT_ERROR
    dirt = [ln for ln in status.stdout.splitlines() if ln.strip()]

    if current != REQUIRED_BRANCH:
        # Loud on purpose, and it names the branch: the point of the message is
        # that whoever reads it can tell a human WHICH work is parked here.
        print(f"REFUSING to sync {name}: checkout is on branch "
              f"'{current or '(detached HEAD)'}', not '{REQUIRED_BRANCH}'.")
        print(f"  {path}")
        if dirt:
            print(f"  ...and {len(dirt)} uncommitted change(s) on it:")
            for ln in dirt[:20]:
                print(f"    {ln}")
            if len(dirt) > 20:
                print(f"    ... and {len(dirt) - 20} more")
        print("Nothing was changed. This is a shared tree: that branch is probably "
              "another agent's in-progress work.")
        print("Do NOT reset, stash, or check out away from it to make this pass — "
              "report this and let a human park the branch.")
        return EXIT_WRONG_BRANCH

    if dirt:
        print(f"REFUSING to sync {name}: on '{REQUIRED_BRANCH}' but the working tree "
              f"has {len(dirt)} uncommitted change(s).")
        print(f"  {path}")
        for ln in dirt[:20]:
            print(f"    {ln}")
        if len(dirt) > 20:
            print(f"    ... and {len(dirt) - 20} more")
        print("Nothing was changed. Do NOT stash or discard these — they may be "
              "another agent's work; report this instead.")
        return EXIT_DIRTY

    # ---- clean on master: the only state in which this command acts ----
    if ns.dry_run:
        print(f"{name}: on '{REQUIRED_BRANCH}', working tree clean — a real run would "
              f"fetch origin {REQUIRED_BRANCH} and merge --ff-only.")
        ahead = _git(path, "log", "--oneline", f"HEAD..origin/{REQUIRED_BRANCH}")
        pending = [ln for ln in ahead.stdout.splitlines() if ln.strip()]
        print(f"  {len(pending)} commit(s) pending in the LAST-FETCHED "
              f"origin/{REQUIRED_BRANCH} (no fetch was performed, so this can be stale):")
        for ln in pending[:20]:
            print(f"    {ln}")
        return EXIT_OK

    before = _git(path, "rev-parse", "HEAD").stdout.strip()

    fetch = _git(path, "fetch", "origin", REQUIRED_BRANCH)
    if fetch.returncode != 0:
        print(f"error: git fetch origin {REQUIRED_BRANCH} failed in {path}:\n"
              f"{fetch.stderr.strip()}")
        return EXIT_ERROR

    # --ff-only, never a plain merge: if origin/master and this checkout have
    # diverged, something committed here that is not upstream, and resolving
    # that is a human's call. The error is the right outcome.
    merge = _git(path, "merge", "--ff-only", "FETCH_HEAD")
    if merge.returncode != 0:
        print(f"error: fast-forward of {name} failed — the checkout has diverged from "
              f"origin/{REQUIRED_BRANCH} (local commits that are not upstream).")
        print(f"  {path}")
        print(merge.stdout.strip() or merge.stderr.strip())
        print("Nothing was discarded. A human needs to look at the local commits.")
        return EXIT_ERROR

    after = _git(path, "rev-parse", "HEAD").stdout.strip()
    if before == after:
        print(f"{name}: already up to date at {after[:12]} ({REQUIRED_BRANCH}, clean).")
        return EXIT_OK

    print(f"{name}: fast-forwarded {before[:12]} -> {after[:12]} on {REQUIRED_BRANCH}.")
    log = _git(path, "log", "--oneline", f"{before}..{after}")
    for ln in log.stdout.splitlines()[:20]:
        print(f"  {ln}")
    return EXIT_OK
