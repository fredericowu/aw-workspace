"""Coordinates AW_WORKSPACE_WORKERS>1 workers so exactly ONE runs the
expensive boot-time app reconcile per fleet boot — even across a uvicorn
worker respawn within that same boot.

**The incident this exists for.** ``_is_boot_provisioner``
(``src/api/app.py``) originally called ``redis_coord.cooldown_acquire`` — a
one-shot ``SET NX EX`` with no renewal ("no release, it just expires"). Its
120s window was picked to cover "how far apart the workers of ONE boot
start", but a live boot reconcile pass over the full app manifest measures
450s+ (``src/apps/routes.py``'s ``_BOOT_RECONCILE_TIMEOUT = 1200.0`` is
sized with margin over that). Any worker respawned by uvicorn AFTER the 120s
window elapsed — because it was SIGKILLed for missing a liveness ping under
memory pressure, say — re-called ``_is_boot_provisioner`` with the SAME
inherited ``boot_id`` (see ``src/api/boot_info.py``), found the claim already
expired, and "won" it again: a SECOND full reconcile pass (47 GitHub
fetches, pip installs, podman starts) starting on top of a FIRST one that
might still be running. Live 2026-09-17 (workspace ``crispal``): up to 21
such passes over 2+ hours, each one's rename-swap window
(``src/apps/fetch.py``) and per-worker route-mounting race causing the
installed-apps list to flap between "added" and "removed" on every
mcp-gateway reload, forever, and the redundant concurrent installs
themselves being a large share of what drove the container to 99% memory /
88% of its PID ceiling.

**The mechanism (Lock C, 2026-10-03).** Two FILES under
``<AW_WORKSPACE_HOME>/locks`` (``src/apps/fs_lock.py``), no Redis anywhere —
leadership is decided by ``flock`` and nothing else, per Frederico's
instruction: *"for defining the leader, it should be flock only, it's not a
fallback, it's the only logic"*.

``boot-reconcile.lock``
    Held — as an open fd — by the ACTIVE leader for the whole pass. **The fd
    IS the heartbeat**, which is why the lease-renewal half of the W4 fix
    (``start_heartbeat``/``stop_heartbeat``/a Lua CAS renewal and two TTLs)
    is gone rather than ported: a Redis key has to keep proving liveness
    because it expires on a clock, where the kernel holds a flock exactly as
    long as the process lives and drops it the instant it dies — exit, crash
    or ``kill -9``. A leader that dies mid-pass releases instantly instead of
    after a TTL; a leader still working can never be preempted.

``boot-reconcile-done-<boot_id>``
    Written ONCE, atomically (tmp + ``os.replace``), when the leader's pass
    ends — success, exception, or its own internal timeout give-up. This is
    the one W4 fact a flock cannot express: *the pass already finished, so a
    worker respawned later in this same boot must not re-lead.* A held fd
    says "in progress"; the marker says "done". Two facts, two file
    primitives, and no renewal code between them.

    NOT a ``.lock`` file, deliberately: ``fs_lock``'s never-unlink rule
    exists because unlink-then-recreate puts two holders on two inodes.
    Markers are the opposite — they must be removable, and the leader
    unlinks the markers of OTHER boot_ids (only those) on its way out so
    they don't accumulate in a host-mounted directory forever.

Respawn correctness falls out of the pair: a worker that finds the marker
attaches; one that finds the flock held waits; one that finds the flock free
with no marker is looking at a leader that died mid-pass, and correctly
re-leads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from src.apps import fs_lock

logger = logging.getLogger(__name__)

#: The leadership lock. One per fleet, not per boot: the done marker is what
#: carries the boot identity, and a single lock file keeps "who is leading
#: right now" answerable with one ``cat``.
LOCK_NAME = "boot-reconcile"

#: Done markers are ``<prefix><boot_id>`` — the prefix is also the glob used
#: to sweep other boots' markers.
DONE_PREFIX = "boot-reconcile-done-"


class BootReconcileCoordinator:
    """One instance per worker process, constructed with this boot's
    ``boot_id``. Call :meth:`coordinate` once at boot; if it returns True,
    call :meth:`finish` in a ``finally`` once the real reconcile pass ends.
    """

    def __init__(
        self,
        boot_id: str,
        *,
        lock_dir: Optional[str] = None,
        # Comfortably above _BOOT_RECONCILE_TIMEOUT (1200s): a waiter must
        # never give up and run its own pass BEFORE the leader's own internal
        # timeout would have made it give up first — that ordering is what
        # guarantees at most one "give up and just do it" per boot instead of
        # two workers deciding that within seconds of each other.
        max_wait_s: float = 1260.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.boot_id = boot_id
        self.max_wait_s = max_wait_s
        self.poll_interval = poll_interval
        self._lock_dir = lock_dir
        self._fd: Optional[int] = None

    @property
    def lock_path(self) -> str:
        return fs_lock.lock_path(LOCK_NAME, self._lock_dir)

    @property
    def done_marker(self) -> str:
        return self._marker_for(self.boot_id)

    def _marker_for(self, boot_id: str) -> str:
        base = self._lock_dir or fs_lock.lock_dir()
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, f"{DONE_PREFIX}{boot_id}")

    def _is_done(self) -> bool:
        return os.path.exists(self.done_marker)

    async def coordinate(self) -> bool:
        """Returns True if THIS call must run the full reconcile itself (it
        won leadership, or every candidate leader it waited on died without
        finishing) — the caller must then call :meth:`finish` in a ``finally``
        once the pass ends. Returns False if another worker already finished
        (fast path) or finished WHILE this call was waiting — the caller
        should just ``attach_on_boot()``.
        """
        deadline = time.monotonic() + self.max_wait_s
        while True:
            if self._is_done():
                return False
            fd = fs_lock.try_acquire(self.lock_path)
            if fd is not None:
                # MANDATORY re-check, not defensive: the leader may have
                # finished (marker written, flock released) in the window
                # between the check above and this acquire, in which case we
                # just "won" leadership of a pass that is already done and
                # would run a second full one on top of it — W4 in miniature.
                if self._is_done():
                    fs_lock.release(fd)
                    return False
                self._fd = fd
                logger.info(
                    "apps: won the boot reconcile lock %s (pid=%s, boot_id=%s) — "
                    "this worker runs this boot's provisioning pass",
                    self.lock_path, os.getpid(), self.boot_id)
                return True
            # Someone else holds it right now. Loop: either the DONE marker
            # appears (they finished — next iteration catches it), or the
            # flock frees up with no marker (they died mid-pass — next
            # iteration wins it for us), or we wait long enough that running
            # our own pass beats staying unattached forever.
            if time.monotonic() >= deadline:
                logger.error(
                    "apps: boot reconcile coordination timed out after %ss "
                    "waiting for %s (boot_id=%s) — running our own pass rather "
                    "than never attaching. Safe but unserialized at THIS layer; "
                    "the provisioning lock (Lock A, src/apps/lifecycle.py) is "
                    "what still keeps the two passes from racing apt.",
                    self.max_wait_s, fs_lock.holder(self.lock_path), self.boot_id)
                return True
            await asyncio.sleep(self.poll_interval)

    async def finish(self) -> None:
        """Leader-only — call in a ``finally`` once the pass ends (success,
        exception, or internal timeout give-up).

        Records the terminal marker every later worker of this boot checks
        first, sweeps other boots' markers, and releases the lock. Marker
        write and sweep are best-effort: failing to record it just means a
        respawned worker re-runs the pass, which is the pre-W4 behaviour, and
        must never be the reason this worker's own boot raises.
        """
        try:
            await asyncio.to_thread(self._write_marker)
        except Exception:
            logger.warning(
                "apps: could not record the boot reconcile done marker %s — a "
                "respawned worker may re-run the pass (boot_id=%s)",
                self.done_marker, self.boot_id, exc_info=True)
        try:
            await asyncio.to_thread(self._sweep_other_markers)
        except Exception:
            logger.warning("apps: could not sweep stale boot reconcile markers",
                           exc_info=True)
        # Last, and always: the marker says "finished", the lock says "in
        # progress". Releasing before the marker exists opens exactly the
        # window coordinate()'s re-check closes, so don't widen it here.
        fs_lock.release(self._fd)
        self._fd = None

    def _write_marker(self) -> None:
        """Atomic: a reader only ever sees the marker absent or complete.
        ``coordinate()`` reads it with no lock held, so a half-written file
        would be a torn read on the one fact the whole respawn story rests
        on."""
        marker = self.done_marker
        tmp = f"{marker}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()} at={time.time():.0f}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, marker)

    def _sweep_other_markers(self) -> None:
        """Drop markers from OTHER boots. Unlinking is safe here in a way it
        never is for a ``.lock`` file: a marker carries no kernel state, only
        the leader of the current boot runs this, and it only ever touches
        boot_ids that are not its own."""
        base = self._lock_dir or fs_lock.lock_dir()
        keep = os.path.basename(self.done_marker)
        for entry in os.listdir(base):
            if not entry.startswith(DONE_PREFIX) or entry == keep:
                continue
            try:
                os.unlink(os.path.join(base, entry))
            except OSError:
                pass
