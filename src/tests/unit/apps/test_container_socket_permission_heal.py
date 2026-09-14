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
problem (see the Dockerfile), so ``_new_client`` now best-effort ``chgrp``s
the socket to our own primary group before building a client, and raises a
``ContainerError`` naming the socket path plus its mode/owner if that does
not fix it.

The ownership reset is a live property of the bind mount — it happens again
every time ``podman.socket``/the VM restarts on the host side, not just once
at boot — so the heal must run lazily on every client build, not as a
one-shot startup fixup. That is what the "skips when already accessible"
test below pins: a heal that ran once and cached "healed" would go stale the
next time the mount reset.
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


# ── the call site ────────────────────────────────────────────────────────
#
# Testing _ensure_socket_accessible alone leaves the bug reachable: _new_client
# could stop calling it and every test above would still pass while the raw
# docker-py PermissionError went straight back to the installer UI.

def test_new_client_heals_before_ever_touching_docker(monkeypatch):
    sup = _sup()
    order = []
    monkeypatch.setattr(sup, "_ensure_socket_accessible", lambda: order.append("heal"))

    class _FakeDockerClient:
        def __init__(self, base_url, timeout):
            order.append(("client", base_url, timeout))

    class _FakeDockerModule:
        DockerClient = _FakeDockerClient

    monkeypatch.setitem(__import__("sys").modules, "docker", _FakeDockerModule)

    sup._new_client(10)

    assert order == ["heal", ("client", "unix:///run/podman.sock", 10)]


def test_new_client_propagates_the_heal_failure_instead_of_a_bare_permission_error(monkeypatch):
    sup = _sup()

    def boom():
        raise ContainerError("container engine socket /run/podman.sock is not accessible")

    monkeypatch.setattr(sup, "_ensure_socket_accessible", boom)

    with pytest.raises(ContainerError, match="not accessible"):
        sup._new_client(10)
