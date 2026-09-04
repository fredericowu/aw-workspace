"""PTY terminal session manager — aw-workspace (BYOD data-plane) port.

Slimmed strangler-fig port of the monolith's ``src/api/terminal_manager.py``.
Spawns interactive login-shell sessions in pseudo-terminals ON THIS machine
(the BYOD workspace container) and fans PTY output out to one or more
WebSocket subscribers.

What was deliberately dropped vs. the monolith (see MIGRATION.md):

* GNU ``screen`` backing + cross-restart reattach + the ``screen_sessions`` /
  ``agent_sessions`` / ``window_sessions`` DB tables. Sessions here are
  in-memory only and sharded by nothing — this is the reason
  ``AW_WORKSPACE_WORKERS`` still ships as 1 (see the Dockerfile/compose)
  even though the boot path (``src/api/app.py``'s ``create_app()``/
  ``lifespan``) and the periodic watchdog tasks are themselves safe at
  N>1 now. A worker bump only becomes safe end-to-end once this module's
  sessions are sharded too — restart persistence is a later card.
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


# PTY children this module forked, so the SIGCHLD handler reaps ONLY those.
_OWN_CHILD_PIDS: set[int] = set()


def _reap_children(signum, frame):
    """SIGCHLD handler — reap this module's OWN zombie PTY children.

    It used to call ``os.waitpid(-1, WNOHANG)`` in a loop, which reaps ANY
    child of this process — including one ``subprocess.run`` is waiting on.
    When the handler wins that race, ``Popen.wait()`` gets ``ECHILD`` and
    Python reports **returncode 0**: a failed command silently becomes a
    successful one, process-wide, for anything importing this module.

    That is not theoretical here. ``src/apps/commands.py`` runs every app's
    installer through ``subprocess.run`` and decides "installed" from its exit
    code, so this handler could turn a failing installer into a clean install
    — the exact silent-success failure mode the CLI health check exists to
    catch. Found 2026-08-12 via a test that started passing when it should
    have failed, in the same process as the terminal tests.

    So: only pids this module forked, and never the ``-1`` wildcard.
    """
    for pid in list(_OWN_CHILD_PIDS):
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            _OWN_CHILD_PIDS.discard(pid)  # already reaped elsewhere
            continue
        if reaped:
            _OWN_CHILD_PIDS.discard(pid)


signal.signal(signal.SIGCHLD, _reap_children)


def _next_id() -> str:
    """Globally-unique terminal window ID (UUID4)."""
    return str(_uuid_mod.uuid4())


def _ps_snapshot() -> dict[int, dict]:
    """Snapshot of all processes: {pid: {"ppid", "state", "cpu", "args"}} via one ps."""
    import subprocess
    procs: dict[int, dict] = {}
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,stat=,%cpu=,args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return procs
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0]); ppid = int(parts[1]); cpu = float(parts[3])
        except ValueError:
            continue
        procs[pid] = {
            "ppid": ppid, "state": parts[2], "cpu": cpu,
            "args": parts[4] if len(parts) > 4 else "",
        }
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


def kill_proc_tree(pid: int, timeout: float = 2.0) -> None:
    """Kill ``pid`` and every process still under it, then wait until any
    descendant left orphaned by the kill is actually reaped.

    ``pid`` is an arbitrary node inside a terminal's process tree, not a
    direct child of this process — reaping IT is its real (still-alive)
    parent's job, same as a shell always reaping the job it just ran.
    What used to leak: killing only ``pid`` left any of ITS OWN live
    children (e.g. ``dpkg-preconfigure`` under a killed ``apt-get``)
    running unsupervised, and the instant one of them finished it had no
    parent left to collect it — it reparented onto this container's PID 1
    and sat ``<defunct>`` forever, because nothing else in this module
    waits for a pid outside ``_OWN_CHILD_PIDS``.

    So: kill the whole subtree first (nothing is left running to orphan
    later), then retry ``waitpid(WNOHANG)`` on each descendant — mirroring
    ``TerminalSession.kill()``'s pattern, for the same reason: SIGKILL isn't
    instant, so the first check almost always answers "not dead yet". A
    descendant not yet reparented onto us raises ``ChildProcessError``
    (ECHILD) rather than confirming reaped, so it's retried, never mistaken
    for done. ``reap_pid1_orphans`` below is the backstop for anything
    still pending once ``timeout`` runs out.
    """
    procs = _ps_snapshot()
    subtree_pids = [p["pid"] for p in session_child_procs(pid, procs)]
    for spid in subtree_pids:
        try:
            os.kill(spid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    pending = {spid for spid in subtree_pids if spid != pid}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        for opid in list(pending):
            try:
                reaped, _ = os.waitpid(opid, os.WNOHANG)
            except ChildProcessError:
                continue  # not (yet) reparented to us, or reaped elsewhere
            if reaped:
                pending.discard(opid)
        if pending:
            time.sleep(0.05)


# Candidates seen zombie-with-ppid-us on the PREVIOUS reap_pid1_orphans()
# tick — see that function's docstring for why a pid must show up twice
# before it's touched.
_PENDING_ORPHANS: set[int] = set()


def reap_pid1_orphans() -> list[int]:
    """Periodic safety net: reap zombies genuinely orphaned onto this process.

    Complements ``_reap_children`` (which only ever reaps ``_OWN_CHILD_PIDS``)
    and ``kill_proc_tree`` (which only cleans up after itself, and only for
    its own ``timeout``) — this is the general backstop for ANY process that
    ends up reparented here without a dedicated reaper of its own, whatever
    orphaned it (``kill_proc_tree`` past its timeout, a ``killpg``'d service
    in ``src/apps/services.py`` whose own children outlive it, or a leak not
    yet found).

    A zombie with ``ppid == os.getpid()`` does not, by itself, prove it is a
    true orphan: a plain ``subprocess.run()`` call anywhere in this process
    (this module's own ``_ps_snapshot`` included) spawns a direct child with
    that exact same ppid, and that child briefly shows the same zombie state
    in the instant between exiting and that caller's own blocking ``wait()``
    reaping it. Racing THAT is exactly the 2026-08-12 bug this module's
    docstring above warns never to reintroduce — a single ``ps`` snapshot
    cannot tell the two situations apart. So this only reaps a pid once it
    has shown up as a zombie on two separate calls: a live
    ``subprocess.run()`` reaps its own child within milliseconds of exit,
    while a genuine PID-1 orphan (whose real parent is gone, not merely
    busy) stays a zombie until something collects it — so two ticks apart
    (this runs on a watchdog interval measured in tens of seconds, not a
    tight loop) is a wide margin, not a coin flip.
    """
    global _PENDING_ORPHANS
    my_pid = os.getpid()
    procs = _ps_snapshot()
    candidates = {
        pid for pid, info in procs.items()
        if info["ppid"] == my_pid
        and info.get("state", "").startswith("Z")
        and pid not in _OWN_CHILD_PIDS
    }
    ready = candidates & _PENDING_ORPHANS
    _PENDING_ORPHANS = candidates

    reaped: list[int] = []
    for pid in ready:
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if got:
            reaped.append(pid)
    if reaped:
        logger.warning("reap_pid1_orphans: reaped %d PID-1 orphan zombie(s): %s",
                        len(reaped), reaped)
    return reaped


class TerminalSession:
    """A single PTY session with fan-out to multiple subscribers."""

    _exit_callback = None

    def __init__(self, session_id: str, fd: int, pid: int, name: str,
                 session_type: str = "terminal", command: str | None = None,
                 insecure: bool = False, agent_session_id: str | None = None):
        self.id = session_id
        self.fd = fd
        self.pid = pid
        self.name = name
        self.type = session_type
        self.command = command
        self.insecure = insecure
        self.agent_session_id = agent_session_id
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
        # SIGTERM went out microseconds ago, so this WNOHANG almost always
        # answers "not dead yet" (0). Discarding the pid on that answer is what
        # leaked every closed terminal: the real SIGCHLD lands a moment later,
        # _reap_children no longer recognises the pid, and the shell stays
        # <defunct> for the life of the container — 25 of them by the time this
        # was found, on 2026-08-20. A pid stops being ours only once waitpid
        # confirms it is gone; until then keep tracking it, and let the handler
        # discard it when it collects (or when it gets ECHILD).
        try:
            reaped, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            _OWN_CHILD_PIDS.discard(self.pid)  # already reaped elsewhere
        except OSError:
            pass
        else:
            if reaped:
                _OWN_CHILD_PIDS.discard(self.pid)


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
            # Register before anything can block: the SIGCHLD handler reaps
            # only pids in this set, so a child missing from it leaks as a
            # zombie (and one that shouldn't be there steals another
            # component's exit status — see _reap_children).
            _OWN_CHILD_PIDS.add(pid)
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
        # to the workspace root), else the workspace root itself — NOT $HOME.
        # A terminal/agent (claude/codex/copilot/cursor-agent) with no caller-
        # supplied cwd always means to start in the workspace checkout, never
        # in the unrelated $HOME the PTY's login shell happens to run under
        # (found 2026-08-04: every Agents-nav-launched CLI session opened
        # Claude's "Accessing workspace: /home/ubuntu" trust prompt instead of
        # /opt/aw-workspace — the frontend never sends `cwd` at all, so this
        # default was the only thing deciding it). A missing dir falls back to
        # HOME so a typo/misconfigured env never drops the shell somewhere
        # nonexistent.
        home = os.environ.get("HOME") or os.path.expanduser("~") or "/root"
        workspace_root = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
        default_cwd = workspace_root if os.path.isdir(workspace_root) else home
        effective_cwd = default_cwd
        if cwd:
            candidate = cwd if os.path.isabs(cwd) else os.path.join(default_cwd, cwd)
            candidate = os.path.normpath(candidate)
            if os.path.isdir(candidate):
                effective_cwd = candidate
            else:
                logger.warning("create: cwd=%s not found, using %s", candidate, default_cwd)

        if command:
            inner = f"cd {_sh_quote(effective_cwd)}; {command}"
        else:
            inner = f"cd {_sh_quote(effective_cwd)}; exec {_sh_quote(shell)} -l"
        cmd_parts = ["bash", "-lc", inner]

        master_fd, pid = self._fork_exec(cmd_parts, rows, cols)
        session = TerminalSession(
            session_id, master_fd, pid, name,
            session_type=session_type, command=command,
            insecure=_is_insecure_command(command, session_type),
            agent_session_id=_extract_agent_session_id(command),
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
                rows: int = 24, cols: int = 80, new_session: bool = False,
                is_insecure: bool | None = None) -> TerminalSession | None:
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
        # `is_insecure` with no fresh `command` is the toggle UI's "detection
        # still pending" fallback (App.jsx's toggleInsecure) — flip the flag
        # in place on whatever command was already running instead of
        # silently dropping the request (the bug this whole block fixes).
        if is_insecure is not None and command is None:
            old_command = _set_command_insecure(old_command, old_type, is_insecure)
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
                "insecure": s.insecure,
                "agent_session_id": s.agent_session_id,
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


# The flag each CLI's "insecure" mode maps to (mirrors aw-workspace-ui's
# App.jsx _buildAgentCommand — the ONE place a session's command is actually
# built). "insecure" was never tracked server-side: list_sessions()/the REST
# payload hardcoded `"insecure": False` regardless of what was really running,
# and restart_terminal() silently dropped an incoming `is_insecure` whenever
# the caller didn't also resend a full `command` — the toggle UI always
# showed "secure" and re-toggling was a no-op. Fixed 2026-08-04 by deriving
# insecure state FROM the actual command string (source of truth, no separate
# bookkeeping to drift) instead of a caller-asserted flag.
_INSECURE_FLAGS = {
    "claude": "--dangerously-skip-permissions",
    "copilot": "--allow-all",
    "cursor": "--approve-mcps --yolo",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "gemini": "--yolo",
}


def _is_insecure_command(command: str | None, session_type: str) -> bool:
    flag = _INSECURE_FLAGS.get(session_type)
    return bool(command and flag and flag in command)


def _set_command_insecure(command: str | None, session_type: str, insecure: bool) -> str | None:
    """Add/remove the type's insecure flag from `command`, preserving the rest.

    Used by restart() when the caller sends `is_insecure` without a fresh
    `command` (the frontend's "detection still pending" fallback) — the old
    command is reused verbatim except for this one flag.
    """
    flag = _INSECURE_FLAGS.get(session_type)
    if not command or not flag:
        return command
    has_flag = flag in command
    if insecure and not has_flag:
        return f"{command} {flag}"
    if not insecure and has_flag:
        return " ".join(command.replace(flag, "").split())
    return command


# Ported from the monolith (agentic-workspace/src/api/terminal_manager.py's
# TerminalManager._extract_resume_argument/_extract_agent_session_id) —
# found 2026-08-04: this BYOD port hardcoded "agent_session_id": None
# everywhere, so the Agents-nav flyout's "detection still pending" state
# (App.jsx/aw-app-code-agent-clis's plugin.jsx) could never resolve — every
# launched session showed "starting…" forever, AND (since the flyout's
# dedup between live terminals and on-disk-discovered sessions keys off
# agent_session_id) the same session appeared a second time as a spurious
# "discovered" entry with a garbled name. The id is trivially recoverable
# from the launch command itself — every launch already embeds
# `--session-id <uuid>` (new) or `--resume <uuid>`/`resume <uuid>`
# (reused) — no file-watching/DB needed for this.
def _extract_resume_argument(command: str | None) -> str | None:
    """Extract ``--resume <id>`` (or codex's bare ``resume <id>``) from a
    command string."""
    if not command:
        return None
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for i, p in enumerate(parts):
        if p == "--resume" and i + 1 < len(parts):
            return parts[i + 1]
        if p == "resume" and i > 0 and parts[i - 1] == "codex" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _extract_agent_session_id(command: str | None) -> str | None:
    """Extract an agent conversation id from resume/start command syntax."""
    resume_arg = _extract_resume_argument(command)
    if resume_arg:
        return resume_arg
    if not command:
        return None
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for i, p in enumerate(parts):
        if p == "--session-id" and i + 1 < len(parts):
            return parts[i + 1]
    return None
