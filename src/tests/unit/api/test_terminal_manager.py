"""Real-PTY unit tests for the terminal manager.

Exercises the actual fork/exec + non-blocking fan-out reader on a live pseudo-
terminal (no HTTP layer) — this is the ground-truth check that a spawned shell
runs on THIS machine and its output is captured, which is the whole point of
the strangler migration into the BYOD data-plane.
"""
from __future__ import annotations

import asyncio

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
