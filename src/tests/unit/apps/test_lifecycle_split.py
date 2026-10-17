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
import contextlib
import json

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
        "attached": [], "detached": [], "reconfigured": [], "errors": []}

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


CONFIG_PLUGIN = """
class AppPlugin:
    async def activate(self, ctx):
        ctx.reloaded = []
        ctx.saved = []
        # Captured ONCE, at activate — the shape every app that reads its
        # config lazily ends up with, and the thing a rebind would kill.
        ctx.captured = ctx.config

    async def deactivate(self):
        pass

    async def on_config_reloaded(self, ctx):
        ctx.reloaded.append(dict(ctx.config))

    async def on_config_saved(self, ctx):
        ctx.saved.append(dict(ctx.config))
"""


def _cfg_pkg(tmp_path, slug="cfgapp", version="1.0.0"):
    """Like ``_pkg`` but with a config_schema that DECLARES A DEFAULT and a
    plugin recording both config hooks. The default is the point: the mirror
    row written by an install holds pre-defaults config while a save writes
    post-defaults, so a converge that compares them raw sees a difference
    forever. A fixture without a default cannot catch that."""
    pkg = tmp_path / slug
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "aw-app.json").write_text(json.dumps({
        "manifest_version": 1,
        "id": slug,
        "name": slug,
        "version": version,
        "tier": "inprocess",
        "runtime": {"entrypoint": "plugin:AppPlugin"},
        "permissions": [],
        "config_schema": {
            "type": "object",
            "properties": {
                "sweep_enabled": {"type": "boolean", "default": False},
                "interval": {"type": "integer", "default": 30},
            },
        },
    }))
    (pkg / "plugin.py").write_text(CONFIG_PLUGIN)
    return str(pkg)


def _cfg_reconciler(runtime, rows, monkeypatch):
    from src.apps.reconciler import Reconciler

    class _Mirror:
        def list(self):
            return [dict(r) for r in rows]

    reconciler = Reconciler(runtime, local=_Mirror())
    reconciler._fetch = lambda *a, **k: pytest.fail("converge fetched from the network")
    monkeypatch.setattr(type(reconciler.cloud), "configured", property(lambda self: False))
    return reconciler


def test_converge_refreshes_config_another_worker_saved(runtime, tmp_path, monkeypatch):
    """The third converge case. A config saved on ANOTHER worker only moved
    that worker's in-memory copy and the shared mirror row; this worker has to
    pick it up off apps:changed, or it answers GET /config — and runs its
    watchdogs — from a stale dict until the next restart."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _cfg_pkg(tmp_path)
    rows = [{"app_id": "cfgapp", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {"sweep_enabled": False},
             "signed": False}]
    reconciler = _cfg_reconciler(runtime, rows, monkeypatch)

    asyncio.run(reconciler.converge_in_process())
    loaded = runtime.get("cfgapp")
    captured = loaded.ctx.captured
    assert loaded.ctx.reloaded == [], "attaching is not a config change"

    # another worker served POST /api/apps/cfgapp/config
    rows[0]["config"] = {"sweep_enabled": True, "interval": 30}
    result = asyncio.run(reconciler.converge_in_process())

    assert result["reconfigured"] == ["cfgapp"], result
    assert loaded.config["sweep_enabled"] is True
    assert loaded.ctx.config["sweep_enabled"] is True
    # ...in the SAME dict the plugin captured at activate time.
    assert loaded.ctx.captured is captured and captured["sweep_enabled"] is True
    assert loaded.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]
    # on_config_saved is the once-per-save PROVISION half — running it here
    # would restart a managed service in all ten workers (the measured reason
    # this hook exists at all).
    assert loaded.ctx.saved == []
    assert fx.shared_total == 0, f"converge reached a shared side effect: {vars(fx)}"


def test_converge_does_not_refire_the_config_hook_for_schema_defaults(runtime, tmp_path,
                                                                      monkeypatch):
    """Both sides of the comparison must go through ``config_with_defaults``.
    The two writers disagree about defaults: a save persists the POST-defaults
    dict, while ``install``'s ``upsert`` persists ``spec.config`` PRE-defaults.
    So a worker holding the normalized form and a row holding the raw one
    describe the SAME configuration — and comparing them raw makes every
    apps:changed in the workspace (one per install, uninstall and save of ANY
    app) 'see a change': it re-fires every app's hook and strips the defaults
    back out of ``loaded.config``."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _cfg_pkg(tmp_path)
    # exactly what upsert(spec, ...) stores: no defaults filled in
    rows = [{"app_id": "cfgapp", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {"interval": 5}, "signed": False}]
    reconciler = _cfg_reconciler(runtime, rows, monkeypatch)

    asyncio.run(reconciler.converge_in_process())
    loaded = runtime.get("cfgapp")
    # ...and this worker holds the normalized form of those same values, which
    # is what a save (or an earlier refresh) leaves behind.
    loaded.config = loaded.manifest.config_with_defaults({"interval": 5})
    assert loaded.config == {"sweep_enabled": False, "interval": 5}

    for _ in range(3):
        assert asyncio.run(reconciler.converge_in_process()) == {
            "attached": [], "detached": [], "reconfigured": [], "errors": []}
    assert loaded.ctx.reloaded == []
    assert loaded.config == {"sweep_enabled": False, "interval": 5}


def test_converge_config_refresh_is_idempotent_under_repeated_delivery(runtime, tmp_path,
                                                                       monkeypatch):
    """Delivery is not once per worker — each RedisBroadcaster in a process
    starts its own relay (40 subscribers were measured for 10 workers), so the
    handler fires more than once per publish. The second delivery must be a
    no-op, not a second hook call."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _cfg_pkg(tmp_path)
    rows = [{"app_id": "cfgapp", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {}, "signed": False}]
    reconciler = _cfg_reconciler(runtime, rows, monkeypatch)
    asyncio.run(reconciler.converge_in_process())
    loaded = runtime.get("cfgapp")

    rows[0]["config"] = {"interval": 5}
    for _ in range(4):
        asyncio.run(reconciler.converge_in_process())

    assert loaded.ctx.reloaded == [{"sweep_enabled": False, "interval": 5}]


def test_converge_survives_an_app_whose_config_hook_raises(runtime, tmp_path, monkeypatch):
    """One bad app must not block the rest — same contract attach/detach have."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _cfg_pkg(tmp_path, slug="badcfg")
    (tmp_path / "badcfg" / "plugin.py").write_text(
        "class AppPlugin:\n"
        "    async def activate(self, ctx):\n        pass\n"
        "    async def deactivate(self):\n        pass\n"
        "    async def on_config_reloaded(self, ctx):\n"
        "        raise RuntimeError('app hook blew up')\n")
    rows = [{"app_id": "badcfg", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {}, "signed": False}]
    reconciler = _cfg_reconciler(runtime, rows, monkeypatch)
    asyncio.run(reconciler.converge_in_process())

    rows[0]["config"] = {"interval": 9}
    result = asyncio.run(reconciler.converge_in_process())

    assert result["reconfigured"] == []
    assert result["errors"] == [{"app_id": "badcfg", "action": "config",
                                 "error": "app hook blew up"}]
    # the refresh itself still landed — only the app's reaction to it failed
    assert runtime.get("badcfg").ctx.config["interval"] == 9


# --- the pending-broadcast wedge (card 3fc5bf3b) -----------------------------


class _RecordingLifecycle:
    """Just enough ``AppLifecycle`` to see whether a publish happened.

    Not the real class and deliberately not Redis-backed: the suite shares the
    LIVE Redis keyspace of this very container, so nothing here may publish
    for real."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str | None]] = []

    async def publish(self, reason: str, app_id: str | None = None) -> None:
        self.published.append((reason, app_id))

    @contextlib.asynccontextmanager
    async def provision_lock(self, name: str = "apps"):
        yield True

    @contextlib.asynccontextmanager
    async def in_process_exclusive(self):
        yield


def _wedge_reconciler(monkeypatch):
    from src.apps.reconciler import Reconciler

    lifecycle = _RecordingLifecycle()
    reconciler = Reconciler(AppRuntime(FastAPI()), lifecycle=lifecycle)
    monkeypatch.setattr(type(reconciler.cloud), "configured",
                        property(lambda self: False))
    return reconciler, lifecycle


def test_a_reconcile_pass_that_raises_leaves_the_flags_disarmed(monkeypatch):
    """The wedge, as a regression test (card 3fc5bf3b).

    ``_pending_broadcast = False`` does not mean "nothing pending" — it means
    "``_trigger_broadcast``, you are inside a pass, defer". Left behind by a
    pass that blew up mid-way, it silently disabled EVERY later broadcast on
    that worker, including the one at the end of every config save it served:
    measured live as 1-in-5 workers holding fresh config, with no log line.
    """
    reconciler, lifecycle = _wedge_reconciler(monkeypatch)

    async def _boom(desired=None):
        # An app installed earlier in the pass asked for a broadcast...
        await reconciler._trigger_broadcast("install", "someapp")
        assert reconciler._pending_broadcast is True  # ...and it was deferred
        raise RuntimeError("the pass blew up halfway")

    monkeypatch.setattr(reconciler, "_reconcile_pass", _boom)

    with pytest.raises(RuntimeError, match="halfway"):
        asyncio.run(reconciler.reconcile())

    assert reconciler._pending_broadcast is None
    assert reconciler._pending_gateway_reload is None
    # A failed pass still owed the fleet a broadcast, and flushed it.
    assert lifecycle.published == [("reconcile", None)]

    # The part that actually bit: the NEXT broadcast must publish immediately
    # instead of being deferred into a pass that is long over.
    asyncio.run(reconciler._trigger_broadcast("config", "knowledgeable"))
    assert lifecycle.published[-1] == ("config", "knowledgeable")


def test_a_cancelled_reconcile_pass_leaves_the_flags_disarmed(monkeypatch):
    """The likelier trigger of the two: ``reconcile_on_boot`` bounds the boot
    pass with ``asyncio.wait_for(..., 1200)`` and a manual reconcile can be cut
    by the tunnel edge at 30s — both CANCEL the coroutine mid-pass.

    Cancelled for real (a task, cancelled from outside) rather than by raising
    ``CancelledError`` by hand, because the property under test is specifically
    that the ``finally`` does not ``await`` on this path: an ``await`` in a
    cancelled task re-raises at once, which would skip whatever came after it.
    """
    reconciler, lifecycle = _wedge_reconciler(monkeypatch)
    started = asyncio.Event()

    async def _hangs(desired=None):
        await reconciler._trigger_broadcast("install", "someapp")
        started.set()
        await asyncio.sleep(30)  # cancelled here, like a wait_for timeout

    monkeypatch.setattr(reconciler, "_reconcile_pass", _hangs)

    async def run():
        task = asyncio.ensure_future(reconciler.reconcile())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(run(), timeout=10))

    assert reconciler._pending_broadcast is None
    assert reconciler._pending_gateway_reload is None
    # No publish attempted: the flush is skipped under cancellation on purpose
    # (the convergence watchdog owns a cancelled pass's convergence).
    assert lifecycle.published == []

    asyncio.run(reconciler._trigger_broadcast("config", "knowledgeable"))
    assert lifecycle.published == [("config", "knowledgeable")]


def test_a_clean_reconcile_pass_still_publishes_exactly_once(monkeypatch):
    """Control case for the two above: the finally must not DOUBLE-publish the
    broadcast the pass already flushed itself."""
    reconciler, lifecycle = _wedge_reconciler(monkeypatch)

    asyncio.run(reconciler.reconcile(desired=[]))

    assert reconciler._pending_broadcast is None
    assert reconciler._pending_gateway_reload is None
    assert lifecycle.published == []  # nothing changed, so nothing to announce

    async def _changes_something(desired=None):
        await reconciler._trigger_broadcast("install", "someapp")
        return {"installed": ["someapp"]}

    monkeypatch.setattr(reconciler, "_reconcile_pass", _changes_something)
    asyncio.run(reconciler.reconcile())
    assert lifecycle.published == [("reconcile", None)]


# --- the generation floor: the two-hook contract, pinned ---------------------


def _shared_mirror(rows):
    """One mirror two 'workers' read, the way two real workers share one
    Postgres. ``update_config`` is what ``save_app_config`` writes through."""

    class _Mirror:
        def list(self):
            return [dict(r) for r in rows]

        def update_config(self, app_id, config):
            for r in rows:
                if r.get("app_id") == app_id:
                    r["config"] = dict(config)

    return _Mirror()


def test_on_config_saved_runs_once_in_total_across_both_converge_paths(
        tmp_path, monkeypatch):
    """The contract that distinguishes the two hooks, over all three paths a
    worker can learn about a save — and the asymmetry most likely to break
    when adding a fourth.

    * ``on_config_saved`` is the PROVISION half: **exactly once in total**,
      only on the worker that served the POST. Running it per worker would
      restart a managed service in every one of them (the measured reason the
      hooks were split at all).
    * ``on_config_reloaded`` is the ATTACH half: once **per worker** — inline
      on the request worker, via ``apps:changed`` on the others, and via the
      generation floor on any worker the broadcast missed.

    Three workers here: A serves the real route, B hears the broadcast, C
    hears nothing and is reached only by ``refresh_configs_in_process`` —
    which is the floor's whole claim, so it is asserted rather than assumed.
    """
    from src.apps import config_store
    from src.apps.routes import register_apps_routes

    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    pkg = _cfg_pkg(tmp_path)
    rows = [{"app_id": "cfgapp", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {}, "signed": False}]

    # Worker A — serves POST /api/apps/cfgapp/config through the real route.
    host_a = FastAPI()
    runtime_a = register_apps_routes(host_a)
    _Effects().wire(runtime_a, monkeypatch)
    reconciler_a = host_a.state.app_reconciler
    reconciler_a.local = _shared_mirror(rows)
    monkeypatch.setattr(type(reconciler_a.cloud), "configured",
                        property(lambda self: False))
    asyncio.run(runtime_a.load(pkg, provision=False))
    loaded_a = runtime_a.get("cfgapp")

    # Workers B and C — never see the request. Both attach first, as they
    # would at boot, so the save below is a genuine config CHANGE for them.
    workers = []
    for _ in range(2):
        rt = AppRuntime(FastAPI(), guard_identity=False)
        _Effects().wire(rt, monkeypatch)
        rec = _cfg_reconciler(rt, rows, monkeypatch)
        asyncio.run(rec.converge_in_process())
        workers.append((rt, rec))
    (runtime_b, reconciler_b), (runtime_c, reconciler_c) = workers

    save = next(r.endpoint for r in host_a.routes
                if getattr(r, "path", "") == "/api/apps/{slug}/config"
                and "POST" in (getattr(r, "methods", None) or set()))
    asyncio.run(save("cfgapp", {"config": {"sweep_enabled": True}}, identity={}))

    # A: both halves, each once.
    assert loaded_a.ctx.saved == [{"sweep_enabled": True, "interval": 30}]
    assert loaded_a.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]
    # ...and the save stamped the floor's token, next to the snapshot it wrote.
    assert config_store.read_generation() is not None

    # B: the broadcast path. One reload, and then the floor finds nothing left.
    asyncio.run(reconciler_b.converge_in_process())
    loaded_b = runtime_b.get("cfgapp")
    assert loaded_b.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]
    assert asyncio.run(reconciler_b.refresh_configs_in_process()) == []
    assert loaded_b.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]

    # C: the floor path ALONE — no broadcast ever reached this worker.
    assert asyncio.run(reconciler_c.refresh_configs_in_process()) == ["cfgapp"]
    loaded_c = runtime_c.get("cfgapp")
    assert loaded_c.ctx.config["sweep_enabled"] is True
    assert loaded_c.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]
    # A second refresh with nothing new must call nothing — the normalization
    # in _refresh_config is load-bearing here (see its own docstring).
    assert asyncio.run(reconciler_c.refresh_configs_in_process()) == []
    assert loaded_c.ctx.reloaded == [{"sweep_enabled": True, "interval": 30}]

    # The whole point: one save, one on_config_saved, fleet-wide.
    assert loaded_b.ctx.saved == []
    assert loaded_c.ctx.saved == []


def test_refresh_configs_in_process_never_attaches_or_detaches(runtime, tmp_path,
                                                               monkeypatch):
    """It runs INLINE in a request (``src/api/app.py``'s generation
    middleware), so it must not be able to do mounting work: an attach imports
    a plugin and runs ``activate()``, a detach unmounts routes. A package that
    appeared or vanished is the broadcast's and the watchdog's business."""
    fx = _Effects()
    fx.wire(runtime, monkeypatch)
    pkg = _cfg_pkg(tmp_path)
    rows = [{"app_id": "cfgapp", "version": "1.0.0", "package_dir": pkg,
             "granted_permissions": [], "config": {}, "signed": False}]
    reconciler = _cfg_reconciler(runtime, rows, monkeypatch)

    # An app in the mirror that this worker does not serve must stay unserved.
    assert asyncio.run(reconciler.refresh_configs_in_process()) == []
    assert not runtime.is_loaded("cfgapp")

    asyncio.run(reconciler.converge_in_process())
    assert runtime.is_loaded("cfgapp")

    # ...and an app this worker serves that the mirror no longer lists must
    # stay served (unlike converge_in_process, which detaches it).
    rows.clear()
    assert asyncio.run(reconciler.refresh_configs_in_process()) == []
    assert runtime.is_loaded("cfgapp")
    assert fx.shared_total == 0, f"the floor reached a shared side effect: {vars(fx)}"


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


def test_lifecycle_publish_and_lock_are_noops_without_redis(monkeypatch, tmp_path):
    """GOLDEN RULE: with no reachable Redis — every environment today, and
    every unit test — nothing here may raise or block.

    The provisioning lock is the one thing that must now still WORK rather
    than degrade: it used to yield ``False`` here (Redis down → no shared
    lock → ten workers in ``apt`` at once), and the whole point of moving it
    to ``flock`` is that an unreachable Redis costs it nothing."""
    from src.apps.lifecycle import AppLifecycle

    monkeypatch.setenv("AW_WORKSPACE_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("AW_REDIS_URL", "redis://127.0.0.1:1/0")
    lc = AppLifecycle(lock_dir=str(tmp_path / "locks"))

    async def run():
        started = await lc.start(lambda payload: asyncio.sleep(0))
        assert started is False  # unreachable, reported honestly
        await lc.publish("install", "w3app")  # must not raise
        async with lc.provision_lock("apps") as held:
            assert held is True  # flock, not Redis — unaffected
        await lc.stop()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_single_worker_never_defers_its_boot_reconcile(monkeypatch):
    """GOLDEN RULE, as a test. ``AW_WORKSPACE_WORKERS<=1`` is what ships, and
    such a worker must ALWAYS run its own boot reconcile — never wait for a
    "provisioner" that does not exist, and never even consult a lock.

    This is a regression test with a scar behind it: gating purely on a Redis
    ``cooldown_acquire`` made a single worker restarting twice inside the
    cooldown window skip both its reconcile and its ``agent sync``, which
    surfaced as two unrelated ``test_skills_routes`` failures.
    """
    from src.api.app import _is_boot_provisioner

    monkeypatch.delenv("AW_WORKSPACE_WORKERS", raising=False)
    assert asyncio.run(_is_boot_provisioner()) == (True, None)

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "1")

    def _boom(*a, **k):
        raise AssertionError("a single worker must not consult the "
                             "coordinator at all")

    monkeypatch.setattr(
        "src.apps.boot_reconcile_coord.BootReconcileCoordinator.__init__", _boom)
    assert asyncio.run(_is_boot_provisioner()) == (True, None)


def test_multi_worker_still_provisions_when_coordination_itself_breaks(monkeypatch):
    """The lock directory being unusable (read-only, full) is the only way
    coordinate() can raise now that no network is involved — and apps must
    still come up. ``coordinator`` in the returned tuple must be ``None``:
    there is no held lock to finish.

    This used to be the ``Redis is down`` test, and that case used to be the
    NORMAL one — which is exactly what made it dangerous, since every worker
    took this branch and led concurrently. It is a genuine edge case now.
    """
    from src.api.app import _is_boot_provisioner
    from src.apps.boot_reconcile_coord import BootReconcileCoordinator

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "10")

    async def _down(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(BootReconcileCoordinator, "coordinate", _down)
    assert asyncio.run(_is_boot_provisioner()) == (True, None)


def test_multi_worker_claim_is_scoped_to_one_fleet_boot(monkeypatch, tmp_path):
    """Exactly one worker of a boot provisions (returns a coordinator), the
    other two attach (return None) — and every one of them was keyed on the
    SAME boot-nonce minted once by the parent process (``boot_info.boot_id()``)
    and inherited by every worker of that one boot — so the NEXT boot gets
    its own claim instead of inheriting this one's window."""
    from src.api import boot_info
    from src.api.app import _is_boot_provisioner
    from src.apps.boot_reconcile_coord import BootReconcileCoordinator

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "3")
    monkeypatch.setattr(boot_info, "compute_git_head", lambda: "")
    boot_info.mint_boot_identity()

    lock_dir = str(tmp_path / "locks")
    real_init = BootReconcileCoordinator.__init__

    def _patched_init(self, boot_id, **kw):
        # Short patience: these three "workers" are awaited SEQUENTIALLY
        # below (one real reconcile pass per worker in production is never
        # concurrent with another worker's own check either), so a follower
        # correctly finds the done marker already written — it only actually
        # WAITS out max_wait_s if something regressed and nobody ever
        # finishes. Keeping that short is what turns a regression into a fast
        # assertion failure instead of the 43-MINUTE hang this test suite
        # caught once already.
        kw.setdefault("lock_dir", lock_dir)
        real_init(self, boot_id, max_wait_s=0.2, poll_interval=0.02, **kw)

    monkeypatch.setattr(BootReconcileCoordinator, "__init__", _patched_init)

    async def three_workers():
        results = []
        for _ in range(3):
            leading, coordinator = await _is_boot_provisioner()
            if coordinator is not None:
                # The leader's pass "finishes" the instant it starts, for
                # this test's purposes — proving the ORCHESTRATION (leader
                # gets a coordinator, followers see done and attach) is not
                # about proving the coordinator waits out a slow pass, which
                # test_boot_reconcile_coord.py already covers directly.
                await coordinator.finish()
            results.append((leading, coordinator))
        return results

    results = asyncio.run(three_workers())
    leading = [r[0] for r in results]
    coordinators = [r[1] for r in results]

    assert leading == [True, False, False]
    assert coordinators[0] is not None
    assert coordinators[1] is None and coordinators[2] is None
    assert coordinators[0].boot_id == boot_info.boot_id()


def test_restart_in_place_with_pid_reuse_gets_a_fresh_claim(monkeypatch, tmp_path):
    """Regression test for the W3 boot-provisioner bug fixed 2026-09-06
    (Kanban card ``aw-workspace-multiworker:proxy-app-auto-start-not-running``).

    The claim key used to be ``f"boot-apps-reconcile:{os.getppid()}"``, on
    the assumption that a restart is always a different master process and so
    always gets its own claim. That assumption is false when the container
    restarts IN PLACE (no recreation, same PID namespace): a fresh master
    reliably gets reassigned the same low pid, so a second boot shortly after
    the first saw the first (already-dead) boot's still-live claim and every
    one of its fresh workers deferred to it, leaving ``ctx.provision=False``
    fleet-wide and no ``autostart=True`` service ever spawned that boot.

    Simulated here by minting a boot-nonce for "boot #1", running its fleet,
    then minting an entirely new one for "boot #2" (what
    ``mint_boot_identity()`` does on every real restart, independent of
    ``os.getppid()``) against a SHARED lock directory — boot #1's done marker
    is still sitting there, which is the worst case for cross-boot leakage.
    Boot #2 must still get its own provisioner, because the marker it looks
    for is keyed on its own boot_id.
    """
    from src.api import boot_info
    from src.api.app import _is_boot_provisioner
    from src.apps.boot_reconcile_coord import BootReconcileCoordinator

    monkeypatch.setenv("AW_WORKSPACE_WORKERS", "3")
    monkeypatch.setattr(boot_info, "compute_git_head", lambda: "")

    lock_dir = str(tmp_path / "locks")
    real_init = BootReconcileCoordinator.__init__

    def _patched_init(self, boot_id, **kw):
        kw.setdefault("lock_dir", lock_dir)
        real_init(self, boot_id, max_wait_s=0.2, poll_interval=0.02, **kw)

    monkeypatch.setattr(BootReconcileCoordinator, "__init__", _patched_init)

    async def fleet():
        results = []
        for _ in range(3):
            leading, coordinator = await _is_boot_provisioner()
            if coordinator is not None:
                await coordinator.finish()
            results.append((leading, coordinator))
        return results

    boot_info.mint_boot_identity()  # boot #1
    results_1 = asyncio.run(fleet())
    assert [r[0] for r in results_1] == [True, False, False]

    boot_info.mint_boot_identity()  # boot #2, restarted in place — same pid,
                                     # new boot_id
    results_2 = asyncio.run(fleet())
    assert [r[0] for r in results_2] == [True, False, False]
    assert results_2[0][1].boot_id != results_1[0][1].boot_id
