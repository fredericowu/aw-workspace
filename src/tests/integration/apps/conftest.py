"""Shared helpers for tests that force the restart loop's fork-vs-setsid
race (see ``test_service_restart_loop.py::test_a_deliberate_stop_is_not_
respawned``'s docstring for the confirmed mechanism) rather than hoping to
land in it. Hoisted out of ``test_services.py`` and
``test_service_restart_loop.py`` — both spawned a detached ``setsid sleep
N`` child and needed the exact same session-leader detection + cleanup to
close it deterministically, and a copy in each file meant a future tweak
(retry cadence, the cwd/argv match) had to remember to patch both.
"""
from __future__ import annotations

import os
import signal
import time

from src.apps.services import ServiceSupervisor

# A deliberately long window for the detached child these tests spawn to
# hold the pump's pipe open — long enough that a scheduling delay under
# CI/coverage load can never make it expire mid-test on its own.
#
# The real race this child is vulnerable to is NOT its own expiry, though:
# ``setsid CMD &`` forks a child that is briefly still a MEMBER of the
# service's own process group until it completes its own ``setsid()`` call,
# and ``stop()``'s ``killpg`` can reach — and kill — that child if it fires
# inside that window, regardless of how long its sleep argument is.
# Lengthening the sleep alone does not close this; waiting for the child to
# become its own session leader before calling stop()/restart() does (see
# ``_find_session_leader`` below).
DETACHED_SLEEP_S = 300


def find_session_leader(argv: list[str], cwd) -> int | None:
    """PID of the ``setsid``'d detached child matching ``argv`` under ``cwd``.

    Reuses ``ServiceSupervisor._candidate_pids``'s scan, but checks session
    leadership (``os.getsid(pid) == pid``, exactly what ``setsid`` produces)
    rather than an orphan/ppid check — this child's parent (the service's
    own ``sh``) is still alive while these tests run, so an orphan check
    would never match it.
    """
    for pid in ServiceSupervisor._candidate_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if [p.decode() for p in f.read().split(b"\0") if p] != argv:
                    continue
            if os.path.realpath(f"/proc/{pid}/cwd") != os.path.realpath(str(cwd)):
                continue
            if os.getsid(pid) != pid:
                continue
            return pid
        except (OSError, UnicodeDecodeError):
            continue
    return None


def kill_detached_sleep(cwd) -> None:
    """End every detached ``sleep`` child under ``cwd`` explicitly, so the
    pump's pipe closes on a signal instead of racing the sleep's own timer.

    A restart spawns a SECOND generation of this same detached command
    (start() backgrounds a fresh one), so one match is not always enough —
    loop until none are left rather than stopping after the first kill. The
    short sleep between attempts gives a just-forked second generation the
    moment it needs to finish its own ``setsid()`` and become matchable.
    """
    for _ in range(10):
        pid = find_session_leader(["sleep", str(DETACHED_SLEEP_S)], cwd)
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.02)
