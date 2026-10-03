"""The two provisioning locks, proved across real OS PROCESSES.

This file exists because the bug it guards is invisible to any in-process
test. ``AW_WORKSPACE_WORKERS=10`` is ten uvicorn worker *processes* in one
container; the mutex that used to serialize their provisioning was a Redis
key with a per-process ``asyncio.Lock`` as its stated fallback, and Redis is
unreachable here by default. So the fallback was not one — ten workers ran
``apt-get`` against one ``/var/cache/apt`` and corrupted installs
(``Could not open file /var/cache/apt/archives/...deb``, reported 2026-10-03
across different apps and different workspaces).

An asyncio-only or threads-only test passes against that broken code. Two
processes do not. Hence ``multiprocessing``, and hence the assertions being
about *interleaving* rather than about the lock API returning True.

Three properties, one per corruption path:

* **Lock A** (``AppLifecycle.provision_lock``) — two workers' provisioning
  passes serialize. Covers boot reconcile, where all ten workers degrade open
  and run a full pass each (``_is_boot_provisioner``, ``src/api/app.py``).
* **Lock B** (``CommandInstaller._run``) — two workers' installer scripts
  serialize. Covers the CLI healer, which reaches ``apt`` *without* ever
  taking Lock A, on every worker, whenever W1's Redis leader lease can't be
  acquired.
* **A→B nests without deadlocking** — a provisioning pass holding A runs an
  installer that takes B, in two processes at once. The ordering is always
  A then B and never the reverse, which is what makes the pair safe; a test
  that only proved each lock separately would not catch an inversion.

Serialization is asserted from a trace file the locked body appends to:
``start``/``end`` must alternate. Two adjacent ``start`` lines are an
overlap, which is exactly the corruption window.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: How long each process stays inside the lock. Long enough that an unlocked
#: pair would reliably overlap (both are released from the same barrier), short
#: enough to keep the file quick.
HOLD_S = 1.5


# ---------------------------------------------------------------------------
# Trace file — the evidence. Appended to from inside the critical section.
# ---------------------------------------------------------------------------

def _mark(trace: str, event: str) -> None:
    """Append one event. ``O_APPEND`` on a short line is atomic enough here:
    the point is the ORDER of events across processes, not byte interleaving,
    and every line is written while its process believes it holds the lock."""
    with open(trace, "a", encoding="utf-8") as f:
        f.write(f"{event} {os.getpid()}\n")
        f.flush()


def _assert_serialized(trace: str, expected_sections: int) -> None:
    lines = [ln.split()[0] for ln in
             open(trace, encoding="utf-8").read().splitlines() if ln.strip()]
    assert len(lines) == expected_sections * 2, (
        f"expected {expected_sections} start/end pairs, got {lines}")
    for i, event in enumerate(lines):
        want = "start" if i % 2 == 0 else "end"
        assert event == want, (
            f"critical sections overlapped — event {i} is {event!r}, expected "
            f"{want!r}. Full trace: {lines}")


# ---------------------------------------------------------------------------
# Worker process bodies — module-level so they survive fork/spawn.
# ---------------------------------------------------------------------------

def _lock_a_worker(lock_dir: str, trace: str, barrier) -> None:
    """One worker taking Lock A around a 'provisioning pass'."""
    from src.apps.lifecycle import AppLifecycle

    lifecycle = AppLifecycle(lock_dir=lock_dir)

    async def body():
        barrier.wait()
        async with lifecycle.provision_lock("fs-lock-test") as held:
            assert held, "a free flock must be acquired, not timed out"
            _mark(trace, "start")
            await asyncio.sleep(HOLD_S)
            _mark(trace, "end")

    asyncio.run(body())


def _lock_b_worker(package_dir: str, lock_dir: str, barrier) -> None:
    """One worker running an installer script, i.e. Lock B only — the shape
    of a CLI-healer tick, which never passes through Lock A."""
    from src.apps.commands import CommandInstaller

    installer = CommandInstaller(timeout=60.0, lock_dir=lock_dir)
    barrier.wait()
    installer.run_installer(package_dir, "install.sh")


def _nested_worker(package_dir: str, lock_dir: str, trace: str, barrier) -> None:
    """A full provisioning pass: hold Lock A, then run an installer that
    takes Lock B from a worker thread — the real call shape
    (``ctx.commands.install_system_cli`` inside ``Reconciler._provisioning``).
    Deadlocks here if the two locks are ever taken in the opposite order."""
    from src.apps.commands import CommandInstaller
    from src.apps.lifecycle import AppLifecycle

    lifecycle = AppLifecycle(lock_dir=lock_dir)
    installer = CommandInstaller(timeout=60.0, lock_dir=lock_dir)

    async def body():
        barrier.wait()
        async with lifecycle.provision_lock("fs-lock-test") as held:
            assert held
            _mark(trace, "start")
            await asyncio.to_thread(installer.run_installer, package_dir, "install.sh")
            _mark(trace, "end")

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _installer_pkg(tmp_path, trace: str, hold_s: float = HOLD_S) -> str:
    """An app package whose 'installer' is a script that marks the trace,
    sleeps, and marks again — a stand-in for the ``apt-get`` run whose
    overlap is the actual bug."""
    pkg = tmp_path / "pkg"
    pkg.mkdir(exist_ok=True)
    script = pkg / "install.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "start $$" >> {trace}\n'
        f"sleep {hold_s}\n"
        f'echo "end $$" >> {trace}\n'
    )
    script.chmod(0o755)
    return str(pkg)


def _run_processes(target, args_list, timeout: float):
    procs = [multiprocessing.Process(target=target, args=args) for args in args_list]
    for p in procs:
        p.start()
    deadline = time.monotonic() + timeout
    try:
        for p in procs:
            p.join(max(0.1, deadline - time.monotonic()))
        for p in procs:
            assert not p.is_alive(), (
                f"process {p.pid} still running after {timeout}s — deadlock")
            assert p.exitcode == 0, f"process {p.pid} exited {p.exitcode}"
    finally:
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join(5)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def test_lock_a_serializes_two_provisioning_processes(tmp_path):
    """Two processes entering ``provision_lock`` concurrently must not
    overlap. Before this change both proceeded immediately (Redis down →
    ``_acquire_shared`` returned False → nothing but a per-process lock)."""
    lock_dir = str(tmp_path / "locks")
    trace = str(tmp_path / "trace-a.txt")
    barrier = multiprocessing.Barrier(2)

    _run_processes(_lock_a_worker,
                   [(lock_dir, trace, barrier)] * 2,
                   timeout=HOLD_S * 2 + 30)
    _assert_serialized(trace, expected_sections=2)


def test_lock_b_serializes_two_installer_processes(tmp_path):
    """Two processes running installer scripts must not overlap, with no
    provisioning lock anywhere in sight — this is the CLI-healer path, which
    has never taken Lock A and which all ten workers run when Redis is
    down."""
    lock_dir = str(tmp_path / "locks")
    trace = str(tmp_path / "trace-b.txt")
    pkg = _installer_pkg(tmp_path, trace)
    barrier = multiprocessing.Barrier(2)

    _run_processes(_lock_b_worker,
                   [(pkg, lock_dir, barrier)] * 2,
                   timeout=HOLD_S * 2 + 30)
    _assert_serialized(trace, expected_sections=2)


def test_nested_a_then_b_does_not_deadlock(tmp_path):
    """Two full provisioning passes (A held, installer taking B inside it)
    run concurrently: both must finish, and both the outer passes and the
    inner installers must be serialized.

    The outer trace proving 2 clean sections is also what proves no
    deadlock — a process stuck waiting on B while holding A would be killed
    by ``_run_processes``'s timeout and fail on ``exitcode``."""
    lock_dir = str(tmp_path / "locks")
    outer_trace = str(tmp_path / "trace-outer.txt")
    inner_trace = str(tmp_path / "trace-inner.txt")
    pkg = _installer_pkg(tmp_path, inner_trace)
    barrier = multiprocessing.Barrier(2)

    _run_processes(_nested_worker,
                   [(pkg, lock_dir, outer_trace, barrier)] * 2,
                   timeout=HOLD_S * 2 + 30)
    _assert_serialized(outer_trace, expected_sections=2)
    _assert_serialized(inner_trace, expected_sections=2)


def test_installer_lock_timeout_raises_instead_of_racing(tmp_path, monkeypatch):
    """Lock B must FAIL rather than proceed when it can't get the lock —
    the opposite of Lock A's proceed-anyway. An installer that runs anyway
    alongside another worker's ``apt`` is the corruption this prevents.

    Single-process on purpose: ``flock`` is per-open-file-description, so a
    second fd in this same process contends exactly like another worker's
    would — which is also what makes the healer's worker thread and the event
    loop's install genuinely exclude each other."""
    from src.apps import commands as commands_mod
    from src.apps import fs_lock

    monkeypatch.setattr(commands_mod, "INSTALLER_LOCK_MARGIN_S", 1.0)
    lock_dir = str(tmp_path / "locks")
    trace = str(tmp_path / "trace-timeout.txt")
    pkg = _installer_pkg(tmp_path, trace)

    installer = commands_mod.CommandInstaller(timeout=0.5, lock_dir=lock_dir)
    held_fd = fs_lock.acquire(
        fs_lock.lock_path(commands_mod.INSTALLER_LOCK_NAME, lock_dir), timeout=5)
    assert held_fd is not None
    try:
        with pytest.raises(commands_mod.CommandError, match="installer lock"):
            installer._run(pkg, "install.sh", what="installer")
    finally:
        fs_lock.release(held_fd)
    assert not os.path.exists(trace), "the script must not have run at all"
