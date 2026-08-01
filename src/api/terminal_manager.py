"""PTY terminal session manager — aw-workspace (BYOD data-plane) port.

Slimmed strangler-fig port of the monolith's ``src/api/terminal_manager.py``.
Spawns interactive login-shell sessions in pseudo-terminals ON THIS machine
(the BYOD workspace container) and fans PTY output out to one or more
WebSocket subscribers.

What was deliberately dropped vs. the monolith (see MIGRATION.md):

* GNU ``screen`` backing + cross-restart reattach + the ``screen_sessions`` /
  ``agent_sessions`` / ``window_sessions`` DB tables. Sessions here are
  in-memory only, so the workspace runs single-worker (see
  ``AW_WORKSPACE_WORKERS`` note in the Dockerfile/compose). Restart
  persistence is a later card.
* Agent-CLI (claude/codex/cursor/gemini) session-id detection + ``--resume``
  reconstruction + the Claude ``PromptDetector``. The slim BYOD image ships
  no agent CLIs, so a terminal is just a shell (or an arbitrary command).

The PTY mechanics (fork/exec, non-blocking fan-out reader, resize, chunked
write, scrollback) mirror the monolith exactly so the ``/ws/terminal`` byte
contract is unchanged.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
import uuid as _uuid_mod

logger = logging.getLogger("terminal")


def _reap_children(signum, frame):
    """SIGCHLD handler — reap all zombie children without blocking."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break


signal.signal(signal.SIGCHLD, _reap_children)


def _next_id() -> str:
    """Globally-unique terminal window ID (UUID4)."""
    return str(_uuid_mod.uuid4())


def _ps_snapshot() -> dict[int, dict]:
    """Snapshot of all processes: {pid: {"ppid", "cpu", "args"}} via one ps."""
    import subprocess
    procs: dict[int, dict] = {}
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,%cpu=,args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return procs
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0]); ppid = int(parts[1]); cpu = float(parts[2])
        except ValueError:
            continue
        procs[pid] = {"ppid": ppid, "cpu": cpu, "args": parts[3] if len(parts) > 3 else ""}
    return procs


def _proc_basename(args: str) -> str:
    """Basename of a command line's first token, no args, leading '-' stripped."""
    if not args:
        return "?"
    first = args.split()[0].lstrip("-")
    return os.path.basename(first) or first or "?"


def session_child_procs(root_pid: int | None, procs: dict[int, dict] | None = None) -> list[dict]:
    """List processes running inside a session — the descendants of ``root_pid``.

    ``root_pid`` is the PID of the session's login shell (the direct child of
    this process). Walks the process tree rooted there and returns every
    descendant as ``{"pid": int, "name": str, "cpu": float}``. The shell
    itself is included so the badge is never empty for a live terminal.
    """
    if not root_pid:
        return []
    if procs is None:
        procs = _ps_snapshot()
    children: dict[int, list[int]] = {}
    for pid, info in procs.items():
        children.setdefault(info["ppid"], []).append(pid)
    result: list[dict] = []
    seen: set[int] = set()
    stack: list[int] = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        info = procs.get(pid)
        if info is None:
            continue
        name = _proc_basename(info.get("args", ""))
        result.append({"pid": pid, "name": name, "cpu": round(info.get("cpu", 0.0), 1)})
        for child in children.get(pid, []):
            stack.append(child)
    result.sort(key=lambda p: p["pid"])
    return result


class TerminalSession:
    """A single PTY session with fan-out to multiple subscribers."""

    _exit_callback = None

    def __init__(self, session_id: str, fd: int, pid: int, name: str,
                 session_type: str = "terminal", command: str | None = None):
        self.id = session_id
        self.fd = fd
        self.pid = pid
        self.name = name
        self.type = session_type
        self.command = command
        self.alive = True
        self._subscribers: set[asyncio.Queue] = set()
        self._reader_started = False
        self._scrollback: list[bytes] = []
        self._scrollback_max = 50

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def get_scrollback(self) -> bytes:
        return b"".join(self._scrollback)

    def _fan_out(self, data: bytes):
        if data:
            self._scrollback.append(data)
            if len(self._scrollback) > self._scrollback_max:
                self._scrollback = self._scrollback[-self._scrollback_max:]
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def start_reader(self, loop: asyncio.AbstractEventLoop):
        """Start the PTY fd reader (once per session, fans out to all subscribers)."""
        if self._reader_started:
            return
        self._reader_started = True

        def on_readable():
            try:
                data = os.read(self.fd, 65536)
                if data:
                    self._fan_out(data)
                else:
                    self._on_eof(loop)
            except OSError:
                self._on_eof(loop)

        loop.add_reader(self.fd, on_readable)

    def _on_eof(self, loop):
        self._fan_out(b"")
        self.alive = False
        try:
            loop.remove_reader(self.fd)
        except Exception:
            pass
        self._reader_started = False
        if TerminalSession._exit_callback:
            try:
                TerminalSession._exit_callback(self.id, self.type)
            except Exception:
                pass

    def stop_reader(self, loop: asyncio.AbstractEventLoop):
        if not self._reader_started:
            return
        try:
            loop.remove_reader(self.fd)
        except Exception:
            pass
        self._reader_started = False

    def resize(self, rows: int, cols: int):
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def write(self, data: bytes):
        """Write input to the PTY, chunked to avoid buffer overflow."""
        try:
            CHUNK = 128
            for i in range(0, len(data), CHUNK):
                os.write(self.fd, data[i:i + CHUNK])
        except OSError:
            self.alive = False

    def kill(self):
        self.alive = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass


class TerminalManager:
    """Manages multiple in-memory PTY sessions (single-worker)."""

    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}

    def _fork_exec(self, cmd_parts: list[str], rows: int = 24, cols: int = 80) -> tuple[int, int]:
        """Fork a child connected to a PTY. Returns (master_fd, pid)."""
        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()
        if pid == 0:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLORTERM"] = "truecolor"
            # Ensure HOME/USER/LOGNAME so login shells don't warn about getpwuid().
            try:
                import pwd as _pwd
                _pw = _pwd.getpwuid(os.getuid())
                os.environ.setdefault("USER", _pw.pw_name)
                os.environ.setdefault("LOGNAME", _pw.pw_name)
                os.environ.setdefault("HOME", _pw.pw_dir)
            except (KeyError, ImportError):
                _u = os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())
                os.environ.setdefault("USER", _u)
                os.environ.setdefault("LOGNAME", _u)
                os.environ.setdefault("HOME", os.path.expanduser("~") or "/root")
            os.execvp(cmd_parts[0], cmd_parts)
        else:
            os.close(slave_fd)
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return master_fd, pid

    @staticmethod
    def _shell() -> str:
        import shutil
        return os.environ.get("SHELL") or shutil.which("bash") or "/bin/bash"

    def create(self, name: str | None = None, rows: int = 24, cols: int = 80,
               command: str | None = None, session_type: str = "terminal",
               session_id: str | None = None, initial_prompt: str | None = None,
               cwd: str | None = None) -> TerminalSession:
        """Spawn a login shell (or ``command``) in a PTY on this machine."""
        session_id = session_id or _next_id()
        name = name or session_id
        shell = self._shell()

        # Resolve the starting directory: caller-supplied (absolute or relative
        # to HOME), else HOME. A missing dir falls back to HOME so a typo never
        # drops the shell somewhere surprising.
        home = os.environ.get("HOME") or os.path.expanduser("~") or "/root"
        effective_cwd = home
        if cwd:
            candidate = cwd if os.path.isabs(cwd) else os.path.join(home, cwd)
            candidate = os.path.normpath(candidate)
            if os.path.isdir(candidate):
                effective_cwd = candidate
            else:
                logger.warning("create: cwd=%s not found, using HOME", candidate)

        if command:
            inner = f"cd {_sh_quote(effective_cwd)}; {command}"
        else:
            inner = f"cd {_sh_quote(effective_cwd)}; exec {_sh_quote(shell)} -l"
        cmd_parts = ["bash", "-lc", inner]

        master_fd, pid = self._fork_exec(cmd_parts, rows, cols)
        session = TerminalSession(
            session_id, master_fd, pid, name,
            session_type=session_type, command=command,
        )
        self.sessions[session_id] = session
        logger.info("Terminal created: %s (%s, type=%s, pid=%d)", session_id, name, session_type, pid)

        if initial_prompt:
            import threading

            def _send_prompt():
                time.sleep(5)
                try:
                    session.write((initial_prompt + "\r").encode("utf-8"))
                except Exception as e:
                    logger.warning("Failed to send initial prompt to %s: %s", session_id, e)

            threading.Thread(target=_send_prompt, daemon=True).start()

        return session

    def restart(self, session_id: str, command: str | None = None, name: str | None = None,
                rows: int = 24, cols: int = 80, new_session: bool = False) -> TerminalSession | None:
        """Kill the existing session and spawn a fresh one with the same ID."""
        old = self.sessions.pop(session_id, None)
        if old:
            try:
                loop = asyncio.get_event_loop()
                old.stop_reader(loop)
            except Exception:
                pass
            old.kill()
        old_type = old.type if old else "terminal"
        old_name = name or (old.name if old else session_id)
        old_command = command if command is not None else (old.command if old else None)
        return self.create(
            name=old_name, rows=rows, cols=cols, command=old_command,
            session_type=old_type, session_id=session_id,
        )

    def get(self, session_id: str) -> TerminalSession | None:
        return self.sessions.get(session_id)

    def remove(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            try:
                loop = asyncio.get_event_loop()
                session.stop_reader(loop)
            except Exception:
                pass
            session.kill()
            logger.info("Terminal removed: %s", session_id)

    def list_sessions(self, include_hidden: bool = False) -> list[dict]:
        dead = [sid for sid, s in self.sessions.items() if not s.alive]
        for sid in dead:
            self.sessions.pop(sid, None)
        return [
            {
                "id": s.id,
                "name": s.name,
                "type": s.type,
                "alive": s.alive,
                "insecure": False,
                "agent_session_id": None,
            }
            for s in self.sessions.values()
        ]

    def cleanup(self):
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            loop = None
        for session in list(self.sessions.values()):
            if loop:
                session.stop_reader(loop)
            session.kill()
        self.sessions.clear()


def _sh_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
