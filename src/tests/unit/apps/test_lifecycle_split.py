"""W3: the provision/attach split in ``src/apps/lifecycle.py``.

The card's constraint 1 is the one that has to be mechanically true, not
merely intended: *"a non-leader worker must NOT run pip/podman/git as part of
converging. If your convergence path can reach ``_install_pip_requires``, the
split has failed."* So the tests here mostly assert about what does NOT
happen — every shared side effect is replaced with a recorder that raises or
counts, and the attach path is required to leave every counter at zero.

Single-process and Redis-free on purpose: this is the shape of the split.
Two real processes over one Redis + one Postgres is
``src/tests/integration/apps/test_multiworker_app_lifecycle.py``.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi import FastAPI

from src.apps.runtime import AppRuntime

PLUGIN = """
from fastapi import FastAPI

class AppPlugin:
    async def activate(self, ctx):
        sub = FastAPI()

        @sub.get("/ping")
        async def ping():
            return {"pong": "w3"}

        ctx.routes.register(sub)
        ctx.commands.install_system_cli("w3cli", "install.sh", uninstall="uninstall.sh")
        ctx.services.register("worker", "sleep 600", autostart=True)

    async def deactivate(self):
        pass
"""


def _pkg(tmp_path, slug="w3app", version="1.0.0"):
    pkg = tmp_path / slug
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": slug,
        "version": version,
        "tier": "inprocess",
        "runtime": {"entrypoint": "plugin:AppPlugin", "pip_requires": ["nonexistent-pkg"]},
        "permissions": ["routes:register", "commands:install", "service:manage"],
    }))
    (pkg / "plugin.py").write_text(PLUGIN)
    (pkg / "install.sh").write_text("#!/bin/sh\ntrue\n")
    (pkg / "uninstall.sh").write_text("#!/bin/sh\ntrue\n")
    return str(pkg)


class _Effects:
    """Every shared side effect a load can reach, replaced by a counter."""

    def __init__(self) -> None:
        self.pip = 0
        self.installer = 0
        self.revert = 0
        self.shim_written = 0
        self.service_started: list[tuple[str, str]] = []
        self.skills_registered = 0
        self.skills_unregistered = 0
        self.migrations = 0
        self.mcp_rendered = 0
        self.repos_cloned = 0

    def wire(self, runtime: AppRuntime, monkeypatch) -> None:
        monkeypatch.setattr(runtime, "_install_pip_requires",
                            lambda manifest: self._bump("pip"))
        monkeypatch.setattr(runtime, "_apply_migrations",
                            lambda manifest, pkg: self._bump("migrations"))
        monkeypatch.setattr(runtime, "_render_mcp_template",
                            lambda loaded: self._bump("mcp_rendered") or True)
        monkeypatch.setattr(runtime, "_register_skills",
                            lambda loaded: self._bump("skills_registered"))
        monkeypatch.setattr(runtime, "_register_tasks", lambda loaded: None)
        monkeypatch.setattr(runtime, "_register_agents", lambda loaded: None)

        async def _repos(manifest):
            self.repos_cloned += 1

        monkeypatch.setattr(runtime, "_register_repos", _repos)
        monkeypatch.setattr(runtime.commands, "run_installer",
                            lambda pkg, script: self._bump("installer") or "")
        monkeypatch.setattr(runtime.commands, "run_revert",
                            lambda pkg, script: self._bump("revert") or "")
        monkeypatch.setattr(runtime.commands, "install_shim",
                            lambda name, pkg, exec_path: (
                                self._bump("shim_written") or f"/tmp/bin/{name}"))
        monkeypatch.setattr(runtime.commands, "remove_shim", lambda p: True)

        real_start = runtime.services.start

        def _start(app_id, service_id):
            self.service_started.append((app_id, service_id))
            return {"service": service_id, "running": True}

        monkeypatch.setattr(runtime.services, "start", _start)
        assert real_start is not _start  # guard against a typo'd attr name

    def _bump(self, name: str):
        setattr(self, name, getattr(self, name) + 1)
        return None

    @property
    def shared_total(self) -> int:
        return (self.pip + self.installer + self.revert + self.shim_written
                + len(self.service_started) + self.skills_registered
                + self.skills_unregistered + self.migrations
                + self.mcp_rendered + self.repos_cloned)


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    rt = AppRuntime(FastAPI(), guard_identity=False)
    return rt


def test_provision_runs_every_shared_side_effect(runtime, tmp_path, monkeypatch):
    """The default path is unchanged — this is the GOLDEN RULE control case."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    asyncio.run(runtime.load(pkg))

    assert runtime.is_loaded("w3app")
    assert fx.pip == 1
    assert fx.installer == 1
    assert fx.shim_written == 0  # this app installs a system CLI, not a shim
    assert fx.service_started == [("w3app", "worker")]
    assert fx.skills_registered == 1
    assert fx.mcp_rendered == 1


def test_attach_mounts_the_routes_and_touches_nothing_shared(runtime, tmp_path, monkeypatch):
    """The whole point of the card: provision=False must still MOUNT."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    asyncio.run(runtime.load(pkg, provision=False))

    # (a) it serves — the mount is on this process's router
    assert runtime.is_loaded("w3app")
    loaded = runtime.get("w3app")
    assert loaded.mount is not None and loaded.mount in runtime.host.router.routes
    assert loaded.host_mount is not None and loaded.host_mount in runtime.host.router.routes

    # (b) and it did NOTHING shared: no pip, no installer script, no shim, no
    #     spawned service, no skills materialization, no migration, no clone.
    assert fx.shared_total == 0, (
        f"attach reached a shared side effect: {vars(fx)}")


def test_detach_unmounts_without_reverting_anything_shared(runtime, tmp_path, monkeypatch):
    """The uninstall-side twin. A detaching worker must not run the app's
    uninstall.sh — that would `apt remove` for the whole container, N times."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    asyncio.run(runtime.load(pkg, provision=False))
    loaded = runtime.get("w3app")
    mount, host_mount = loaded.mount, loaded.host_mount

    asyncio.run(runtime.unload("w3app", provision=False))

    assert not runtime.is_loaded("w3app")
    assert mount not in runtime.host.router.routes
    assert host_mount not in runtime.host.router.routes
    assert fx.revert == 0, "a detaching worker ran the app's uninstall script"
    assert fx.skills_unregistered == 0
    assert fx.shared_total == 0
    # per-process supervisor state IS dropped, so a later re-attach is clean
    assert runtime.services.registered() == []


def test_provisioning_unload_does_revert_the_shared_effects(runtime, tmp_path, monkeypatch):
    """Control case for the above: the provisioning worker still reverts."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    asyncio.run(runtime.load(pkg))
    asyncio.run(runtime.unload("w3app"))

    assert fx.revert == 1, "the provisioning worker must run the app's uninstall script"


def test_attach_then_provision_are_the_same_loaded_shape(runtime, tmp_path, monkeypatch):
    """A worker that attached and a worker that provisioned must be
    indistinguishable to a request — same mount path, same host mount, same
    effective grant. Anything else is a route that answers on some workers."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    asyncio.run(runtime.load(pkg, provision=False))
    attached = runtime.get("w3app")
    shape_attached = (attached.mount.path, attached.host_mount.host,
                      sorted(attached.granted_permissions))
    asyncio.run(runtime.unload("w3app", provision=False))

    asyncio.run(runtime.load(pkg))
    provisioned = runtime.get("w3app")
    shape_provisioned = (provisioned.mount.path, provisioned.host_mount.host,
                         sorted(provisioned.granted_permissions))

    assert shape_attached == shape_provisioned


def test_converge_in_process_attaches_and_detaches_from_the_mirror(runtime, tmp_path,
                                                                   monkeypatch):
    """``converge_in_process`` is the apps:changed handler — it must reach the
    same end state as the mirror without being able to install anything."""
    from src.apps.reconciler import Reconciler

    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path)

    rows: list[dict] = []

    class _Mirror:
        def list(self):
            return list(rows)

    reconciler = Reconciler(runtime, local=_Mirror())
    # A convergence pass that could reach either of these has failed the card.
    reconciler._fetch = lambda *a, **k: pytest.fail("converge fetched from the network")
    monkeypatch.setattr(type(reconciler.cloud), "configured", property(lambda self: False))

    # mirror says "w3app is installed" -> converge attaches it
    rows.append({"app_id": "w3app", "version": "1.0.0", "package_dir": pkg,
                 "granted_permissions": ["routes:register", "commands:install",
                                         "service:manage"],
                 "config": {}, "signed": False})
    result = asyncio.run(reconciler.converge_in_process())
    assert result["attached"] == ["w3app"], result
    assert runtime.is_loaded("w3app")
    assert fx.shared_total == 0, f"converge reached a shared side effect: {vars(fx)}"

    # idempotent — a second apps:changed for an unchanged mirror is a no-op
    assert asyncio.run(reconciler.converge_in_process()) == {
        "attached": [], "detached": [], "errors": []}

    # mirror row gone -> converge detaches it
    rows.clear()
    result = asyncio.run(reconciler.converge_in_process())
    assert result["detached"] == ["w3app"], result
    assert not runtime.is_loaded("w3app")
    assert fx.shared_total == 0


def test_converge_reattaches_when_another_worker_upgraded_the_version(runtime, tmp_path,
                                                                     monkeypatch):
    """A version bump provisioned elsewhere must not leave this worker serving
    the old code forever."""
    from src.apps.reconciler import Reconciler

    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _pkg(tmp_path, version="1.0.0")
    row = {"app_id": "w3app", "version": "1.0.0", "package_dir": pkg,
           "granted_permissions": ["routes:register", "commands:install",
                                   "service:manage"],
           "config": {}, "signed": False}

    class _Mirror:
        def list(self):
            return [dict(row)]

    reconciler = Reconciler(runtime, local=_Mirror())
    monkeypatch.setattr(type(reconciler.cloud), "configured", property(lambda self: False))

    asyncio.run(reconciler.converge_in_process())
    assert runtime.get("w3app").manifest.version == "1.0.0"

    # the provisioning worker upgraded: new bytes on disk, new mirror version
    _pkg(tmp_path, version="2.0.0")
    row["version"] = "2.0.0"

    result = asyncio.run(reconciler.converge_in_process())
    assert result["detached"] == ["w3app"] and result["attached"] == ["w3app"], result
    assert runtime.get("w3app").manifest.version == "2.0.0"
    assert fx.shared_total == 0


def test_attach_refuses_when_the_package_is_not_on_disk_yet(runtime, tmp_path, monkeypatch):
    """Attach must FAIL rather than fetch. The next apps:changed retries."""
    from src.apps.reconciler import Reconciler

    class _Mirror:
        def list(self):
            return [{"app_id": "ghost", "version": "1.0.0",
                     "package_dir": str(tmp_path / "not-there"),
                     "granted_permissions": [], "config": {}, "signed": False}]

    reconciler = Reconciler(runtime, local=_Mirror())
    reconciler._fetch = lambda *a, **k: pytest.fail("attach fetched from the network")
    monkeypatch.setattr(type(reconciler.cloud), "configured", property(lambda self: False))

    result = asyncio.run(reconciler.converge_in_process())
    assert result["attached"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["action"] == "attach"
    assert "not on disk" in result["errors"][0]["error"]


def test_provision_lock_is_reentrant_across_nested_installs():
    """``install`` recurses into itself for dependencies and ``reconcile``
    calls it per app. A non-reentrant lock would deadlock the worker on its
    own install — so this asserts the depth guard, not the lock."""
    from src.apps.lifecycle import AppLifecycle
    from src.apps.reconciler import Reconciler

    reconciler = Reconciler(AppRuntime(FastAPI()), lifecycle=AppLifecycle())

    async def nested():
        async with reconciler._provisioning():
            async with reconciler._provisioning():
                async with reconciler._provisioning():
                    return reconciler._provision_depth

    assert asyncio.run(asyncio.wait_for(nested(), timeout=5)) == 3
    assert reconciler._provision_depth == 0


def test_lifecycle_publish_and_lock_are_noops_without_redis(monkeypatch):
    """GOLDEN RULE: with no reachable Redis — every environment today, and
    every unit test — nothing here may raise or block."""
    from src.apps.lifecycle import AppLifecycle

    monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("AW_REDIS_URL", "redis://127.0.0.1:1/0")
    lc = AppLifecycle()

    async def run():
        started = await lc.start(lambda payload: asyncio.sleep(0))
        assert started is False  # unreachable, reported honestly
        await lc.publish("install", "w3app")  # must not raise
        async with lc.provision_lock("apps") as held:
            assert held is False  # local lock only
        await lc.stop()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_single_worker_never_defers_its_boot_reconcile(monkeypatch):
    """GOLDEN RULE, as a test. ``AW_WORKSPACE_WORKERS<=1`` is what ships, and
    such a worker must ALWAYS run its own boot reconcile — never wait for a
    "provisioner" that does not exist.

    This is a regression test with a scar behind it: gating purely on a Redis
    ``cooldown_acquire`` made a single worker restarting twice inside the
    cooldown window skip both its reconcile and its ``agent sync``, which
    surfaced as two unrelated ``test_skills_routes`` failures.
    """
    from src.api.app import _is_boot_provisioner

    monkeypatch.delenv("AW_WORKSPACE_WORKERS", raising=False)
    assert asyncio.run(_is_boot_provisioner()) is True

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "1")

    async def _boom(*a, **k):
        raise AssertionError("a single worker must not consult Redis at all")

    monkeypatch.setattr("src.libs.redis_coord.cooldown_acquire", _boom)
    assert asyncio.run(_is_boot_provisioner()) is True


def test_multi_worker_falls_back_to_provisioning_when_redis_is_down(monkeypatch):
    """No coordination available -> every worker converges independently.
    Wasteful, but it is exactly the pre-W3 behaviour, which is the right
    failure mode: apps come up."""
    from src.api.app import _is_boot_provisioner

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "10")

    async def _down(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr("src.libs.redis_coord.cooldown_acquire", _down)
    assert asyncio.run(_is_boot_provisioner()) is True


def test_multi_worker_claim_is_scoped_to_one_fleet_boot(monkeypatch):
    """Exactly one worker of a boot provisions, and the claim key is the
    uvicorn master they were forked from — so the NEXT boot gets its own
    claim instead of inheriting this one's window."""
    from src.api.app import _is_boot_provisioner

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "3")
    claimed: set[str] = set()
    keys: list[str] = []

    async def _claim(key, seconds=0.0, redis_url=None):
        keys.append(key)
        if key in claimed:
            return False
        claimed.add(key)
        return True

    monkeypatch.setattr("src.libs.redis_coord.cooldown_acquire", _claim)

    async def three_workers():
        return [await _is_boot_provisioner() for _ in range(3)]

    assert asyncio.run(three_workers()) == [True, False, False]
    assert len(set(keys)) == 1 and str(os.getppid()) in keys[0]
