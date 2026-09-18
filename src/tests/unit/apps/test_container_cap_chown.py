"""Every Tier-2 container gets CAP_CHOWN unconditionally — see the block
comment in ContainerSupervisor.start() for the incident this closes:
aw-app-browser crash-looping because its bind-mounted chrome-profile dir
(freshly `os.makedirs`'d by aw-workspace's own uid) was owned wrong for
the image's `seluser`, and the image's own `sudo chown` fix-up could not
run — sudo's startup self-check refused even though the binary's on-disk
mode/owner were genuinely correct, a nested-user-namespace limitation, not
a permissions mistake fixable from inside that container.

Also covers the merge bug this fix would have been trivial to reintroduce:
a bare `kwargs.update(hostpower.docker_kwargs(...))` REPLACES `cap_add`
rather than extending it, which would silently drop the baseline CHOWN
grant for exactly the apps that also request a host_power capability.
"""
from __future__ import annotations

import pytest

from src.apps.containers import ContainerSupervisor


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

    def get(self, name: str):
        from docker.errors import NotFound
        raise NotFound("no such container")

    def run(self, image: str, **kwargs):
        self._client.runs.append(kwargs)
        return _FakeContainerObj()


class _FakeImages:
    def pull(self, image: str, **kwargs):
        return None

    def get(self, image: str):
        return object()


class _FakeClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout
        self.containers = _FakeContainers(self)
        self.images = _FakeImages()
        self.runs: list[dict] = []


@pytest.fixture
def sup(monkeypatch):
    monkeypatch.delenv("AW_CONTAINER_CLIENT_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_CREATE_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_NETWORK", raising=False)
    monkeypatch.delenv("AW_CONTAINER_PROXY_HOST", raising=False)
    s = ContainerSupervisor(socket="/dev/null")
    shared = _FakeClient(timeout=120)
    monkeypatch.setattr(s, "_new_client", lambda timeout: shared)
    return s, shared


def test_plain_app_gets_cap_chown_with_no_other_capabilities(sup):
    s, client = sup
    s.register("kb", image="ghcr.io/example/kb:latest", port=9000)

    s.start("kb")

    assert client.runs[0]["cap_add"] == ["CHOWN"]
    assert client.runs[0]["privileged"] is False


def test_app_with_a_host_power_grant_keeps_both_capabilities(sup, monkeypatch):
    """The merge, not the overwrite: an app whose host_power resolves to its
    own cap_add (not privileged, not device-only) must end up with CHOWN
    AND whatever hostpower granted — never just the last one to run."""
    s, client = sup
    from src.apps import hostpower

    monkeypatch.setattr(
        hostpower, "docker_kwargs", lambda grants: {"cap_add": ["SYS_PTRACE"]})

    s.register("debugger", image="ghcr.io/example/debugger:latest", port=9001,
               host_power=["gpu"])
    s.start("debugger")

    caps = client.runs[0]["cap_add"]
    assert set(caps) == {"CHOWN", "SYS_PTRACE"}


def test_app_with_a_host_power_grant_that_repeats_chown_does_not_duplicate_it(sup, monkeypatch):
    s, client = sup
    from src.apps import hostpower

    monkeypatch.setattr(
        hostpower, "docker_kwargs", lambda grants: {"cap_add": ["CHOWN", "SYS_ADMIN"]})

    s.register("weird", image="ghcr.io/example/weird:latest", port=9002,
               host_power=["gpu"])
    s.start("weird")

    caps = client.runs[0]["cap_add"]
    assert caps.count("CHOWN") == 1
    assert set(caps) == {"CHOWN", "SYS_ADMIN"}


def test_privileged_grant_still_short_circuits_correctly(sup, monkeypatch):
    """hostpower.docker_kwargs returns bare {"privileged": True} (no cap_add
    key at all) for the privileged grant — the merge logic must not choke on
    that shape, and privileged must still win over the baseline False."""
    s, client = sup
    from src.apps import hostpower

    monkeypatch.setattr(hostpower, "docker_kwargs", lambda grants: {"privileged": True})

    s.register("trusted", image="ghcr.io/example/trusted:latest", port=9003,
               host_power=["privileged"])
    s.start("trusted")

    assert client.runs[0]["privileged"] is True
