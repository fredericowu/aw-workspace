"""App-repo fetch (ADR Decision 5 — F3).

Given an app to install (``repo`` + ``ref`` from the cloud registry / marketplace
catalog, e.g. ``tekflox/aw-app-git``), clone or pull the app's git repo into the
**host-mounted** workspace tree so the package survives container recreation, and
**pin** the working tree to the catalog's ``ref`` (branch, tag, or commit sha).

Package layout (minimal F3 — the ADR's per-version ``~/.aw-workspace/apps/<slug>/
<version>/`` layout is F8's upgrade concern): one clone per app at
``<APPS_ROOT>/<slug>``. ``APPS_ROOT`` defaults to ``/opt/agentic-workspace/apps``
(the WORKDIR is bind-mounted from the host — see
``aw-workspace-base-dir-host-mount``), overridable with ``AW_APPS_ROOT`` (tests
point it at a tmp dir).

Private repos: a git token (``AW_APP_GIT_TOKEN`` env, or passed explicitly) is
injected into an ``https://github.com/<owner>/<repo>`` URL as
``https://x-access-token:<token>@github.com/...`` for the clone/fetch only — it
is never written into the repo's persisted ``remote.origin.url`` (we set the
clean URL back afterwards), so the token never lands on disk.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

_DEFAULT_APPS_ROOT = "/opt/agentic-workspace/apps"


def apps_root() -> str:
    return os.environ.get("AW_APPS_ROOT", _DEFAULT_APPS_ROOT)


def package_dir_for(slug: str) -> str:
    return os.path.join(apps_root(), slug)


class FetchError(RuntimeError):
    """Raised when cloning/pinning an app repo fails."""


def _run(args: list[str], cwd: str | None = None, check: bool = True) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # never block on an auth prompt
    )
    if check and proc.returncode != 0:
        # keep the token out of any surfaced error text
        raise FetchError(
            f"git {' '.join(a for a in args if 'x-access-token' not in a)} failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.returncode, proc.stdout.strip()


def _rev_exists(rev: str, cwd: str) -> bool:
    rc, _ = _run(["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                 cwd=cwd, check=False)
    return rc == 0


def _normalize_repo_url(repo: str) -> str:
    """``owner/name`` -> a full GitHub https URL; a full URL is returned as-is."""
    if "://" in repo or repo.startswith("git@"):
        return repo
    return f"https://github.com/{repo.removesuffix('.git')}.git"


def _with_token(url: str, token: str | None) -> str:
    if not token or not url.startswith("https://"):
        return url
    parts = urlsplit(url)
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def fetch_app_repo(repo: str, ref: str = "HEAD", *, slug: str,
                   token: str | None = None, dest: str | None = None) -> str:
    """Clone or update ``repo`` at ``dest`` and pin the tree to ``ref``.

    Returns the package dir (containing the app's ``aw-app.json``). Idempotent:
    an existing clone is fetched + hard-reset to ``ref`` rather than re-cloned,
    so a recreated-but-host-mount-preserved workspace converges cheaply.
    """
    token = token or os.environ.get("AW_APP_GIT_TOKEN") or None
    clean_url = _normalize_repo_url(repo)
    auth_url = _with_token(clean_url, token)
    dest = dest or package_dir_for(slug)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    if os.path.isdir(os.path.join(dest, ".git")):
        _run(["git", "remote", "set-url", "origin", auth_url], cwd=dest)
        _run(["git", "fetch", "--tags", "--force", "origin"], cwd=dest)
    else:
        if os.path.exists(dest):
            shutil.rmtree(dest)
        _run(["git", "clone", auth_url, dest])

    # Pin the working tree. Prefer the remote-tracking ref (``origin/<ref>``)
    # for a branch so a re-fetch advances to the new tip; fall back to the ref
    # as-is for a tag or commit sha. Checkout is detached — we only ever read.
    target = f"origin/{ref}" if _rev_exists(f"origin/{ref}", dest) else ref
    _run(["git", "checkout", "--force", "--detach", target], cwd=dest)
    _run(["git", "reset", "--hard", target], cwd=dest)

    if token:  # never persist the token in the on-disk remote
        _run(["git", "remote", "set-url", "origin", clean_url], cwd=dest)

    log.info("apps: fetched %s@%s into %s", repo, ref, dest)
    return dest


def remove_app_repo(slug: str, dest: str | None = None) -> bool:
    """Delete an app's cloned repo (uninstall). Returns True if anything was removed."""
    dest = dest or package_dir_for(slug)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
        log.info("apps: removed repo %s", dest)
        return True
    return False
