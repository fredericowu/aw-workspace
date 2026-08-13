"""App-contributed repository clones (``contributes.repos``).

An app whose feature set *is* a checkout — aw-app-crispal is the store's
WordPress tree, mounted straight into a WordPress container — used to depend
on somebody having cloned that repo by hand into ``repos/`` first. Nothing
declared the dependency, so a fresh workspace installed the app, started the
containers, and served an empty document root.

    "contributes": {
      "repos": [
        {"name": "crispal", "url": "https://github.com/tekflox/crispal.git"}
      ]
    }

Each entry is cloned into ``paths.repos_dir()/<name>`` (``/opt/aw-workspace/
repos/<name>``) — the same top-level directory a workspace terminal clones
into, deliberately NOT a private per-app dir. The point is that the checkout
is a first-class workspace repo: the git app's watchdog sees it, the KB
indexes it, an agent can ``cd`` into it. An app reads it back by declaring a
``$AW_WORKSPACE_REPOS`` volume.

Clone-once semantics
--------------------

Same posture as ``contributes.tasks`` / ``contributes.agents``: **seeded, not
owned**. Identity is the directory name.

* ``repos/<name>`` already exists → left completely alone, no fetch, no pull
* nothing there                   → cloned

A checkout is a working tree — the user has branches, uncommitted work, 24 GB
of WordPress uploads that were never in git. Re-asserting the app's version
of it on every boot is the one thing that must never happen, so this never
runs a second git command against an existing directory. Nothing is removed
on uninstall either, for the same reason: the clone belongs to the user the
moment it exists.

Cloning is slow (minutes, for a big repo) and network-bound, so it runs off
the event loop and a failure is logged, never fatal — an app whose *code*
works but whose checkout didn't land is a far better outcome than an app that
refuses to install. ``depth`` is honoured when declared, and credentials come
from whatever the workspace's git already has configured (``.git-credentials``
at the workspace root), exactly as if a terminal had run the clone.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess

from src.apps import paths

log = logging.getLogger(__name__)

#: A clone that hangs must not hang activation forever.
CLONE_TIMEOUT_S = 1800


class RepoError(Exception):
    """Raised when a ``contributes.repos`` entry is invalid."""


def resolve_dest(name: str) -> str:
    """Absolute path ``repos/<name>``, refusing anything that escapes it.

    ``name`` is a plain directory name, never a path: ``../../etc`` and
    ``a/b`` are both rejected rather than normalised, since an app choosing
    where in the filesystem to write is precisely what this surface must not
    allow.
    """
    clean = str(name or "").strip()
    if not clean or clean in (".", "..") or "/" in clean or "\\" in clean:
        raise RepoError(f"invalid repo name {name!r} (must be a plain directory name)")
    root = os.path.realpath(paths.repos_dir())
    dest = os.path.realpath(os.path.join(root, clean))
    if dest != os.path.join(root, clean) or not dest.startswith(root + os.sep):
        raise RepoError(f"repo name {name!r} escapes the repos dir")
    return dest


class ReposRegistry:
    """Runtime-owned backend for the ``contributes.repos`` surface."""

    def __init__(self, clone_timeout_s: int = CLONE_TIMEOUT_S) -> None:
        self._clone_timeout_s = clone_timeout_s

    def clone(self, app_id: str, spec: dict) -> str | None:
        """Clone one declared repo. Returns the path if it created it, else None.

        None covers both "already there" (the common case, every boot after
        the first) and "we tried and failed" — neither is something the
        caller can act on differently, and both are logged here.
        """
        name = str(spec.get("name") or "").strip()
        url = str(spec.get("url") or "").strip()
        if not name or not url:
            raise RepoError("each contributes.repos entry needs a 'name' and a 'url'")
        dest = resolve_dest(name)
        if os.path.exists(dest):
            log.debug("apps: repo %r already checked out, leaving it alone", name)
            return None
        if shutil.which("git") is None:
            log.warning("apps: cannot clone %r for %s — no git on PATH", name, app_id)
            return None

        cmd = ["git", "clone"]
        depth = spec.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0:
            cmd += ["--depth", str(depth)]
        branch = str(spec.get("branch") or "").strip()
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, dest]

        log.info("apps: cloning %s -> %s for %s", url, dest, app_id)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self._clone_timeout_s,
                # Never sit on a credential prompt: a private repo the
                # workspace has no credentials for must fail fast, not block
                # activation until the timeout.
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            log.error("apps: clone of %r for %s timed out after %ss",
                      name, app_id, self._clone_timeout_s)
            self._cleanup(dest)
            return None
        except OSError:
            log.exception("apps: clone of %r for %s failed to start", name, app_id)
            return None
        if proc.returncode != 0:
            log.error("apps: clone of %r for %s failed (rc=%s): %s",
                      name, app_id, proc.returncode, (proc.stderr or "").strip()[:500])
            # git leaves a partial directory behind on failure; a half-clone
            # would be indistinguishable from a real checkout on the next boot
            # and would never be retried.
            self._cleanup(dest)
            return None
        log.info("apps: cloned repo %r for %s", name, app_id)
        return dest

    def register(self, app_id: str, specs: list[dict]) -> list[str]:
        """Clone every declared repo. Returns the paths actually created."""
        created: list[str] = []
        for spec in specs or []:
            try:
                path = self.clone(app_id, dict(spec))
            except Exception:  # noqa: BLE001 — a bad entry must not fail activation
                log.exception("apps: failed to clone a repo declared by %s", app_id)
                continue
            if path:
                created.append(path)
        return created

    @staticmethod
    def _cleanup(dest: str) -> None:
        shutil.rmtree(dest, ignore_errors=True)
