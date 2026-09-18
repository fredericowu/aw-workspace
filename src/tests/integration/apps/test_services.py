"""service:manage contribution point (F4).

An app granted ``service:manage`` registers a managed process the runtime can
start / stop / report status for, and uninstall stops every service the app
registered (no orphan processes). A short-lived ``sleep`` stands in for a real
service so the test is fast and self-contained.
"""
from __future__ import annotations

import asyncio
import os
import textwrap
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src.apps.journal import ActionJournal
from src.apps.runtime import AppRuntime
from src.apps.service_lease import is_lease_held_anywhere
from src.apps.service_relay import ServiceCommandRelay
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


def test_restart_on_the_owner_keeps_the_lease_for_the_new_process(tmp_path):
    """Regression: the old process's log pump must not release the NEW
    process's lease.

    ``ServiceSupervisor.start()``'s reader-thread closure used to read
    ``svc.lease`` at process-EXIT time. A restart is ``stop()`` then
    ``start()`` back-to-back: ``stop()`` returns as soon as ``proc.wait()``
    does, ``start()`` acquires a fresh lease, and the dead process's pump
    then reaches its ``finally`` and released whatever ``svc.lease`` happened
    to hold by then — the new process's claim. The result was a RUNNING
    service with no ownership record: ``status()`` reports it off fleet-wide,
    and the next worker to click Start spawns exactly the duplicate the lease
    exists to prevent. The fix captures the lease at spawn and clears the
    field only on an identity match.
    """
    suffix = uuid.uuid4().hex[:8]
    app_id = f"svc-{suffix}"
    service_id = "worker"

    # The interleaving has to be FORCED, not waited for. With a plain `sleep`
    # the log pipe closes the instant the process dies, so the old pump
    # usually reaches its finally before the restart's start() has acquired
    # the new lease — and then the bug is invisible. `setsid sleep 5`
    # inherits the captured stdout but lands in its OWN session, so stop()'s
    # killpg cannot reach it and the pipe stays open for 5s after the service
    # process itself is gone. That puts the old pump's finally deterministically
    # AFTER the new lease exists, which is exactly the window a real service
    # with a surviving child (or a merely slow thread) hits by chance.
    worker = ServiceSupervisor()
    worker.register(app_id, service_id, "sh -c 'setsid sleep 5 & echo ready; sleep 60'",
                    str(tmp_path), autostart=False)
    try:
        first = worker.start(app_id, service_id)
        assert first["running"] is True
        assert is_lease_held_anywhere(app_id, service_id) is True
        old_pump = worker._require(app_id, service_id)._reader_thread
        # Wait for the detached child to actually exist: restarting faster
        # than sh can fork it would kill the whole tree at once and quietly
        # hand back the easy interleaving this test exists to avoid.
        for _ in range(200):
            if "ready" in worker.logs(app_id, service_id):
                break
            time.sleep(0.02)
        else:
            pytest.fail("the service never reported its detached child")

        second = worker.restart(app_id, service_id)
        assert second["running"] is True
        assert second["pid"] != first["pid"]

        # The forced interleaving, asserted rather than assumed: if this pump
        # had already finished, the detached child never held the pipe (no
        # `setsid`? no fork yet?) and everything below would pass vacuously,
        # proving nothing about the fix.
        assert old_pump.is_alive() is True, (
            "the old process's log pipe closed before the restart completed — "
            "this test cannot reach the window the bug lives in")

        # Now let it run its finally to completion.
        old_pump.join(timeout=30)
        assert old_pump.is_alive() is False

        assert is_lease_held_anywhere(app_id, service_id) is True, (
            "the old process's pump released the NEW process's lease — the "
            "service is running with no ownership record, so status() reports "
            "it off fleet-wide and the next worker to click Start duplicates it")
        assert worker.owns_locally(app_id, service_id) is True
    finally:
        worker.stop(app_id, service_id)
        assert is_lease_held_anywhere(app_id, service_id) is False


def test_restart_dispatched_at_a_non_owning_worker_restarts_the_real_process(tmp_path):
    """The point of the whole card: two workers, one real Redis, and a
    ``restart`` issued against the worker that owns NOTHING must change the
    pid of the process owned by the other one.

    Two ``ServiceSupervisor`` + two ``ServiceCommandRelay`` instances stand in
    for two ``AW_WORKSPACE_WORKERS`` processes, exactly as the duplicate-spawn
    test above does — each real worker constructs its own at import time; here
    we construct two explicitly to share one pytest process and one Redis.
    They share no Python state that matters: B resolves the owner purely
    through Redis PUBLISH/PSUBSCRIBE, and A decides for itself that the
    command is its to execute.

    Before this change, B's restart was a safe no-op — it confirmed the
    service was running somewhere and left A's process untouched, which is
    what a human clicking Restart hit ~9 times out of 10.
    """
    suffix = uuid.uuid4().hex[:8]
    app_id = f"svc-{suffix}"
    service_id = "worker"

    worker_a = ServiceSupervisor()
    worker_b = ServiceSupervisor()
    for w in (worker_a, worker_b):
        w.register(app_id, service_id, "sleep 30", str(tmp_path), autostart=False)

    relay_a = ServiceCommandRelay(SimpleNamespace(services=worker_a), timeout=15.0)
    relay_b = ServiceCommandRelay(SimpleNamespace(services=worker_b), timeout=15.0)

    async def run():
        # start/stop both relays inside ONE loop: a RedisBroadcaster's client
        # and pubsub belong to the loop that created them.
        assert await relay_a.start() is True, "this test needs a reachable Redis"
        assert await relay_b.start() is True
        try:
            await _exercise()
        finally:
            await relay_a.stop()
            await relay_b.stop()

    async def _exercise():
        started = worker_a.start(app_id, service_id)
        pid_a = started["pid"]
        assert pid_a
        assert worker_a.owns_locally(app_id, service_id) is True
        assert worker_b.owns_locally(app_id, service_id) is False

        result = await relay_b.dispatch(app_id, service_id, "restart")

        # B never spawned anything of its own...
        assert worker_b.status(app_id, service_id)["pid"] is None
        # ...and A's real process was genuinely replaced.
        assert result["forwarded"] is True
        assert result["pid"] != pid_a
        assert worker_a.status(app_id, service_id)["pid"] == result["pid"]
        assert worker_a.owns_locally(app_id, service_id) is True
        # The old process is gone — a restart, not a second copy.
        with pytest.raises(ProcessLookupError):
            os.kill(pid_a, 0)

        # A start dispatched at B while A owns it stays a no-op on B and
        # leaves A's pid alone (forwarded to A, whose start() sees it running).
        again = await relay_b.dispatch(app_id, service_id, "start")
        assert again["pid"] == result["pid"]
        assert worker_b.status(app_id, service_id)["pid"] is None

    try:
        _async(run())
    finally:
        worker_a.stop(app_id, service_id)
        worker_b.stop(app_id, service_id)
        assert is_lease_held_anywhere(app_id, service_id) is False
