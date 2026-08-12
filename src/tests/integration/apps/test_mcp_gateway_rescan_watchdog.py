"""The periodic mcp-gateway rescan (AppRuntime.start_mcp_gateway_rescan).

Why it exists: the install/uninstall/config-save hooks push a reload the
moment an app changes what the gateway's app-scan would find, but nothing
orders the BOOT path — an already-running mcp-gateway scans
``AW_APP_SCAN_ROOTS`` once, in its own lifespan, and an inprocess app that
(re)writes its mcp.json during activate() can land after that scan. Observed
live 2026-08-12: aw-app-whiteboard and aw-app-presentations were missing from
the gateway (183 tools instead of 209) until a manual POST /reload.

mcp-gateway is container-tier with no in-process plugin, so it cannot use the
``watchdog:tasks`` capability to register this for itself — the task is owned
by core runtime code under the ``__system__`` sentinel, same as the system-CLI
healer it is modelled on.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from src.apps import routes as routes_mod
from src.apps.runtime import _MCP_RESCAN_TASK_ID, _SYSTEM_APP_ID, AppRuntime


def _async(coro):
    return asyncio.run(coro)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    return AppRuntime(FastAPI(), guard_identity=False)


def test_rescan_task_is_registered_under_the_system_sentinel(runtime):
    async def go():
        runtime.start_mcp_gateway_rescan(interval_s=300)
        assert _MCP_RESCAN_TASK_ID in runtime.watchdog.task_ids_for(_SYSTEM_APP_ID)
        runtime.watchdog.cancel_all_for(_SYSTEM_APP_ID)

    asyncio.run(go())


def test_registration_is_idempotent(runtime):
    """reconcile_on_boot can run more than once in a long-lived process."""
    async def go():
        runtime.start_mcp_gateway_rescan(interval_s=300)
        runtime.start_mcp_gateway_rescan(interval_s=300)
        assert runtime.watchdog.task_ids_for(_SYSTEM_APP_ID).count(_MCP_RESCAN_TASK_ID) == 1
        runtime.watchdog.cancel_all_for(_SYSTEM_APP_ID)

    asyncio.run(go())


def test_zero_interval_disables_the_watchdog(runtime):
    async def go():
        runtime.start_mcp_gateway_rescan(interval_s=0)
        assert runtime.watchdog.task_ids_for(_SYSTEM_APP_ID) == []

    asyncio.run(go())


def test_first_tick_is_deferred_not_immediate(runtime, monkeypatch):
    """Boot reloads explicitly at t=0 (see the boot-reload test below), so the
    watchdog's own first tick belongs one interval later.

    run_immediately=True would be actively worse than deferring: a boot-race
    failure feeds the supervisor's exponential backoff
    (min(interval * 2**n, 1800s)), pushing the retry out past the plain
    interval it was meant to beat."""
    calls = []

    async def fake_reload(rt, **kwargs):
        calls.append(rt)

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)

    async def go():
        runtime.start_mcp_gateway_rescan(interval_s=300)
        await asyncio.sleep(0.05)  # let the task reach its first sleep
        assert calls == []
        runtime.watchdog.cancel_all_for(_SYSTEM_APP_ID)

    asyncio.run(go())


def test_tick_calls_the_gateway_reload(runtime, monkeypatch):
    calls = []

    async def fake_reload(rt, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)

    asyncio.run(runtime._rescan_mcp_gateway())

    assert len(calls) == 1
    # raise_on_failure is what makes the supervisor's backoff + last_error
    # introspection meaningful for this task.
    assert calls[0].get("raise_on_failure") is True


def test_tick_propagates_failure_so_the_supervisor_backs_off(runtime, monkeypatch):
    async def boom(rt, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", boom)

    with pytest.raises(RuntimeError, match="gateway down"):
        asyncio.run(runtime._rescan_mcp_gateway())


def test_reload_raises_for_the_watchdog_when_gateway_is_not_installed(runtime):
    """Best-effort by default (a config save must not fail over this), but
    the watchdog needs the signal — a silent no-op every 5 minutes is worse
    than no watchdog."""
    assert not runtime.is_loaded("mcp-gateway")

    _async(routes_mod._reload_mcp_gateway(runtime))  # default: swallowed

    with pytest.raises(RuntimeError, match="not installed"):
        _async(routes_mod._reload_mcp_gateway(runtime, raise_on_failure=True))


def test_boot_reloads_the_gateway_once_the_inprocess_apps_have_written_theirs(
        tmp_path, monkeypatch):
    """A plain restart changes nothing, so reconcile fires no coalesced
    reload -- and the gateway keeps a stale upstream set until the watchdog's
    first tick a full interval later. Measured live 2026-08-12: aw-diff-tool,
    aw-presentation and whiteboard were all missing from the gateway after a
    restart, so every agent got "Unknown tool" for them until a manual reload.
    """
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path / "home"))
    calls = []

    async def fake_reload(rt, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(routes_mod, "_reload_mcp_gateway", fake_reload)

    app = FastAPI()
    routes_mod.register_apps_routes(app)

    async def go():
        await routes_mod.reconcile_on_boot(app)
        app.state.app_runtime.watchdog.cancel_all_for(_SYSTEM_APP_ID)

    _async(go())

    assert len(calls) == 1
    # Best-effort: a gateway that is not listening yet must not turn a boot
    # into a failure -- the watchdog still covers it on its own schedule.
    assert calls[0].get("raise_on_failure") in (None, False)
