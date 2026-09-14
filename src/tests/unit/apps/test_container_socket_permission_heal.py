"""Self-healing the podman socket's permissions before connecting.

THE BUG (fredericowu BYOD, host 824decc7e0610089, 2026-09-14). Since the
2026-08-01 switch to running this image as ``ubuntu`` (uid 1001) instead of
root, the BYOD podman socket — bind-mounted from the host VM at
``/run/podman.sock``, owned ``root:root`` (or whatever uid rootless podman
runs under on that VM), mode 660 — is routinely unreadable by our own
process. Connecting docker-py to it raised a bare
``PermissionError(13, 'Permission denied')`` wrapped in docker-py's eager
server-version-fetch, with nothing telling the installer UI *why*.

``ubuntu`` carries NOPASSWD ``sudo`` in this image for exactly this class of
problem (see the Dockerfile), so ``_ensure_socket_accessible`` now
best-effort ``chgrp``s the socket to our own primary group before building a
client, and raises a ``ContainerError`` naming the socket path plus its
mode/owner if that does not fix it.

The ownership reset is a live property of the bind mount — it happens again
every time ``podman.socket``/the VM restarts on the host side, not just once
at boot — so the heal must run lazily on every client USE, not as a
one-shot startup fixup. That is what the "skips when already accessible"
test below pins: a heal that ran once and cached "healed" would go stale the
next time the mount reset.

FOLLOW-UP BUG (QA, same day, live-reproduced on host 824decc7e0610089): the
first cut of this fix only called the heal from ``_new_client``, which
``_docker``/``_docker_for_pull``/``_docker_for_create`` each call at most
once per process — the client they build is then cached on
``self._client``/``self._pull_client``/``self._create_client`` forever, so
every call after the first CLIENT BUILD skipped the heal even though the
socket can (and does) go bad again long after that. The "reheals on every
call, not just the first client build" tests below pin that: build a client
while the socket is accessible, then break access, then call the accessor
again and confirm it re-checks (heals or raises) instead of silently
returning the stale cached client.
"""
from __future__ import annotations

import grp
import os
import subprocess

import pytest

from src.apps.containers import ContainerError, ContainerSupervisor


class _Stat:
    def __init__(self, mode=0o100660, uid=0, gid=0):
        self.st_mode = mode
        self.st_uid = uid
        self.st_gid = gid


def _sup():
    return ContainerSupervisor(socket="/run/podman.sock")


def test_an_already_accessible_socket_is_left_alone(monkeypatch):
    """No sudo call at all when the socket is already reachable — this must
    run on every client build, so it cannot afford to be expensive/noisy in
    the common case."""
    monkeypatch.setattr(os, "access", lambda *a, **kw: True)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a))

    _sup()._ensure_socket_accessible()

    assert calls == []


def test_an_inaccessible_socket_is_healed_via_sudo_chgrp(monkeypatch):
    """THE FIX: chgrp to OUR OWN group (derived from os.getgid(), not a
    hardcoded 'ubuntu') fixes a mode-660 socket owned by a different group,
    and the re-check after the heal succeeds."""
    results = iter([False, True])
    monkeypatch.setattr(os, "access", lambda *a, **kw: next(results))
    monkeypatch.setattr(os, "getgid", lambda: 1001)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: type("G", (), {"gr_name": "ubuntu"})())
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    _sup()._ensure_socket_accessible()  # must not raise

    assert calls == [["sudo", "-n", "chgrp", "ubuntu", "/run/podman.sock"]]


def test_a_socket_still_inaccessible_after_the_heal_raises_a_clear_error(monkeypatch):
    """THE REGRESSION THIS GUARDS AGAINST: without this, docker-py's own
    connect attempt would raise a bare PermissionError with no socket path,
    mode or owner in it — exactly what reached the installer UI as
    'PermissionError(13, Permission denied)' with no way to act on it."""
    monkeypatch.setattr(os, "access", lambda *a, **kw: False)
    monkeypatch.setattr(os, "getgid", lambda: 1001)
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: type("G", (), {"gr_name": "ubuntu"})())
    monkeypatch.setattr(subprocess, "run",
                         lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    real_stat = os.stat
    monkeypatch.setattr(
        os, "stat",
        lambda p, *a, **kw: _Stat(mode=0o100660, uid=0, gid=0) if p == "/run/podman.sock"
        else real_stat(p, *a, **kw))

    with pytest.raises(ContainerError) as exc_info:
        _sup()._ensure_socket_accessible()

    message = str(exc_info.value)
    assert "/run/podman.sock" in message
    assert "0o660" in message
    assert "uid=0" in message and "gid=0" in message


def test_a_sudo_call_that_itself_errors_still_falls_through_to_the_clear_error(monkeypatch):
    """Best-effort means best-effort: a broken/missing sudo must not crash the
    heal path itself, it must just leave the socket inaccessible so the
    normal clear-error path fires."""
    monkeypatch.setattr(os, "access", lambda *a, **kw: False)
    monkeypatch.setattr(os, "getgid", lambda: 1001)
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: type("G", (), {"gr_name": "ubuntu"})())

    def raising_run(cmd, **kw):
        raise FileNotFoundError("sudo: command not found")

    monkeypatch.setattr(subprocess, "run", raising_run)
    real_stat = os.stat
    monkeypatch.setattr(
        os, "stat",
        lambda p, *a, **kw: _Stat() if p == "/run/podman.sock" else real_stat(p, *a, **kw))

    with pytest.raises(ContainerError, match="not accessible"):
        _sup()._ensure_socket_accessible()


# ── the call sites: _docker / _docker_for_pull / _docker_for_create ───────
#
# Testing _ensure_socket_accessible alone leaves the bug reachable: nothing
# forces the three client accessors to actually call it. That is exactly how
# the QA-found gap slipped through the first cut of this fix — every unit
# test built a FRESH ContainerSupervisor per check, so none of them exercised
# the cached-client path a second time.

_ACCESSORS = ["_docker", "_docker_for_pull", "_docker_for_create"]


def _install_fake_docker(monkeypatch):
    calls = []

    class _FakeDockerClient:
        def __init__(self, base_url, timeout):
            calls.append((base_url, timeout))

    class _FakeDockerModule:
        DockerClient = _FakeDockerClient

    monkeypatch.setitem(__import__("sys").modules, "docker", _FakeDockerModule)
    return calls


@pytest.mark.parametrize("accessor_name", _ACCESSORS)
def test_accessor_heals_before_ever_touching_docker(monkeypatch, accessor_name):
    sup = _sup()
    order = []
    monkeypatch.setattr(sup, "_ensure_socket_accessible", lambda: order.append("heal"))
    client_calls = _install_fake_docker(monkeypatch)

    getattr(sup, accessor_name)()

    assert order == ["heal"]
    assert len(client_calls) == 1


@pytest.mark.parametrize("accessor_name", _ACCESSORS)
def test_accessor_propagates_the_heal_failure_instead_of_a_bare_permission_error(
        monkeypatch, accessor_name):
    sup = _sup()

    def boom():
        raise ContainerError("container engine socket /run/podman.sock is not accessible")

    monkeypatch.setattr(sup, "_ensure_socket_accessible", boom)

    with pytest.raises(ContainerError, match="not accessible"):
        getattr(sup, accessor_name)()


@pytest.mark.parametrize("accessor_name", _ACCESSORS)
def test_accessor_reheals_on_every_call_not_just_the_first_client_build(
        monkeypatch, accessor_name):
    """THE QA-FOUND GAP, live-reproduced on host 824decc7e0610089: a client
    cached after a successful first build must not let every later call
    skip the accessibility check. Build once while accessible, then call
    again — the heal must fire a SECOND time (not just once for the process
    lifetime), and since the heal still succeeds (chgrp fixes it again),
    the accessor reuses the same cached client rather than rebuilding it —
    a cached docker.DockerClient holds only config, not a live connection,
    so there is nothing to gain by discarding it."""
    sup = _sup()
    heal_calls = []
    monkeypatch.setattr(sup, "_ensure_socket_accessible", lambda: heal_calls.append(1))
    _install_fake_docker(monkeypatch)

    first_client = getattr(sup, accessor_name)()
    assert heal_calls == [1]

    second_client = getattr(sup, accessor_name)()
    assert heal_calls == [1, 1]
    assert second_client is first_client


@pytest.mark.parametrize("accessor_name", _ACCESSORS)
def test_accessor_raises_instead_of_silently_returning_a_stale_cached_client(
        monkeypatch, accessor_name):
    """THE EXACT SCENARIO QA LIVE-REPRODUCED on host 824decc7e0610089: a
    client already cached and working, then the socket flips back to
    root:root (a routine host-side podman.socket/VM restart). A subsequent
    call through the same accessor must raise ContainerError — not
    silently hand back the stale cached client, and not leak docker-py's
    bare PermissionError as it did before this fix (traced through
    containers.py's status() -> client.containers.get())."""
    sup = _sup()
    accessible = [True]

    def fake_heal():
        if not accessible[0]:
            raise ContainerError(
                "container engine socket /run/podman.sock is not accessible")

    monkeypatch.setattr(sup, "_ensure_socket_accessible", fake_heal)
    _install_fake_docker(monkeypatch)

    getattr(sup, accessor_name)()  # first call: caches a working client

    accessible[0] = False  # the host resets the socket's ownership again
    with pytest.raises(ContainerError, match="not accessible"):
        getattr(sup, accessor_name)()


def test_reheal_socket_calls_ensure_socket_accessible_when_a_real_socket_is_configured(
        monkeypatch):
    sup = _sup()
    calls = []
    monkeypatch.setattr(sup, "_ensure_socket_accessible", lambda: calls.append(1))

    sup._reheal_socket()

    assert calls == [1]


def test_reheal_socket_noops_with_an_injected_client(monkeypatch):
    """An injected (test/fixture) client stands in for a real connection —
    there is no live socket to re-check, and doing so anyway would just
    re-run the real chgrp/sudo dance against whatever AW_CONTAINER_SOCKET
    happens to be in this process's environment. The accessors must skip
    the reheal entirely once a client was injected."""
    sup = ContainerSupervisor(socket="/run/podman.sock", client=object())
    calls = []
    monkeypatch.setattr(sup, "_ensure_socket_accessible", lambda: calls.append(1))

    sup._reheal_socket()

    assert calls == []
