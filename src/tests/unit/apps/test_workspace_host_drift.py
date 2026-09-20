"""A Tier-2 container left dialling a workspace container that no longer exists.

THE INCIDENT (2026-09-20). ``start()`` bakes ``AW_WORKSPACE_HOST`` —
``socket.gethostname()``, the name a sibling on the shared network resolves
this process by — into every network-joined app container, once, at creation
time. Nothing ever refreshed it. The workspace's own container was recreated
(``a9ec92ec4328`` → ``8c0fd9d828c3``) while ``aw-app-browser``'s container was
only restarted in place by the engine's ``unless-stopped`` policy, so 12h
later its Chromium was still being launched with
``--proxy-server=a9ec92ec4328:9124`` against a hostname that had stopped
resolving, and every page it opened failed with ERR_PROXY_CONNECTION_FAILED.

There was no self-healing path at all: the boot reconcile re-creates a
container only for an app it actually installs or starts, so an
``auto_start: false`` app (which is exactly what aw-app-browser was — the
workspace registered its container at boot and never started it) stayed stale
until someone restarted it by hand. Same class of bug as
aw-app-agents-platform-runners' ``test_on_workspace_mcp_changed``: something
external moved, and warm state that captured it once was never invalidated.

Run: .venv/aw/bin/python -m pytest src/tests/unit/apps/test_workspace_host_drift.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from src.apps.containers import WORKSPACE_HOST_ENV, ContainerSupervisor

OLD = "a9ec92ec4328"   # the workspace container that was recreated away
NOW = "8c0fd9d828c3"   # this process's container, right now


class _FakeContainer:
    def __init__(self, name: str, host: str | None, status: str = "running") -> None:
        self.name = name
        self.id = f"id-{name}"
        self.status = status
        env = ["AW_WORKSPACE_SLUG=crispal"]
        if host is not None:
            env.append(f"{WORKSPACE_HOST_ENV}={host}")
        self.attrs = {"Config": {"Env": env}}
        self.removed = False

    def reload(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def stop(self, timeout: int | None = None) -> None:
        pass


class _FakeClient:
    """Enough Docker API for get / pull / run, recording what was created."""

    def __init__(self, store: dict[str, _FakeContainer],
                 unreadable: set[str] | None = None,
                 run_fails: set[str] | None = None) -> None:
        self.store = store
        self.unreadable = unreadable or set()
        self.run_fails = run_fails or set()
        self.created: list[str] = []
        self.run_kwargs: list[dict] = []
        self.containers = self
        self.images = self

    # containers
    def get(self, name: str) -> _FakeContainer:
        if name in self.unreadable:
            raise RuntimeError(f"engine refused to inspect {name}")
        c = self.store.get(name)
        if c is None or c.removed:
            raise NotFound(name)
        return c

    def run(self, image: str, name: str = "", **kwargs):
        if name in self.run_fails:
            raise RuntimeError(f"no space left creating {name}")
        self.created.append(name)
        self.run_kwargs.append({"image": image, "name": name, **kwargs})
        c = _FakeContainer(name, (kwargs.get("environment") or {}).get(WORKSPACE_HOST_ENV))
        self.store[name] = c
        return c

    # images
    def pull(self, image: str, auth_config: dict | None = None) -> None:
        pass


def _supervisor(monkeypatch, running: dict[str, str | None], **client_kwargs):
    """A supervisor with one registered container per ``running`` entry, each
    already live with the ``AW_WORKSPACE_HOST`` given (None = created without
    the var at all)."""
    monkeypatch.setattr("src.apps.containers.workspace_host", lambda: NOW)
    store = {name: _FakeContainer(name, host) for name, host in running.items()}
    client = _FakeClient(store, **client_kwargs)
    sup = ContainerSupervisor(socket="/dev/null", network="aw-remote-host", client=client)
    return sup, client


def _register(sup, app_id: str, *, network: str | None = "aw-remote-host") -> None:
    sup.register(app_id, image=f"img/{app_id}", port=7900)
    sup._require(app_id).network = network


# ── the audit ────────────────────────────────────────────────────────────────


def test_a_container_this_workspace_started_is_not_drifted(monkeypatch):
    sup, _ = _supervisor(monkeypatch, {"aw-app-browser": NOW})
    _register(sup, "browser")
    assert sup.workspace_host_drift() == []


def test_a_container_from_a_previous_workspace_container_is_drifted(monkeypatch):
    """THE BUG. The app is up and healthy; only its calls back into the
    workspace fail, which is why nothing else ever noticed."""
    sup, _ = _supervisor(monkeypatch, {"aw-app-browser": OLD})
    _register(sup, "browser")
    assert sup.workspace_host_drift() == [
        {"app": "browser", "container": "aw-app-browser", "was": OLD, "now": NOW}]


def test_a_stopped_container_is_left_alone(monkeypatch):
    """Recreating it would START it, overriding an ``auto_start: false`` the
    user chose — and a container that isn't running is dialling nothing."""
    sup, client = _supervisor(monkeypatch, {"aw-app-browser": OLD})
    client.store["aw-app-browser"].status = "exited"
    _register(sup, "browser")
    assert sup.workspace_host_drift() == []


def test_a_container_without_the_var_is_left_alone(monkeypatch):
    """No baked address means nothing to go stale — an app from before this
    var existed must not be recreated on every tick forever."""
    sup, _ = _supervisor(monkeypatch, {"aw-app-browser": None})
    _register(sup, "browser")
    assert sup.workspace_host_drift() == []


def test_an_app_off_the_shared_network_is_not_audited(monkeypatch):
    """``start()`` only injects the var for ``c.network``; a port-published
    app never had one."""
    sup, _ = _supervisor(monkeypatch, {"aw-app-browser": OLD})
    _register(sup, "browser", network=None)
    assert sup.workspace_host_drift() == []


def test_a_container_that_no_longer_exists_is_skipped(monkeypatch):
    sup, _ = _supervisor(monkeypatch, {})
    _register(sup, "browser")
    assert sup.workspace_host_drift() == []


def test_one_unreadable_container_does_not_hide_the_rest(monkeypatch):
    """An engine that errors on one inspect must not cost the audit every
    other app — that is how a whole-pass failure turns into silence."""
    sup, _ = _supervisor(monkeypatch, {"aw-app-kb": OLD, "aw-app-browser": OLD},
                         unreadable={"aw-app-kb"})
    _register(sup, "kb")
    _register(sup, "browser")
    assert [r["app"] for r in sup.workspace_host_drift()] == ["browser"]


# ── the repair ───────────────────────────────────────────────────────────────


def test_refresh_recreates_only_the_drifted_container(monkeypatch):
    sup, client = _supervisor(monkeypatch, {"aw-app-browser": OLD, "aw-app-kb": NOW})
    _register(sup, "browser")
    _register(sup, "kb")

    result = sup.refresh_workspace_host()

    assert result["refreshed"] == ["browser"]
    assert result["errors"] == []
    assert client.created == ["aw-app-browser"]


def test_the_recreated_container_carries_the_CURRENT_host(monkeypatch):
    """The point of the whole fix: a recreate re-reads the identity. A repair
    that re-baked the same stale value would satisfy every test above."""
    sup, client = _supervisor(monkeypatch, {"aw-app-browser": OLD})
    _register(sup, "browser")

    sup.refresh_workspace_host()

    assert client.run_kwargs[0]["environment"][WORKSPACE_HOST_ENV] == NOW
    assert sup.workspace_host_drift() == [], "the audit must converge after one pass"


def test_a_sidecar_is_recreated_before_the_app_that_dials_it(monkeypatch):
    """Same ordering as ``AppRuntime._load_container``: aw-app-crispal's MCP
    dials its database as it comes up, so replacing the database underneath a
    freshly recreated app is a first tool call that fails."""
    sup, client = _supervisor(monkeypatch,
                              {"aw-app-crispal": OLD, "aw-app-crispal-db": OLD})
    _register(sup, "crispal")
    sup.register_sidecar("crispal", "db", image="mysql:8.0", port=3306)
    sup._require("crispal:db").network = "aw-remote-host"

    sup.refresh_workspace_host()

    assert client.created == ["aw-app-crispal-db", "aw-app-crispal"]


def test_one_failed_recreate_does_not_block_the_others(monkeypatch):
    sup, client = _supervisor(monkeypatch, {"aw-app-browser": OLD, "aw-app-kb": OLD},
                              run_fails={"aw-app-browser"})
    _register(sup, "browser")
    _register(sup, "kb")

    result = sup.refresh_workspace_host()

    assert result["refreshed"] == ["kb"]
    assert result["errors"] == [{"app": "browser", "error": "no space left creating aw-app-browser"}]


def test_no_container_engine_is_a_quiet_no_op(monkeypatch):
    """A Tier-1-only (or slim BYOD) workspace has no socket at all."""
    monkeypatch.setattr("src.apps.containers.workspace_host", lambda: NOW)
    sup = ContainerSupervisor(socket=None, network=None, client=None)
    assert sup.workspace_host_drift() == []
    assert sup.refresh_workspace_host()["refreshed"] == []


# ── the watchdog tick ────────────────────────────────────────────────────────


def _runtime(result, *, available: bool = True):
    from src.apps.runtime import AppRuntime

    def refresh():
        if not available:
            raise AssertionError("must not touch the engine when Tier-2 is unavailable")
        return result

    rt = AppRuntime.__new__(AppRuntime)
    rt.containers = SimpleNamespace(available=available, refresh_workspace_host=refresh)
    return rt


def test_a_clean_tick_does_not_raise():
    rt = _runtime({"host": NOW, "drifted": [], "refreshed": [], "errors": []})
    asyncio.run(rt._heal_workspace_host_drift())


def test_a_tick_that_could_not_repair_raises_so_the_backoff_means_something():
    """Same contract as ``_rescan_mcp_gateway``: a watchdog that silently
    no-ops every 5 minutes is worse than no watchdog, and ``last_error`` in
    ``GET /api/apps/-/watchdog`` is the only place this is visible."""
    from src.apps.containers import ContainerError

    rt = _runtime({"host": NOW, "drifted": ["browser"], "refreshed": [],
                   "errors": [{"app": "browser", "error": "boom"}]})
    with pytest.raises(ContainerError, match="browser"):
        asyncio.run(rt._heal_workspace_host_drift())


def test_the_tick_is_skipped_where_there_is_no_container_engine():
    rt = _runtime(None, available=False)
    asyncio.run(rt._heal_workspace_host_drift())


# ── the boot wiring ──────────────────────────────────────────────────────────


def test_boot_registers_the_healer_and_its_first_tick_repairs_now(monkeypatch):
    """Registration alone is not the claim — the other two runtime watchdogs
    are registered with ``run_immediately=False`` and would leave a workspace
    whose container has JUST been recreated broken for a whole interval. Boot
    is exactly when this drift exists, so the first pass has to run then.
    """
    from src.apps import routes as routes_mod
    from src.apps.runtime import _SYSTEM_APP_ID, _WORKSPACE_HOST_TASK_ID, AppRuntime

    sup, client = _supervisor(monkeypatch, {"aw-app-browser": OLD})
    _register(sup, "browser")

    rt = AppRuntime.__new__(AppRuntime)
    from src.apps.watchdog import WatchdogSupervisor
    rt.watchdog = WatchdogSupervisor()
    rt.containers = sup
    rt._apps = {}  # the gateway-rescan waiter polls is_loaded until cancelled
    rt.start_system_cli_healer = lambda *a, **k: None
    rt.start_mcp_gateway_rescan = lambda *a, **k: None
    rt.start_zombie_reaper = lambda *a, **k: None
    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway",
                        lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("src.api.otel.ensure_export_state", lambda _rt: None)

    app = SimpleNamespace(state=SimpleNamespace(
        app_reconciler=SimpleNamespace(reconcile=_noop_reconcile), app_runtime=rt))

    async def run():
        await routes_mod.reconcile_on_boot(app)
        assert _WORKSPACE_HOST_TASK_ID in rt.watchdog.task_ids_for(_SYSTEM_APP_ID)
        for _ in range(500):  # the first tick is scheduled, not awaited inline
            if client.created:
                break
            await asyncio.sleep(0.01)
        assert client.created == ["aw-app-browser"], (
            "the healer's first tick must run at boot, not one interval later")
        rt.watchdog.cancel_all_for(_SYSTEM_APP_ID)

    asyncio.run(run())


def test_a_worker_that_did_not_run_the_reconcile_registers_it_too():
    """Measured live 2026-09-20, on the first cut of this fix: at
    ``AW_WORKSPACE_WORKERS=5`` the boot provisioner is not necessarily the
    ``RedisLease("core")`` holder, so registering only in ``reconcile_on_boot``
    produced "registered watchdog __system__/workspace-host-drift (paused —
    not the lease leader)" and the task never ticked at all. Every worker
    registers it (W1); only the leader's supervisor spins it.
    """
    from src.apps import routes as routes_mod
    from src.apps.runtime import _SYSTEM_APP_ID, _WORKSPACE_HOST_TASK_ID, AppRuntime
    from src.apps.watchdog import WatchdogSupervisor

    rt = AppRuntime.__new__(AppRuntime)
    rt.watchdog = WatchdogSupervisor()
    rt.watchdog.pause()  # this worker is not the lease holder
    rt.containers = ContainerSupervisor(socket=None, network=None, client=None)
    rt.start_system_cli_healer = lambda *a, **k: None
    rt.start_mcp_gateway_rescan = lambda *a, **k: None
    rt.start_zombie_reaper = lambda *a, **k: None

    app = SimpleNamespace(state=SimpleNamespace(
        app_reconciler=SimpleNamespace(converge_in_process=_noop_converge),
        app_runtime=rt))

    async def run():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("src.api.otel.ensure_export_state", lambda _rt: None)
            await routes_mod.attach_on_boot(app)

    asyncio.run(run())
    assert _WORKSPACE_HOST_TASK_ID in rt.watchdog.task_ids_for(_SYSTEM_APP_ID), (
        "a non-provisioning worker must register it, or a failover leader "
        "comes up with no healer at all")


async def _noop_converge():
    return {"attached": []}


async def _noop_reconcile():
    return {"installed": []}
