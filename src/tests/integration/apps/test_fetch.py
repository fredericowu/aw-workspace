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
import subprocess
import tarfile
from contextlib import contextmanager

import pytest

from src.apps import fetch as fetch_mod


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
