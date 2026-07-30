"""App-repo fetch (ADR Decision 5 — F3).

Given an app to install (``repo`` + ``ref`` from the cloud registry / marketplace
catalog, e.g. ``tekflox/aw-app-git``), download the app's code as a **public
GitHub tarball over HTTP** and extract it into the **host-mounted** workspace
tree so the package survives container recreation. No git: the aw-workspace
base image is intentionally slim (git is what the ``aw-app-git`` app installs)
so fetching itself must not depend on it. Consistent with the project's public/
raw-GET model (see ``catalog.py``'s marketplace fetch).

Package layout (minimal F3 — the ADR's per-version ``~/.aw-workspace/apps/<slug>/
<version>/`` layout is F8's upgrade concern): one extracted tree per app at
``<APPS_ROOT>/<slug>``. ``APPS_ROOT`` defaults to ``/opt/aw-workspace/apps``
(the WORKDIR is bind-mounted from the host — see
``aw-workspace-base-dir-host-mount``), overridable with ``AW_APPS_ROOT`` (tests
point it at a tmp dir).

Pin = the ``ref`` baked into the tarball URL (branch, tag, or commit sha) —
there is no local git history to check against. GitHub's tarball endpoint
(``https://api.github.com/repos/<owner>/<repo>/tarball/<ref>``) resolves all
three ref shapes uniformly and works unauthenticated for public repos,
redirecting to codeload.github.com.

Private repos: an optional token (``AW_APP_GIT_TOKEN`` env, or passed
explicitly) is sent as an ``Authorization: Bearer`` header on the download
request only — never persisted anywhere.
"""
from __future__ import annotations

import logging
import os
import shutil
import tarfile
import tempfile
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

_DEFAULT_APPS_ROOT = "/opt/aw-workspace/apps"


def apps_root() -> str:
    return os.environ.get("AW_APPS_ROOT", _DEFAULT_APPS_ROOT)


def package_dir_for(slug: str) -> str:
    return os.path.join(apps_root(), slug)


class FetchError(RuntimeError):
    """Raised when downloading/extracting an app repo fails."""


def _normalize_repo_url(repo: str) -> str:
    """``owner/name`` or a full GitHub URL -> ``owner/name``."""
    if repo.startswith("git@"):
        path = repo.split(":", 1)[-1]  # git@github.com:owner/name.git
    elif "://" in repo:
        path = urlsplit(repo).path.lstrip("/")
    else:
        path = repo
    return path.removesuffix(".git")


def _tarball_url(repo: str, ref: str) -> str:
    owner_repo = _normalize_repo_url(repo)
    ref = ref or "HEAD"
    return f"https://api.github.com/repos/{owner_repo}/tarball/{ref}"


def _safe_extract(tf: tarfile.TarFile, dest: str) -> None:
    """Extract ``tf`` into ``dest``, stripping the tarball's single root dir.

    GitHub tarballs wrap everything in one ``<owner>-<repo>-<sha>/`` directory;
    its contents are extracted directly into ``dest``. Guards against path
    traversal (``..`` components, absolute paths, symlinks escaping ``dest``).
    """
    members = tf.getmembers()
    if not members:
        raise FetchError("tarball is empty")

    root_prefix = members[0].name.split("/", 1)[0] + "/"
    dest_abs = os.path.abspath(dest)

    safe_members = []
    for member in members:
        name = member.name
        if name == root_prefix.rstrip("/"):
            continue  # the root dir entry itself
        if not name.startswith(root_prefix):
            raise FetchError(f"tarball member {name!r} escapes the expected root dir")
        rel = name[len(root_prefix):]
        if not rel:
            continue
        if os.path.isabs(rel) or ".." in rel.split("/"):
            raise FetchError(f"tarball member {name!r} attempts path traversal")
        target = os.path.abspath(os.path.join(dest_abs, rel))
        if not (target == dest_abs or target.startswith(dest_abs + os.sep)):
            raise FetchError(f"tarball member {name!r} escapes the destination dir")
        if member.issym() or member.islnk():
            link_target = os.path.normpath(os.path.join(os.path.dirname(target), member.linkname))
            if not (link_target == dest_abs or link_target.startswith(dest_abs + os.sep)):
                raise FetchError(f"tarball member {name!r} has a link escaping the destination dir")
        member.name = rel
        safe_members.append(member)

    os.makedirs(dest, exist_ok=True)
    tf.extractall(dest, members=safe_members, filter="data")


def fetch_app_repo(repo: str, ref: str = "HEAD", *, slug: str,
                   token: str | None = None, dest: str | None = None) -> str:
    """Download ``repo``'s tarball at ``ref`` and extract it to ``dest``.

    Returns the package dir (containing the app's ``aw-app.json``). Idempotent:
    extraction lands in a tmp dir first, then atomically swaps into place, so a
    re-fetch (e.g. after the source advanced) fully replaces the prior tree.
    """
    token = token or os.environ.get("AW_APP_GIT_TOKEN") or None
    dest = dest or package_dir_for(slug)
    url = _tarball_url(repo, ref)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=parent, prefix=f".{slug}.fetch-") as tmp_dir:
        archive_path = os.path.join(tmp_dir, "repo.tar.gz")
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60.0) as resp:
                resp.raise_for_status()
                with open(archive_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise FetchError(f"download of {url} failed: {e}") from e

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                _safe_extract(tf, extract_dir)
        except tarfile.TarError as e:
            raise FetchError(f"extraction of {url} failed: {e}") from e

        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.move(extract_dir, dest)

    log.info("apps: fetched %s@%s into %s", repo, ref, dest)
    return dest


def remove_app_repo(slug: str, dest: str | None = None) -> bool:
    """Delete an app's extracted repo (uninstall). Returns True if anything was removed."""
    dest = dest or package_dir_for(slug)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
        log.info("apps: removed repo %s", dest)
        return True
    return False
