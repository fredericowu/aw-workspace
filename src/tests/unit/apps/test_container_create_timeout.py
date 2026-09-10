"""Container *create* must not inherit the 10s status-read cap.

``_docker`` caps at 10s so an unresponsive podman socket can't freeze the
worker's event loop through a ``/ws/status`` snapshot (2026-09-04, see
``test_event_loop_not_blocked_by_container_status.py``). That cap is right for
a read and wrong for a create: under nested podman — the hosted-workspace
engine, podman inside podman — create routinely runs past 10s, so when the
reconciler converged two Tier-2 apps in one pass, whichever created SECOND hit
the cap and failed while the first succeeded. Measured deterministically on
2026-09-10 with ``kb`` + ``browser``: alternating success/failure, always the
second app in the pass.

These tests pin both halves — the create path gets its own, longer,
env-configurable timeout, AND the status path keeps the short one. Asserting
only the first would let a "fix" that widens the shared client pass while
quietly re-opening the event-loop freeze.

``_new_client`` is monkeypatched rather than a client being injected via the
constructor: ``client=`` sets ``_injected_client``, which makes all three
accessors hand back the same object on purpose (so the existing fake-client
tests keep working) — and that would erase the very distinction under test.
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
        return _FakeContainerObj()

    def run(self, image: str, **kwargs):
        # The call under test: record which client's timeout the create used.
        self._client.create_timeouts.append(self._client.timeout)
        return _FakeContainerObj()


class _FakeImages:
    def pull(self, image: str, **kwargs):
        return None

    def get(self, image: str):
        return object()


class _FakeClient:
    """One per ``_new_client`` call, tagged with the timeout it was built with."""

    #: shared across every client this test builds, so the assertion can see
    #: which timeout the create actually ran under regardless of which
    #: accessor produced the client.
    create_timeouts: list[int] = []

    def __init__(self, timeout: int) -> None:
        self.timeout = timeout
        self.containers = _FakeContainers(self)
        self.images = _FakeImages()


@pytest.fixture
def sup(monkeypatch):
    monkeypatch.delenv("AW_CONTAINER_CLIENT_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_CREATE_TIMEOUT", raising=False)
    monkeypatch.delenv("AW_CONTAINER_NETWORK", raising=False)
    monkeypatch.delenv("AW_CONTAINER_PROXY_HOST", raising=False)
    s = ContainerSupervisor(socket="/dev/null")
    monkeypatch.setattr(s, "_new_client", lambda timeout: _FakeClient(timeout))
    return s


def test_create_client_is_not_capped_at_the_status_timeout(sup):
    """The regression itself: create's cap must be well clear of a nested create."""
    assert sup._docker().timeout == 10, "status reads must keep the short cap"
    assert sup._docker_for_create().timeout == 120


def test_create_timeout_is_env_configurable(monkeypatch, sup):
    monkeypatch.setenv("AW_CONTAINER_CREATE_TIMEOUT", "45")
    assert sup._docker_for_create().timeout == 45
    # ...and doing so must not drag the status cap along with it.
    assert sup._docker().timeout == 10


def test_status_timeout_stays_independently_configurable(monkeypatch, sup):
    monkeypatch.setenv("AW_CONTAINER_CLIENT_TIMEOUT", "7")
    assert sup._docker().timeout == 7
    assert sup._docker_for_create().timeout == 120


def test_start_creates_the_container_on_the_create_client(sup):
    """Not just that the accessor exists — that ``start`` actually uses it.

    Without the fix this records 10 (``start`` held ``_docker()``), which is
    the value that made the second app in a reconcile pass fail.
    """
    _FakeClient.create_timeouts = []
    sup.register("kb", image="ghcr.io/example/kb:latest", port=9000)

    sup.start("kb")

    assert _FakeClient.create_timeouts == [120]
