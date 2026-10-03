"""Lock C across real OS PROCESSES: exactly one worker of a boot leads the
app reconcile, the rest attach.

Why ``multiprocessing`` and not asyncio tasks: ``AW_WORKSPACE_WORKERS>1`` is
N uvicorn worker *processes* in one container, and the whole class of bug
this card came from (card
``reliability:provisioning-lock-redis-fallback-race``) is invisible to an
in-process test — a per-process lock passes one and protects nothing in the
other. The in-process half of Lock C's behaviour (marker re-check, sweep,
give-up deadline) is unit-tested in
``src/tests/unit/apps/test_boot_reconcile_coord.py``; this file is the
cross-process proof.

Each worker here does what ``_boot_reconcile_and_sync`` does
(``src/api/app.py``): ``coordinate()``, run the "pass", ``finish()`` in a
``finally`` — and records what it decided, so the parent can assert on the
fleet as a whole rather than on one process's self-report.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: How long the "reconcile pass" takes. Long enough that every follower is
#: provably waiting on a LIVE leader rather than racing past a free lock.
PASS_S = 1.0


def _boot_worker(boot_id: str, lock_dir: str, result_dir: str, barrier) -> None:
    """One worker of one boot, shaped like app.py's _boot_reconcile_and_sync."""
    from src.apps.boot_reconcile_coord import BootReconcileCoordinator

    async def main():
        pid = os.getpid()
        coordinator = BootReconcileCoordinator(
            boot_id, lock_dir=lock_dir, max_wait_s=60.0, poll_interval=0.05)
        barrier.wait()
        leading = await coordinator.coordinate()
        verdict = "lead" if leading else "attach"
        if leading:
            try:
                # The "pass": mark the window so an overlap is detectable.
                with open(os.path.join(result_dir, "pass-trace"), "a",
                          encoding="utf-8") as f:
                    f.write(f"start {pid}\n")
                await asyncio.sleep(PASS_S)
                with open(os.path.join(result_dir, "pass-trace"), "a",
                          encoding="utf-8") as f:
                    f.write(f"end {pid}\n")
            finally:
                await coordinator.finish()
        with open(os.path.join(result_dir, str(pid)), "w", encoding="utf-8") as f:
            f.write(verdict)

    asyncio.run(main())


def _dying_leader(boot_id: str, lock_dir: str, barrier) -> None:
    """Wins leadership, then dies mid-pass without ever calling finish() —
    an OOM kill. ``os._exit`` skips every Python-level cleanup, so only the
    kernel's own fd teardown can release the lock."""
    from src.apps.boot_reconcile_coord import BootReconcileCoordinator

    async def main():
        coordinator = BootReconcileCoordinator(
            boot_id, lock_dir=lock_dir, max_wait_s=5.0, poll_interval=0.05)
        assert await coordinator.coordinate() is True
        barrier.wait()
        os._exit(0)

    asyncio.run(main())


def _run(procs, timeout: float) -> None:
    for p in procs:
        p.start()
    deadline = time.monotonic() + timeout
    try:
        for p in procs:
            p.join(max(0.1, deadline - time.monotonic()))
        for p in procs:
            assert not p.is_alive(), f"process {p.pid} still running after {timeout}s"
            assert p.exitcode == 0, f"process {p.pid} exited {p.exitcode}"
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join(5)


def _verdicts(result_dir: str) -> dict[str, str]:
    out = {}
    for name in os.listdir(result_dir):
        if name == "pass-trace":
            continue
        with open(os.path.join(result_dir, name), encoding="utf-8") as f:
            out[name] = f.read().strip()
    return out


def test_exactly_one_worker_of_a_boot_leads_the_reconcile(tmp_path):
    """Five workers of ONE boot, released from a barrier together. One runs
    the pass; the other four must see the done marker and attach — never run
    a second pass of their own.

    With the Redis coordinator this replaced, an unreachable Redis (the
    normal case here) made ``_is_boot_provisioner`` degrade open and every
    one of the five lead concurrently."""
    lock_dir = str(tmp_path / "locks")
    result_dir = str(tmp_path / "results")
    os.makedirs(result_dir, exist_ok=True)
    n = 5

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(n)
    _run([ctx.Process(target=_boot_worker,
                      args=("boot-mp-1", lock_dir, result_dir, barrier))
          for _ in range(n)],
         timeout=PASS_S + 60)

    verdicts = _verdicts(result_dir)
    assert len(verdicts) == n, f"only {len(verdicts)} of {n} reported: {verdicts}"
    leaders = [pid for pid, v in verdicts.items() if v == "lead"]
    assert len(leaders) == 1, f"expected exactly 1 leader, got {leaders}: {verdicts}"

    trace = [ln.split()[0] for ln in
             open(os.path.join(result_dir, "pass-trace"), encoding="utf-8")
             .read().splitlines() if ln.strip()]
    assert trace == ["start", "end"], f"a second pass ran: {trace}"


def test_a_leader_that_dies_mid_pass_is_replaced_not_waited_out(tmp_path):
    """The failover the lease's TTL used to buy, now from the kernel. A
    leader that dies without finishing leaves no marker and no flock, so the
    next worker leads immediately — and runs the pass that never completed,
    which is the correct outcome (an unfinished reconcile must still
    happen)."""
    lock_dir = str(tmp_path / "locks")
    result_dir = str(tmp_path / "results")
    os.makedirs(result_dir, exist_ok=True)

    ctx = multiprocessing.get_context("fork")
    gate = ctx.Barrier(2)  # the dying leader + this parent
    dying = ctx.Process(target=_dying_leader, args=("boot-mp-2", lock_dir, gate))
    dying.start()
    gate.wait()  # leadership is held at this point
    dying.join(timeout=10)
    assert not dying.is_alive()

    # Now a fresh worker of the SAME boot.
    solo = ctx.Barrier(1)
    t0 = time.monotonic()
    _run([ctx.Process(target=_boot_worker,
                      args=("boot-mp-2", lock_dir, result_dir, solo))],
         timeout=PASS_S + 30)
    elapsed = time.monotonic() - t0

    verdicts = _verdicts(result_dir)
    assert list(verdicts.values()) == ["lead"], (
        f"the dead leader's pass was never picked up: {verdicts}")
    assert elapsed < PASS_S + 10, (
        f"took {elapsed:.1f}s — looks like it waited out a timeout rather "
        "than finding the lock already free")


def test_a_worker_respawned_after_the_pass_finished_attaches(tmp_path):
    """W4, cross-process: a worker that starts LATER in the same boot — the
    uvicorn respawn that re-ran the whole reconcile 21 times — must see this
    boot's done marker and attach, even though the lock is now free."""
    lock_dir = str(tmp_path / "locks")
    first_dir = str(tmp_path / "first")
    late_dir = str(tmp_path / "late")
    os.makedirs(first_dir, exist_ok=True)
    os.makedirs(late_dir, exist_ok=True)

    ctx = multiprocessing.get_context("fork")
    solo = ctx.Barrier(1)
    _run([ctx.Process(target=_boot_worker,
                      args=("boot-mp-3", lock_dir, first_dir, solo))],
         timeout=PASS_S + 30)
    assert list(_verdicts(first_dir).values()) == ["lead"]

    late = ctx.Barrier(1)
    _run([ctx.Process(target=_boot_worker,
                      args=("boot-mp-3", lock_dir, late_dir, late))],
         timeout=30)
    assert list(_verdicts(late_dir).values()) == ["attach"], (
        "a respawned worker re-led a finished pass — the done marker is not "
        "doing its job (W4)")
