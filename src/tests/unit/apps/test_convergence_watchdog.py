"""Unit tests for the post-boot convergence reconcile watchdog
(``_start_convergence_watchdog`` in src/apps/routes.py).

Written from the 2026-10-08 incident (see ``boot_reconcile_coord``'s W5
note). The boot reconcile's own timeout branch used to log, accurately,
"there is no periodic reconcile watchdog today — convergence only resumes on
the next restart or a manual POST /api/apps/reconcile". With 60 apps
installing at concurrency 3, a boot that ran out of its 1200s budget left
the workspace permanently short of converged, and the only cure was another
boot — which restarted the same expensive pass from scratch. These tests pin
the three properties that make the watchdog a fix rather than a new source
of load: it is registered, it does not fire immediately, and it never runs
two passes at once.
"""
from __future__ import annotations

import asyncio
import types

from src.apps import routes


class _FakeWatchdog:
    """Records register() calls the way WatchdogSupervisor would, without
    starting any asyncio task — these tests drive the registered coroutine
    directly, so the supervisor's own loop/backoff (covered by its own
    tests) is out of scope here."""

    def __init__(self):
        self.registered: list[dict] = []

    def task_ids_for(self, app_id: str) -> list[str]:
        return [r["task_id"] for r in self.registered if r["app_id"] == app_id]

    def register(self, app_id, task_id, fn, interval_s, run_immediately):
        self.registered.append({
            "app_id": app_id, "task_id": task_id, "fn": fn,
            "interval_s": interval_s, "run_immediately": run_immediately,
        })


def _app(reconcile_impl):
    """Minimal stand-in for the FastAPI app object the function reads two
    attributes off (app_runtime.watchdog and app_reconciler)."""
    watchdog = _FakeWatchdog()
    return types.SimpleNamespace(state=types.SimpleNamespace(
        app_runtime=types.SimpleNamespace(watchdog=watchdog),
        app_reconciler=types.SimpleNamespace(reconcile=reconcile_impl),
    )), watchdog


async def _noop_reconcile():
    return {}


def test_watchdog_is_registered_under_the_core_app():
    app, watchdog = _app(_noop_reconcile)
    routes._start_convergence_watchdog(app, interval_s=900.0)

    assert len(watchdog.registered) == 1
    entry = watchdog.registered[0]
    assert entry["app_id"] == routes._CONVERGENCE_APP_ID
    assert entry["task_id"] == routes._CONVERGENCE_TASK_ID
    assert entry["interval_s"] == 900.0


def test_first_tick_is_not_immediate():
    """``run_immediately=True`` would re-run the reconcile seconds after the
    boot pass finished or gave up — i.e. exactly the competing-pass
    behaviour the W5 fix exists to stop."""
    app, watchdog = _app(_noop_reconcile)
    routes._start_convergence_watchdog(app)

    assert watchdog.registered[0]["run_immediately"] is False


def test_registration_is_idempotent():
    app, watchdog = _app(_noop_reconcile)
    routes._start_convergence_watchdog(app)
    routes._start_convergence_watchdog(app)

    assert len(watchdog.registered) == 1


def test_a_non_positive_interval_disables_it_entirely():
    """The escape hatch back to restart-only semantics."""
    app, watchdog = _app(_noop_reconcile)
    routes._start_convergence_watchdog(app, interval_s=0)

    assert watchdog.registered == []


def test_overlapping_ticks_never_run_two_passes_at_once():
    """A pass can outlast the interval. Two concurrent reconciles over one
    venv and one podman socket is the original stampede, so a tick that
    arrives while the previous one is still running must be skipped, not
    queued."""
    started = []
    release = asyncio.Event()

    async def slow_reconcile():
        started.append(1)
        await release.wait()
        return {}

    app, watchdog = _app(slow_reconcile)
    routes._start_convergence_watchdog(app)
    tick = watchdog.registered[0]["fn"]

    async def scenario():
        first = asyncio.ensure_future(tick())
        await asyncio.sleep(0.05)          # let the first pass get going
        await tick()                        # second tick, must be skipped
        assert len(started) == 1, "a second concurrent reconcile pass started"
        release.set()
        await first
        # Guard released: a later tick runs normally again.
        await tick()
        assert len(started) == 2

    asyncio.run(scenario())


def test_the_guard_is_released_even_when_a_pass_times_out():
    """A timed-out pass must not wedge the watchdog permanently — the whole
    point is that the NEXT tick picks up where it left off."""
    calls = []

    async def hanging_reconcile():
        calls.append(1)
        await asyncio.sleep(10)

    app, watchdog = _app(hanging_reconcile)
    routes._start_convergence_watchdog(app)
    tick = watchdog.registered[0]["fn"]

    async def scenario():
        # Shrink the per-pass cap so the timeout branch is reached fast.
        original = routes._BOOT_RECONCILE_TIMEOUT
        routes._BOOT_RECONCILE_TIMEOUT = 0.05
        try:
            await tick()      # times out internally, must NOT raise
            await tick()      # proves the in-flight guard was released
        finally:
            routes._BOOT_RECONCILE_TIMEOUT = original
        assert len(calls) == 2

    asyncio.run(scenario())


def test_a_timed_out_pass_does_not_propagate_to_the_supervisor_backoff():
    """Raising here would feed WatchdogSupervisor's min(interval*2**n, 1800s)
    backoff, stretching the interval out to 30min — punishing the
    slow-but-progressing case, where each pass installs some apps and
    leaves fewer for the next."""
    async def hanging_reconcile():
        await asyncio.sleep(10)

    app, watchdog = _app(hanging_reconcile)
    routes._start_convergence_watchdog(app)
    tick = watchdog.registered[0]["fn"]

    original = routes._BOOT_RECONCILE_TIMEOUT
    routes._BOOT_RECONCILE_TIMEOUT = 0.05
    try:
        asyncio.run(tick())   # must not raise
    finally:
        routes._BOOT_RECONCILE_TIMEOUT = original
