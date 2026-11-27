"""An app container runs ``repo:v<installed manifest version>`` when the manifest names ``latest``
(card 4245bf3b). Tag absent -> declared image + recorded fallback; registry down -> cache."""
from __future__ import annotations

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from src.apps.containers import ContainerSupervisor, versioned_image
from src.tests.unit.apps.test_container_arch_mismatch_pull import (
    _REGISTRY_UNREACHABLE, _FakeClient, _FakeImageObj, _wire)

DECLARED = "ghcr.io/tekflox/aw-app-kali-linux:latest"
VERSIONED = "ghcr.io/tekflox/aw-app-kali-linux:v0.11.0"


class _Client(_FakeClient):
    """pull errors keyed by image ref; ``cached`` is the set of refs present locally."""

    def __init__(self, errors=None, cached=()):
        super().__init__(arch="x86_64", cached_image=_FakeImageObj("amd64"))
        self.errors = errors or {}
        self.cached = set(cached)

        def pull(image, **kw):
            self.pull_calls.append({"image": image, **kw})
            if image in self.errors:
                raise self.errors[image]

        def get(image):
            if image in self.cached:
                return _FakeImageObj("amd64")
            raise ImageNotFound("no such image")

        self.images.pull = pull
        self.images.get = get


@pytest.fixture
def sup(monkeypatch):
    monkeypatch.delenv("AW_APP_VERSIONED_IMAGES", raising=False)
    monkeypatch.delenv("AW_CONTAINER_NETWORK", raising=False)
    return ContainerSupervisor(socket="/dev/null")


def _start(sup, client, monkeypatch, version="0.11.0", image=DECLARED):
    _wire(sup, client, monkeypatch)
    ran = []
    orig = client.containers.run
    client.containers.run = lambda img, **kw: (ran.append(img), orig(img, **kw))[1]
    sup.register("kali", image=image, port=9000, version=version)
    sup.start("kali")
    return ran


def test_versioned_image_only_rewrites_latest():
    assert versioned_image("a/b:latest", "0.11.0") == "a/b:v0.11.0"
    assert versioned_image("a/b", "v0.11.0") == "a/b:v0.11.0"
    assert versioned_image("localhost:5000/a/b", "1.0") == "localhost:5000/a/b:v1.0"
    assert versioned_image("a/b:v0.9.0", "0.11.0") is None
    assert versioned_image("a/b:latest", None) is None
    assert versioned_image("a/b@sha256:abc", "1") is None


def test_runs_the_installed_version_tag(sup, monkeypatch):
    client = _Client()
    assert _start(sup, client, monkeypatch) == [VERSIONED]
    assert sup._containers["kali"].image_fallback is None


def test_missing_tag_falls_back_to_declared_and_records_it(sup, monkeypatch):
    client = _Client(errors={VERSIONED: NotFound("manifest unknown")})
    assert _start(sup, client, monkeypatch) == [DECLARED]
    assert "not published" in sup._containers["kali"].image_fallback


def test_registry_unreachable_uses_cached_versioned_image(sup, monkeypatch):
    client = _Client(errors={VERSIONED: _REGISTRY_UNREACHABLE, DECLARED: _REGISTRY_UNREACHABLE},
                     cached={VERSIONED, DECLARED})
    assert _start(sup, client, monkeypatch) == [VERSIONED]
    assert sup._containers["kali"].image_fallback is None  # not a tag problem


def test_registry_unreachable_without_versioned_cache_keeps_todays_fallback(sup, monkeypatch):
    client = _Client(errors={VERSIONED: _REGISTRY_UNREACHABLE, DECLARED: _REGISTRY_UNREACHABLE},
                     cached={DECLARED})
    assert _start(sup, client, monkeypatch) == [DECLARED]
    assert sup._containers["kali"].image_fallback is None


def test_kill_switch_and_pinned_image_are_untouched(sup, monkeypatch):
    monkeypatch.setenv("AW_APP_VERSIONED_IMAGES", "0")
    assert _start(sup, _Client(), monkeypatch) == [DECLARED]


def test_pinned_declared_image_is_not_rewritten(sup, monkeypatch):
    pinned = "ghcr.io/tekflox/aw-app-kali-linux:v0.9.0"
    assert _start(sup, _Client(), monkeypatch, image=pinned) == [pinned]


def test_arch_mismatch_retries_versioned_ref_under_amd64(monkeypatch):
    monkeypatch.delenv("AW_APP_VERSIONED_IMAGES", raising=False)
    sup = ContainerSupervisor(socket="/dev/null")
    client = _Client()
    client.arch = "aarch64"
    client._arch = "aarch64"
    mismatch = APIError("no matching manifest for linux/arm64/v8 in the manifest list entries")
    calls = []

    def pull(image, **kw):
        calls.append((image, kw.get("platform")))
        if image == VERSIONED and not kw.get("platform"):
            raise mismatch

    client.images.pull = pull
    assert _start(sup, client, monkeypatch) == [VERSIONED]
    assert (VERSIONED, "linux/amd64") in calls


def test_image_not_found_after_a_pull_is_not_a_confirmed_missing_tag(sup, monkeypatch):
    """docker-py swallows a streamed outage then raises ImageNotFound from its local lookup."""
    client = _Client(errors={VERSIONED: ImageNotFound("no such image")}, cached={VERSIONED})
    assert _start(sup, client, monkeypatch) == [VERSIONED]
    assert sup._containers["kali"].image_fallback is None


def test_unrecognised_native_error_still_tries_amd64_for_the_versioned_ref(monkeypatch):
    sup = ContainerSupervisor(socket="/dev/null")
    client = _Client()
    client._arch = "aarch64"
    calls = []

    def pull(image, **kw):
        calls.append((image, kw.get("platform")))
        if image == VERSIONED and not kw.get("platform"):
            raise APIError("some new wording nobody has classified")

    client.images.pull = pull
    assert _start(sup, client, monkeypatch) == [VERSIONED]
    assert (VERSIONED, "linux/amd64") in calls
