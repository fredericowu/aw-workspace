"""Managed-service supervisor (ADR contribution point ``services``, gated by the
F2 ``service:manage`` capability) — F4.

An app registers a long-running process the runtime can start / stop / report
status for. F4 models a service as a **shell command line** launched from the
app's package dir (the ADR's ``module:func`` form is a later refinement); the
supervisor tracks the live ``Popen`` keyed by ``(app_id, service_id)``. On app
uninstall the runtime stops every service the app registered (journal reverse
replay) — no orphan processes survive an uninstall.

**Process health lives here** (``docs/design/service-supervision-activation-audit.md``
§B). A managed service that dies is respawned by the reader thread that
observed its death — exponential backoff, and a sticky give-up after five
consecutive startup-window crashes so a service that cannot start is not
respawned forever. Two rules make the difference between a restart loop and a
new outage, and both are tested:

* ``stop()`` sets ``desired = stopped`` **before** it signals. The pump's
  ``finally`` runs *after* ``stop()`` returns, so a flag set afterwards would
  let the pump read a stale ``running`` and respawn the service the operator
  just stopped.
* a pump only respawns ``svc.proc is proc`` — the process it was reading. A
  ``restart()`` is ``stop()`` + ``start()`` back to back and the old pump can
  reach its ``finally`` long after the replacement is live (see
  ``src/tests/integration/apps/test_services.py``); without the identity
  check that late ``finally`` spawns a third copy.

What this module deliberately does NOT do is notice a service that was never
started, or one whose owning worker died — the supervisor's knowledge dies
with its worker. That is the activation audit (§A of the same design), which
is **not built**: see ``docs/standards/core-restart-blast-radius.md`` for what
is and is not guaranteed today.
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque

from src.apps.service_lease import (ServiceLease, clear_gave_up,
                                    is_lease_held_anywhere, set_gave_up)

log = logging.getLogger(__name__)

# Bounded backlog kept per service so a log-window snapshot has something to
# show without holding unbounded process output in memory.
_LOG_BACKLOG = 500

#: Respawn delay is ``min(_BACKOFF_BASE_S * 2 ** n, _BACKOFF_CAP_S)`` where
#: ``n`` is the number of consecutive startup-window crashes so far: 1s, 2s,
#: 4s, 8s, 16s — then give up. The cap only matters if a future change raises
#: _GIVE_UP_AFTER.
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 60.0

#: A run shorter than this is a "startup-window crash" and counts towards
#: give-up; anything that survived longer is a service that WAS working and
#: died later, so its counter resets and it gets the full budget again.
_HEALTHY_RUN_S = 60.0

#: How long the reader thread waits for its process to actually be reaped
#: once stdout closed, before concluding it is still alive with its output
#: closed. See the ``finally`` in ``start()``'s ``_pump``.
_EXIT_REAP_S = 5.0

#: Five consecutive startup-window crashes (~31s of backoff plus five crash
#: durations) is where respawning stops being recovery and starts being a
#: tighter loop than the bug it is fixing: port flapping, CPU burn, log spam.
_GIVE_UP_AFTER = 5

#: Substrings that identify the one crash class a respawn can never win:
#: something else already holds the port. After a core-process-only restart
#: that something is usually this very service, orphaned (``start_new_session
#: =True`` means it outlives its worker) — so backing off 1s, 2s, 4s... would
#: just fail five times. Give up immediately with a reason that names it.
#: The strings come from CPython's own socket errors; an app that prints its
#: own wording instead simply takes the ordinary backoff path.
_PORT_COLLISION_MARKERS = ("address already in use", "errno 98", "eaddrinuse")

DESIRED_RUNNING = "running"
DESIRED_STOPPED = "stopped"

STATE_RUNNING = "running"
STATE_BACKING_OFF = "backing_off"
STATE_GAVE_UP = "gave_up"
STATE_STOPPED = "stopped"


class ServiceError(RuntimeError):
    pass


class _Service:
    def __init__(self, app_id: str, service_id: str, start: str,
                 package_dir: str, autostart: bool) -> None:
        self.app_id = app_id
        self.service_id = service_id
        self.start = start
        self.package_dir = package_dir
        self.autostart = autostart
        self.proc: subprocess.Popen | None = None
        # Real stdout/stderr of the managed subprocess, captured by a
        # background reader thread — see ServiceSupervisor.start(). This is
        # the only log source available for a `tier: inprocess` app's
        # managed service; there is no separate container/log driver to
        # query the way ContainerSupervisor does.
        self.log_lines: deque[str] = deque(maxlen=_LOG_BACKLOG)
        self._reader_thread: threading.Thread | None = None
        # Set by the reader thread once stdout closes (process exited), so a
        # service that failed to even exec (e.g. a dead venv interpreter) is
        # reported as "off" alongside WHY, not just silently off exactly like
        # a service nobody ever asked to start.
        self.last_exit_code: int | None = None
        # This worker's cross-worker ownership claim for the CURRENTLY
        # running local process, or None when nothing is running locally.
        # See src/apps/service_lease.py.
        self.lease: ServiceLease | None = None

        # --- restart-loop state (design §B.2) ---------------------------
        # `desired` is the correctness keystone, NOT a status field: it is
        # the only thing that distinguishes "this process died" from "this
        # process was stopped on purpose". A manual stop, an uninstall and
        # an upgrade's stop-half all set it to stopped BEFORE signalling.
        self.desired = DESIRED_STOPPED
        # Reported state, policy-side: running | backing_off | gave_up |
        # stopped. `status()` derives what it reports from this PLUS live
        # process/lease facts, so a service running on another worker is
        # never described by this worker's local policy state.
        self.state = STATE_STOPPED
        self.consecutive_fast_exits = 0
        self.last_spawn_monotonic: float | None = None
        self.give_up_reason: str | None = None
        # Guards the fields above only. Never held across Popen() (fork+exec
        # under a lock is how a reader thread ends up blocked on a service
        # that is slow to exec) and never used from asyncio.
        self.lock = threading.RLock()
        self.respawn_timer: threading.Timer | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.app_id, self.service_id)


class ServiceSupervisor:
    """Runtime-owned registry + lifecycle for apps' managed services."""

    def __init__(self) -> None:
        self._services: dict[tuple[str, str], _Service] = {}

    def register(self, app_id: str, service_id: str, start: str,
                 package_dir: str, autostart: bool = False) -> None:
        key = (app_id, service_id)
        if key in self._services:
            raise ServiceError(f"service {service_id!r} already registered for {app_id!r}")
        svc = _Service(app_id, service_id, start, package_dir, autostart)
        self._services[key] = svc
        log.info("apps: registered service %s/%s (autostart=%s)",
                 app_id, service_id, autostart)
        # A registration is an install, an update/reinstall or a fresh
        # worker attaching — all three are "start over", so a give-up
        # recorded by whatever ran before this is cleared here. An app that
        # is merely already loaded never reaches this method (register
        # raises on a duplicate key), so this cannot quietly re-arm a
        # give-up every 900s convergence pass.
        clear_gave_up(app_id, service_id)
        if autostart:
            self.start(app_id, service_id)

    def start(self, app_id: str, service_id: str, *, manual: bool = True) -> dict:
        """Spawn the service if it isn't already running somewhere.

        ``manual=False`` is the restart loop's own re-entry (see
        :meth:`_schedule_respawn`): same spawn, but it must not clear the
        give-up marker or reset the backoff budget it is itself spending.
        Every other caller — the CLI, the component routes, the relay, an
        app's own ``ctx.services.start`` — is a deliberate human-or-app
        action and re-arms both.
        """
        svc = self._require(app_id, service_id)
        with svc.lock:
            # A deliberate start supersedes a pending backoff: without this
            # the timer fires later and spawns a second copy of whatever we
            # are about to start here.
            self._cancel_respawn(svc)
            svc.desired = DESIRED_RUNNING
            if manual:
                svc.consecutive_fast_exits = 0
                svc.give_up_reason = None
                if svc.state == STATE_GAVE_UP:
                    log.info("apps: service %s/%s was in gave_up — a manual start "
                             "re-arms the restart loop", app_id, service_id)
                    svc.state = STATE_STOPPED
        if manual:
            clear_gave_up(app_id, service_id)

        if svc.proc is not None and svc.proc.poll() is None:
            return self.status(app_id, service_id)  # already running

        # Cross-worker ownership gate (see src/apps/service_lease.py): at
        # AW_WORKSPACE_WORKERS>1, a request to start/restart this service can
        # land on a worker that isn't running it while another worker's
        # instance already is — without this check that spawns a REAL second
        # subprocess that collides on the service's port with the one
        # already running elsewhere.
        lease = ServiceLease(app_id, service_id)
        if not lease.acquire():
            log.warning(
                "apps: service %s/%s is already owned by another worker — refusing to "
                "spawn a duplicate subprocess that would collide on its port",
                app_id, service_id)
            return self.status(app_id, service_id)

        # We hold the lease, so no live worker owns this service — anything
        # on this host still running its exact command line is an orphan
        # from a worker that died (start_new_session=True keeps it alive and
        # keeps its port bound). Reap it before spawning, or the spawn is
        # guaranteed to lose to a bind collision.
        self._reap_orphan(svc)

        svc.lease = lease
        svc.log_lines.clear()
        svc.last_exit_code = None
        svc.proc = subprocess.Popen(
            shlex.split(svc.start), cwd=svc.package_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,  # own process group so stop kills children
        )
        with svc.lock:
            svc.last_spawn_monotonic = time.monotonic()
            svc.state = STATE_RUNNING
        log.info("apps: started service %s/%s pid=%s", app_id, service_id, svc.proc.pid)

        def _pump(proc: subprocess.Popen, buf: deque[str], own_lease: ServiceLease) -> None:
            try:
                if proc.stdout is None:
                    return
                for line in iter(proc.stdout.readline, ""):
                    buf.append(line.rstrip("\n"))
            except Exception:
                log.exception("apps: log reader for service %s/%s crashed", app_id, service_id)
            finally:
                # wait(), not poll(): stdout closing and the child being
                # reaped are two different events, and poll() loses the race
                # often enough that the exit code read "None" for roughly
                # half the crashes in a crash loop — which then showed up as
                # an unexplained give-up ("exit code None"). This thread
                # exists for this one process, so blocking it here costs
                # nothing. Bounded, because stdout closing does NOT prove the
                # process is gone (a service that daemonizes closes it and
                # keeps running) and that case must not hang the thread.
                try:
                    svc.last_exit_code = proc.wait(timeout=_EXIT_REAP_S)
                except subprocess.TimeoutExpired:
                    svc.last_exit_code = None
                if svc.last_exit_code not in (None, 0):
                    log.warning("apps: service %s/%s exited with code %s",
                                app_id, service_id, svc.last_exit_code)
                # Process exited on its own (crash, or was killed outside of
                # stop()) — release the claim so another worker's start()
                # isn't blocked by an ownership record nobody is renewing.
                #
                # `own_lease` is captured at SPAWN time, not read off `svc` at
                # exit time, and the field is cleared only on an identity
                # match. A restart is stop()+start() back-to-back: stop()
                # returns as soon as proc.wait() does, start() then acquires a
                # NEW lease, and this pump reaches its finally a moment later.
                # Reading `svc.lease` here would find — and release — the
                # lease belonging to the process that just replaced this one,
                # leaving a RUNNING service with no ownership record: status()
                # reports it off fleet-wide, and the next worker to click Start
                # spawns exactly the duplicate the lease exists to prevent.
                # (Releasing an already-released lease is a no-op: release()
                # only touches Redis while it still holds `_owned`.)
                own_lease.release()
                if svc.lease is own_lease:
                    svc.lease = None
                # Same identity discipline, now for the restart policy: this
                # pump may only decide anything about a process that is still
                # THE process (see the module docstring).
                self._after_exit(svc, proc)

        # Heartbeat BEFORE the reader thread, not after. The pump's `finally`
        # releases this lease, and a service that exits instantly (the first
        # crash of a crash loop, every time) can get there while start_heartbeat
        # is still between constructing its thread and starting it — which
        # made release() join an unstarted thread and raise, killing the pump
        # before it could decide anything about the exit. Arming the heartbeat
        # on a lease this worker already owns is safe at any point after
        # acquire(); the reverse order is not.
        lease.start_heartbeat()
        svc._reader_thread = threading.Thread(
            target=_pump, args=(svc.proc, svc.log_lines, lease), daemon=True,
        )
        svc._reader_thread.start()
        return self.status(app_id, service_id)

    # ------------------------------------------------------------------
    # Restart loop (design §B.2)
    # ------------------------------------------------------------------

    def _after_exit(self, svc: _Service, proc: subprocess.Popen) -> None:
        """Decide what a just-exited process means. Called from its own
        reader thread, after the lease has been released.

        Everything that reads or writes policy state happens under the
        per-service lock; the two side effects that can block — a Redis
        write and arming a timer — happen after it is dropped.
        """
        decision: str = "none"
        delay = 0.0
        reason = ""
        exit_code: int | None = None
        detail = ""
        with svc.lock:
            if svc.proc is not proc:
                # A newer start() already replaced this process. Its pump
                # owns the policy now; this one is a straggler (the
                # `setsid` interleaving in test_services.py is exactly
                # this) and must not spawn a third copy.
                return
            if proc.poll() is None:
                # Stdout closed but the process is still running — a service
                # that daemonizes, or one that redirects its own logging
                # after startup. It is NOT dead, and respawning it would put
                # a second copy next to a live one. Say so once and leave it
                # alone; `status()` keeps reporting it from the live Popen.
                log.warning(
                    "apps: service %s/%s closed its output but is still running "
                    "(pid=%s) — not respawning. Its logs stop here.",
                    svc.app_id, svc.service_id, proc.pid)
                svc.state = STATE_RUNNING
                return
            exit_code = svc.last_exit_code
            if svc.desired != DESIRED_RUNNING:
                svc.state = STATE_STOPPED
                return
            ran_for = time.monotonic() - (svc.last_spawn_monotonic or time.monotonic())
            if ran_for >= _HEALTHY_RUN_S:
                svc.consecutive_fast_exits = 0
            crashes = svc.consecutive_fast_exits
            detail = " | ".join([l for l in svc.log_lines if l.strip()][-3:])
            if self._looks_like_port_collision(svc):
                # No amount of backoff wins against a port someone else
                # holds, and after a worker respawn that someone is usually
                # this service's own orphan (see _reap_orphan / §D.2).
                decision, reason = "give_up", "orphan-suspected"
            elif crashes + 1 >= _GIVE_UP_AFTER and ran_for < _HEALTHY_RUN_S:
                decision, reason = "give_up", "crash-loop"
            else:
                decision = "respawn"
                delay = min(_BACKOFF_BASE_S * (2 ** crashes), _BACKOFF_CAP_S)
            if ran_for < _HEALTHY_RUN_S:
                svc.consecutive_fast_exits = crashes + 1
            if decision == "give_up":
                svc.state = STATE_GAVE_UP
                svc.give_up_reason = reason
            else:
                svc.state = STATE_BACKING_OFF

        if decision == "give_up":
            log.error(
                "apps: GIVING UP on service %s/%s after %s — exit code %s, last "
                "output: %s. No further respawns until a manual start/restart, an "
                "app update/reinstall or a config save. See "
                "docs/standards/core-restart-blast-radius.md",
                svc.app_id, svc.service_id, reason, exit_code, detail or "(none)")
            set_gave_up(svc.app_id, svc.service_id, reason=reason,
                        exit_code=exit_code, detail=detail)
            return
        if decision == "respawn":
            log.warning(
                "apps: service %s/%s died (exit %s) — respawning in %.0fs "
                "(consecutive startup-window crashes: %s)",
                svc.app_id, svc.service_id, exit_code, delay,
                svc.consecutive_fast_exits)
            self._schedule_respawn(svc, delay)

    def _looks_like_port_collision(self, svc: _Service) -> bool:
        """Did this exit say "something else already holds my port"?

        Read from the service's own captured stdout/stderr — the only log
        source a Tier-1 service has. ``apps/proxy``'s bind failure used to be
        masked by its own server class and now surfaces as a real
        ``OSError``; see ``service_lease.py``'s docstring.
        """
        tail = [l.lower() for l in list(svc.log_lines)[-20:]]
        return any(m in line for line in tail for m in _PORT_COLLISION_MARKERS)

    def _schedule_respawn(self, svc: _Service, delay: float) -> None:
        timer = threading.Timer(delay, self._respawn, args=(svc,))
        timer.daemon = True
        timer.name = f"service-respawn-{svc.app_id}-{svc.service_id}"
        with svc.lock:
            # Re-checked under the lock: a stop() between the decision and
            # here means the operator wins and nothing is armed.
            if svc.desired != DESIRED_RUNNING or svc.state == STATE_GAVE_UP:
                return
            self._cancel_respawn(svc)
            svc.respawn_timer = timer
        timer.start()

    def _cancel_respawn(self, svc: _Service) -> None:
        """Disarm a pending respawn. Caller holds ``svc.lock``."""
        timer = svc.respawn_timer
        svc.respawn_timer = None
        if timer is not None:
            timer.cancel()

    def _respawn(self, svc: _Service) -> None:
        with svc.lock:
            svc.respawn_timer = None
            if svc.desired != DESIRED_RUNNING or svc.state == STATE_GAVE_UP:
                return
            if svc.proc is not None and svc.proc.poll() is None:
                return  # something already started it while we waited
        try:
            # Through the ordinary start(), so lease acquisition still
            # arbitrates cross-worker duplicates — a respawn that loses the
            # lease is the existing logged no-op, correct by construction.
            self.start(svc.app_id, svc.service_id, manual=False)
        except Exception:
            log.exception("apps: respawn of service %s/%s failed",
                          svc.app_id, svc.service_id)

    # ------------------------------------------------------------------
    # Orphan reaping (design §D.2)
    # ------------------------------------------------------------------

    def _reap_orphan(self, svc: _Service) -> None:
        """SIGTERM a *verified* orphan of this service, if one is running.

        A core-process-only restart (or a uvicorn worker respawn) does not
        kill managed services: they are spawned with
        ``start_new_session=True``, so they survive their worker with the
        port still bound while the lease heartbeat dies. The next ``start()``
        would then lose to ``OSError: Address already in use`` forever.

        Only called once this worker HOLDS the lease, i.e. no live worker
        owns the service — and a candidate still has to match on all FOUR of:
        exact argv, the service's own package dir as cwd, being its own
        session leader (what ``start_new_session=True`` produces), and having
        been reparented to pid 1. Anything less and we would be killing
        someone else's process; anything missed falls through to the
        bind-failure give-up instead, which is the safe direction.

        The reparenting check is what makes this safe when Redis is away:
        ``ServiceLease.acquire`` deliberately degrades OPEN, so two workers
        can both believe they own the service. A peer worker's live child has
        that worker as its parent and is never touched; only a process whose
        parent is actually gone is.

        Reading ``/proc`` directly rather than recording the pid in the lease
        payload: the lease expires 30s after its worker dies, which is
        precisely when the orphan starts mattering, so a pid stored there is
        gone exactly when it would be needed.
        """
        argv = shlex.split(svc.start)
        want_cwd = os.path.realpath(svc.package_dir)
        own_pid = os.getpid()
        for entry in self._candidate_pids():
            if entry == own_pid:
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    parts = [p.decode() for p in f.read().split(b"\0") if p]
                if parts != argv:
                    continue
                if os.path.realpath(f"/proc/{entry}/cwd") != want_cwd:
                    continue
                if os.getsid(entry) != entry:
                    continue
                if self._ppid(entry) != 1:
                    continue  # still has a living parent — not an orphan
            except (OSError, ProcessLookupError, UnicodeDecodeError):
                continue
            log.warning(
                "apps: reaping an orphaned %s/%s (pid=%s) — same command line, same "
                "package dir, its own session leader, reparented to pid 1, and no "
                "worker holds its lease: a service that outlived the worker that "
                "spawned it. Its port would otherwise collide with the process "
                "about to start.",
                svc.app_id, svc.service_id, entry)
            self._terminate_pid(entry)

    @staticmethod
    def _ppid(pid: int) -> int:
        """Parent pid, read from ``/proc/<pid>/status`` rather than ``stat``
        — a process whose ``comm`` contains spaces or parentheses breaks
        naive field-splitting of ``stat``, and "which field is ppid" is not
        something to get wrong in code that sends SIGTERM."""
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        return -1

    @staticmethod
    def _candidate_pids() -> list[int]:
        try:
            return sorted(int(n) for n in os.listdir("/proc") if n.isdigit())
        except OSError:
            return []

    @staticmethod
    def _terminate_pid(pid: int, timeout: float = 5.0) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def restart(self, app_id: str, service_id: str) -> dict:
        """Stop then start as ONE unit, so a forwarded ``restart`` command is
        a single action executable on the owning worker.

        Previously this only existed as a route-level stop-then-start pair
        (``src/api/components.py``), which cannot be forwarded as one thing:
        relaying the halves separately would let another worker win the lease
        in between and leave the restart half-done on two machines.
        """
        self.stop(app_id, service_id)
        return self.start(app_id, service_id)

    def owns_locally(self, app_id: str, service_id: str) -> bool:
        """True only if THIS process is running the service *and* still holds
        its cross-worker claim — the question a broadcast command asks itself
        before acting on it (``src/apps/service_relay.py``).

        Both halves matter. A worker whose lease was stolen or expired (the
        heartbeat clears ``owned``, see ``service_lease.py``) still has a live
        ``Popen`` and would otherwise answer for a service it no longer owns.
        Unregistered here answers False rather than raising: most workers have
        never loaded the app whose service a broadcast names.
        """
        svc = self._services.get((app_id, service_id))
        if svc is None or svc.proc is None or svc.proc.poll() is not None:
            return False
        return svc.lease is not None and svc.lease.owned

    def logs(self, app_id: str, service_id: str) -> list[str]:
        """Return the buffered stdout/stderr backlog for a managed service."""
        svc = self._require(app_id, service_id)
        return list(svc.log_lines)

    def registered(self) -> list[tuple[str, str]]:
        """``(app_id, service_id)`` pairs for every registered service."""
        return list(self._services.keys())

    def stop(self, app_id: str, service_id: str, timeout: float = 5.0) -> dict:
        svc = self._require(app_id, service_id)
        # BEFORE the signal, never after. The reader thread's `finally` runs
        # after this method returns, so a flag set later lets the pump read a
        # stale `running` and respawn the service this call just killed — the
        # service rises from the dead and a deliberate stop becomes
        # impossible. Cancelling any armed backoff timer belongs to the same
        # instant, for the same reason.
        with svc.lock:
            svc.desired = DESIRED_STOPPED
            svc.state = STATE_STOPPED
            svc.consecutive_fast_exits = 0
            self._cancel_respawn(svc)
        proc = svc.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait(timeout=timeout)
            log.info("apps: stopped service %s/%s", app_id, service_id)
        # Only this worker's own claim, if any — a worker that never spawned
        # this service locally (svc.proc/svc.lease both None, e.g. it landed
        # here on a non-owning worker) has nothing of its own to release, and
        # must not touch another worker's still-live lease.
        if svc.lease is not None:
            svc.lease.release()
            svc.lease = None
        svc.proc = None
        return {"service": service_id, "running": False}

    def status(self, app_id: str, service_id: str) -> dict:
        svc = self._require(app_id, service_id)
        running = svc.proc is not None and svc.proc.poll() is None
        # Cross-worker: this worker isn't running it locally, but another
        # worker's lease may still be live — report the TRUE fleet-wide
        # state instead of a false "off" (see src/apps/service_lease.py).
        remote = False if running else is_lease_held_anywhere(app_id, service_id)
        result = {
            "service": service_id,
            "running": running or remote,
            "pid": svc.proc.pid if running and svc.proc else None,
            "autostart": svc.autostart,
            # Live facts beat this worker's policy state: a service running
            # here or on another worker IS running, whatever our own backoff
            # bookkeeping last recorded. Only when nothing is running
            # anywhere does the policy state become the answer, and that is
            # exactly when the distinction matters — "off" vs "backing off"
            # vs "gave up" vs "deliberately stopped".
            "state": (STATE_RUNNING if (running or remote) else svc.state),
            "desired": svc.desired,
        }
        if svc.give_up_reason and not (running or remote):
            result["give_up_reason"] = svc.give_up_reason
        if remote and not running:
            result["remote_worker"] = True
        if not running and not remote and svc.last_exit_code not in (None, 0):
            result["last_exit_code"] = svc.last_exit_code
            tail = [l for l in svc.log_lines if l.strip()][-3:]
            if tail:
                result["last_error"] = " | ".join(tail)
        return result

    def stop_all_for(self, app_id: str) -> None:
        """Stop + drop every service an app registered (uninstall)."""
        for (aid, sid) in [k for k in self._services if k[0] == app_id]:
            try:
                self.stop(aid, sid)
            except Exception:
                log.exception("apps: failed to stop service %s/%s on uninstall", aid, sid)
            # The give-up marker is deliberately durable — nothing but an
            # explicit re-arm clears it — so an uninstall has to purge it
            # here or an app that gave up once leaves a Redis key behind it
            # that nothing registered will ever point at again.
            clear_gave_up(aid, sid)
            self._services.pop((aid, sid), None)

    def forget_all_for(self, app_id: str) -> None:
        """Drop every registration for an app WITHOUT stopping anything (W3).

        The counterpart to :meth:`stop_all_for` for a worker that is merely
        detaching. A managed service is a real ``subprocess.Popen`` plus a
        reader thread living in exactly ONE process — the worker that
        provisioned it — so a worker that never spawned it has nothing to
        signal, and calling ``stop`` here would at best be a no-op and at
        worst kill a process it does not own (the pid is recorded per-process,
        but the process group is not). Forget the bookkeeping; leave the
        process to its owner. See src/apps/lifecycle.py.
        """
        for key in [k for k in self._services if k[0] == app_id]:
            svc = self._services.pop(key, None)
            # A detaching worker leaves the PROCESS to its owner but must not
            # leave a timer behind that would fire into a service it no
            # longer knows about.
            if svc is not None:
                with svc.lock:
                    svc.desired = DESIRED_STOPPED
                    self._cancel_respawn(svc)

    def clear_gave_up_for(self, app_id: str) -> None:
        """Re-arm the restart loop for every service an app registered,
        without starting anything.

        The config-save half of the design's "give-up is cleared by manual
        start/restart, app update/reinstall, and app config save": saving an
        app's settings is a statement that the operator has changed something
        about how it runs, so the supervisor owes it a fresh budget — but a
        save must never *start* a service that was deliberately stopped
        (the same rule ``_apply_runtime_config`` follows for containers).
        """
        for (aid, sid), svc in list(self._services.items()):
            if aid != app_id:
                continue
            with svc.lock:
                svc.consecutive_fast_exits = 0
                svc.give_up_reason = None
                if svc.state == STATE_GAVE_UP:
                    svc.state = STATE_STOPPED
            clear_gave_up(aid, sid)

    def _require(self, app_id: str, service_id: str) -> _Service:
        svc = self._services.get((app_id, service_id))
        if svc is None:
            raise ServiceError(f"service {service_id!r} is not registered for {app_id!r}")
        return svc
