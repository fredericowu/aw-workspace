"""WatchdogSupervisor (F6 Capability 3): periodic run, failure backoff + reset,
live callable interval, cancel-on-unload, and the introspection endpoint.
"""
from __future__ import annotations

import asyncio
import textwrap

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.identity import require_identity
from src.apps.journal import ActionJournal
from src.apps.routes import register_apps_routes
from src.apps.runtime import AppRuntime
from src.apps.watchdog import WatchdogSupervisor


def _async(coro):
    return asyncio.run(coro)


def _write_app(tmp_path, slug, plugin_src, permissions):
    pkg = tmp_path / slug
    pkg.mkdir()
    (pkg / "aw-app.json").write_text(textwrap.dedent(f"""
    {{
      "manifest_version": 1,
      "id": "{slug}",
      "name": "{slug}",
      "version": "1.0.0",
      "tier": "inprocess",
      "runtime": {{"entrypoint": "plugin:AppPlugin"}},
      "permissions": {permissions},
      "contributes": {{}}
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(plugin_src))
    return str(pkg)


def test_facade_registers_and_unload_cancels(tmp_path):
    """ctx.watchdog.register journals + runs; unload cancels the task (no residue)."""
    plugin_src = """
        class AppPlugin:
            async def activate(self, ctx):
                async def tick():
                    pass
                ctx.watchdog.register("poll", tick, 0.01)
            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "wdapp", plugin_src, '["watchdog:tasks"]')

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["watchdog:tasks"])
        await asyncio.sleep(0.03)
        assert rt.watchdog.task_ids_for("wdapp") == ["poll"]
        assert any(e.kind == "watchdog:register" and e.target == "poll"
                   for e in rt.journal.entries_for("wdapp"))

        await rt.unload("wdapp")
        assert rt.watchdog.task_ids_for("wdapp") == []  # cancelled, no residue
        assert rt.journal.entries_for("wdapp") == []

    _async(run())


def test_facade_denied_without_grant(tmp_path):
    plugin_src = """
        class AppPlugin:
            async def activate(self, ctx):
                async def tick():
                    pass
                ctx.watchdog.register("poll", tick, 1.0)  # no grant → raises
            async def deactivate(self):
                return None
    """
    pkg = _write_app(tmp_path, "nowd", plugin_src, '[]')

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        raised = False
        try:
            await rt.load(pkg, granted_permissions=[])
        except PermissionError:
            raised = True
        assert raised
        assert not rt.is_loaded("nowd")

    _async(run())


def test_register_runs_periodically_then_cancel():
    async def run():
        wd = WatchdogSupervisor()
        counter = {"n": 0}

        async def fn():
            counter["n"] += 1

        wd.register("app", "t", fn, 0.01)
        await asyncio.sleep(0.06)
        assert counter["n"] >= 2

        snap = wd.snapshot()
        assert len(snap) == 1
        assert snap[0]["task_id"] == "t" and snap[0]["last_ok"] is True

        wd.cancel_all_for("app")
        assert wd.snapshot() == []
        frozen = counter["n"]
        await asyncio.sleep(0.04)
        assert counter["n"] == frozen  # cancelled → no more runs

    _async(run())


def test_failure_backs_off_then_resets_on_success():
    async def run():
        wd = WatchdogSupervisor()
        state = {"fail": True}

        async def fn():
            if state["fail"]:
                raise RuntimeError("boom")

        wd.register("a", "t", fn, 0.01)
        await asyncio.sleep(0.08)
        snap = wd.snapshot()[0]
        assert snap["last_ok"] is False
        assert snap["consecutive_failures"] >= 1
        assert "boom" in (snap["last_error"] or "")

        state["fail"] = False
        await asyncio.sleep(0.5)  # let the backed-off retry fire and succeed
        snap2 = wd.snapshot()[0]
        assert snap2["last_ok"] is True
        assert snap2["consecutive_failures"] == 0

        wd.cancel_all_for("a")

    _async(run())


def test_callable_interval_reevaluated_each_cycle():
    async def run():
        wd = WatchdogSupervisor()
        interval = {"v": 0.01}
        runs = {"n": 0}

        async def fn():
            runs["n"] += 1

        wd.register("a", "t", fn, lambda: interval["v"])
        await asyncio.sleep(0.06)
        n1 = runs["n"]
        assert n1 >= 2  # fast cadence

        interval["v"] = 5.0  # widen live → next cycle sleeps 5s
        await asyncio.sleep(0.05)
        assert runs["n"] <= n1 + 1  # at most the already-scheduled cycle fired

        wd.cancel_all_for("a")

    _async(run())


def test_duplicate_registration_rejected():
    async def run():
        wd = WatchdogSupervisor()

        async def fn():
            pass

        wd.register("a", "t", fn, 1.0)
        try:
            wd.register("a", "t", fn, 1.0)
            raised = False
        except Exception:
            raised = True
        assert raised
        wd.cancel_all_for("a")

    _async(run())


def test_watchdog_introspection_endpoint():
    app = FastAPI()
    runtime = register_apps_routes(app)
    app.dependency_overrides[require_identity] = lambda: {"sub": "t"}
    runtime.watchdog.snapshot = lambda: [{
        "app": "git", "task_id": "pr-poll", "last_run": 1.0, "last_ok": True,
        "last_error": None, "consecutive_failures": 0, "next_run": 301.0,
    }]
    body = TestClient(app).get("/api/apps/-/watchdog").json()
    assert body["tasks"][0]["task_id"] == "pr-poll"
    assert body["tasks"][0]["app"] == "git"
