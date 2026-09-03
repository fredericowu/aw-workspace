"""Managed-service supervisor (ADR contribution point ``services``, gated by the
F2 ``service:manage`` capability) — F4.

An app registers a long-running process the runtime can start / stop / report
status for. F4 models a service as a **shell command line** launched from the
app's package dir (the ADR's ``module:func`` form is a later refinement); the
supervisor tracks the live ``Popen`` keyed by ``(app_id, service_id)``. On app
uninstall the runtime stops every service the app registered (journal reverse
replay) — no orphan processes survive an uninstall.
"""
from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import threading
from collections import deque

log = logging.getLogger(__name__)

# Bounded backlog kept per service so a log-window snapshot has something to
# show without holding unbounded process output in memory.
_LOG_BACKLOG = 500


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
        if autostart:
            self.start(app_id, service_id)

    def start(self, app_id: str, service_id: str) -> dict:
        svc = self._require(app_id, service_id)
        if svc.proc is not None and svc.proc.poll() is None:
            return self.status(app_id, service_id)  # already running
        svc.log_lines.clear()
        svc.last_exit_code = None
        svc.proc = subprocess.Popen(
            shlex.split(svc.start), cwd=svc.package_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,  # own process group so stop kills children
        )
        log.info("apps: started service %s/%s pid=%s", app_id, service_id, svc.proc.pid)

        def _pump(proc: subprocess.Popen, buf: deque[str]) -> None:
            try:
                if proc.stdout is None:
                    return
                for line in iter(proc.stdout.readline, ""):
                    buf.append(line.rstrip("\n"))
            except Exception:
                log.exception("apps: log reader for service %s/%s crashed", app_id, service_id)
            finally:
                svc.last_exit_code = proc.poll()
                if svc.last_exit_code not in (None, 0):
                    log.warning("apps: service %s/%s exited with code %s",
                                app_id, service_id, svc.last_exit_code)

        svc._reader_thread = threading.Thread(
            target=_pump, args=(svc.proc, svc.log_lines), daemon=True,
        )
        svc._reader_thread.start()
        return self.status(app_id, service_id)

    def logs(self, app_id: str, service_id: str) -> list[str]:
        """Return the buffered stdout/stderr backlog for a managed service."""
        svc = self._require(app_id, service_id)
        return list(svc.log_lines)

    def registered(self) -> list[tuple[str, str]]:
        """``(app_id, service_id)`` pairs for every registered service."""
        return list(self._services.keys())

    def stop(self, app_id: str, service_id: str, timeout: float = 5.0) -> dict:
        svc = self._require(app_id, service_id)
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
        svc.proc = None
        return {"service": service_id, "running": False}

    def status(self, app_id: str, service_id: str) -> dict:
        svc = self._require(app_id, service_id)
        running = svc.proc is not None and svc.proc.poll() is None
        result = {
            "service": service_id,
            "running": running,
            "pid": svc.proc.pid if running and svc.proc else None,
            "autostart": svc.autostart,
        }
        if not running and svc.last_exit_code not in (None, 0):
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
            self._services.pop((aid, sid), None)

    def _require(self, app_id: str, service_id: str) -> _Service:
        svc = self._services.get((app_id, service_id))
        if svc is None:
            raise ServiceError(f"service {service_id!r} is not registered for {app_id!r}")
        return svc
