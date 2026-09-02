"""The boot reconcile pass must not freeze the event loop.

This is the trigger behind the 2026-09-02 incident: four unrelated GET routes
returned 200 but took 23-25s each right after a restart. ``get_session`` is a
SYNCHRONOUS session over sync psycopg and the process runs ONE uvicorn worker
(``AW_WORKSPACE_WORKERS=1``), so the single event-loop thread serves every
request. ``Reconciler.reconcile()`` walks every installed app SERIALLY (47 in
the live workspace) and used to do each ``LocalMirror`` read/write inline —
dozens of blocking DB round-trips on the one thread that also answers HTTP.

The sibling fix (commits 89f7b99 → 9f3d94c) moved the boot reconcile to a
background task so it no longer gates *startup*; it did not change what the
pass does while running. This test covers that residual half: while the
mirror is blocked, the loop must stay free to run other work.

No Postgres and no HTTP server needed — a heartbeat coroutine is a direct
measure of whether the loop is still turning, which is exactly the property
that broke.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.apps.reconciler import Reconciler


class _BlockingMirror:
    """A ``LocalMirror`` whose DB read blocks, standing in for a slow psycopg
    round-trip. ``released`` is set by the test to let it finish."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.entered = threading.Event()
        self.calls = 0

    def list(self):
        self.calls += 1
        self.entered.set()
        # Blocks the calling thread — the whole question is WHICH thread.
        self.released.wait(timeout=30)
        return []


class _FakeCloud:
    configured = False


class _FakeRuntime:
    def loaded_slugs(self):
        return []

    def get(self, _app_id):
        return None

    def is_loaded(self, _app_id):
        return False


@pytest.mark.asyncio
async def test_reconcile_keeps_the_event_loop_responsive():
    mirror = _BlockingMirror()
    reconciler = Reconciler(
        runtime=_FakeRuntime(), cloud=_FakeCloud(), local=mirror,
        fetch=lambda *a, **k: "", remove=lambda *a, **k: False,
    )

    ticks = 0

    async def heartbeat() -> None:
        """Stands in for every other in-flight request. If the blocking mirror
        read runs ON the loop thread, this stops being scheduled entirely."""
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    sampled: dict[str, int] = {}

    def observer() -> None:
        """Samples the heartbeat from a plain OS thread — deliberately NOT a
        coroutine. An in-loop observer cannot measure a frozen loop: its own
        `await` would not resume until the freeze was already over, which is
        how the first version of this test passed against the unfixed code.
        """
        mirror.entered.wait(timeout=10)
        before = ticks
        time.sleep(0.3)          # wall-clock, independent of the loop
        sampled["delta"] = ticks - before
        mirror.released.set()

    beat = asyncio.create_task(heartbeat())
    # Let the heartbeat prove it is running before anything blocks.
    await asyncio.sleep(0.05)
    assert ticks > 0, "heartbeat never started; the test itself is broken"

    watcher = threading.Thread(target=observer, daemon=True)
    watcher.start()
    try:
        await reconciler.reconcile()
    finally:
        mirror.released.set()
        watcher.join(timeout=5)
        beat.cancel()

    assert mirror.entered.is_set(), "reconcile never reached the local mirror"
    assert sampled.get("delta", 0) > 0, (
        "the event loop was frozen for the whole time LocalMirror.list() "
        "blocked — the reconcile pass is doing synchronous DB work on the "
        "loop thread, which stalls every concurrent request "
        "(AW_WORKSPACE_WORKERS=1 means one loop thread serves them all)"
    )
