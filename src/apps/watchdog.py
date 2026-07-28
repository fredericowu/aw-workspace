"""In-process periodic-task supervisor (ADR F6 Capability 3, gated by the new
``watchdog:tasks`` capability) — distinct from ``service:manage``.

``ServiceSupervisor`` (F4) manages **subprocesses** (``Popen``). A watchdog task
is an **in-process async loop**: an ``async`` callable the runtime runs on a
fixed cadence, with a cache/broadcast side effect owned by the app (e.g. the
``GitHubWatchdog`` poller ported into aw-app-git). Each task is one
``asyncio.Task``: run ``fn`` → sleep ``interval`` → repeat; on exception it logs
and applies exponential backoff (``min(interval * 2**n, 1800s)``) that resets on
the next success, so a wedged endpoint (gh logged out) can't crash-loop.

On app uninstall the runtime cancels every task the app registered
(``cancel_all_for`` — called BEFORE drain so the app stops producing while its
sockets close); the journal reverse-replay cancels again idempotently.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 1800.0


class WatchdogError(RuntimeError):
    pass


class _Task:
    def __init__(self, app_id: str, task_id: str,
                 fn: Callable[[], Awaitable[Any]],
                 interval_s: float | Callable[[], float],
                 run_immediately: bool) -> None:
        self.app_id = app_id
        self.task_id = task_id
        self.fn = fn
        self.interval_s = interval_s
        self.run_immediately = run_immediately
        self.task: asyncio.Task | None = None
        # introspection
        self.last_run: float | None = None
        self.last_ok: bool | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self.next_run: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.app_id, self.task_id)

    def interval(self) -> float:
        """Resolve the cadence — a callable is re-evaluated every cycle so a
        config change (e.g. ``poll_interval``) applies live."""
        raw = self.interval_s() if callable(self.interval_s) else self.interval_s
        return max(0.0, float(raw))

    def snapshot(self) -> dict[str, Any]:
        return {
            "app": self.app_id,
            "task_id": self.task_id,
            "last_run": self.last_run,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "next_run": self.next_run,
        }


class WatchdogSupervisor:
    """Runtime-owned registry + lifecycle for apps' periodic tasks."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], _Task] = {}

    def register(self, app_id: str, task_id: str,
                 fn: Callable[[], Awaitable[Any]],
                 interval_s: float | Callable[[], float],
                 run_immediately: bool = True) -> dict[str, Any]:
        key = (app_id, task_id)
        if key in self._tasks:
            raise WatchdogError(f"watchdog task {task_id!r} already registered for {app_id!r}")
        if not callable(fn):
            raise WatchdogError("watchdog fn must be an async callable")
        t = _Task(app_id, task_id, fn, interval_s, run_immediately)
        self._tasks[key] = t
        t.task = asyncio.ensure_future(self._run(t))
        log.info("apps: registered watchdog %s/%s", app_id, task_id)
        return {"task_id": task_id, "registered": True}

    async def _run(self, t: _Task) -> None:
        """One task's loop: run ``fn`` on cadence, back off on failure."""
        if not t.run_immediately:
            interval = t.interval()
            t.next_run = time.time() + interval
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
        while True:
            t.last_run = time.time()
            try:
                await t.fn()
                t.last_ok = True
                t.last_error = None
                t.consecutive_failures = 0
                delay = t.interval()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — surfaced via introspection
                t.last_ok = False
                t.last_error = f"{type(e).__name__}: {e}"
                t.consecutive_failures += 1
                base = t.interval()
                delay = min(base * (2 ** t.consecutive_failures), MAX_BACKOFF_S)
                log.warning("apps: watchdog %s/%s failed (%d in a row), backing off %.0fs: %s",
                            t.app_id, t.task_id, t.consecutive_failures, delay, e)
            t.next_run = time.time() + delay
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    def cancel_all_for(self, app_id: str) -> None:
        """Cancel + drop every task an app registered (uninstall / unload)."""
        for key in [k for k in self._tasks if k[0] == app_id]:
            t = self._tasks.pop(key)
            if t.task is not None and not t.task.done():
                t.task.cancel()

    def snapshot(self) -> list[dict[str, Any]]:
        """Introspection for ``GET /api/apps/-/watchdog``."""
        return [t.snapshot() for t in self._tasks.values()]

    def task_ids_for(self, app_id: str) -> list[str]:
        return [k[1] for k in self._tasks if k[0] == app_id]
