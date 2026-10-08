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

**W5 (2026-10-08) — the stampede that outlived two rewrites.** Everything
above was in place and none of it was what failed. ``coordinate()``'s
``max_wait_s`` branch was: it returned True — *run your own pass* — the
moment a wall clock elapsed, **without ever asking whether the leader was
still alive**, which flatly contradicts this module's own invariant three
paragraphs up ("a leader still working can never be preempted"). It
survived the W4 Redis→lease rewrite and the Lock C Redis→flock rewrite
unchanged, because both rewrites replaced the liveness PRIMITIVE and left
the one branch that ignores liveness alone.

Observed on this project's own host at ``AW_WORKSPACE_WORKERS=10``:

* The margin was never real. 1260s against the leader's 1200s
  ``_BOOT_RECONCILE_TIMEOUT`` assumed both clocks start together. They do
  not: a follower's starts when ITS process boots, the leader's only when
  its reconcile call begins, tens of seconds later behind worker start skew
  and the priority-app install. 60s of paper margin went negative.
* All nine followers expired at once — they began waiting together, so they
  reached the deadline within milliseconds of each other and every one of
  them decided the same thing in the same instant. Ten concurrent passes
  over one venv and one podman socket is *slower* than one, so the leader
  overran its own timeout too and the next boot repeated it. The logs are
  unambiguous: "coordination timed out" 18x, "another worker is running
  this boot's app reconcile" **0x** — not one follower ever attached.

Downstream, that is also where a pile of symptoms that looked unrelated
came from: the workspace never reached convergence (hence fewer running
containers than the remote host's healthcheck expects, hence *unhealthy*),
podman containers churned continuously (every create/destroy reloads
aardvark-dns, which is the whole of the "intermittent DNS"
``[Errno -3]``/``[Errno -5]`` failures), and the in-process
``agents-platform-runners`` app's routes sat unmounted ~6.5min per boot,
which is what agents-platform-multitenant saw as ``/execute`` → 404.

The fix is to ask the question the deadline was a proxy for. ``flock``
makes it exact — EAGAIN *is* the fact that a live process holds the lock,
with no reachability caveat a Redis SET NX would have had. So the deadline
now gates on the lock still being held, and backs off instead of
preempting. See ``coordinate()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
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

#: Ceiling on a single deadline extension in coordinate()'s backoff. Half an
#: hour between "still alive, still waiting" lines is rare enough not to be
#: noise and frequent enough that a genuinely stuck fleet stays visible.
_MAX_WAIT_EXTENSION_S = 1800.0


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
        # How long to wait before CHECKING WHETHER THE LEADER IS STILL ALIVE
        # — no longer "how long before taking over regardless", which is the
        # W5 bug (see the module docstring). It was 1260.0, a bare 60s over
        # _BOOT_RECONCILE_TIMEOUT (1200s), on the reasoning that a waiter
        # must never give up before the leader's own timeout would have. The
        # ordering argument was right; the margin was imaginary, because the
        # two clocks do not start at the same moment. Now that expiry only
        # triggers a liveness check rather than a takeover, this value sets
        # how often a long pass is reported, not whether it is interrupted.
        max_wait_s: float = 2400.0,
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
        waits_past_deadline = 0
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
            # iteration wins it for us).
            #
            # **Reaching the deadline is not permission to preempt.** This
            # branch used to `return True` unconditionally here, and that one
            # line is the W5 stampede: nine followers of one boot all expire
            # in the same millisecond and all start competing passes over one
            # venv and one podman socket. We only got to this point because
            # `try_acquire` just returned None, and with flock that means a
            # LIVE process holds the lock — the kernel drops it on exit,
            # crash or kill -9, so "held" and "alive" are the same fact. The
            # leader is working. Waiting is correct; report and keep waiting.
            #
            # The deadline's real job — never wait forever on a leader that
            # will never finish — is already done by the two loop exits
            # above, both of which fire the moment the lock is released.
            if time.monotonic() >= deadline:
                waits_past_deadline += 1
                # Exponential, capped. The cap is the point: this must stay
                # observable, so the log line keeps firing at a decreasing
                # but non-zero rate for as long as the anomaly lasts, rather
                # than going silent.
                extension = min(
                    self.max_wait_s * (2 ** (waits_past_deadline - 1)),
                    _MAX_WAIT_EXTENSION_S,
                )
                deadline = time.monotonic() + extension
                logger.warning(
                    "apps: boot reconcile has been running for over %ss "
                    "(holder=%s, boot_id=%s), which is longer than expected — "
                    "but it still HOLDS the flock, so it is alive and working. "
                    "NOT starting a competing pass: that is the 10-worker "
                    "stampede of 2026-10-08 (see this module's W5 note). "
                    "Waiting a further %ss; check %d past the deadline.",
                    self.max_wait_s, fs_lock.holder(self.lock_path),
                    self.boot_id, extension, waits_past_deadline)
            # Jittered so the N followers of one boot never poll — or reach a
            # deadline — in lockstep. Polling together is harmless by itself;
            # DECIDING together is what turned one impatient follower into a
            # nine-way herd.
            await asyncio.sleep(self.poll_interval * (0.5 + random.random()))

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
