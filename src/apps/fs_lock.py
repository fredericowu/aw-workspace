"""Cross-process file locks for the PROVISION half of W3.

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
