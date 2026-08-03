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
import textwrap

import pytest
from docker.errors import ImageNotFound, NotFound
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from src.api.identity import require_identity
from src.apps.containers import ContainerError, ContainerSupervisor
from src.apps.journal import ActionJournal
from src.apps.routes import register_apps_routes
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

def _write_container_app(tmp_path, *, perms=None, runtime_extra=None):
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
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": "Browser",
        "version": "1.0.0",
        "tier": "container",
        "runtime": runtime,
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
