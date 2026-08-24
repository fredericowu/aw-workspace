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
import json
import os
import shutil
import textwrap
import time

import pytest
from docker.errors import ImageNotFound, NotFound
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from src.api.identity import require_identity
from src.apps import paths
from src.apps.containers import ContainerError, ContainerSupervisor
from src.apps.journal import ActionJournal
from src.apps.routes import register_apps_routes
from src.apps.runtime import AppRuntime


@pytest.fixture(autouse=True)
def _no_ambient_container_env(monkeypatch):
    """These tests drive a FAKE docker client, so every condition they exercise
    has to come from the test — not from the machine running it.

    ContainerSupervisor falls back to AW_CONTAINER_NETWORK / _PROXY_HOST when
    the caller passes none, so on a workspace that actually runs app containers
    on a podman network, five of these failed asserting the no-network URL
    (`http://127.0.0.1:9222` vs `http://aw-app-browser:9222`) — the very case
    their own comment says they are testing. The failure was invisible outside
    the workspace container, which is the only place that env is set.
    """
    monkeypatch.delenv("AW_CONTAINER_NETWORK", raising=False)
    monkeypatch.delenv("AW_CONTAINER_PROXY_HOST", raising=False)
    # Same reason, one layer down: the volume tests set AW_WORKSPACE_CONTAINER_DIR
    # to a tmp tree, and _container_host_bind_path translates that to the HOST
    # side via AW_WORKSPACE_HOST_DIR. With the real one set, the assertion got
    # /home/aw-remote-host/aw-workspace/repos instead of the tmp path it built.
    # A test that owns the container root has to own the host root as well.
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    # And the socket, for the same reason: with a real one in the environment,
    # _container_host_bind_path REFUSES to fall back to the container path
    # ("bind mounts must be translated") — correct in production, but these
    # tests hand the supervisor socket="/dev/null" explicitly, so the ambient
    # one is not theirs to inherit.
    monkeypatch.delenv("AW_CONTAINER_SOCKET", raising=False)


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
    def __init__(self, present: bool = True, pull_delay: float = 0.0) -> None:
        self.present = present
        self.pulled: list[str] = []
        self.pulled_auth: list[dict | None] = []
        # A real blocking sleep (not asyncio.sleep) — simulates docker-py's
        # actual synchronous network I/O for the event-loop-blocking regression
        # test below. 0 by default so every other test is unaffected.
        self.pull_delay = pull_delay

    def get(self, image: str):
        if not self.present:
            raise ImageNotFound(image)
        return object()

    # `auth_config` mirrors docker-py's real signature — ContainerSupervisor
    # passes the publishing marketplace's credential there (266d930). Recorded
    # rather than ignored so a test can assert the pull was authenticated.
    def pull(self, image: str, auth_config: dict | None = None) -> None:
        if self.pull_delay:
            time.sleep(self.pull_delay)
        self.pulled.append(image)
        self.pulled_auth.append(auth_config)
        self.present = True


class _FakeDocker:
    def __init__(self, image_present: bool = True, pull_delay: float = 0.0) -> None:
        self.store: dict = {}
        self.run_calls: list = []
        self.containers = _FakeContainers(self.store, self.run_calls)
        self.images = _FakeImages(image_present, pull_delay=pull_delay)


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


def test_start_sets_unless_stopped_restart_policy():
    # Root cause of an app container not "coming back up on its own" after a
    # crash or the container engine restarting independently of the
    # aw-workspace process: without this, only the NEXT aw-workspace boot
    # (reconcile_on_boot -> start()) would ever revive it.
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("browser", "img:1", 9222)
    sup.start("browser")

    call = fake.run_calls[-1]
    assert call["restart_policy"] == {"Name": "unless-stopped"}


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


def test_network_can_publish_udp_and_rtp_range():
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", network="aw-remote-host", client=fake)
    sup.register("phone", "asterisk:test", 9412, publish=[
        {"container": 5060, "host": 5060, "protocol": "udp"},
        {"container": "10000-10002", "host": "10000-10002", "protocol": "udp"},
    ])
    sup.start("phone")
    assert fake.run_calls[-1]["ports"] == {
        "5060/udp": 5060,
        "10000/udp": 10000,
        "10001/udp": 10001,
        "10002/udp": 10002,
    }


def test_network_injects_workspace_host_env():
    """On the shared network, the container must be told how to call BACK into
    the workspace process (e.g. aw-app-browser's Chrome reaching aw-app-proxy) —
    127.0.0.1 inside the app container is its own loopback, never the
    workspace's. Without a network, there's no shared reachability story, so
    nothing is injected."""
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", network="aw-remote-host", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")
    assert fake.run_calls[-1]["environment"]["AW_WORKSPACE_HOST"]

    fake2 = _FakeDocker()
    sup2 = ContainerSupervisor(socket="/dev/null", client=fake2)
    sup2.register("app", "img", 8080)
    sup2.start("app")
    assert "AW_WORKSPACE_HOST" not in fake2.run_calls[-1]["environment"]


def test_network_injects_self_host_env():
    """The reverse direction of AW_WORKSPACE_HOST: an app that needs to
    publish its OWN reachable address (e.g. aw-mcp-gateway writing its entry
    into the host .mcp.json) can't use 127.0.0.1 either — same reasoning,
    mirrored. Network-gated like AW_WORKSPACE_HOST, since there's no shared
    reachability story without one."""
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", network="aw-remote-host", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")
    assert fake.run_calls[-1]["environment"]["AW_APP_SELF_HOST"] == "aw-app-app"

    fake2 = _FakeDocker()
    sup2 = ContainerSupervisor(socket="/dev/null", client=fake2)
    sup2.register("app", "img", 8080)
    sup2.start("app")
    assert "AW_APP_SELF_HOST" not in fake2.run_calls[-1]["environment"]


def test_injects_workspace_slug_env_when_set(monkeypatch):
    """AW_WORKSPACE_SLUG lets a container app namespace something by this
    workspace's identity (e.g. aw-mcp-gateway prefixing published tool
    names) — unlike AW_WORKSPACE_HOST this isn't network-gated, since it's
    identity metadata, not a reachability detail."""
    monkeypatch.setenv("AW_WORKSPACE", "fredericowu")
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)  # no network
    sup.register("app", "img", 8080)
    sup.start("app")
    assert fake.run_calls[-1]["environment"]["AW_WORKSPACE_SLUG"] == "fredericowu"


def test_no_workspace_slug_env_when_unset(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")
    assert "AW_WORKSPACE_SLUG" not in fake.run_calls[-1]["environment"]


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

def _write_container_app(tmp_path, *, perms=None, runtime_extra=None, skills=None):
    slug = "browser"
    pkg = tmp_path / slug
    pkg.mkdir()
    perms = perms if perms is not None else ["containers:manage"]
    import json
    runtime = {"image": "ghcr.io/browserless/chromium", "port": 9222,
               "run_flags_needed": ["--shm-size=1g"],
               "resources": {"cpus": 0.5, "mem_mb": 512}}
    if runtime_extra:
        runtime.update(runtime_extra)
    manifest = {
        "manifest_version": 1,
        "id": slug,
        "name": "Browser",
        "version": "1.0.0",
        "tier": "container",
        "runtime": runtime,
        "permissions": perms,
    }
    if skills is not None:
        manifest["contributes"] = {"skills": skills}
    (pkg / "aw-app.json").write_text(json.dumps(manifest))
    if skills:
        for entry in skills:
            skill_md = pkg / entry["path"]
            skill_md.parent.mkdir(parents=True, exist_ok=True)
            skill_md.write_text("# how to use this app\n")
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


def test_load_container_app_does_not_block_the_event_loop(tmp_path):
    """Regression (reported live 2026-08-05, Frederico): installing/updating
    a Tier-2 (container) app used to call the synchronous docker-py client
    (image pull + container run) directly from async code — since asyncio is
    single-threaded, that blocking call froze the ENTIRE workspace (every
    other request/WS/terminal) for as long as the pull took, not just the
    one app's own install. Fixed by offloading to a thread
    (asyncio.to_thread); this proves a concurrent task can make progress
    DURING a slow "pull" instead of waiting for it to finish first.
    """
    pkg = _write_container_app(tmp_path)
    PULL_DELAY = 0.3
    TICK_TOTAL = 0.2  # 4 ticks * 50ms

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(
            socket="/dev/null", client=_FakeDocker(image_present=False, pull_delay=PULL_DELAY))

        async def other_work():
            for _ in range(4):
                await asyncio.sleep(0.05)

        start = time.monotonic()
        load_task = asyncio.create_task(rt.load(pkg, granted_permissions=["containers:manage"], signed=True))
        other_task = asyncio.create_task(other_work())
        await asyncio.gather(load_task, other_task)
        elapsed = time.monotonic() - start

        assert rt.is_loaded("browser")
        # Concurrent (fixed): wall time ~= max(PULL_DELAY, TICK_TOTAL) = 0.3s —
        # other_work overlaps the "pull" instead of waiting for it to finish
        # first. Blocking (the bug): wall time ~= PULL_DELAY + TICK_TOTAL =
        # 0.5s, since the sync pull call hogs the only event loop thread and
        # other_work's asyncio.sleep ticks can't even get scheduled until it
        # returns. The midpoint (0.4s) cleanly separates the two.
        assert elapsed < 0.4, (
            f"load() took {elapsed:.3f}s alongside other_work — event loop was "
            f"blocked (expected ~{max(PULL_DELAY, TICK_TOTAL)}s if truly concurrent, "
            f"~{PULL_DELAY + TICK_TOTAL}s if serialized)"
        )

    _async(run())


def test_runtime_registers_skills_for_a_container_tier_app(tmp_path, monkeypatch):
    """Tier-2 (container) apps declare ``contributes.skills`` same as Tier-1 —
    the early ``return`` in ``_load_container`` used to skip the
    ``_register_skills`` call entirely, so a container app's skill never
    reached the shared skills index Claude Code discovers from."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "root"))
    pkg = _write_container_app(tmp_path, skills=[
        {"id": "how-to", "path": "skills/how-to/SKILL.md"},
    ])

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

        index = rt.skills_index()
        assert index == [{
            "app": "browser", "id": "how-to", "description": "",
            "skill_md_path": os.path.join(paths.skills_dir(), "how-to", "SKILL.md"),
            "registered": True,
        }]

        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("browser")]
        assert ("skill:register", "how-to") in kinds

        await rt.unload("browser")
        assert not os.path.isdir(os.path.join(paths.skills_dir(), "how-to"))

    _async(run())


def test_runtime_mounts_package_relative_container_volumes(tmp_path):
    pkg = _write_container_app(
        tmp_path,
        runtime_extra={
            "volumes": [
                {"source": "back/config", "target": "/app/config", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

        host_config = str((tmp_path / "browser" / "back" / "config").resolve())
        assert fake.run_calls[-1]["volumes"] == {
            host_config: {"bind": "/app/config", "mode": "rw"}
        }

    _async(run())


def test_runtime_translates_container_volume_sources_to_host_bind_dir(tmp_path, monkeypatch):
    container_root = tmp_path / "container" / "aw-workspace"
    host_root = tmp_path / "host" / "aw-workspace"
    (container_root / "apps").mkdir(parents=True)
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.setenv("AW_WORKSPACE_HOST_DIR", str(host_root))
    pkg = _write_container_app(
        container_root / "apps",
        runtime_extra={
            "volumes": [
                {"source": "back/config", "target": "/app/config", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

        assert fake.run_calls[-1]["volumes"] == {
            str((host_root / "apps" / "browser" / "back" / "config").resolve()): {
                "bind": "/app/config",
                "mode": "rw",
            }
        }

    _async(run())


def test_runtime_rejects_untranslated_container_workspace_volume_with_host_socket(
    tmp_path, monkeypatch
):
    container_root = tmp_path / "container" / "aw-workspace"
    (container_root / "apps").mkdir(parents=True)
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.setenv("AW_CONTAINER_SOCKET", "/run/user/501/podman/podman.sock")
    pkg = _write_container_app(
        container_root / "apps",
        runtime_extra={
            "volumes": [
                {"source": "back/config", "target": "/app/config", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())

        with pytest.raises(ContainerError, match="AW_WORKSPACE_HOST_DIR"):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_runtime_mounts_apps_root_read_only(tmp_path, monkeypatch):
    apps_root = tmp_path / "installed-apps"
    monkeypatch.setenv("AW_APPS_ROOT", str(apps_root))
    pkg = _write_container_app(
        tmp_path,
        runtime_extra={
            "env": {"AW_APP_SCAN_ROOTS": "/workspace/apps"},
            "volumes": [
                {"source": "$AW_APPS_ROOT", "target": "/workspace/apps", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

        assert fake.run_calls[-1]["environment"]["AW_APP_SCAN_ROOTS"] == "/workspace/apps"
        assert fake.run_calls[-1]["volumes"] == {
            str(apps_root.resolve()): {"bind": "/workspace/apps", "mode": "ro"}
        }

    _async(run())


def test_runtime_translates_apps_root_volume_to_host_bind_dir(tmp_path, monkeypatch):
    container_root = tmp_path / "container" / "aw-workspace"
    host_root = tmp_path / "host" / "aw-workspace"
    apps_root = container_root / "apps"
    apps_root.mkdir(parents=True)
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.setenv("AW_WORKSPACE_HOST_DIR", str(host_root))
    monkeypatch.setenv("AW_APPS_ROOT", str(apps_root))
    pkg = _write_container_app(
        apps_root,
        runtime_extra={
            "volumes": [
                {"source": "$AW_APPS_ROOT", "target": "/workspace/apps", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

        assert fake.run_calls[-1]["volumes"] == {
            str((host_root / "apps").resolve()): {"bind": "/workspace/apps", "mode": "ro"}
        }

    _async(run())


def test_runtime_rejects_untranslated_apps_root_volume_with_host_socket(
    tmp_path, monkeypatch
):
    container_root = tmp_path / "container" / "aw-workspace"
    apps_root = container_root / "apps"
    apps_root.mkdir(parents=True)
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.setenv("AW_CONTAINER_SOCKET", "/run/user/501/podman/podman.sock")
    monkeypatch.setenv("AW_APPS_ROOT", str(apps_root))
    pkg = _write_container_app(
        apps_root,
        runtime_extra={
            "volumes": [
                {"source": "$AW_APPS_ROOT", "target": "/workspace/apps", "mode": "ro"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())

        with pytest.raises(ContainerError, match="AW_WORKSPACE_HOST_DIR"):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_runtime_rejects_writable_apps_root_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_APPS_ROOT", str(tmp_path / "installed-apps"))
    pkg = _write_container_app(
        tmp_path,
        runtime_extra={
            "volumes": [
                {"source": "$AW_APPS_ROOT", "target": "/workspace/apps", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_runtime_mounts_mcp_json_read_write(tmp_path, monkeypatch):
    container_root = tmp_path / "aw-workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.delenv("AW_CONTAINER_SOCKET", raising=False)
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "mcp:register-gateway"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_MCP_JSON", "target": "/host-mcp.json", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "mcp:register-gateway"],
                      signed=True)

        mcp_json = container_root / ".mcp.json"
        assert mcp_json.is_file()  # created on the fly since it didn't exist
        assert json.loads(mcp_json.read_text()) == {"mcpServers": {}}
        assert fake.run_calls[-1]["volumes"] == {
            str(mcp_json.resolve()): {"bind": "/host-mcp.json", "mode": "rw"}
        }

    _async(run())


def test_runtime_mounts_existing_mcp_json_without_clobbering_it(tmp_path, monkeypatch):
    container_root = tmp_path / "aw-workspace"
    container_root.mkdir()
    (container_root / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {}}}))
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.delenv("AW_CONTAINER_SOCKET", raising=False)
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "mcp:register-gateway"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_MCP_JSON", "target": "/host-mcp.json", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        await rt.load(pkg, granted_permissions=["containers:manage", "mcp:register-gateway"],
                      signed=True)

        assert json.loads((container_root / ".mcp.json").read_text()) == {
            "mcpServers": {"other": {}}
        }

    _async(run())


def test_runtime_rejects_readonly_mcp_json_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.delenv("AW_CONTAINER_SOCKET", raising=False)
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "mcp:register-gateway"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_MCP_JSON", "target": "/host-mcp.json", "mode": "ro"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError, match="read-write"):
            await rt.load(pkg, granted_permissions=["containers:manage", "mcp:register-gateway"],
                          signed=True)

    _async(run())


def test_runtime_rejects_mcp_json_volume_without_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))
    monkeypatch.delenv("AW_WORKSPACE_HOST_DIR", raising=False)
    monkeypatch.delenv("AW_CONTAINER_SOCKET", raising=False)
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage"],  # missing mcp:register-gateway
        runtime_extra={
            "volumes": [
                {"source": "$AW_MCP_JSON", "target": "/host-mcp.json", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError, match="mcp:register-gateway"):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_runtime_mounts_app_data_read_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_APP_DATA", "target": "/app/persist", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                      signed=True)

        data_dir = tmp_path / "home" / "data" / "browser"
        assert data_dir.is_dir()  # created on the fly since it didn't exist
        assert fake.run_calls[-1]["volumes"] == {
            str(data_dir.resolve()): {"bind": "/app/persist", "mode": "rw"}
        }

    _async(run())


def test_runtime_mounts_kb_dir_at_top_level_not_namespaced_by_app_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_KB_DIR", "target": "/app/kb_output", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                      signed=True)

        # Unlike $AW_APP_DATA (home/data/<app_id>), this is a shared top-level
        # dir — no "browser" (the test app's id) segment in the path.
        kb_dir = tmp_path / "home" / "knowledge_base"
        assert kb_dir.is_dir()
        assert fake.run_calls[-1]["volumes"] == {
            str(kb_dir.resolve()): {"bind": "/app/kb_output", "mode": "rw"}
        }

    _async(run())


def test_runtime_mounts_workspace_repos_read_only(tmp_path, monkeypatch):
    container_root = tmp_path / "workspace"
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_REPOS", "target": "/workspace-repos", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
                      signed=True)

        repos_dir = container_root / "repos"
        assert repos_dir.is_dir()
        assert fake.run_calls[-1]["volumes"] == {
            str(repos_dir.resolve()): {"bind": "/workspace-repos", "mode": "ro"}
        }

    _async(run())


def test_runtime_rejects_writable_workspace_repos_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "workspace"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_REPOS", "target": "/workspace-repos", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(ContainerError, match="read-only"):
            await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                          signed=True)

    _async(run())


def test_runtime_app_data_survives_uninstall_and_reinstall(tmp_path, monkeypatch):
    """The whole point of $AW_APP_DATA: unlike a package-relative volume
    (removed wholesale by uninstall's shutil.rmtree of the package dir —
    see remove_app_repo in fetch.py), this directory lives OUTSIDE the
    package dir and must still be there — with whatever the app wrote to
    it — after an uninstall + reinstall cycle."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_APP_DATA", "target": "/app/persist", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                      signed=True)
        data_dir = tmp_path / "home" / "data" / "browser"
        (data_dir / "important.db").write_text("state that must survive")

        await rt.unload("browser")
        # Simulate uninstall's package-dir wipe (fetch.remove_app_repo) —
        # the reconciler always re-fetches a fresh package dir on install,
        # so this is what a real uninstall+reinstall does to `pkg`.
        shutil.rmtree(pkg)
        pkg2 = _write_container_app(
            tmp_path,
            perms=["containers:manage", "fs:workspace-data"],
            runtime_extra={
                "volumes": [
                    {"source": "$AW_APP_DATA", "target": "/app/persist", "mode": "rw"}
                ]
            },
        )
        await rt.load(pkg2, granted_permissions=["containers:manage", "fs:workspace-data"],
                      signed=True)

        assert (data_dir / "important.db").read_text() == "state that must survive"

    _async(run())


def test_runtime_rejects_readonly_app_data_volume(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_APP_DATA", "target": "/app/persist", "mode": "ro"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError, match="read-write"):
            await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                          signed=True)

    _async(run())


def test_runtime_rejects_app_data_volume_without_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage"],  # missing fs:workspace-data
        runtime_extra={
            "volumes": [
                {"source": "$AW_APP_DATA", "target": "/app/persist", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError, match="fs:workspace-data"):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_runtime_namespaces_app_data_by_app_id(tmp_path, monkeypatch):
    """Two different apps mounting $AW_APP_DATA must land in two different
    host directories — never share state just because both opted in."""
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        pkg_a = tmp_path / "app-a"
        pkg_a.mkdir()
        (pkg_a / "aw-app.json").write_text(json.dumps({
            "manifest_version": 1, "id": "app-a", "name": "A", "version": "1.0.0",
            "tier": "container",
            "runtime": {"image": "img", "port": 9000,
                        "volumes": [{"source": "$AW_APP_DATA", "target": "/data", "mode": "rw"}]},
            "permissions": ["containers:manage", "fs:workspace-data"],
        }))
        pkg_b = tmp_path / "app-b"
        pkg_b.mkdir()
        (pkg_b / "aw-app.json").write_text(json.dumps({
            "manifest_version": 1, "id": "app-b", "name": "B", "version": "1.0.0",
            "tier": "container",
            "runtime": {"image": "img", "port": 9001,
                        "volumes": [{"source": "$AW_APP_DATA", "target": "/data", "mode": "rw"}]},
            "permissions": ["containers:manage", "fs:workspace-data"],
        }))

        await rt.load(str(pkg_a), granted_permissions=["containers:manage", "fs:workspace-data"], signed=True)
        await rt.load(str(pkg_b), granted_permissions=["containers:manage", "fs:workspace-data"], signed=True)

        vol_a = [c["volumes"] for c in fake.run_calls if c["name"] == "aw-app-app-a"][0]
        vol_b = [c["volumes"] for c in fake.run_calls if c["name"] == "aw-app-app-b"][0]
        assert list(vol_a.keys()) != list(vol_b.keys())
        assert str(tmp_path / "home" / "data" / "app-a") in vol_a
        assert str(tmp_path / "home" / "data" / "app-b") in vol_b

    _async(run())


def test_runtime_rejects_container_volume_escape(tmp_path):
    pkg = _write_container_app(
        tmp_path,
        runtime_extra={
            "volumes": [
                {"source": "../outside", "target": "/app/config", "mode": "rw"}
            ]
        },
    )

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        with pytest.raises(ContainerError):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    _async(run())


def test_container_auto_start_false_registers_without_starting(tmp_path):
    pkg = _write_container_app(tmp_path)

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(
            pkg,
            granted_permissions=["containers:manage"],
            signed=True,
            config={"auto_start": False},
        )
        assert rt.is_loaded("browser")
        assert fake.run_calls == []

        mounts = [r for r in rt.host.router.routes
                  if isinstance(r, Mount) and r.path == "/api/apps/browser"]
        assert len(mounts) == 1

    _async(run())


def test_config_endpoint_applies_auto_start_toggle(tmp_path):
    pkg = _write_container_app(tmp_path)

    class _Local:
        def __init__(self):
            self.saved = None

        def update_config(self, app_id, config):
            self.saved = (app_id, config)

    async def run():
        app = FastAPI()
        rt = register_apps_routes(app)
        local = _Local()
        app.state.app_reconciler.local = local
        app.dependency_overrides[require_identity] = lambda: {"sub": "test"}

        fake = _FakeDocker()
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)
        assert fake.run_calls

        client = TestClient(app)
        r = client.post("/api/apps/browser/config", json={"config": {"auto_start": False}})
        assert r.status_code == 200
        assert r.json()["config"]["auto_start"] is False
        assert local.saved[0] == "browser"
        assert local.saved[1]["auto_start"] is False
        assert fake.store["aw-app-browser"].removed is True

    _async(run())


def test_container_app_loads_unsigned(tmp_path):
    # Signature/trust gating is disabled (Frederico decision 2026-08-01) —
    # see filter_grants' docstring. An unsigned app now keeps containers:manage
    # same as a signed one, so this loads instead of raising PermissionError.
    pkg = _write_container_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
        await rt.load(pkg, granted_permissions=["containers:manage"], signed=False)
        assert rt.is_loaded("browser")

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


def test_runtime_mounts_workspace_skills_read_only(tmp_path, monkeypatch):
    """load_skill in the kb app reads $KB_SKILLS_DIR off its own filesystem,
    but no placeholder ever mounted the workspace skills tree — so it failed
    for EVERY skill, including correctly installed and indexed ones, and the
    agents whose entire system prompt is "call load_skill and follow it" ran
    with no instructions while still reporting success."""
    container_root = tmp_path / "workspace"
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_SKILLS", "target": "/app/skills", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
                      signed=True)

        skills = container_root / "skills"
        assert skills.is_dir()
        assert fake.run_calls[-1]["volumes"] == {
            str(skills.resolve()): {"bind": "/app/skills", "mode": "ro"}
        }

    _async(run())


def test_runtime_mounts_workspace_root_read_only(tmp_path, monkeypatch):
    """The whole tree in one bind, for the one app whose window IS the user's
    workspace: code-server used to root its editor at repos/ only, so half of
    what the user actually works on (src/, skills/, apps/) was unreachable
    from the editor that was supposed to show it."""
    container_root = tmp_path / "workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_ROOT", "target": "/opt/aw-workspace", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
                      signed=True)

        assert fake.run_calls[-1]["volumes"] == {
            str(container_root.resolve()): {"bind": "/opt/aw-workspace", "mode": "ro"}
        }

    _async(run())


def test_runtime_rejects_writable_workspace_root_without_write_permission(tmp_path, monkeypatch):
    """Reading the tree and rewriting core's own source are not the same
    request, so fs:workspace-read alone must not buy the writable bind."""
    container_root = tmp_path / "workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_ROOT", "target": "/opt/aw-workspace", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(Exception):
            await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data", "fs:workspace-read"],
                          signed=True)

    _async(run())


def test_runtime_mounts_workspace_root_read_write_with_permission(tmp_path, monkeypatch):
    """code-server is meant to be an editor, and an editor that cannot save
    is half a tool — so the writable bind exists, behind its own grant."""
    container_root = tmp_path / "workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    perms = ["containers:manage", "fs:workspace-data", "fs:workspace-read",
             "fs:workspace-write"]
    pkg = _write_container_app(
        tmp_path,
        perms=perms,
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_ROOT", "target": "/opt/aw-workspace", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)

        await rt.load(pkg, granted_permissions=perms, signed=True)

        assert fake.run_calls[-1]["volumes"] == {
            str(container_root.resolve()): {"bind": "/opt/aw-workspace", "mode": "rw"}
        }

    _async(run())


def test_runtime_refuses_workspace_write_to_an_unsigned_app(tmp_path, monkeypatch):
    """High-risk means signed/marketplace only. Side-loading is exactly how
    an unreviewed manifest gets in, so the grant is filtered out there and
    the rw mount must fail rather than quietly downgrade to ro."""
    container_root = tmp_path / "workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    perms = ["containers:manage", "fs:workspace-data", "fs:workspace-read",
             "fs:workspace-write"]
    pkg = _write_container_app(
        tmp_path,
        perms=perms,
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_ROOT", "target": "/opt/aw-workspace", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(Exception):
            await rt.load(pkg, granted_permissions=perms, signed=False)

    _async(run())


def test_runtime_rejects_ungated_workspace_root_mount(tmp_path, monkeypatch):
    """Widest read in the vocabulary — secrets, the workspace .env and every
    app's data dir — so a manifest claiming no filesystem access must not get
    it by declaring the placeholder alone."""
    container_root = tmp_path / "workspace"
    container_root.mkdir()
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_ROOT", "target": "/opt/aw-workspace", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(Exception):
            await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                          signed=True)

    _async(run())


def test_runtime_rejects_writable_workspace_skills_mount(tmp_path, monkeypatch):
    """The skill corpus is owned by `agent sync`, not by a container."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path / "workspace"))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage", "fs:workspace-data"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_SKILLS", "target": "/app/skills", "mode": "rw"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(Exception):
            await rt.load(pkg, granted_permissions=["containers:manage", "fs:workspace-data"],
                          signed=True)

    _async(run())


def test_workspace_repos_volume_refused_without_the_capability(tmp_path, monkeypatch):
    """The whole point of the 2026-08-16 gate: mounting the user's entire
    checkout tree must be something a manifest has to ask for out loud."""
    container_root = tmp_path / "workspace"
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(container_root))
    pkg = _write_container_app(
        tmp_path,
        perms=["containers:manage"],
        runtime_extra={
            "volumes": [
                {"source": "$AW_WORKSPACE_REPOS", "target": "/workspace-repos", "mode": "ro"}
            ]
        },
    )

    async def run():
        fake = _FakeDocker()
        rt = AppRuntime(FastAPI(), journal=ActionJournal(), guard_identity=False)
        rt.containers = ContainerSupervisor(socket="/dev/null", client=fake)
        with pytest.raises(Exception, match="fs:workspace-read"):
            await rt.load(pkg, granted_permissions=["containers:manage"], signed=True)

    asyncio.run(run())


# ---- host power (elevated device access) -------------------------------------

def test_no_host_power_leaves_the_run_call_unelevated():
    """The path every app installed today takes. It must stay exactly what it
    was before host_power existed: no privilege, no devices, no added caps."""
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("app", "img", 8080)
    sup.start("app")

    call = fake.run_calls[-1]
    assert call["privileged"] is False
    assert "devices" not in call
    assert "cap_add" not in call


def test_granted_host_power_reaches_the_run_call():
    """A QEMU guest without /dev/kvm falls back to software emulation and is
    unusably slow — the whole point is that the device actually lands."""
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("windows", "dockurr/windows", 8006, host_power=("kvm", "tun"))
    sup.start("windows")

    call = fake.run_calls[-1]
    assert call["devices"] == ["/dev/kvm:/dev/kvm:rwm", "/dev/net/tun:/dev/net/tun:rwm"]
    assert call["cap_add"] == ["NET_ADMIN"]
    assert call["privileged"] is False


def test_privileged_host_power_sets_the_flag_and_nothing_else():
    fake = _FakeDocker()
    sup = ContainerSupervisor(socket="/dev/null", client=fake)
    sup.register("app", "img", 8080, host_power=("privileged",))
    sup.start("app")

    call = fake.run_calls[-1]
    assert call["privileged"] is True
    assert "devices" not in call


def test_privileged_run_flag_still_rejected_and_points_at_host_power():
    """run_flags carries none of host_power's checks — no capability match, no
    host opt-in — so honouring --privileged there would route around both."""
    sup = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
    with pytest.raises(ContainerError, match="runtime.host_power"):
        sup.register("app", "img", 8080, run_flags=["--privileged"])


def test_unknown_host_power_grant_fails_at_register():
    sup = ContainerSupervisor(socket="/dev/null", client=_FakeDocker())
    with pytest.raises(Exception, match="unknown host power grant"):
        sup.register("app", "img", 8080, host_power=("gpu",))
