"""PTY terminal session manager — aw-workspace (BYOD data-plane) port.

Slimmed strangler-fig port of the monolith's ``src/api/terminal_manager.py``.
Spawns interactive login-shell sessions in pseudo-terminals ON THIS machine
(the BYOD workspace container) and fans PTY output out to one or more
WebSocket subscribers.

W5 restored the GNU ``screen`` backing this port originally dropped. A PTY
master fd is a *file descriptor*, so it cannot be handed to another process:
whichever worker forked the shell was the only one that could ever serve
``/ws/terminal/<id>`` for it. ``screen`` breaks that ownership because the
screen server is a process external to every worker — any worker can
``screen -x`` into it. The three guarantees ported from aw-backend's F5
(``repos/aw-backend/src/api/terminal_manager.py``):

1. Attach is ALWAYS ``screen -x`` (shared, non-owning), never ``-r``
   (which steals the session from whoever else is attached). Note the
   semantics this buys: ``screen -x`` resizes the window to the SMALLEST
   attached client, so two browsers on one terminal see the smaller one's
   geometry.
2. Session metadata lives in a Redis hash (``…:term:meta:<session_id>``),
   not in per-process memory, so every worker can discover — and attach to
   — a session it did not create.
3. Concurrent creation of the same screen name is deduped with
   ``SET …:term:creating:<name> NX EX 30``: two workers handed simultaneous
   creates produce ONE screen.

Both backings are kept, and which one runs is decided by whether a ``screen``
binary exists (``screen_backing_enabled()``). That is not a hedge — the
workspace image did not ship ``screen`` until this card added it, and
``/opt/aw-workspace`` is a bind mount, so a core deploy lands new code on a
*running, older* container (see the deploy path in MIGRATION.md). Falling back
to the direct PTY there is what makes this change safe to ship ahead of the
image rebuild, and it is byte-for-byte the pre-W5 behaviour. Same for Redis:
with none reachable the meta store no-ops and the creation claim always
succeeds, which is exactly single-worker behaviour.

Still dropped vs. the monolith (see MIGRATION.md):

* The ``screen_sessions`` / ``agent_sessions`` / ``window_sessions`` DB
  tables. Session metadata lives in Redis now, not Postgres; sessions
  survive a worker restart because the screen does, but the workspace does
  not re-enumerate them into the SPA across a full restart.
* Agent-CLI (claude/codex/cursor/gemini) session-id detection + ``--resume``
  reconstruction + the Claude ``PromptDetector``. The slim BYOD image ships
  no agent CLIs, so a terminal is just a shell (or an arbitrary command).
* A session whose ``command`` exits is gone, not inspectable. A screen dies
  with its command, so a one-shot — or, far more commonly, a
  command-not-found — leaves no session to list, attach to, or read
  scrollback from; the direct-PTY path kept the dead session around with its
  output. This is a knowing, permanent exception to this card's "behaviour
  identical at workers=1" rule, and it is NOT gated on worker count:
  ``screen_backing_enabled()`` keys off the ``screen`` binary alone. Harmless
  today, because every SPA terminal is a login shell that never exits and no
  agent CLI ships in this image, but it turns a visible "command not found"
  into a terminal that simply vanishes. ``_create_screen()`` logs the
  ambiguity. Surfacing it to the *user* is a follow-up card
  (3d15bf3b-9510-81ae-bce6-cd0efad541ef), and deliberately NOT
  ``; exec $SHELL -l`` here — that would make every command-backed session
  immortal and leak a screen server per launch.

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
import threading as _threading_mod
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


# ---------------------------------------------------------------------------
# W5: Redis-backed session metadata + screen-creation dedup
# ---------------------------------------------------------------------------
#
# Keys are scoped under the same ``aw:ws:<slug>:`` prefix every other
# cross-worker primitive in this workspace uses (see src/libs/redis_coord.py's
# key layout) rather than aw-backend's flat ``aw:term:*`` — one shared Redis
# can host several workspaces, and a terminal id colliding across them would
# hand one workspace's shell to another.

_META_SUFFIX = "term:meta:"
_CREATING_SUFFIX = "term:creating:"
_CREATING_TTL = 30

_redis_client = None
_redis_lock = _threading_mod.Lock()


def _term_key(suffix: str, name: str) -> str:
    from src.libs.redis_coord import get_workspace_slug
    return f"aw:ws:{get_workspace_slug()}:{suffix}{name}"


def _get_redis():
    """Lazily-connected SYNC Redis client, best-effort (``None`` if absent).

    Sync, not ``redis.asyncio``, on purpose: every caller here runs on the
    fork/exec path, which is already blocking and is reached from
    ``asyncio.to_thread``-able REST handlers — an async client would force
    this module's whole surface to become async for no gain.

    The address comes from ``redis_coord.get_workspace_redis_url()`` so this
    store can never disagree with ``RedisBroadcaster``/``RedisLease`` about
    which Redis it is on. Failure is deliberately silent-but-logged and
    degrades to ``None``: a workspace with no Redis must still serve
    terminals exactly as it does today, which is the card's golden rule.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    with _redis_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis
            from src.libs.redis_coord import get_workspace_redis_url
            client = redis.Redis.from_url(
                get_workspace_redis_url(),
                decode_responses=True, socket_connect_timeout=1)
            client.ping()
            _redis_client = client
        except Exception as exc:
            logger.warning("terminal_manager: Redis unavailable (%s) — session "
                           "meta is process-local, so terminals only work on "
                           "the worker that created them", exc)
            _redis_client = None
    return _redis_client


def _reset_redis_client() -> None:
    """Drop the cached client so the next call re-resolves the URL. Tests
    only — they point ``AW_REDIS_URL`` at a throwaway instance after this
    module has already been imported."""
    global _redis_client
    with _redis_lock:
        _redis_client = None


class SessionMetaStore:
    """Per-session terminal metadata, one Redis hash per session.

    This is the piece that makes a terminal discoverable from a worker that
    did not create it: the PTY fd stays process-local forever, but
    ``screen_name`` — the only thing a second worker needs in order to
    ``screen -x`` its way in — does not.

    Every method degrades to a no-op / empty read with no Redis rather than
    raising, for the reason in ``_get_redis``: no Redis means one worker,
    and one worker never needs this store.
    """

    _BOOL_FIELDS = {"insecure", "hidden"}

    def get(self, session_id: str) -> dict:
        client = _get_redis()
        if client is None or not session_id:
            return {}
        try:
            return self._decode(client.hgetall(_term_key(_META_SUFFIX, session_id)))
        except Exception as exc:
            logger.warning("SessionMetaStore.get(%s) failed: %s", session_id, exc)
            return {}

    def update(self, session_id: str, **fields) -> None:
        client = _get_redis()
        encoded = {k: self._encode(v) for k, v in fields.items() if v is not None}
        if client is None or not session_id or not encoded:
            return
        try:
            client.hset(_term_key(_META_SUFFIX, session_id), mapping=encoded)
        except Exception as exc:
            logger.warning("SessionMetaStore.update(%s) failed: %s", session_id, exc)

    def delete(self, session_id: str) -> None:
        client = _get_redis()
        if client is None or not session_id:
            return
        try:
            client.delete(_term_key(_META_SUFFIX, session_id))
        except Exception as exc:
            logger.warning("SessionMetaStore.delete(%s) failed: %s", session_id, exc)

    def all(self) -> dict[str, dict]:
        client = _get_redis()
        if client is None:
            return {}
        prefix = _term_key(_META_SUFFIX, "")
        result: dict[str, dict] = {}
        try:
            for key in client.scan_iter(match=f"{prefix}*"):
                raw = client.hgetall(key)
                if raw:
                    result[key[len(prefix):]] = self._decode(raw)
        except Exception as exc:
            logger.warning("SessionMetaStore.all() failed: %s", exc)
        return result

    @staticmethod
    def _encode(v) -> str:
        if isinstance(v, bool):
            return "1" if v else "0"
        return str(v)

    @classmethod
    def _decode(cls, raw: dict) -> dict:
        out = dict(raw)
        for f in cls._BOOL_FIELDS:
            if f in out:
                out[f] = out[f] in ("1", "true", "True")
        return out


def _claim_screen_creation(screen_name: str) -> bool:
    """``SET …:term:creating:<name> NX EX 30`` — True if THIS caller won the
    race to create ``screen_name``.

    Best-effort by design: with no Redis it always claims, which is the
    single-worker behaviour that ships. The TTL (not a delete-on-success)
    is what makes a worker that dies mid-creation self-healing — the claim
    simply expires and the next create retries, instead of the name being
    permanently unclaimable.
    """
    client = _get_redis()
    if client is None:
        return True
    try:
        return bool(client.set(_term_key(_CREATING_SUFFIX, screen_name), "1",
                               nx=True, ex=_CREATING_TTL))
    except Exception as exc:
        logger.warning("_claim_screen_creation(%s) failed: %s", screen_name, exc)
        return True


# ---------------------------------------------------------------------------
# W5: GNU screen backing
# ---------------------------------------------------------------------------


def _find_screen() -> str | None:
    """Path to a usable ``screen``, or ``None`` if this box has none.

    ``None`` is a supported answer, not an error — see the module docstring
    for why the direct-PTY fallback has to exist.
    """
    import shutil
    return shutil.which("screen")


_SCREEN_BIN = _find_screen()


def screen_backing_enabled() -> bool:
    """Whether terminals are screen-backed (and therefore cross-worker).

    Re-resolved rather than read off the module constant so a workspace that
    installs ``screen`` at runtime (``sudo apt install screen`` from a
    terminal — this image gives every session sudo) picks it up on the next
    create instead of needing a restart.
    """
    global _SCREEN_BIN
    if _SCREEN_BIN is None:
        _SCREEN_BIN = _find_screen()
    return _SCREEN_BIN is not None


def _screenrc_path() -> str:
    """``.tmp/aw-screenrc`` under the workspace root — the shared scratch dir
    this repo's AGENTS.md designates, not ``/tmp`` (which is process-scratch
    and invisible to the screen server on a restart)."""
    root = os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    return os.path.join(root, ".tmp", "aw-screenrc")


def _ensure_screenrc() -> str:
    """Write the screenrc every screen in this workspace runs under.

    Ported verbatim in intent from aw-backend, whose comments record what
    each line is load-bearing for. The short version: xterm.js is the only
    client, and a default screen mangles what it sends.
    """
    path = _screenrc_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("defscrollback 10000\n")
        # `screen-256color` is the canonical "inside screen" terminfo — apps
        # look it up to pick escape sequences screen knows how to forward.
        # Passing `xterm-256color` through instead works for plain ANSI and
        # then breaks on truecolor/OSC/DCS, whose codes leak into the browser
        # as visible `[38;5;XXm` text.
        f.write("term screen-256color\n")
        f.write("truecolor on\n")
        f.write("startup_message off\n")
        f.write("vbell off\n")
        # Disable the alternate screen (ti/te) so xterm.js scrollback survives
        # a vim/less. Matches both an xterm* and a screen* outer TERM.
        f.write("termcapinfo xterm*|screen* ti@:te@\n")
        f.write("mousetrack off\n")
    return path


def _screen_sessions() -> dict[str, list[int]]:
    """Every live screen on this box: ``{name: [server pid, ...]}``.

    ``screen -ls`` prints one tab-indented line per session,
    ``\\t12345.aw-terminal-abc\\t(date)\\t(Detached)`` — the integer before the
    first dot is the server pid. Its exit code is non-zero whenever sessions
    exist, so it is deliberately never checked.

    One parse of one ``screen -ls`` for the whole list, because
    ``list_sessions()`` runs on every ``terminal_update`` broadcast and a
    per-session subprocess there would be N forks per keystroke-adjacent
    event.

    ``(Dead ???)`` entries are EXCLUDED, and wiped. A screen server dies with
    its container (a restart kills every process in it — a screen survives an
    app-process restart, not a container one) and leaves its socket behind,
    and ``screen -ls`` keeps listing that socket in the same shape as a live
    one. Counting those as live is the terminal-shaped version of this
    workspace's standard failure: every session from before a restart would
    still be listed for the SPA, ``get()`` would attach to a socket with no
    server, and the user would get a terminal that opens blank and never
    responds — with nothing logged anywhere. Found 2026-09-04 by reading
    ``screen -ls`` on the live workspace after a restart, which had four.
    """
    if not screen_backing_enabled():
        return {}
    import subprocess
    try:
        out = subprocess.run([_SCREEN_BIN, "-ls"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return {}
    found: dict[str, list[int]] = {}
    dead = 0
    for line in out.splitlines():
        s = line.strip()
        if not s or not s[0].isdigit():
            continue
        if "(dead" in s.lower():
            dead += 1
            continue
        pid_str, _, nm = s.split()[0].partition(".")
        if nm and pid_str.isdigit():
            found.setdefault(nm, []).append(int(pid_str))
    if dead:
        _wipe_dead_screens(dead)
    return found


#: Last time ``screen -wipe`` ran, so a box with dead sockets doesn't fork one
#: per liveness check. Correctness never depends on the wipe — the parse above
#: already excludes dead entries — so throttling it costs nothing.
_last_wipe = 0.0
_WIPE_INTERVAL = 60.0


def _wipe_dead_screens(dead: int) -> None:
    """Reap dead sockets, at most once a minute.

    Unthrottled this was a real problem, not a theoretical one: ``_create_screen``
    polls ``_screen_exists`` up to 20 times, and with 5 dead sockets left by a
    container restart that meant 20 × (``screen -ls`` + ``screen -wipe``)
    subprocesses on a single create — which is what turned one POST
    /api/terminals into a >10s call on 2026-09-04.
    """
    global _last_wipe
    now = time.monotonic()
    if now - _last_wipe < _WIPE_INTERVAL:
        return
    _last_wipe = now
    import subprocess
    try:
        subprocess.run([_SCREEN_BIN, "-wipe"], capture_output=True, timeout=5)
        logger.info("screen: wiped %d dead session socket(s)", dead)
    except Exception:
        pass


def _screen_server_pids(screen_name: str) -> list[int]:
    """PID(s) of the GNU screen *server* process(es) backing ``screen_name``."""
    return _screen_sessions().get(screen_name, [])


def _screen_exists(screen_name: str) -> bool:
    return bool(_screen_server_pids(screen_name))


def _create_screen(screen_name: str, inner: str) -> None:
    """Spawn a detached screen (``-dmS``) running ``inner`` under ``bash -lc``.

    Detached, then attached separately via ``_attach_screen``: that split is
    the whole point — the screen outlives every attach, so the worker that
    created it holds nothing the others need.
    """
    import subprocess
    screenrc = _ensure_screenrc()
    env = os.environ.copy()
    # screen and `bash -l` both print "getpwuid() can't identify your account!"
    # if these are missing, straight into the user's terminal.
    try:
        import pwd as _pwd
        _pw = _pwd.getpwuid(os.getuid())
        env.setdefault("USER", _pw.pw_name)
        env.setdefault("LOGNAME", _pw.pw_name)
        env.setdefault("HOME", _pw.pw_dir)
    except (KeyError, ImportError):
        _u = env.get("USER") or env.get("LOGNAME") or str(os.getuid())
        env.setdefault("USER", _u)
        env.setdefault("LOGNAME", _u)
        env.setdefault("HOME", os.path.expanduser("~") or "/root")
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    proc = subprocess.run(
        [_SCREEN_BIN, "-c", screenrc, "-T", "xterm-256color", "-dmS",
         screen_name, "bash", "-lc", inner],
        capture_output=True, timeout=10, env=env,
    )
    # screen -dmS returns before its server is listening; without this the
    # attach that immediately follows can race it and find no such session.
    for _ in range(20):
        if _screen_exists(screen_name):
            logger.info("Screen session created: %s", screen_name)
            return
        time.sleep(0.1)
    # Two very different things end up here and both are worth saying out
    # loud, because the symptom either way is a terminal that opens blank:
    # the command exited immediately (a screen dies with its command — normal
    # for a one-shot, and the session really is over), or screen cannot run on
    # this host at all. Deliberately NOT retried as a direct PTY: we cannot
    # tell those apart after the fact, and re-running a command that already
    # ran would repeat its side effects.
    logger.warning(
        "screen %s did not come up within 2s (rc=%s, stderr=%r). Either its "
        "command exited immediately, or screen is broken on this host — in "
        "which case terminals will open blank until it is fixed.",
        screen_name, proc.returncode,
        (proc.stderr or b"").decode(errors="replace")[:300])


def _release_screen_creation(screen_name: str) -> None:
    """Drop a name's creation claim, so the name can be created again.

    Without this a ``restart`` is broken for the length of the claim TTL: it
    destroys the screen and immediately re-creates it under the SAME name,
    the still-held claim makes ``create`` take the "another worker is making
    it" branch, and it then waits for a screen nobody is making and attaches
    to nothing. Caught by test_insecure_state_reported_and_toggle_flips_it,
    whose restart is well inside 30s. The claim only ever means "a creation
    for this name is in flight"; once the screen is gone, none is.
    """
    client = _get_redis()
    if client is None or not screen_name:
        return
    try:
        client.delete(_term_key(_CREATING_SUFFIX, screen_name))
    except Exception as exc:
        logger.warning("_release_screen_creation(%s) failed: %s", screen_name, exc)


def _destroy_screen(screen_name: str) -> None:
    """``screen -X quit``, retried — a session with a still-dying process in
    it ignores the first quit often enough to matter."""
    if not screen_name or not screen_backing_enabled():
        return
    import subprocess
    for _ in range(3):
        try:
            subprocess.run([_SCREEN_BIN, "-S", screen_name, "-X", "quit"],
                           capture_output=True, timeout=5)
        except Exception:
            break
        if not _screen_exists(screen_name):
            break
        time.sleep(0.3)
    _release_screen_creation(screen_name)
    logger.info("Screen session destroyed: %s", screen_name)


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

    W5 MEASUREMENT — read this before trusting the name. The function selects
    on ``ppid == os.getpid()``, and W5's card carried an (explicitly
    unverified) hypothesis that multi-worker would break that, because the
    uvicorn master is PID 1 and orphans reparent to it rather than to the
    worker that registered the reaper. Measured on 2026-09-04 inside the real
    workspace container, at BOTH worker counts, by forking a PTY shell that
    backgrounds a SIGHUP-immune child and then exits::

        workers=1  forking process = 40647   orphan ppid after orphaning = 1
        workers=2  forking worker  = 40682   orphan ppid after orphaning = 1

    So the hypothesis' *conclusion* is right and its *premise* is wrong: this
    selector already matches nothing, and has since long before multi-worker.
    ``os.getpid()`` is never 1 in this container — PID 1 is podman's
    ``/run/podman-init`` and the server is PID 2 (``ps -p 1`` in
    ``aw-remote-host-workspace``). Multi-worker changes nothing here; it was
    never the cause.

    Nothing leaks as a result, which is why this was invisible: podman-init
    IS a reaper — collecting orphans is the entire job of ``--init`` — and it
    reaps whatever lands on it regardless of worker count. The live container
    showed 0 zombies after 2h28m of uptime. That is also why this function is
    left in place rather than deleted or replaced with
    ``PR_SET_CHILD_SUBREAPER``: on a host whose PID 1 is *not* an init (a bare
    ``python -m src.start.workspace``, where ``os.getpid() == 1``) the
    selector does match and this is the backstop. It is a correct no-op where
    an init already does the job, and correct where one doesn't.

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
                 insecure: bool = False, agent_session_id: str | None = None,
                 screen_name: str | None = None):
        self.id = session_id
        self.fd = fd
        self.pid = pid
        self.name = name
        self.type = session_type
        self.command = command
        self.insecure = insecure
        self.agent_session_id = agent_session_id
        #: W5 — the GNU screen this PTY is an attach OF, or None when this
        #: session is a direct PTY (no screen binary on this box).
        self.screen_name = screen_name
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

    def proc_root_pid(self) -> int | None:
        """The pid whose descendants are "the processes in this terminal".

        For a direct PTY that is our forked shell. For a screen-backed one it
        is the SCREEN SERVER, not ``self.pid`` — ``self.pid`` is only the
        ``screen -x`` attach client, and the shell is a child of the server,
        not of the attach. Reading the tree from ``self.pid`` on a
        screen-backed session finds nothing at all, which would quietly empty
        the SPA's per-terminal process badge and make its "kill this process"
        action refuse every pid as not belonging to the session.
        """
        if self.screen_name:
            pids = _screen_server_pids(self.screen_name)
            return pids[0] if pids else None
        return self.pid

    def child_procs(self, procs: dict[int, dict] | None = None) -> list[dict]:
        """Processes running in this terminal, whichever backing it has.

        The screen server and any nested screen are dropped from the result:
        the caller wants the shell and what it launched, not the plumbing —
        and since ``terminal.py``'s kill route only accepts a pid present in
        this list, omitting them also stops the UI killing the screen out
        from under every other attached client.
        """
        root = self.proc_root_pid()
        if root is None:
            return []
        found = session_child_procs(root, procs)
        if not self.screen_name:
            return found
        return [p for p in found if p["name"].lower() != "screen"]

    def kill(self, destroy_screen: bool = False):
        """Terminate this worker's PTY.

        For a screen-backed session that is only a DETACH — the screen (and
        everything running in it) stays up for other workers and for the next
        attach. Passing ``destroy_screen=True`` is what actually ends the
        session, and belongs only to explicit teardown (delete/restart).
        """
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

        if destroy_screen:
            _destroy_screen(self.screen_name)


class TerminalManager:
    """Manages PTY sessions.

    Screen-backed (and therefore reachable from every worker) wherever a
    ``screen`` binary exists; a direct per-process PTY where it doesn't. The
    per-worker ``self.sessions`` dict is a CACHE of this worker's own attach
    PTYs in either case — never the source of truth for which sessions exist.
    That role belongs to ``self._meta`` plus the live screen list, which is
    what lets ``get()`` attach to a session another worker created.
    """

    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}
        self._meta = SessionMetaStore()

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

    def _attach_screen(self, session_id: str, name: str, screen_name: str,
                       command: str | None = None, session_type: str = "terminal",
                       rows: int = 24, cols: int = 80) -> TerminalSession:
        """Attach THIS worker to an existing screen through a fresh PTY.

        ``-x`` (multi-attach, shared) and never ``-r``: no worker owns a
        screen, and ``-r`` would detach whichever worker — or whichever other
        browser tab — is already attached, turning a second viewer into a
        session hijack. This is guarantee (1), and it is the single line that
        makes a terminal serveable from any worker.
        """
        screenrc = _ensure_screenrc()
        attach_cmd = [_SCREEN_BIN, "-c", screenrc, "-T", "xterm-256color",
                      "-x", screen_name]
        master_fd, pid = self._fork_exec(attach_cmd, rows, cols)
        session = TerminalSession(
            session_id, master_fd, pid, name,
            session_type=session_type, command=command,
            insecure=_is_insecure_command(command, session_type),
            agent_session_id=_extract_agent_session_id(command),
            screen_name=screen_name,
        )
        self.sessions[session_id] = session
        logger.info("Attached to screen: %s -> %s (pid=%d)", session_id, screen_name, pid)
        return session

    def _adopt(self, session_id: str, meta: dict) -> TerminalSession | None:
        """Attach to a session THIS worker never created, from its Redis meta.

        The cross-worker path in one method: a ``/ws/terminal/<id>`` that
        landed here instead of on the creating worker resolves the screen
        name out of Redis and attaches to it locally. Returns ``None`` when
        there is nothing to attach to — no screen name recorded, or the
        screen is gone — so the caller still answers "session not found"
        rather than handing back an empty PTY.
        """
        screen_name = meta.get("screen_name")
        if not screen_name or not _screen_exists(screen_name):
            return None
        return self._attach_screen(
            session_id, meta.get("name") or session_id, screen_name,
            command=meta.get("command") or None,
            session_type=meta.get("type") or "terminal",
        )

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

        screen_name = None
        if screen_backing_enabled():
            screen_name = _screen_name_for(session_id, session_type)
            if _claim_screen_creation(screen_name):
                _create_screen(screen_name, inner)
            else:
                # Another worker won the race for this name. Wait for its
                # screen to appear rather than spawning a second one — that
                # is guarantee (3), and without the wait this worker would
                # attach to a name that does not exist yet and get a dead PTY.
                logger.info("Skipping screen creation for %s — another worker "
                            "is creating it", screen_name)
                for _ in range(30):
                    if _screen_exists(screen_name):
                        break
                    time.sleep(0.1)
                else:
                    # Attaching anyway would hand back a PTY onto nothing,
                    # which reads in the SPA as a terminal that opens blank
                    # and never responds — the exact silent-degradation shape
                    # this workspace's AGENTS.md warns about. Say so.
                    logger.error(
                        "create: waited 3s for screen %s and it never "
                        "appeared — the worker that claimed it likely died "
                        "mid-creation. This terminal will be dead; its claim "
                        "expires in %ds.", screen_name, _CREATING_TTL)
            session = self._attach_screen(
                session_id, name, screen_name, command=command,
                session_type=session_type, rows=rows, cols=cols)
        else:
            master_fd, pid = self._fork_exec(["bash", "-lc", inner], rows, cols)
            session = TerminalSession(
                session_id, master_fd, pid, name,
                session_type=session_type, command=command,
                insecure=_is_insecure_command(command, session_type),
                agent_session_id=_extract_agent_session_id(command),
            )
            self.sessions[session_id] = session

        self._meta.update(
            session_id, name=name, type=session_type,
            command=command or "", screen_name=screen_name or "",
            insecure=session.insecure,
            agent_session_id=session.agent_session_id or "",
        )
        logger.info("Terminal created: %s (%s, type=%s, pid=%d, screen=%s)",
                    session_id, name, session_type, session.pid, screen_name or "-")

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
                is_insecure: bool | None = None,
                loop: asyncio.AbstractEventLoop | None = None) -> TerminalSession | None:
        """Kill the existing session and spawn a fresh one with the same ID."""
        old = self.sessions.pop(session_id, None)
        # Fall back to Redis for a restart that landed on a worker holding no
        # PTY for this session — without it the restart would silently spawn a
        # plain login shell instead of re-running the old command.
        meta = self._meta.get(session_id) if old is None else {}
        old_screen = old.screen_name if old else (meta.get("screen_name") or None)
        if old:
            _stop_reader(old, loop)
            old.kill()
        # A restart replaces what is RUNNING, so the old screen must go — a
        # detach would leave the previous command alive and unreachable.
        _destroy_screen(old_screen)
        old_type = old.type if old else (meta.get("type") or "terminal")
        old_name = name or (old.name if old else (meta.get("name") or session_id))
        old_command = command if command is not None else (
            old.command if old else (meta.get("command") or None))
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
        """This worker's PTY for ``session_id``, attaching one if it has none.

        The local dict first, because that is the common case and costs
        nothing. On a miss, and only when screen-backed, fall back to Redis
        and adopt the session — that miss is precisely what a multi-worker
        deployment produces N-1 times out of N, and answering "not found"
        there is the whole W5 bug.
        """
        session = self.sessions.get(session_id)
        if session is not None:
            if session.alive:
                return session
            # The reader flips `alive` on EOF, which is what a screen going
            # away looks like from this side (its `screen -x` attach exits).
            # Serving the stale object anyway would hand the SPA a PTY onto a
            # closed fd — a terminal that opens blank. Drop it and re-resolve:
            # if the screen is genuinely gone the honest answer is "not
            # found", and if it is not, we simply attach again below.
            self.sessions.pop(session_id, None)
        if not screen_backing_enabled():
            return None
        meta = self._meta.get(session_id)
        if not meta:
            return None
        return self._adopt(session_id, meta)

    def remove(self, session_id: str,
               loop: asyncio.AbstractEventLoop | None = None):
        """End the session everywhere — not just detach this worker.

        ``remove`` is the user closing a terminal, so the screen has to go
        too; leaving it would keep the shell (and whatever it is running)
        alive forever with no window pointing at it. Deliberately resolves
        the screen name from Redis when this worker holds no PTY, so a delete
        that lands on a non-owning worker still works.
        """
        session = self.sessions.pop(session_id, None)
        screen_name = session.screen_name if session else None
        if screen_name is None:
            screen_name = self._meta.get(session_id).get("screen_name") or None
        if session:
            _stop_reader(session, loop)
            session.kill()
        _destroy_screen(screen_name)
        self._meta.delete(session_id)
        if session or screen_name:
            logger.info("Terminal removed: %s", session_id)

    def list_sessions(self, include_hidden: bool = False) -> list[dict]:
        """Every live session in the FLEET, not just this worker's.

        Local PTYs plus anything in Redis whose screen is still running, so
        the SPA's terminal list is the same on whichever worker serves it.
        A screen-backed entry with no live screen is dropped and its meta
        deleted — a screen that died (crash, host reboot, ``screen -wipe``)
        is how a session really ends, so that is the liveness check.
        """
        live_screens = _screen_sessions()

        # A local PTY is stale two ways: its own shell exited (``alive``), or
        # ANOTHER worker ended the session and destroyed the screen out from
        # under this attach. Only the first was checked at first, and the
        # second left a ghost terminal in the SPA's list forever — caught by
        # test_list_sessions_shows_sessions_created_on_another_worker.
        dead = [
            sid for sid, s in self.sessions.items()
            if not s.alive or (s.screen_name and s.screen_name not in live_screens)
        ]
        for sid in dead:
            self.sessions.pop(sid, None)

        listed: dict[str, dict] = {
            s.id: {
                "id": s.id,
                "name": s.name,
                "type": s.type,
                "alive": s.alive,
                "insecure": s.insecure,
                "agent_session_id": s.agent_session_id,
            }
            for s in self.sessions.values()
        }
        if screen_backing_enabled():
            for sid, meta in self._meta.all().items():
                if sid in listed:
                    continue
                screen_name = meta.get("screen_name")
                if not screen_name:
                    continue
                if screen_name not in live_screens:
                    self._meta.delete(sid)
                    continue
                listed[sid] = {
                    "id": sid,
                    "name": meta.get("name") or sid,
                    "type": meta.get("type") or "terminal",
                    "alive": True,
                    "insecure": bool(meta.get("insecure")),
                    "agent_session_id": meta.get("agent_session_id") or None,
                }
        return list(listed.values())

    def set_name(self, session_id: str, name: str) -> None:
        """Rename, write-through to Redis so every worker sees it at once —
        guarantee (2). A rename that only touched ``self.sessions`` would
        show the new name on one worker and the old one on the rest."""
        session = self.sessions.get(session_id)
        if session:
            session.name = name
        self._meta.update(session_id, name=name)

    def cleanup(self, loop: asyncio.AbstractEventLoop | None = None):
        """Shutdown: detach this worker, leave the screens running.

        Deliberately NOT ``destroy_screen=True``. A screen surviving the
        process is the point — it is what lets a restarted (or simply
        different) worker pick the session back up, and killing them here
        would throw away every user's live shell on every deploy.
        """
        for session in list(self.sessions.values()):
            _stop_reader(session, loop)
            session.kill()
        self.sessions.clear()


def _sh_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _stop_reader(session: "TerminalSession", loop=None) -> None:
    """Remove ``session``'s fd reader from the loop that installed it.

    ``loop`` is passed in by callers that now run off the event-loop thread
    (see terminal.py — creating/removing a screen-backed session does enough
    blocking subprocess work to need ``asyncio.to_thread``). From a worker
    thread ``asyncio.get_event_loop()`` raises, and silently swallowing that
    would leave an ``add_reader`` callback installed on a closed fd, which the
    loop then wakes on forever.
    """
    if loop is None:
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            return
    try:
        session.stop_reader(loop)
    except Exception:
        pass


def _screen_name_for(session_id: str, session_type: str) -> str:
    """Screen name for a session — ``aw-<type>-<session_id>``.

    Derived from the session id rather than stored, so any worker can compute
    it without a round-trip, and unique per session so two terminals never
    collide on one screen. ``screen -ls`` matching is exact-string, so the
    id's dashes are fine.
    """
    return f"aw-{session_type or 'terminal'}-{session_id}"


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
