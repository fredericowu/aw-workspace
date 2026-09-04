"""Real-PTY unit tests for the terminal manager.

Exercises the actual fork/exec + non-blocking fan-out reader on a live pseudo-
terminal (no HTTP layer) — this is the ground-truth check that a spawned shell
runs on THIS machine and its output is captured, which is the whole point of
the strangler migration into the BYOD data-plane.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from src.api.terminal_manager import TerminalManager


async def _wait_for(session, needle: str, timeout: float = 10.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
        text = session.get_scrollback().decode("utf-8", "replace")
        if needle in text:
            return text
    return session.get_scrollback().decode("utf-8", "replace")


@pytest.mark.integration
def test_pty_shell_runs_and_echoes():
    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="unit")
        try:
            session.start_reader(asyncio.get_running_loop())
            await asyncio.sleep(0.8)  # let the login shell come up
            session.write(b"echo READY_$((6*7))\n")
            text = await _wait_for(session, "READY_42")
            assert "READY_42" in text, repr(text[-200:])

            # procs: the shell (and descendants) are visible for the badge.
            procs = __import__(
                "src.api.terminal_manager", fromlist=["session_child_procs"]
            ).session_child_procs(session.pid)
            assert len(procs) >= 1
            assert all({"pid", "name", "cpu"} <= p.keys() for p in procs)

            # resize is a no-op-safe ioctl; must not raise.
            session.resize(40, 120)
        finally:
            mgr.remove(session.id)
        assert mgr.get(session.id) is None

    asyncio.run(run())


@pytest.mark.integration
def test_command_session_runs_given_command():
    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="cmd", command="echo ONESHOT_$((1+1)); sleep 30", session_type="terminal")
        try:
            session.start_reader(asyncio.get_running_loop())
            text = await _wait_for(session, "ONESHOT_2")
            assert "ONESHOT_2" in text, repr(text[-200:])
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


@pytest.mark.integration
def test_default_cwd_is_workspace_root_not_home(tmp_path, monkeypatch):
    """No caller-supplied cwd (the Agents-nav path, launching claude/codex/
    copilot/cursor-agent) must start in the workspace root, not $HOME —
    regression for the 2026-08-04 bug where every agent session opened
    Claude's "Accessing workspace: /home/ubuntu" trust prompt instead of
    /opt/aw-workspace."""
    monkeypatch.setenv("AW_WORKSPACE_CONTAINER_DIR", str(tmp_path))

    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="cwd-check", command="pwd", session_type="terminal")
        try:
            session.start_reader(asyncio.get_running_loop())
            text = await _wait_for(session, str(tmp_path))
            assert str(tmp_path) in text, repr(text[-200:])
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


@pytest.mark.integration
def test_fanout_to_multiple_subscribers():
    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="fan")
        try:
            session.start_reader(asyncio.get_running_loop())
            q1 = session.subscribe()
            q2 = session.subscribe()
            await asyncio.sleep(0.8)
            session.write(b"echo FAN_$((2*2))\n")
            # Both subscriber queues receive the same PTY bytes.
            got1 = got2 = b""
            deadline = asyncio.get_running_loop().time() + 10
            while asyncio.get_running_loop().time() < deadline:
                try:
                    got1 += await asyncio.wait_for(q1.get(), 0.3)
                except asyncio.TimeoutError:
                    pass
                try:
                    got2 += await asyncio.wait_for(q2.get(), 0.3)
                except asyncio.TimeoutError:
                    pass
                if b"FAN_4" in got1 and b"FAN_4" in got2:
                    break
            assert b"FAN_4" in got1 and b"FAN_4" in got2
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


def test_is_insecure_command_detects_type_specific_flag():
    from src.api.terminal_manager import _is_insecure_command

    assert _is_insecure_command("claude --session-id x --dangerously-skip-permissions", "claude")
    assert not _is_insecure_command("claude --session-id x", "claude")
    assert _is_insecure_command("copilot --allow-all", "copilot")
    assert not _is_insecure_command(None, "claude")
    assert not _is_insecure_command("claude --session-id x", "terminal")  # no flag mapped


def test_set_command_insecure_adds_and_removes_flag():
    from src.api.terminal_manager import _set_command_insecure

    on = _set_command_insecure("claude --session-id x", "claude", True)
    assert on == "claude --session-id x --dangerously-skip-permissions"

    off = _set_command_insecure(on, "claude", False)
    assert off == "claude --session-id x"

    # idempotent — already in the target state is a no-op
    assert _set_command_insecure(on, "claude", True) == on
    assert _set_command_insecure(off, "claude", False) == off


@pytest.mark.integration
def test_insecure_state_reported_and_toggle_flips_it():
    """Regression for 2026-08-04: list_sessions()/the REST payload hardcoded
    insecure=False regardless of the actual running command, and restart's
    `is_insecure` was silently dropped whenever no fresh `command` was also
    sent — the toggle UI always showed "secure" and re-toggling did nothing."""
    async def run():
        mgr = TerminalManager()
        session = mgr.create(
            name="sec", command="claude --session-id x --dangerously-skip-permissions",
            session_type="claude",
        )
        try:
            session.start_reader(asyncio.get_running_loop())
            listed = {s["id"]: s for s in mgr.list_sessions()}
            assert listed[session.id]["insecure"] is True

            # Toggle to secure WITHOUT resending `command` — the frontend's
            # "detection still pending" fallback path.
            restarted = mgr.restart(session.id, name="sec", is_insecure=False)
            restarted.start_reader(asyncio.get_running_loop())
            listed = {s["id"]: s for s in mgr.list_sessions()}
            assert listed[session.id]["insecure"] is False
            assert "--dangerously-skip-permissions" not in restarted.command
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


def test_extract_agent_session_id_from_session_id_flag():
    from src.api.terminal_manager import _extract_agent_session_id

    assert _extract_agent_session_id(
        "claude --session-id abc-123 --dangerously-skip-permissions"
    ) == "abc-123"


def test_extract_agent_session_id_from_resume_flag():
    from src.api.terminal_manager import _extract_agent_session_id

    assert _extract_agent_session_id("claude --resume abc-123") == "abc-123"
    assert _extract_agent_session_id("cursor-agent --resume xyz-789") == "xyz-789"


def test_extract_agent_session_id_from_codex_bare_resume():
    from src.api.terminal_manager import _extract_agent_session_id

    assert _extract_agent_session_id("codex resume abc-123") == "abc-123"


def test_extract_agent_session_id_none_for_plain_terminal():
    from src.api.terminal_manager import _extract_agent_session_id

    assert _extract_agent_session_id(None) is None
    assert _extract_agent_session_id("bash -l") is None


@pytest.mark.integration
def test_created_session_reports_real_agent_session_id():
    """Regression for 2026-08-04: agent_session_id was hardcoded None in
    list_sessions()/the REST payload, so the Agents-nav flyout's "detection
    still pending" state never resolved (stuck "starting…" forever) and the
    session showed a second time as a spurious on-disk-discovered duplicate."""
    async def run():
        mgr = TerminalManager()
        session = mgr.create(
            name="claude - abcd",
            command="claude --session-id real-uuid-1234 --dangerously-skip-permissions",
            session_type="claude",
        )
        try:
            session.start_reader(asyncio.get_running_loop())
            listed = {s["id"]: s for s in mgr.list_sessions()}
            assert listed[session.id]["agent_session_id"] == "real-uuid-1234"
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


@pytest.mark.integration
def test_sigchld_handler_does_not_steal_another_component_s_exit_status():
    """The handler used to reap ANY child (`waitpid(-1)`), including one
    `subprocess.run` was waiting on — Popen.wait() then got ECHILD and Python
    reported **returncode 0**, turning a failed command into a successful one
    process-wide. src/apps/commands.py decides "installed" from exactly that
    exit code, so a failing app installer could read as a clean install.
    """
    import subprocess

    from src.api.terminal_manager import TerminalManager

    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="reaper", command="sleep 5", session_type="terminal")
        try:
            session.start_reader(asyncio.get_running_loop())
            await asyncio.sleep(0.3)
            # A command that must be seen to fail, while a PTY child is alive
            # and SIGCHLD is firing.
            for _ in range(5):
                proc = subprocess.run(["bash", "-c", "exit 7"], capture_output=True)
                assert proc.returncode == 7, "SIGCHLD handler stole the exit status"
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


def _proc_state(pid: int) -> str | None:
    """State letter from /proc/<pid>/stat, or None once the pid is gone.

    The comm field can contain spaces and parentheses, so split after the last
    ')' rather than on whitespace.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    return raw[raw.rindex(")") + 2].strip() or None


@pytest.mark.integration
def test_closed_terminal_does_not_leak_a_defunct_shell(monkeypatch):
    """Closing a terminal must actually reap its shell.

    ``kill()`` used to ``waitpid(WNOHANG)`` microseconds after sending SIGTERM
    — which all but always answers "not dead yet" — and then drop the pid from
    ``_OWN_CHILD_PIDS`` regardless of that answer. The real SIGCHLD landed a
    moment later, ``_reap_children`` no longer recognised the pid, and the
    shell stayed ``<defunct>`` for the life of the container. Found 2026-08-20
    with 25 leaked shells in a workspace up 2.7 days.

    Pinned to the direct-PTY backing (W5, 2026-09-04). The race this test
    creates depends on the PTY child ignoring SIGTERM, and it gets that from a
    ``trap`` in the session's own command — which on a screen-backed session
    runs INSIDE the screen, while ``session.pid`` is the ``screen -x`` attach
    that has no such trap and dies instantly. The reaping code under test is
    the same for both backings (``_OWN_CHILD_PIDS`` + ``kill()``'s waitpid),
    so this pins the one where the race can be forced; the screen attach's own
    reaping is covered by test_w5_terminal_multiworker.py.
    """
    import time

    from src.api import terminal_manager as tm

    monkeypatch.setattr(tm, "_SCREEN_BIN", None)
    monkeypatch.setattr(tm, "_find_screen", lambda: None)

    async def run():
        # The shell ignores both the SIGHUP from closing the PTY master and the
        # SIGTERM kill() sends, so it is guaranteed to still be alive when
        # kill()'s WNOHANG runs — which is exactly the race a shell that is
        # merely *slow* to exit loses in production. Without it the child
        # usually dies fast enough to be collected right there and the leak
        # hides.
        mgr = TerminalManager()
        session = mgr.create(name="reap", command="trap '' TERM HUP; sleep 3")
        pid = session.pid
        session.start_reader(asyncio.get_running_loop())
        await asyncio.sleep(0.5)
        assert pid in tm._OWN_CHILD_PIDS, "PTY child was never registered"
        assert _proc_state(pid) not in (None, "Z")

        mgr.remove(session.id)  # -> session.kill()
        assert _proc_state(pid) not in (None, "Z"), "shell died before the race could happen"

        # The shell outlives its `sleep 3`, so poll well past that: the pid must
        # leave the process table entirely. Stopping at "not a zombie *yet*"
        # is what let the buggy version pass — at that point it was still
        # running, not reaped.
        deadline = time.time() + 20
        while time.time() < deadline and _proc_state(pid) is not None:
            await asyncio.sleep(0.2)

        state = _proc_state(pid)
        assert state is None, f"shell {pid} never reaped (state={state!r})"
        assert pid not in tm._OWN_CHILD_PIDS, f"pid {pid} still tracked"

    asyncio.run(run())


def _set_child_subreaper() -> None:
    """Make this test process a Linux "subreaper" (PR_SET_CHILD_SUBREAPER) so
    an orphaned grandchild reparents onto US instead of the real system
    init. Mirrors what being a container's PID 1 gives ``kill_proc_tree``/
    ``reap_pid1_orphans`` for free in production — pytest itself is not
    PID 1, so without this an orphan in these tests would reparent
    somewhere we can't observe or reap from.
    """
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    PR_SET_CHILD_SUBREAPER = 36
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _extract_pid(text: str, marker: str) -> int:
    """Digits immediately after the LAST occurrence of ``marker`` in PTY
    scrollback text.

    A PTY echoes typed input back before executing it, so a command like
    ``echo MID_PID=$!`` makes ``marker`` (``"MID_PID="``) appear TWICE: once
    literally (with ``$!`` unexpanded, in the echoed keystrokes) and once
    with the real value (after execution). Search from the end for the
    real one, and require a digit immediately after — landing on the
    unexpanded echo would otherwise silently scan forward into unrelated
    terminal escape sequences (e.g. ``\\x1b[?2004h``) and return THEIR
    digits instead.
    """
    idx = text.rindex(marker) + len(marker)
    digits = ""
    for ch in text[idx:]:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        raise ValueError(f"{marker!r} not immediately followed by digits in {text!r}")
    return int(digits)


@pytest.mark.integration
def test_kill_proc_tree_reaps_orphaned_grandchildren():
    """``kill_proc_tree`` must not just kill a mid-tree pid — it must also
    kill and reap whatever of ITS OWN children were still alive.

    This is the exact production leak: ``kill_proc`` used to bare
    ``os.kill()`` the target pid only, leaving a live child (e.g.
    dpkg-preconfigure/git/gh under a killed apt-get/dpkg/bash) running
    unsupervised — the instant it finished it had no parent left to collect
    it and sat ``<defunct>`` forever (see ``kill_proc_tree``'s docstring).
    """
    import time

    from src.api import terminal_manager as tm

    _set_child_subreaper()

    async def run():
        mgr = TerminalManager()
        session = mgr.create(name="subtree")
        try:
            session.start_reader(asyncio.get_running_loop())
            await asyncio.sleep(0.5)

            # MID backgrounds LEAF and never waits for it — killing MID
            # alone (the old behaviour) is exactly what orphaned LEAF.
            session.write(
                b"bash -c 'trap \"\" TERM HUP; sleep 30 & echo LEAF_PID=$!; wait' & "
                b"echo MID_PID=$!\n"
            )
            text = await _wait_for(session, "LEAF_PID=")
            if "MID_PID=" not in text:
                text = await _wait_for(session, "MID_PID=")
            leaf_pid = _extract_pid(text, "LEAF_PID=")
            mid_pid = _extract_pid(text, "MID_PID=")

            assert _proc_state(mid_pid) not in (None, "Z")
            assert _proc_state(leaf_pid) not in (None, "Z")

            await asyncio.to_thread(tm.kill_proc_tree, mid_pid)

            deadline = time.time() + 10
            while time.time() < deadline and (
                _proc_state(mid_pid) is not None or _proc_state(leaf_pid) is not None
            ):
                await asyncio.sleep(0.1)

            assert _proc_state(mid_pid) is None, f"MID {mid_pid} never reaped"
            assert _proc_state(leaf_pid) is None, (
                f"LEAF {leaf_pid} never reaped — this is the zombie leak "
                "kill_proc_tree exists to close"
            )
        finally:
            mgr.remove(session.id)

    asyncio.run(run())


@pytest.mark.integration
def test_reap_pid1_orphans_reaps_true_orphan_after_two_ticks():
    """A genuine PID-1 orphan (reparented here because its real parent
    already exited) must be reaped by ``reap_pid1_orphans`` — but only after
    showing up as a zombie on two SEPARATE calls, never on the first.

    That throttle is what keeps this from ever racing the 2026-08-12 bug
    (see the function's docstring): a live ``subprocess.run()`` reaps its
    own child within milliseconds, long before this test's own second call,
    which — unlike a real watchdog tick — happens immediately, so requiring
    two sightings is the only thing standing between "safe" and "steals a
    live wait()'s exit status" in this test too.
    """
    import time

    from src.api import terminal_manager as tm

    _set_child_subreaper()
    tm._PENDING_ORPHANS = set()

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        gpid = os.fork()
        if gpid == 0:
            os.close(w)
            time.sleep(0.3)
            os._exit(0)
        os.write(w, str(gpid).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    try:
        grandchild_pid = int(os.read(r, 32).decode())
    finally:
        os.close(r)
    os.waitpid(pid, 0)  # reap the immediate child; grandchild is now our orphan

    deadline = time.time() + 5
    while time.time() < deadline and _proc_state(grandchild_pid) != "Z":
        time.sleep(0.05)
    assert _proc_state(grandchild_pid) == "Z", "grandchild never became a zombie"

    first = tm.reap_pid1_orphans()
    assert grandchild_pid not in first, "reaped on first sighting — should wait a tick"
    assert _proc_state(grandchild_pid) == "Z", "orphan disappeared before its second tick"

    second = tm.reap_pid1_orphans()
    assert grandchild_pid in second, "not reaped on its second consecutive sighting"
    assert _proc_state(grandchild_pid) is None
