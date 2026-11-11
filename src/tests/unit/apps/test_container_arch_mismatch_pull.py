"""``ContainerSupervisor.start()``'s arch-mismatch pull fallback (card
4155bf3b-9510-8127-9303-d3eeb4922daf): on an arm64 BYOD host, an app image
published amd64-only used to raise ``ImageNotFound`` straight through
``start()`` as a bare 500 with a Python traceback — the cache-fallback
branch only protects a registry-unreachable pull, and a manifest-list arch
mismatch isn't that.

Three states pinned here, matching the Architect's design:

* a manifest-list arch mismatch on a non-amd64 engine retries the pull with
  ``platform="linux/amd64"`` BEFORE the cache fallback, and succeeds;
* a genuine registry-unreachable error with a cached image falls back to
  the cache exactly as before, with NO platform retry attempted — the
  offline path must not pay an extra pull it doesn't need;
* when neither the native pull, the platform retry, nor the cache can
  satisfy the start, ``ContainerError`` names the image, the host
  architecture and the missing platform — never a bare ``ImageNotFound``.

Mutation condition: ``test_an_arch_mismatch_pull_retries_under_amd64_emulation``
fails on current master (raises ``ImageNotFound`` instead of succeeding).
"""
from __future__ import annotations

import pytest
from docker.errors import APIError, ImageNotFound

from src.apps.containers import ContainerSupervisor


class _FakeImageObj:
    def __init__(self, architecture: str) -> None:
        self.attrs = {"Architecture": architecture}


class _FakeContainerObj:
    id = "id-fake"
    status = "running"

    def reload(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        pass


class _FakeContainers:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client
        self._created: _FakeContainerObj | None = None

    def get(self, name: str):
        if self._created is not None:
            return self._created
        from docker.errors import NotFound
        raise NotFound("no such container")

    def run(self, image: str, **kwargs):
        self._client.runs.append(kwargs)
        self._created = _FakeContainerObj()
        return self._created


class _FakeImages:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client

    def pull(self, image: str, **kwargs):
        self._client.pull_calls.append({"image": image, **kwargs})
        if kwargs.get("platform") == "linux/amd64":
            if self._client.amd64_pull_error is not None:
                raise self._client.amd64_pull_error
            return None
        if self._client.native_pull_error is not None:
            raise self._client.native_pull_error
        return None

    def get(self, image: str):
        if self._client.cached_image is None:
            raise ImageNotFound(f"no such image: {image}")
        return self._client.cached_image


class _FakeClient:
    def __init__(self, timeout: int = 120, *, arch: str = "aarch64",
                 native_pull_error: Exception | None = None,
                 amd64_pull_error: Exception | None = None,
                 cached_image=None) -> None:
        self.timeout = timeout
        self.containers = _FakeContainers(self)
        self.images = _FakeImages(self)
        self.runs: list[dict] = []
        self.pull_calls: list[dict] = []
        self._arch = arch
        self.native_pull_error = native_pull_error
        self.amd64_pull_error = amd64_pull_error
        self.cached_image = cached_image

    def version(self):
        return {"Arch": self._arch}


_ARCH_MISMATCH = APIError(
    "no matching manifest for linux/arm64/v8 in the manifest list entries")
_REGISTRY_UNREACHABLE = APIError("Get \"https://ghcr.io/v2/\": dial tcp: lookup "
                                  "ghcr.io: no such host")


@pytest.fixture
def sup(monkeypatch):
    monkeypatch.delenv("AW_CONTAINER_CLIENT_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_PULL_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_CREATE_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_NETWORK", raising=False)
    monkeypatch.delenv("AW_CONTAINER_PROXY_HOST", raising=False)
    return ContainerSupervisor(socket="/dev/null")


def _wire(sup, client, monkeypatch):
    monkeypatch.setattr(sup, "_new_client", lambda timeout: client)


def test_an_arch_mismatch_pull_retries_under_amd64_emulation(sup, monkeypatch):
    """The happy path: current master has no coverage for this and raises
    ``ImageNotFound`` instead of starting — this fails on current master.

    ``cached_image`` stands in for the amd64 image the platform pull above
    just fetched, so ``status()``'s own arch comparison (image amd64 vs.
    engine arm64) can be pinned in the same assertion."""
    client = _FakeClient(arch="aarch64", native_pull_error=_ARCH_MISMATCH,
                          cached_image=_FakeImageObj("amd64"))
    _wire(sup, client, monkeypatch)
    sup.register("kb", image="ghcr.io/tekflox/aw-app-kb:latest", port=9000)

    result = sup.start("kb")

    platform_calls = [c for c in client.pull_calls if c.get("platform") == "linux/amd64"]
    assert len(platform_calls) == 1
    assert client.runs, "the container must actually have been created"
    assert result["emulated"] is True
    assert result["image_arch"] == "amd64"


def test_registry_unreachable_falls_back_to_cache_with_no_platform_retry(sup, monkeypatch):
    """Protects the offline path: a genuine registry-unreachable error with
    a cached image must fall back exactly as before — and must NOT pay for
    a platform retry it doesn't need."""
    cached = object()
    client = _FakeClient(arch="aarch64", native_pull_error=_REGISTRY_UNREACHABLE,
                          cached_image=cached)
    _wire(sup, client, monkeypatch)
    sup.register("kb", image="ghcr.io/tekflox/aw-app-kb:latest", port=9000)

    sup.start("kb")

    platform_calls = [c for c in client.pull_calls if c.get("platform") == "linux/amd64"]
    assert platform_calls == []
    assert client.runs, "the container must still have started from the cached image"


def test_unsatisfiable_arch_mismatch_raises_a_clear_container_error(sup, monkeypatch):
    """Neither pull works and nothing is cached: ContainerError, naming the
    image, the host arch, and the missing platform — never a bare
    ``ImageNotFound`` traceback."""
    client = _FakeClient(arch="aarch64", native_pull_error=_ARCH_MISMATCH,
                          amd64_pull_error=APIError("connection refused"))
    _wire(sup, client, monkeypatch)
    sup.register("kb", image="ghcr.io/tekflox/aw-app-kb:latest", port=9000)

    from src.apps.containers import ContainerError
    with pytest.raises(ContainerError) as excinfo:
        sup.start("kb")

    message = str(excinfo.value)
    assert "ghcr.io/tekflox/aw-app-kb:latest" in message
    assert "aarch64" in message or "arm64" in message
    assert "linux/amd64" in message
    assert not client.runs, "a start that could not pull must not create a container"


def test_structural_backstop_retries_platform_even_if_the_error_string_drifts(sup, monkeypatch):
    """The error-string match is a podman-version maintenance point — a
    cache miss on a non-amd64 engine must still get one platform pull
    attempt even when the native failure's text matches none of the known
    arch-mismatch markers (simulating a drifted/unrecognized error)."""
    unrecognized = APIError("some future podman error text nobody wrote a marker for")
    client = _FakeClient(arch="aarch64", native_pull_error=unrecognized)
    _wire(sup, client, monkeypatch)
    sup.register("kb", image="ghcr.io/tekflox/aw-app-kb:latest", port=9000)

    result = sup.start("kb")

    platform_calls = [c for c in client.pull_calls if c.get("platform") == "linux/amd64"]
    assert len(platform_calls) == 1
    assert client.runs
    assert result["running"] is True


def test_amd64_host_hitting_an_unresolvable_image_gets_no_fallback_attempt(sup, monkeypatch):
    """On an amd64 host the fallback target (linux/amd64) IS the native
    arch, so no retry is attempted at all — the error is still a clear
    ContainerError, not a bare traceback, but it carries no "amd64 pull"
    detail since nothing was retried."""
    client = _FakeClient(arch="x86_64", native_pull_error=_ARCH_MISMATCH)
    _wire(sup, client, monkeypatch)
    sup.register("kb", image="ghcr.io/example/arm-only:latest", port=9000)

    from src.apps.containers import ContainerError
    with pytest.raises(ContainerError) as excinfo:
        sup.start("kb")

    message = str(excinfo.value)
    assert "amd64" in message
    platform_calls = [c for c in client.pull_calls if c.get("platform") == "linux/amd64"]
    assert platform_calls == []
