"""service:manage contribution point (F4).

An app granted ``service:manage`` registers a managed process the runtime can
start / stop / report status for, and uninstall stops every service the app
registered (no orphan processes). A short-lived ``sleep`` stands in for a real
service so the test is fast and self-contained.
"""
from __future__ import annotations

import asyncio
import textwrap
import uuid

from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime
from src.apps.service_lease import is_lease_held_anywhere
from src.apps.services import ServiceSupervisor


def _async(coro):
    return asyncio.run(coro)


_PLUGIN = """
    class AppPlugin:
        async def activate(self, ctx):
            ctx.services.register("worker", "sleep 30", autostart=True)
            ctx._probe = ctx.services.status("worker")
        async def deactivate(self):
            return None
"""


def _write_service_app(tmp_path):
    slug = "svc"
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
      "permissions": ["service:manage"]
    }}
    """))
    (pkg / "plugin.py").write_text(textwrap.dedent(_PLUGIN))
    return str(pkg)


def test_service_registers_autostarts_and_is_stopped_on_uninstall(tmp_path):
    pkg = _write_service_app(tmp_path)

    async def run():
        rt = AppRuntime(FastAPI(), journal=ActionJournal())
        await rt.load(pkg, granted_permissions=["service:manage"])

        # autostarted → running with a real pid
        st = rt.services.status("svc", "worker")
        assert st["running"] is True
        assert st["pid"]
        pid = st["pid"]

        # registration journaled
        kinds = [(e.kind, e.target) for e in rt.journal.entries_for("svc")]
        assert ("service:register", "worker") in kinds

        # stop/start round-trip
        rt.services.stop("svc", "worker")
        assert rt.services.status("svc", "worker")["running"] is False
        rt.services.start("svc", "worker")
        assert rt.services.status("svc", "worker")["running"] is True

        # uninstall stops + drops the service (no orphan)
        await rt.unload("svc")
        import os
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert alive is False

    _async(run())


def test_manual_start_on_non_owning_worker_does_not_spawn_a_duplicate(tmp_path):
    """Regression test for the cross-worker duplicate-spawn bug: two
    ServiceSupervisor instances simulate two AW_WORKSPACE_WORKERS processes
    sharing this workspace's real Redis (each worker has its own in-memory
    ServiceSupervisor, exactly like two real uvicorn worker processes do).

    Before the fix, ComponentRoutes.start_component/restart_component
    (src/api/components.py) called ServiceSupervisor.start()/stop() directly
    on the local worker with no ownership check — a manual start/restart
    landing on the non-owning worker spawned a REAL second subprocess that
    collided on the service's port with the one already running elsewhere.
    """
    suffix = uuid.uuid4().hex[:8]
    app_id = f"svc-{suffix}"
    service_id = "worker"
    pkg_dir = str(tmp_path)

    worker_a = ServiceSupervisor()
    worker_b = ServiceSupervisor()
    for w in (worker_a, worker_b):
        w.register(app_id, service_id, "sleep 30", pkg_dir, autostart=False)

    try:
        # Worker A is whichever worker actually starts it first (e.g. the
        # boot-time leader's autostart, or the first manual start request).
        status_a = worker_a.start(app_id, service_id)
        assert status_a["running"] is True
        pid_a = status_a["pid"]
        assert pid_a

        # Worker B's manual start (e.g. a UI "Start" click landed on it)
        # must NOT spawn a second subprocess — it must see the true
        # cross-worker state and refuse to duplicate.
        status_b = worker_b.start(app_id, service_id)
        assert status_b["running"] is True
        assert status_b.get("pid") is None  # nothing running locally on B
        assert status_b.get("remote_worker") is True

        # A "restart" landing on worker B must not touch A's real process
        # either — stop() is a local no-op, start() refuses to duplicate.
        worker_b.stop(app_id, service_id)
        status_b2 = worker_b.start(app_id, service_id)
        assert status_b2["running"] is True
        assert status_b2.get("remote_worker") is True

        # Worker A's process was never touched by any of worker B's calls.
        assert worker_a.status(app_id, service_id)["pid"] == pid_a
    finally:
        worker_a.stop(app_id, service_id)
        worker_b.stop(app_id, service_id)
        assert is_lease_held_anywhere(app_id, service_id) is False
