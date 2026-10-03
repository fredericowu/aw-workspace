"""Unit tests for BootReconcileCoordinator (src/apps/boot_reconcile_coord.py)
— Lock C, the flock + done-marker pair that decides which worker runs a
boot's app reconcile. No Redis anywhere: as of 2026-10-03 leadership here is
an ``fcntl.flock`` and only an flock (Frederico: "it's not a fallback, it's
the only logic").

Two contenders in ONE process is a legitimate shape for these tests, which it
would not be for a Redis lease: ``fcntl.flock`` is per-open-file-description,
so each ``coordinate()`` call opens its own fd and the two exclude each other
exactly as two worker processes would. The cross-PROCESS proof — real
``multiprocessing`` workers, which is the only thing that would have caught
the per-process-``asyncio.Lock`` bug this whole card came from — lives in
``src/tests/integration/apps/test_boot_reconcile_flock_multiprocess.py``.

Written from the W4 incident (workspace ``crispal``, 2026-09-17 — see the
module docstring in ``boot_reconcile_coord.py``): a worker respawned mid-boot
re-won an expired claim and started a SECOND full reconcile on top of a
running first one, up to 21 times over 2+ hours. The marker re-check after
winning the flock (``test_marker_written_between_check_and_acquire_...``) is
that incident in miniature, and the reason it is tested directly.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from src.apps import fs_lock
from src.apps.boot_reconcile_coord import DONE_PREFIX, BootReconcileCoordinator


@pytest.fixture
def lock_dir(tmp_path) -> str:
    return str(tmp_path / "locks")


def _coordinator(boot_id: str, lock_dir: str, **kw) -> BootReconcileCoordinator:
    return BootReconcileCoordinator(boot_id, lock_dir=lock_dir, **kw)


async def _run(coro):
    return await asyncio.wait_for(coro, timeout=10.0)


def test_first_caller_leads(lock_dir):
    leader = _coordinator("boot-1", lock_dir)
    assert asyncio.run(_run(leader.coordinate())) is True
    assert leader._fd is not None, "the leader must HOLD the lock, not just win it"


def test_follower_behind_a_live_leader_eventually_gives_up_and_leads(lock_dir):
    """No done marker ever appears and the leader never releases (a wedged
    pass) — the follower must not wait forever; it gives up at max_wait_s and
    runs its own pass rather than never attaching. Safe in a way it was not
    before Lock A existed: the two passes still serialize on the provisioning
    mutex one layer down."""
    leader = _coordinator("boot-1", lock_dir)
    follower = _coordinator("boot-1", lock_dir, max_wait_s=0.15, poll_interval=0.02)

    assert asyncio.run(_run(leader.coordinate())) is True

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is True
    assert 0.12 <= elapsed <= 0.6, f"gave up too fast or too slow: {elapsed}s"


def test_done_marker_short_circuits_a_follower_immediately(lock_dir):
    leader = _coordinator("boot-2", lock_dir)
    follower = _coordinator("boot-2", lock_dir, max_wait_s=5.0, poll_interval=0.01)

    assert asyncio.run(_run(leader.coordinate())) is True
    asyncio.run(_run(leader.finish()))

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is False
    assert elapsed < 1.0, "should have short-circuited on the done marker, not polled"


def test_a_working_leader_cannot_lose_its_own_claim(lock_dir):
    """What the heartbeat/renewal machinery used to buy, now for free. The
    lease this replaced had to keep proving liveness against a TTL — the gap
    that let a long pass lose its own claim. A held fd has no TTL, so a
    follower polling for ten lease-TTLs' worth of time still never wins."""
    leader = _coordinator("boot-3", lock_dir)
    follower = _coordinator("boot-3", lock_dir, max_wait_s=0.4, poll_interval=0.02)

    async def scenario():
        assert await leader.coordinate() is True
        # Any amount of elapsed time: nothing is being renewed, and nothing
        # needs to be.
        return await follower.coordinate(), fs_lock.holder(leader.lock_path)

    gave_up_and_led, breadcrumb = asyncio.run(_run(scenario()))
    assert gave_up_and_led is True, "the follower should have given up, not won"
    assert f"pid={os.getpid()}" in breadcrumb, (
        "the lock file must name its holder — that breadcrumb is how a "
        "minutes-long wait is diagnosed instead of looking like a hang")


def test_a_leader_that_dies_without_finishing_is_reclaimed_instantly(lock_dir):
    """An OOM kill mid-pass: no marker, and the fd gone. The kernel drops the
    flock the moment the holder's fd closes, so a follower reclaims on its
    very next poll — where the Redis lease had to wait out a whole TTL."""
    leader = _coordinator("boot-4", lock_dir)
    follower = _coordinator("boot-4", lock_dir, max_wait_s=3.0, poll_interval=0.03)

    assert asyncio.run(_run(leader.coordinate())) is True
    fs_lock.release(leader._fd)  # == the holding process dying
    leader._fd = None

    t0 = time.monotonic()
    result = asyncio.run(_run(follower.coordinate()))
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 0.5, f"reclaim took {elapsed}s — should be immediate"


def test_exactly_one_of_many_concurrent_racers_leads(lock_dir):
    """6 workers race for the same boot_id at once. The winner does a
    (simulated, fast) pass and finishes — every other racer must see the
    marker before its own max_wait_s runs out, and stay a follower."""
    boot_id = "boot-5"

    async def race(coordinator):
        leading = await coordinator.coordinate()
        if leading:
            await asyncio.sleep(0.02)  # stand-in for a fast real pass
            await coordinator.finish()
        return leading

    coordinators = [_coordinator(boot_id, lock_dir, max_wait_s=0.5, poll_interval=0.02)
                    for _ in range(6)]

    async def scenario():
        return await asyncio.gather(*(race(c) for c in coordinators))

    results = asyncio.run(_run(scenario()))
    assert sum(results) == 1, f"expected exactly one leader, got {sum(results)}"


def test_marker_written_between_check_and_acquire_does_not_re_lead(lock_dir):
    """W4, in miniature — and the one step of the algorithm that is mandatory
    rather than defensive.

    A follower checks the marker (absent), then acquires the flock. If the
    real leader finished in that window — marker written, flock released —
    the follower now "legitimately" holds leadership of a pass that is
    already done, and without the SECOND marker check it would run a full
    duplicate reconcile. That is exactly the shape of the respawned worker
    that re-won an expired claim 21 times.

    Simulated by making the first check miss and the marker appear before the
    acquire, which is what a real interleaving does.
    """
    leader = _coordinator("boot-6", lock_dir)
    follower = _coordinator("boot-6", lock_dir)

    real_is_done = follower._is_done
    calls = {"n": 0}

    def _is_done_then_finish():
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # first check: genuinely not done yet
        return real_is_done()

    async def scenario():
        assert await leader.coordinate() is True
        follower._is_done = _is_done_then_finish
        # The leader finishes in the window between the follower's first
        # check and its acquire.
        await leader.finish()
        return await follower.coordinate()

    assert asyncio.run(_run(scenario())) is False, (
        "the follower re-led a finished pass — the post-acquire marker "
        "re-check is missing or was removed")
    assert calls["n"] >= 2, "the post-acquire re-check never ran"
    assert follower._fd is None, "a follower must not keep the lock it probed"


def test_finish_sweeps_other_boots_markers_but_never_its_own(lock_dir):
    """Markers live in a host-mounted directory, so one per boot forever is a
    real leak. The leader drops the others on its way out — and only the
    others. (Lock FILES are never unlinked; markers carry no kernel state,
    which is why they are deliberately not ``.lock`` files.)"""
    os.makedirs(lock_dir, exist_ok=True)
    stale = os.path.join(lock_dir, f"{DONE_PREFIX}boot-ancient")
    with open(stale, "w", encoding="utf-8") as f:
        f.write("pid=1 at=0\n")

    leader = _coordinator("boot-7", lock_dir)

    async def scenario():
        assert await leader.coordinate() is True
        await leader.finish()

    asyncio.run(_run(scenario()))

    assert not os.path.exists(stale), "a previous boot's marker was left behind"
    assert os.path.exists(leader.done_marker), "this boot's own marker was swept"
    assert os.path.exists(leader.lock_path), (
        "the LOCK file must never be unlinked — unlink+recreate puts two "
        "holders on two inodes (see fs_lock.py)")


def test_a_fresh_boot_leads_even_with_a_previous_boots_marker_present(lock_dir):
    """A stale claim from a PREVIOUS boot must never block a fresh one — the
    reason this is keyed on ``boot_info.boot_id()`` rather than on something
    that can repeat (e.g. a reused low pid after an in-place restart).

    Stronger here than with the Redis lease it replaces: a *key* from a dead
    boot outlived its owner and had to be TTL'd out, where a flock from a
    dead boot does not exist at all — the kernel released it when the process
    died. Only the marker can cross boots, and it is boot-scoped.
    """
    boot_a = _coordinator("boot-a", lock_dir)
    boot_b = _coordinator("boot-b", lock_dir, max_wait_s=1.0, poll_interval=0.02)

    async def scenario():
        assert await boot_a.coordinate() is True
        await boot_a.finish()  # boot-a's container goes away: fd released
        return await boot_b.coordinate()

    assert asyncio.run(_run(scenario())) is True


def test_finish_releases_so_the_lock_is_free_for_the_next_boot(lock_dir):
    leader = _coordinator("boot-8", lock_dir)

    async def scenario():
        assert await leader.coordinate() is True
        await leader.finish()
        assert leader._fd is None
        # Free, as proved by taking it.
        fd = fs_lock.try_acquire(leader.lock_path)
        assert fd is not None, "finish() did not release the lock"
        fs_lock.release(fd)

    asyncio.run(_run(scenario()))


def test_finish_survives_an_unwritable_marker(lock_dir, monkeypatch):
    """Best-effort by contract: failing to record the marker just means a
    respawned worker re-runs the pass (the pre-W4 behaviour). It must never
    be the reason this worker's own boot raises out of its ``finally``."""
    leader = _coordinator("boot-9", lock_dir)

    def _boom():
        raise OSError("read-only file system")

    async def scenario():
        assert await leader.coordinate() is True
        monkeypatch.setattr(leader, "_write_marker", _boom)
        await leader.finish()  # must not raise

    asyncio.run(_run(scenario()))
    assert leader._fd is None, "the lock must be released even if the marker failed"
