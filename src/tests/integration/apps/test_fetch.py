"""App-repo fetch tests (F3) — tarball HTTP, no git.

``fetch_app_repo`` downloads a GitHub tarball (``api.github.com/repos/<o>/<r>/
tarball/<ref>``) and extracts it with ``tarfile`` — no git involved (the
aw-workspace base image doesn't have git; see ``src/apps/fetch.py``'s
docstring). These tests fake the HTTP layer (``httpx.stream``) with an
in-memory ``.tar.gz`` fixture, so no network is hit, and assert ``subprocess``
is never invoked.
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
from contextlib import contextmanager

import httpx
import pytest

from src.apps import catalog as catalog_mod
from src.apps import fetch as fetch_mod


@pytest.fixture(autouse=True)
def _no_catalog_network(monkeypatch):
    """Every call here that doesn't pass an explicit ``token`` falls through
    ``_download_headers`` into ``catalog.auth_headers_for_repo`` ->
    ``get_catalog()`` — a REAL network fetch of the marketplace catalog from
    raw.githubusercontent.com, which this file's own docstring claims never
    happens. Undetected because whichever test hit it first here always ran
    after an earlier, still-hanging CI test elsewhere in the suite (see
    incident 2026-09-03, commit 50c1671) — this file's "no network is hit"
    promise was already broken on its own before that fix ever landed.
    """
    monkeypatch.setattr(catalog_mod, "auth_headers_for_repo", lambda repo, url: {})


def _make_tarball(root: str, files: dict[str, str | None]) -> bytes:
    """Build an in-memory .tar.gz with everything nested under ``root/``.

    ``files`` maps relative path -> content; ``None`` content means "this is
    a directory entry" for path-traversal test fixtures.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        root_info = tarfile.TarInfo(name=root)
        root_info.type = tarfile.DIRTYPE
        root_info.mode = 0o755
        tf.addfile(root_info)
        for rel, content in files.items():
            info = tarfile.TarInfo(name=f"{root}/{rel}")
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
                continue
            data = content.encode()
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, body: bytes, url: str = ""):
        self._body = body
        self.url = url

    def raise_for_status(self):
        pass

    def iter_bytes(self):
        yield self._body


def _patch_httpx_stream(monkeypatch, body: bytes, capture: dict):
    @contextmanager
    def fake_stream(method, url, *, headers=None, follow_redirects=None, timeout=None):
        capture["method"] = method
        capture["url"] = url
        capture["headers"] = headers
        yield _FakeResponse(body)

    monkeypatch.setattr(fetch_mod.httpx, "stream", fake_stream)


def _forbid_subprocess(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("fetch_app_repo must not shell out to subprocess/git")
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


DEFAULT_FILES = {
    "aw-app.json": '{"manifest_version": 1, "id": "widget", "name": "widget"}',
    "plugin.py": "class AppPlugin:\n    async def activate(self, ctx):\n        pass\n",
    "src": None,
    "src/util.py": "x = 1\n",
}


def test_fetch_strips_root_dir_and_lands_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    _forbid_subprocess(monkeypatch)
    tarball = _make_tarball("acme-widget-abc123", DEFAULT_FILES)
    capture = {}
    _patch_httpx_stream(monkeypatch, tarball, capture)

    dest = fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert dest == str(tmp_path / "apps" / "widget")
    assert (tmp_path / "apps" / "widget" / "aw-app.json").read_text().startswith('{"manifest_version"')
    assert (tmp_path / "apps" / "widget" / "src" / "util.py").read_text() == "x = 1\n"
    # no leftover root-dir wrapper
    assert not (tmp_path / "apps" / "widget" / "acme-widget-abc123").exists()


def test_pin_by_ref_in_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    tarball = _make_tarball("acme-widget-def456", DEFAULT_FILES)
    capture = {}
    _patch_httpx_stream(monkeypatch, tarball, capture)

    fetch_mod.fetch_app_repo("acme/widget", "v1.2.3", slug="widget")

    assert capture["url"] == "https://api.github.com/repos/acme/widget/tarball/v1.2.3"
    assert capture["method"] == "GET"


def test_optional_token_sent_as_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    tarball = _make_tarball("acme-widget-abc123", DEFAULT_FILES)
    capture = {}
    _patch_httpx_stream(monkeypatch, tarball, capture)

    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget", token="s3cr3t")

    assert capture["headers"]["Authorization"] == "Bearer s3cr3t"


def test_no_token_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AW_APP_GIT_TOKEN", raising=False)
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    tarball = _make_tarball("acme-widget-abc123", DEFAULT_FILES)
    capture = {}
    _patch_httpx_stream(monkeypatch, tarball, capture)

    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert "Authorization" not in capture["headers"]


def test_refetch_is_idempotent_and_replaces_content(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    capture = {}
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-v1", DEFAULT_FILES), capture)
    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    v2_files = {**DEFAULT_FILES, "marker.txt": "v2"}
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-v2", v2_files), capture)
    dest = fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert (tmp_path / "apps" / "widget" / "marker.txt").read_text() == "v2"
    assert not (tmp_path / "apps" / "widget" / "acme-widget-v1").exists()

    assert fetch_mod.remove_app_repo("widget") is True
    assert not (tmp_path / "apps" / "widget").exists()
    assert fetch_mod.remove_app_repo("widget") is False
    assert dest == str(tmp_path / "apps" / "widget")


def test_download_failure_leaves_existing_dest_intact(tmp_path, monkeypatch):
    """The 2026-08-19 incident: agents-platform-runners' dest got rmtree'd before
    the replacement was ready, so a download failure emptied the app entirely.
    A prior install at ``dest`` must survive a download that never even lands.
    """
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)

    dest = tmp_path / "apps" / "widget"
    dest.mkdir(parents=True)
    (dest / "aw-app.json").write_text('{"manifest_version": 1, "id": "widget"}')
    (dest / "marker.txt").write_text("original")

    def fake_stream(method, url, *, headers=None, follow_redirects=None, timeout=None):
        raise fetch_mod.httpx.ConnectError("connection reset by tunnel edge")

    monkeypatch.setattr(fetch_mod.httpx, "stream", fake_stream)

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert dest.is_dir()
    assert (dest / "aw-app.json").exists()
    assert (dest / "marker.txt").read_text() == "original"
    assert not (tmp_path / "apps" / "widget.old").exists()


def test_swap_failure_restores_previous_install(tmp_path, monkeypatch):
    """A failure *between* the two renames (the exact window that used to have
    no rollback) must restore the prior tree, not leave ``dest`` missing.
    """
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    dest = tmp_path / "apps" / "widget"
    dest.mkdir(parents=True)
    (dest / "marker.txt").write_text("original")

    tarball = _make_tarball("acme-widget-v2", DEFAULT_FILES)
    capture = {}
    _patch_httpx_stream(monkeypatch, tarball, capture)

    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the extract_dir -> dest swap
            raise OSError("simulated disk failure mid-swap")
        return real_replace(src, dst)

    monkeypatch.setattr(fetch_mod.os, "replace", flaky_replace)

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert dest.is_dir()
    assert (dest / "marker.txt").read_text() == "original"
    assert not (tmp_path / "apps" / "widget.old").exists()


def test_download_failure_after_retries_leaves_existing_dest_intact(tmp_path, monkeypatch):
    """resilience:app-fetch-destroys-before-replacement — a download that never

    recovers must not touch the app already on disk. This is the exact shape
    of the 2026-08-19 incident: agents-platform-runners' repo vanished because
    the old code did ``rmtree(dest)`` before it had anything to replace it
    with, and every dispatch against it then 404'd for hours.
    """
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    capture = {}
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-v1", DEFAULT_FILES), capture)
    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    marker = tmp_path / "apps" / "widget" / "aw-app.json"
    original_content = marker.read_text()

    def _boom(method, url, *, headers=None, follow_redirects=None, timeout=None):
        raise httpx.ConnectError("tunnel cut the connection")

    monkeypatch.setattr(fetch_mod.httpx, "stream", _boom)
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)  # no real backoff delay in tests

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert (tmp_path / "apps" / "widget").is_dir()
    assert marker.read_text() == original_content
    assert not (tmp_path / "apps" / "widget.new").exists()
    assert not (tmp_path / "apps" / "widget.old").exists()


def test_download_retries_transient_transport_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda *_: None)
    tarball = _make_tarball("acme-widget-abc123", DEFAULT_FILES)
    calls = {"n": 0}

    @contextmanager
    def flaky_then_ok(method, url, *, headers=None, follow_redirects=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("tunnel edge cut it")
        yield _FakeResponse(tarball)

    monkeypatch.setattr(fetch_mod.httpx, "stream", flaky_then_ok)

    dest = fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert calls["n"] == 3
    assert (tmp_path / "apps" / "widget" / "aw-app.json").exists()
    assert dest == str(tmp_path / "apps" / "widget")


def test_download_http_status_error_fails_fast_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    calls = {"n": 0}

    @contextmanager
    def not_found(method, url, *, headers=None, follow_redirects=None, timeout=None):
        calls["n"] += 1
        resp = _FakeResponse(b"")

        def _raise():
            raise httpx.HTTPStatusError("404", request=None, response=None)
        resp.raise_for_status = _raise
        yield resp

    monkeypatch.setattr(fetch_mod.httpx, "stream", not_found)

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert calls["n"] == 1  # a real 404 doesn't get retried


def test_swap_failure_restores_prior_install(tmp_path, monkeypatch):
    """The failure window the old code had between ``rmtree(dest)`` and

    ``move(extract_dir, dest)``: if the final rename into place blows up,
    whatever was at ``dest`` before must still be there afterwards.
    """
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    capture = {}
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-v1", DEFAULT_FILES), capture)
    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    marker = tmp_path / "apps" / "widget" / "aw-app.json"
    original_content = marker.read_text()

    v2_files = {**DEFAULT_FILES, "marker.txt": "v2"}
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-v2", v2_files), capture)

    dest = str(tmp_path / "apps" / "widget")
    dest_old = dest + ".old"
    real_replace = fetch_mod.os.replace

    def flaky_replace(src, dst):
        if dst == dest and src != dest_old:
            raise OSError("simulated failure mid-swap")
        return real_replace(src, dst)

    monkeypatch.setattr(fetch_mod.os, "replace", flaky_replace)

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert (tmp_path / "apps" / "widget").is_dir()
    assert marker.read_text() == original_content
    assert not (tmp_path / "apps" / "widget.old").exists()
    assert not (tmp_path / "apps" / "widget" / "marker.txt").exists()


@pytest.mark.parametrize("malicious_name", [
    "root/../../etc/passwd",
    "root/../outside.txt",
])
def test_path_traversal_is_rejected(tmp_path, monkeypatch, malicious_name):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        root_info = tarfile.TarInfo(name="root")
        root_info.type = tarfile.DIRTYPE
        tf.addfile(root_info)
        data = b"pwned"
        info = tarfile.TarInfo(name=malicious_name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    capture = {}
    _patch_httpx_stream(monkeypatch, buf.getvalue(), capture)

    with pytest.raises(fetch_mod.FetchError):
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert not (tmp_path / "apps" / "widget").exists()
    assert not (tmp_path.parent / "etc" / "passwd").exists()


# --- stale staging-dir sweep + transient EACCES during extraction (card 4245bf3b) ---


def test_sweep_removes_only_old_fetch_dirs(tmp_path):
    apps = tmp_path / "apps"
    old = apps / ".widget.fetch-dead0001" / "extracted" / "sub"
    old.mkdir(parents=True)
    (old / "f.txt").write_text("x")
    os.chmod(old, 0o500)  # read-only leftover must still be removable
    fresh = apps / ".widget.fetch-live0002"
    fresh.mkdir()
    keep = apps / "widget"
    keep.mkdir()
    for d in (apps / ".widget.fetch-dead0001",):
        os.utime(d, (1, 1))

    removed = fetch_mod.sweep_stale_fetch_dirs(str(apps))

    assert removed == [str(apps / ".widget.fetch-dead0001")]
    assert not (apps / ".widget.fetch-dead0001").exists()
    assert fresh.exists() and keep.exists()


def test_fetch_sweeps_stale_dirs_before_extracting(tmp_path, monkeypatch):
    apps = tmp_path / "apps"
    stale = apps / ".widget.fetch-dead0001"
    stale.mkdir(parents=True)
    os.utime(stale, (1, 1))
    monkeypatch.setenv("AW_APPS_ROOT", str(apps))
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-abc", DEFAULT_FILES), {})

    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert not stale.exists()
    assert (apps / "widget" / "aw-app.json").exists()


def test_transient_eacces_during_extract_is_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-abc", DEFAULT_FILES), {})
    real, calls = fetch_mod._safe_extract, {"n": 0}

    def flaky(tf, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            os.makedirs(dest, exist_ok=True)
            os.chmod(dest, 0o000)  # the half-made, non-searchable dir
            raise PermissionError(13, "Permission denied", os.path.join(dest, "x", "aw-app.json"))
        return real(tf, dest)

    monkeypatch.setattr(fetch_mod, "_safe_extract", flaky)
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: None)

    dest = fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert calls["n"] == 2
    assert (tmp_path / "apps" / "widget" / "aw-app.json").exists()
    assert not [n for n in os.listdir(tmp_path / "apps") if ".fetch-" in n]


def test_persistent_eacces_is_a_fetch_error_with_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-abc", DEFAULT_FILES), {})

    def always(tf, dest):
        raise PermissionError(13, "Permission denied", os.path.join(dest, "a.json"))

    monkeypatch.setattr(fetch_mod, "_safe_extract", always)
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: None)

    with pytest.raises(fetch_mod.FetchError, match=r"uid=\d+ umask=") as ei:
        fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")
    assert "Permission denied" in str(ei.value)
    assert not (tmp_path / "apps" / "widget").exists()


def test_sweep_and_cleanup_never_chmod_through_symlinks(tmp_path):
    apps = tmp_path / "apps"
    installed = apps / "widget"
    installed.mkdir(parents=True)
    os.chmod(installed, 0o755)
    link_root = apps / ".widget.fetch-link0001"
    link_root.symlink_to(installed)
    os.utime(link_root, (1, 1), follow_symlinks=False)
    stale = apps / ".widget.fetch-dead0002"
    (stale / "inner").mkdir(parents=True)
    (stale / "inner" / "to-installed").symlink_to(installed)
    os.utime(stale, (1, 1))

    fetch_mod.sweep_stale_fetch_dirs(str(apps))

    assert (installed.stat().st_mode & 0o777) == 0o755
    assert not stale.exists()


def test_download_heartbeats_staging_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-abc", DEFAULT_FILES), {})
    touched = []
    real = os.utime
    monkeypatch.setattr(fetch_mod.os, "utime", lambda p, *a, **k: (touched.append(str(p)), real(p, *a, **k))[1])

    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")

    assert any(".widget.fetch-" in t for t in touched)


def test_cleanup_failure_does_not_mask_retry_or_final_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "apps"))
    _patch_httpx_stream(monkeypatch, _make_tarball("acme-widget-abc", DEFAULT_FILES), {})
    calls = {"n": 0}
    real = fetch_mod._safe_extract

    def flaky(tf, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "Permission denied", os.path.join(dest, "a"))
        return real(tf, dest)

    def bad_cleanup(path):
        raise OSError("cannot remove")

    monkeypatch.setattr(fetch_mod, "_safe_extract", flaky)
    monkeypatch.setattr(fetch_mod, "_force_rmtree", bad_cleanup)
    monkeypatch.setattr(fetch_mod.time, "sleep", lambda s: None)

    fetch_mod.fetch_app_repo("acme/widget", "main", slug="widget")
    assert calls["n"] == 2
    assert (tmp_path / "apps" / "widget" / "aw-app.json").exists()
