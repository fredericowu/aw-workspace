"""Cross-process file locks — the provisioning mutex AND leader election.

``src/apps/lifecycle.py`` explains *why* provisioning has to be serialized;
this is *how*. The original mechanism was a Redis ``SET NX PX`` key, which
degraded to nothing whenever Redis was unreachable — "the normal case today"
by that module's own docstring, and by ``src/libs/redis_coord.py``'s. With no
shared lock left, the only guard was a per-process :class:`asyncio.Lock`,
and ``AW_WORKSPACE_WORKERS=10`` means ten OS processes. Ten unguarded
``apt-get`` runs against one ``/var/cache/apt`` is what produced the reported
corruption: ``Could not open file /var/cache/apt/archives/...deb`` mid-dpkg,
across different apps and different workspaces.

``flock`` is the right primitive for exactly this shape:

* **Every worker is a process in ONE container sharing ONE filesystem.** That
  is what uvicorn ``--workers N`` means, so a file lock on that filesystem is
  a complete mutex over every resource provisioning touches (the venv,
  ``/var/cache/apt``, the podman socket, ``skills/``, ``.aw-workspace/bin``).
  Sibling containers that bind-mount ``/opt/aw-workspace`` get covered for
  free, same kernel, same inode.
* **There is no staleness to manage.** The kernel drops an ``flock`` when the
  holding open-file-description is closed — including on exit, crash and
  ``kill -9``. That is strictly better than the 900s TTL this replaced, where
  a killed holder locked everyone else out for fifteen minutes.
* **Per-open-file-description, not per-process.** ``fcntl.flock`` is why this
  module exists rather than ``fcntl.lockf``: POSIX record locks are released
  when the process closes *any* fd on the file, and are not held between
  threads of one process. Both matter here — the CLI healer runs installers on
  a worker thread (``asyncio.to_thread``) while the same process may be
  provisioning on its event loop.

**Never unlink a lock file.** Unlink-then-recreate puts two holders on two
different inodes, which is a mutex that silently stops being one. The files
are tiny, they persist, and the lock state does not.

The file's *contents* are not the lock — they are a breadcrumb
(``pid=… since=…``) so a wait that is taking minutes can say who it is
waiting for. :func:`holder` reads it best-effort.

Lock directory: ``<AW_WORKSPACE_HOME>/locks`` (persistent, host-mounted),
overridable per-instance (``AppLifecycle(lock_dir=…)``,
``CommandInstaller(lock_dir=…)``) and process-wide via ``AW_APPS_LOCK_DIR``.
The test suite sets that env var to a tmp dir for every test — a test that
took the LIVE lock could block, or block behind, a real provisioning pass.

**Leadership is a flock too, and only a flock** (2026-10-03, Frederico:
"for defining the leader, it should be flock only, it's not a fallback, it's
the only logic"). Everything above describes a mutex *around* work; the same
primitive also answers *who* does the work at all:

* :class:`FlockLease` — holding ``locks/<name>.lock`` IS being the leader
  (Lock D: the watchdog's periodic tasks). Replaces ``RedisLease("core")``,
  now deleted from ``src/libs/redis_coord.py``.
* ``src/apps/boot_reconcile_coord.py`` — holding ``locks/boot-reconcile.lock``
  IS leading this boot's app reconcile (Lock C).

Why that is strictly better than the Redis ``SET NX`` it replaces, and not
merely cheaper: a Redis call can fail *without carrying any fact* about who
holds the key, so every caller needed a degrade-open branch — and with Redis
unreachable by default here (``src/libs/redis_coord.py``'s own docstring) that
branch WAS the behaviour. Confirmed live: 45/45 samples of
``GET /api/apps/-/watchdog`` reported no leader at all. ``flock`` has no
reachability failure mode; ``EAGAIN`` *is* the fact that someone else holds
it, so the state is binary and "nobody is the leader" cannot be represented
while at least one worker is alive.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

#: Process-wide override for the lock directory. Set by ``src/tests/conftest.py``
#: so no test ever contends with the live workspace.
LOCK_DIR_ENV = "AW_APPS_LOCK_DIR"

#: How often to retry a lock that is currently held. Not a busy-wait: these
#: locks are held for whole installs (seconds to minutes), so a coarse poll
#: costs nothing and keeps the async path off the event loop's critical path.
POLL_S = 0.5

#: How often to log while still waiting. A wait on flock means a LIVE holder
#: (unlike the Redis key this replaced, which could outlive its owner), so a
#: long wait is normal during a cold boot reconcile — but it must still be
#: visible, with the holder's pid, rather than looking like a hang.
PROGRESS_S = 60.0


def lock_dir() -> str:
    """The directory lock files live in, created if missing."""
    override = os.environ.get(LOCK_DIR_ENV)
    if override:
        base = override
    else:
        from src.apps import paths

        base = os.path.join(paths.workspace_home(), "locks")
    os.makedirs(base, exist_ok=True)
    return base


def lock_path(name: str, base_dir: Optional[str] = None) -> str:
    """``<lock dir>/<name>.lock``. ``base_dir`` overrides :func:`lock_dir`."""
    base = base_dir or lock_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{name}.lock")


def holder(path: str) -> str:
    """Best-effort "who holds this" for a log line. Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _stamp(fd: int) -> None:
    """Write our pid + acquisition time into the file we just locked."""
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid={os.getpid()} since={time.time():.0f}\n".encode())


def _try_acquire(path: str) -> Optional[int]:
    """One non-blocking attempt. Returns the held fd, or ``None`` if busy.

    The fd IS the lock — keep it open for the whole hold and close it to
    release (see :func:`release`). Anything other than "someone else holds
    it" propagates: a lock dir that is read-only or full is a real problem,
    not something to silently proceed past.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    except BaseException:
        os.close(fd)
        raise
    _stamp(fd)
    return fd


def try_acquire(path: str) -> Optional[int]:
    """Public, non-blocking single attempt — the leader-election primitive.

    Same contract as the internal one the polling helpers use: the returned
    fd IS the lock (hold it, :func:`release` it), ``None`` means someone else
    holds it right now. Exposed because leadership is decided by *one* try,
    not by waiting: a worker that loses simply stands by (``FlockLease``, the
    boot-reconcile coordinator, the one-shot boot claims in
    ``src/api/app.py``), where a worker that loses the *provisioning* mutex
    has to wait its turn and do the work anyway.
    """
    return _try_acquire(path)


def release(fd: Optional[int]) -> None:
    """Drop the lock. Closing the fd is what releases it; never raises."""
    if fd is None:
        return
    with contextlib.suppress(OSError):
        os.close(fd)


def _on_progress(path: str, waited: float, timeout: float) -> None:
    log.warning(
        "apps: still waiting for the file lock %s after %.0fs of %.0fs — held by "
        "%s. This is another worker provisioning, not a hang; a cold install "
        "(apt/pip/image pull) legitimately takes minutes.",
        path, waited, timeout, holder(path))


async def acquire_async(path: str, *, timeout: float, poll_s: float = POLL_S,
                        progress_s: float = PROGRESS_S) -> Optional[int]:
    """Poll for the lock without blocking the event loop. ``None`` on timeout.

    Cancellation-safe: the only ``await`` in the loop is the sleep, which
    happens while holding no fd, and the one place an fd exists is handed
    straight back to the caller — but the ``finally`` is kept explicit so a
    future edit that adds an await after :func:`_try_acquire` cannot leak a
    held lock. ``asyncio.wait_for`` around the boot reconcile pass
    (``src/apps/routes.py``) makes cancellation here a real path, not a
    hypothetical one.
    """
    fd: Optional[int] = None
    started = time.monotonic()
    next_progress = started + progress_s
    try:
        while True:
            fd = _try_acquire(path)
            if fd is not None:
                return fd
            now = time.monotonic()
            if now - started >= timeout:
                return None
            if now >= next_progress:
                _on_progress(path, now - started, timeout)
                next_progress = now + progress_s
            await asyncio.sleep(min(poll_s, timeout - (now - started)))
    except BaseException:
        release(fd)
        raise


def acquire(path: str, *, timeout: float, poll_s: float = POLL_S,
            progress_s: float = PROGRESS_S) -> Optional[int]:
    """Blocking twin of :func:`acquire_async`, for code already on a thread.

    ``time.sleep`` rather than a plain blocking ``flock(LOCK_EX)`` so the
    progress logging and the deadline both still work — a bare blocking flock
    gives neither, and "waited forever, said nothing" is the failure mode this
    whole change exists to remove.
    """
    fd: Optional[int] = None
    started = time.monotonic()
    next_progress = started + progress_s
    try:
        while True:
            fd = _try_acquire(path)
            if fd is not None:
                return fd
            now = time.monotonic()
            if now - started >= timeout:
                return None
            if now >= next_progress:
                _on_progress(path, now - started, timeout)
                next_progress = now + progress_s
            time.sleep(min(poll_s, timeout - (now - started)))
    except BaseException:
        release(fd)
        raise


@contextlib.asynccontextmanager
async def async_held(path: str, *, timeout: float, poll_s: float = POLL_S,
                     progress_s: float = PROGRESS_S):
    """``async with`` wrapper yielding whether the lock was actually taken.

    Yields ``False`` on timeout rather than raising — whether that is
    acceptable is the caller's call, and the two callers in this codebase
    answer it differently (see ``AppLifecycle.provision_lock`` vs
    ``CommandInstaller._run``).
    """
    fd = await acquire_async(path, timeout=timeout, poll_s=poll_s,
                             progress_s=progress_s)
    try:
        yield fd is not None
    finally:
        release(fd)


@contextlib.contextmanager
def held(path: str, *, timeout: float, poll_s: float = POLL_S,
         progress_s: float = PROGRESS_S):
    """Blocking twin of :func:`async_held`."""
    fd = acquire(path, timeout=timeout, poll_s=poll_s, progress_s=progress_s)
    try:
        yield fd is not None
    finally:
        release(fd)


#: How often a standby re-contends for leadership. Also the failover bound:
#: a leader that dies has its flock dropped by the kernel instantly, so the
#: gap is just the next poll — against the 15s TTL ``RedisLease`` needed.
LEASE_POLL_S = 5.0


class FlockLease:
    """Single-leader election (Lock D) where holding the flock IS leading.

    Wired into ``src/api/app.py``'s lifespan as the gate on
    ``WatchdogSupervisor`` (``on_acquire`` → ``resume()``, ``on_release`` →
    ``pause()``) — the same callback contract ``RedisLease("core")`` had, so
    ``src/apps/watchdog.py`` did not change when this replaced it.

    How it differs from the lease it replaces, beyond the transport:

    * **No renewal, no TTL, no heartbeat.** The held open-file-description
      *is* the liveness proof. The kernel drops it on exit, crash and
      ``kill -9`` — nothing to renew, and no window in which a live leader
      can lose its own claim to a clock.
    * **Binary state, no "unknown".** ``RedisLease`` needed a third state
      because a raised connection error carries no fact about who holds the
      key, and that third state is precisely what made every worker fall
      back to "ungated, run everything locally". ``EAGAIN`` from ``flock``
      IS the fact that another process holds it, so a standby is a standby.
    * **Failover in one poll** (:data:`LEASE_POLL_S`), not one TTL.

    Only started at ``AW_WORKSPACE_WORKERS>1``: with one worker there is no
    rival, ``WatchdogSupervisor`` is ungated by default, and
    ``GET /api/apps/-/watchdog`` reports ``gate:"ungated"`` — which is what
    the absence of a lease on ``app.state`` already meant.
    """

    def __init__(
        self,
        name: str,
        *,
        lock_dir: Optional[str] = None,
        poll_interval: float = LEASE_POLL_S,
        on_acquire=None,
        on_release=None,
    ) -> None:
        self.name = name
        self.poll_interval = poll_interval
        self._lock_dir = lock_dir
        self._on_acquire = on_acquire
        self._on_release = on_release
        self._fd: Optional[int] = None
        self._task: Optional[asyncio.Task] = None
        # None only before start() — "no attempt made yet", not a steady
        # state the way RedisLease's "unknown" was. start() resolves it
        # inline before the lifespan proceeds, so nothing ever observes it.
        self._state: Optional[str] = None

    @property
    def path(self) -> str:
        return lock_path(self.name, self._lock_dir)

    @property
    def is_leader(self) -> bool:
        return self._state == "leader"

    async def start(self) -> None:
        """Contend once inline, then poll.

        The inline attempt matters: the lifespan continues immediately after
        this, and a standby must already have fired ``on_release`` by then —
        ``WatchdogSupervisor`` defaults to leader, so a worker that has not
        yet been told it lost is a worker running the periodic tasks.
        """
        if self._task is not None:
            raise RuntimeError("lease already started")
        await self._contend()
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Cancel the poll loop and drop the lock, so the next worker's poll
        can take over within :data:`LEASE_POLL_S` instead of waiting for this
        process to actually die.

        Deliberately does NOT fire ``on_release``: this runs in the lifespan's
        shutdown path, where pausing a supervisor that is about to be
        destroyed is noise, and a callback reaching into half-torn-down state
        is a real risk. Never raises — a shutdown must not hang or crash on
        the teardown of a lock we may never have held.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("apps: FlockLease(%s) poll loop raised during stop",
                              self.name)
            self._task = None
        release(self._fd)
        self._fd = None
        self._state = "standby"

    async def _contend(self) -> None:
        """One attempt, with the edge-triggered callbacks.

        Edge-triggered, not per-poll: a standby re-contends every
        :data:`LEASE_POLL_S` forever, and firing ``on_release`` on each losing
        attempt would call ``watchdog.pause()`` twelve times a minute on every
        non-leader worker for the life of the process.
        """
        fd = try_acquire(self.path)
        if fd is not None:
            self._fd = fd
            if self._state != "leader":
                self._state = "leader"
                log.warning("apps: FlockLease(%s) acquired — this worker (pid=%s) "
                            "is the leader", self.name, os.getpid())
                if self._on_acquire is not None:
                    await self._on_acquire()
            return
        if self._state != "standby":
            self._state = "standby"
            log.warning("apps: FlockLease(%s) held by %s — this worker (pid=%s) "
                        "is standby", self.name, holder(self.path), os.getpid())
            if self._on_release is not None:
                await self._on_release()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            # A live holder cannot lose an flock, so there is nothing to
            # re-check or renew — only a standby has anything to do.
            if self._state == "leader":
                continue
            try:
                await self._contend()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken lock dir (read-only, full) is the only way this
                # can fail, and it is not a reason to kill the loop: the
                # same bug once darkened a paused worker permanently, since
                # it never polled again to notice things had recovered (see
                # redis_coord's _run before the W1 fix).
                log.exception("apps: FlockLease(%s) attempt failed — staying %s, "
                              "will retry in %.0fs", self.name, self._state,
                              self.poll_interval)
