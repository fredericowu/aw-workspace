"""Tier-2 container supervisor + runtime tier:container path (Phase 6).

The real container engine (host rootless podman socket) is not reachable from
the test env, so a fake Docker-API client is injected into the supervisor. This
exercises register/start/stop/status/base_url, image pull-if-missing, run-flag +
resource mapping, and the runtime's tier=container load/unload (proxy mount,
containers:manage enforcement, journaled revert). Live spawn is validated by the
orchestrator with the socket mounted.
"""
from __future__ import annotations

import asyncio
import textwrap

import pytest
from docker.errors import ImageNotFound, NotFound
from fastapi import FastAPI
from starlette.routing import Mount

from src.apps.containers import ContainerError, ContainerSupervisor
from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime


def _async(coro):
    return asyncio.run(coro)


# ---- fake Docker-API client -------------------------------------------------

class _FakeContainer:
    def __init__(self, name: str) -> None:
        self.id = f"id-{name}"
        self.name = name
        self.status = "running"
        self.removed = False

    def reload(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self, store: dict, run_calls: list) -> None:
        self.store = store
        self.run_calls = run_calls

    def get(self, name: str) -> _FakeContainer:
        c = self.store.get(name)
        if c is None or c.removed:
            raise NotFound(name)
        return c

    def run(self, image: str, name: str = "", **kwargs):
        self.run_calls.append({"image": image, "name": name, **kwargs})
        c = _FakeContainer(name)
        self.store[name] = c
        return c


class _FakeImages:
    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.pulled: list[str] = []

    def get(self, image: str):
        if not self.present:
            raise ImageNotFound(image)
        return object()

    def pull(self, image: str) -> None:
        self.pulled.append(image)
        self.present = True


class _FakeDocker:
    def __init__(self, image_present: bool = True) -> None:
        self.store: dict = {}
        self.run_calls: list = []
        self.containers = _FakeContainers(self.store, self.run_calls)
        self.images = _FakeImages(image_present)


# ---- supervisor -------------------------------------------------------------

def test_register_start_status_stop_roundtrip():
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    assert sup.available is True

    sup.register("browser", "img:1", 9222, run_flags=["--shm-size=1g"],
                 resources={"cpus": 0.5, "mem_mb": 512})
    sup.start("browser")

    st = sup.status("browser")
    assert st["running"] is True
    assert st["container"] == "aw-app-browser"
    assert st["url"] == "http://127.0.0.1:9222"  # no network → proxy host

    # run flags + resources mapped onto the docker run call, never privileged
    call = fake.run_calls[-1]
    assert call["name"] == "aw-app-browser"
    assert call["shm_size"] == "1g"
    assert call["mem_limit"] == "512m"
    assert call["nano_cpus"] == 500_000_000
    assert call["privileged"] is False

    sup.stop("browser")
    assert sup.status("browser")["running"] is False


def test_pulls_missing_image():
    fake = _FakeDocker(image_present=False)
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("app", "img:missing", 8080)
    sup.start("app")
    assert "img:missing" in fake.images.pulled


def test_network_gives_name_based_url():
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", network="aw-remote-host", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")
    assert sup.base_url("app") == "http://aw-app-app:8080"
    assert fake.run_calls[-1]["network"] == "aw-remote-host"
    # network path publishes no host port
    assert "ports" not in fake.run_calls[-1]


def test_privileged_flag_rejected():
    sup = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
    with pytest.raises(ContainerError):
        sup.register("app", "img", 8080, run_flags=["--privileged"])


def test_unavailable_without_socket():
    sup = ContainerSupervisor(socket="", network=None)
    assert sup.available is False


def test_stop_all_for_drops_registration():
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")
    sup.stop_all_for("app")
    assert fake.store["aw-app-app"].removed is True
    with pytest.raises(ContainerError):
        sup.status("app")  # no longer registered


# ---- runtime tier=container -------------------------------------------------

def _write_container_app(tmp_path, *, perms=None):
    slug = "browser"
    pkg = tmp_path / slug
    pkg.mkdir()
    perms = perms if perms is not None else ["containers:manage"]
    import json
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": "Browser",
        "version": "1.0.0",
        "tier": "container",
        "runtime": {"image": "ghcr.io/browserless/chromium", "port": 9222,
                    "run_flags_needed": ["--shm-size=1g"],
                    "resources": {"cpus": 0.5, "mem_mb": 512}},
        "permissions": perms,
    }))
    return str(pkg)


def test_runtime_loads_container_app_and_reverts_on_unload(tmp_path):
    pkg = _write_container_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)
        assert rt.is_loaded("browser")

        # proxy mounted under /api/apps/browser
        mounts = [r for r in rt.host.router.routes
                  if isinstance(r, Mount) and r.path == "/api/apps/browser"]
        assert len(mounts) == 1

        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("browser")]
        assert ("container:register", "ghcr.io/browserless/chromium") in kinds
        assert ("route:mount", "/api/apps/browser") in kinds

        # uninstall stops+removes the container and leaves no residue
        await rt.unload("browser")
        assert not rt.is_loaded("browser")
        assert rt.journal.entries_for("browser") == []
        assert not [r for r in rt.host.router.routes
                    if isinstance(r, Mount) and r.path == "/api/apps/browser"]

    _async(run())


def test_container_app_requires_containers_manage(tmp_path):
    # unsigned → filter_grants strips the high-risk cap → load refused
    pkg = _write_container_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(PermissionError):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=False)
        assert not rt.is_loaded("browser")

    _async(run())


def test_container_app_refused_when_engine_unavailable(tmp_path):
    pkg = _write_container_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="", network=None)  # no engine
        with pytest.raises(ContainerError):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)
        assert not rt.is_loaded("browser")

    _async(run())
