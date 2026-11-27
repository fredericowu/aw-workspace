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
import stat
import tarfile
import tempfile
import time
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


def _download_headers(repo: str, url: str, token: str | None) -> dict[str, str]:
    """Auth headers for the tarball download.

    Precedence: an explicitly-passed ``token`` (a caller that already knows
    which credential to use) → the credential of the marketplace source that
    published this app (so a private app from a private marketplace installs,
    not just lists) → the legacy global ``AW_APP_GIT_TOKEN``.
    """
    if token:
        return {"Authorization": f"Bearer {token}"}
    try:
        from src.apps import catalog

        headers = catalog.auth_headers_for_repo(repo, url)
        if headers:
            return headers
    except Exception as e:  # noqa: BLE001 — never block an install on this
        log.debug("apps: marketplace credential lookup failed for %s (%s)", repo, e)
    env_token = os.environ.get("AW_APP_GIT_TOKEN")
    return {"Authorization": f"Bearer {env_token}"} if env_token else {}


_DOWNLOAD_ATTEMPTS = 3
_EXTRACT_ATTEMPTS = 2

#: A ``.<slug>.fetch-*`` staging dir older than this belongs to a fetch that
#: was killed (container restart, OOM) before ``TemporaryDirectory`` could
#: clean up; no live fetch runs for anywhere near this long.
STALE_FETCH_DIR_AGE_S = 3600


def _force_rmtree(path: str) -> None:
    """``rmtree`` that also deletes trees holding read-only / non-searchable dirs."""
    def _open_up(d: str) -> None:
        try:
            if stat.S_ISDIR(os.lstat(d).st_mode):  # never chmod through a symlink
                os.chmod(d, 0o700)
        except OSError:
            pass

    _open_up(path)
    for root, dirs, _files in os.walk(path, topdown=True):
        for d in dirs:  # top-down: make each child listable before os.walk descends
            _open_up(os.path.join(root, d))
    shutil.rmtree(path, ignore_errors=True)
    if os.path.lexists(path):
        raise OSError(f"could not remove {path}")


def sweep_stale_fetch_dirs(parent: str, *, max_age_s: float = STALE_FETCH_DIR_AGE_S,
                           now: float | None = None) -> list[str]:
    """Delete leftover ``.<slug>.fetch-*`` staging dirs under ``parent``.

    Only dirs older than ``max_age_s`` are touched, so a fetch running right
    now in another thread/process is never swept. Returns the removed paths.
    """
    now = time.time() if now is None else now
    removed: list[str] = []
    try:
        names = os.listdir(parent)
    except OSError:
        return removed
    for name in names:
        if not (name.startswith(".") and ".fetch-" in name):
            continue
        path = os.path.join(parent, name)
        try:
            st = os.lstat(path)
            if now - st.st_mtime < max_age_s or not stat.S_ISDIR(st.st_mode):
                continue
            _force_rmtree(path)
            removed.append(path)
        except OSError as e:
            log.warning("apps: could not sweep stale fetch dir %s: %s", path, e)
    if removed:
        log.info("apps: swept %d stale fetch dir(s) under %s", len(removed), parent)
    return removed


def _describe_path_modes(path: str) -> str:
    """``mode`` of each existing component of ``path`` (diagnostic for EACCES)."""
    parts, cur, out = path.strip(os.sep).split(os.sep), os.sep, []
    for part in parts:
        cur = os.path.join(cur, part)
        try:
            out.append(f"{part}={os.lstat(cur).st_mode & 0o7777:o}")
        except OSError as e:
            out.append(f"{part}=!{e.errno}")
            break
    return f"uid={os.getuid()} umask={_current_umask()} " + " ".join(out[-5:])


def _current_umask() -> str:
    # Read, never set: os.umask() can only read by writing, and a transient
    # process-wide write is exactly the kind of thing this diagnostic hunts.
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return line.split()[1]
    except OSError:
        pass
    return "?"


def fetch_app_repo(repo: str, ref: str = "HEAD", *, slug: str,
                   token: str | None = None, dest: str | None = None) -> str:
    """Download ``repo``'s tarball at ``ref`` and extract it to ``dest``.

    Returns the package dir (containing the app's ``aw-app.json``). Idempotent:
    download and extraction land in a sibling tmp dir first, and ``dest`` is
    only ever touched afterwards, via two ``os.replace`` calls (POSIX rename,
    atomic on the same filesystem — both the tmp dir and ``dest.old`` are
    created next to ``dest`` for exactly this reason) — the existing ``dest``
    is renamed to ``dest.old`` and the new tree renamed into ``dest``'s place.
    If the second replace fails, ``dest.old`` is renamed straight back so a
    failed re-fetch never leaves ``dest`` missing or half-written. The
    download itself retries with backoff before any of this starts, since a
    transport failure (e.g. a proxy/tunnel cutting the connection) is the
    common way this gets interrupted.
    """
    dest = dest or package_dir_for(slug)
    url = _tarball_url(repo, ref)
    headers = _download_headers(repo, url, token)

    parent = os.path.dirname(dest) or "."
    os.makedirs(parent, exist_ok=True)

    sweep_stale_fetch_dirs(parent)

    with tempfile.TemporaryDirectory(dir=parent, prefix=f".{slug}.fetch-") as tmp_dir:
        archive_path = os.path.join(tmp_dir, "repo.tar.gz")
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60.0) as resp:
                    resp.raise_for_status()
                    with open(archive_path, "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
                            # heartbeat: file writes don't bump the dir's mtime, and the
                            # sweep ages staging dirs by it
                            os.utime(tmp_dir)
                break
            except httpx.TransportError as e:
                if attempt == _DOWNLOAD_ATTEMPTS:
                    raise FetchError(f"download of {url} failed after {attempt} attempts: {e}") from e
                delay = 2 ** (attempt - 1)
                log.warning("apps: download of %s failed (attempt %d/%d), retrying in %ds: %s",
                            url, attempt, _DOWNLOAD_ATTEMPTS, delay, e)
                time.sleep(delay)
            except httpx.HTTPError as e:
                raise FetchError(f"download of {url} failed: {e}") from e

        for attempt in range(1, _EXTRACT_ATTEMPTS + 1):
            # own path per attempt: a failed attempt's partial tree never has to
            # be removed before the next one starts
            extract_dir = os.path.join(tmp_dir, "extracted" if attempt == 1 else f"extracted-{attempt}")
            try:
                with tarfile.open(archive_path, "r:gz") as tf:
                    _safe_extract(tf, extract_dir)
                break
            except tarfile.TarError as e:
                raise FetchError(f"extraction of {url} failed: {e}") from e
            except PermissionError as e:
                # Seen on the aw-desktop (virtiofs) workspace 2026-11-26: EACCES
                # from tarfile's path check on a dir this same process had just
                # created. Not reproducible on demand, so record the modes that
                # were actually on disk and retry once in a fresh dir.
                diag = _describe_path_modes(e.filename or extract_dir)
                log.warning("apps: extraction of %s hit EACCES (attempt %d/%d): %s [%s]",
                            url, attempt, _EXTRACT_ATTEMPTS, e, diag)
                if attempt == _EXTRACT_ATTEMPTS:
                    raise FetchError(f"extraction of {url} failed: {e} [{diag}]") from e
                try:
                    _force_rmtree(extract_dir)
                except OSError as cleanup_err:  # best effort; TemporaryDirectory retries at exit
                    log.warning("apps: could not remove partial extraction %s: %s", extract_dir, cleanup_err)
                time.sleep(0.2)

        dest_old = dest + ".old"
        if os.path.exists(dest_old):
            shutil.rmtree(dest_old)

        swapped_old = False
        if os.path.exists(dest):
            os.replace(dest, dest_old)
            swapped_old = True
        try:
            os.replace(extract_dir, dest)
        except OSError as e:
            if swapped_old:
                os.replace(dest_old, dest)
                log.error("apps: swap into %s failed (%s); restored previous install", dest, e)
            raise FetchError(f"swap into {dest} failed: {e}") from e

        if swapped_old:
            shutil.rmtree(dest_old)

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
