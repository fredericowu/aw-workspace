"""PTY terminal session manager — aw-workspace (BYOD data-plane) port.

Slimmed strangler-fig port of the monolith's ``src/api/terminal_manager.py``.
Spawns interactive login-shell sessions in pseudo-terminals ON THIS machine
(the BYOD workspace container) and fans PTY output out to one or more
WebSocket subscribers.

W7 replaced the GNU ``screen`` relay W5 had restored with TWO Redis Streams
per session. The PTY is still forked by, and owned by, exactly ONE worker —
a master fd cannot cross a process boundary and nothing changes that. What
changed is how the OTHER workers reach it:

* OUTPUT — ``aw:ws:<slug>:term:out:<session_id>``. The owner ``XADD``s PTY
  bytes; EVERY worker (the owner included) ``XREAD BLOCK``s the stream and
  pushes what it reads to its own WebSocket subscribers.
* INPUT — ``aw:ws:<slug>:term:in:<session_id>``. ANY worker ``XADD``s
  keystrokes and resize frames; ONLY the owner consumes them and does the
  ``os.write(fd, …)``.

Streams, not pub/sub, for three reasons in order of weight:

1. The output stream IS the scrollback. ``terminal.py`` replays
   ``session.get_scrollback()`` on every WS connect, and that buffer used to
   be owner-local — with pub/sub a client landing on a non-owner worker would
   get a blank terminal on connect. A new subscriber reads the stream's tail
   instead and gets the same replay from any worker: one mechanism, not two.
2. Ordered ids mean a reconnect resumes at a cursor — nothing lost, nothing
   duplicated. Pub/sub silently drops whatever is published while a consumer
   is reconnecting, which for keystrokes is a correctness bug.
3. ``MAXLEN ~`` bounds memory with no bookkeeping of our own.

The shape is not invented here: ``RedisPollQueue`` (src/libs/redis_coord.py)
is already XADD + ``MAXLEN ~`` + XREAD-BLOCK-with-cursor in this repo, and
this is its byte-oriented sibling.

**Single delivery path.** The owner does NOT fan out to its own local WS
clients directly *and* consume the stream. It XADDs and receives its own
bytes back through its own consumer, exactly like every other worker; every
writer XADDs to the input stream, including a WS client sitting on the owner
worker itself, and only the input consumer touches the fd. That is
``RedisBroadcaster``'s rule (src/libs/redis_coord.py) and W4 shipped on it:
one path, no "local vs remote" branch to drift apart. It costs one loopback
round trip and it buys the absence of the whole bug family behind W5b
(commit 0ec19b1 — doubled keystrokes from two writers into one shell). There
is deliberately no fast path for the owner.

**Liveness is now conclusive.** ``screen -ls`` was a subprocess that could
time out, and an inconclusive read deleting the metadata of every terminal in
the workspace was the entire W5b bug. The two checks that replace it cannot
time out and do not fork: ``/proc/<shell_pid>`` (every worker shares one PID
namespace) and an owner heartbeat key, ``…:term:owner:<session_id>``, which
the owner refreshes every 10s under a 30s TTL. A missing owner key after the
TTL is a conclusive "this session is gone"; a Redis call that RAISED is not,
and prunes nothing — see ``list_sessions``.

**What this costs, stated plainly:**

* A PTY now dies with its owning worker — deploy, crash, or worker recycle.
  A screen used to survive an app-process restart; nothing does now. This is
  the real price of dropping the dependency (see MIGRATION.md).
* Every terminal's bytes transit Redis. On the loopback companion Redis that
  is free; if this workspace's Redis ever moves off-box, a ``cat`` of a large
  file becomes network traffic.
* One XREAD BLOCK holds a connection per (worker x open terminal). Fine for a
  single-user BYOD data-plane, and the first thing that would bite if
  terminal counts grew. The fix then is one multi-key XREAD per worker
  instead of one per session — noted, not built.

**Resize semantics changed, deliberately.** ``screen -x`` sized the window to
the SMALLEST attached client. A resize is now a typed frame on the input
stream, so it is last-writer-wins: the most recent client to resize sets the
geometry for everyone.

Still dropped vs. the monolith (see MIGRATION.md):

* The ``screen_sessions`` / ``agent_sessions`` / ``window_sessions`` DB
  tables. Session metadata lives in Redis, not Postgres.
* Agent-CLI (claude/codex/cursor/gemini) session-id detection + ``--resume``
  reconstruction + the Claude ``PromptDetector``. The slim BYOD image ships
  no agent CLIs, so a terminal is just a shell (or an arbitrary command).

With no reachable Redis every one of these paths degrades to exactly today's
single-worker behaviour: metadata is process-local, the owner serves its own
sessions from memory, and terminals still work. That is the golden rule of
the whole W-series — "no Redis" must never mean "no terminals".

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
from collections import deque

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
# Redis: session metadata, creation dedup, owner heartbeat, byte streams
# ---------------------------------------------------------------------------
#
# Keys are scoped under the same ``aw:ws:<slug>:`` prefix every other
# cross-worker primitive in this workspace uses (see src/libs/redis_coord.py's
# key layout) rather than aw-backend's flat ``aw:term:*`` — one shared Redis
# can host several workspaces, and a terminal id colliding across them would
# hand one workspace's shell to another.

_META_SUFFIX = "term:meta:"
_CREATING_SUFFIX = "term:creating:"
_OWNER_SUFFIX = "term:owner:"
_OUT_SUFFIX = "term:out:"
_IN_SUFFIX = "term:in:"

_CREATING_TTL = 30
#: Owner heartbeat: refreshed every ``_OWNER_HEARTBEAT``s under ``_OWNER_TTL``.
#: The gap is what tolerates a slow tick without ever declaring a live owner
#: dead; the TTL is what tells the fleet a crashed owner is gone.
_OWNER_TTL = 30
_OWNER_HEARTBEAT = 10

#: Output-stream bound. Entries are coalesced PTY chunks capped at
#: ``_OUT_CHUNK_MAX``, so ``MAXLEN ~ _OUT_MAXLEN`` implies a byte bound too:
#: 128 x 32 KiB = **4 MiB of scrollback per session** worst case, and far less
#: in practice (interactive output arrives in bytes, not 32 KiB blocks). That
#: is deliberately in line with the pre-W7 in-memory buffer, which held 50
#: chunks of up to 64 KiB.
_OUT_MAXLEN = 128
_OUT_CHUNK_MAX = 32 * 1024
#: Input frames are keystrokes and resizes — tiny, and only interesting for a
#: moment. Bounded purely so a wedged owner cannot grow the stream forever.
_IN_MAXLEN = 512

#: TTL put on both streams once a session has EOF'd. The normal teardown
#: paths delete them outright; this bounds the one case that can outlive
#: those — a ``remove()`` on a NON-owning worker deletes the streams while the
#: owner's PTY is still EOF'ing, and the owner's pump then re-creates the
#: output stream with its final EOF frame. Long enough for any consumer still
#: draining the tail, short enough that it is not a leak.
_STREAM_EOF_TTL = 60

#: Output coalescing. ``on_readable`` can fire thousands of times a second on
#: a `cat` of a large file or a `yes`, and one XADD per readable event would
#: put all of that on Redis. Buffer instead, and flush on whichever comes
#: first: ~8ms, or 64 KiB.
_FLUSH_INTERVAL = 0.008
_FLUSH_BYTES = 64 * 1024

#: How long an XREAD parks before looping. Also the worst-case latency for a
#: consumer thread to notice it has been asked to stop.
_XREAD_BLOCK_MS = 1000

#: Frame types. One byte each, on both streams.
_F_DATA = b"d"      # output: PTY bytes
_F_EOF = b"e"       # output: the shell is gone
_F_INPUT = b"i"     # input: raw keystrokes
_F_RESIZE = b"r"    # input: "<rows>x<cols>"

_redis_client = None
_redis_bytes_client = None
_redis_lock = _threading_mod.Lock()


def _term_key(suffix: str, name: str) -> str:
    from src.libs.redis_coord import get_workspace_slug
    return f"aw:ws:{get_workspace_slug()}:{suffix}{name}"


def _get_redis():
    """Lazily-connected SYNC Redis client, best-effort (``None`` if absent).

    Sync, not ``redis.asyncio``, on purpose: every caller here runs on the
    fork/exec path or on a plain daemon thread (the output pump, the input
    consumer, ``_send_prompt``), both already blocking and reached from
    ``asyncio.to_thread``-able REST handlers — an async client would force
    this module's whole surface to become async for no gain.

    ``decode_responses=True``: this client is for the metadata hash and the
    small control keys only. PTY bytes go through ``_get_redis_bytes``.

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


def _get_redis_bytes():
    """A SECOND client, ``decode_responses=False``, for the byte streams.

    PTY output is arbitrary bytes: not valid UTF-8 in general, and a multibyte
    character can split across two 64 KiB reads. Pushing that through the
    decoded client above is either a ``UnicodeDecodeError`` on read or mojibake
    in xterm.js. Base64 would avoid the second client at 33% on the hot path;
    a second client is free.

    ``None`` (never raising) whenever ``_get_redis`` is ``None``, so every
    stream call site degrades the same way the meta store does.
    """
    global _redis_bytes_client
    if _redis_bytes_client is not None:
        return _redis_bytes_client
    if _get_redis() is None:
        return None
    with _redis_lock:
        if _redis_bytes_client is not None:
            return _redis_bytes_client
        try:
            import redis
            from src.libs.redis_coord import get_workspace_redis_url
            client = redis.Redis.from_url(
                get_workspace_redis_url(),
                decode_responses=False, socket_connect_timeout=1)
            client.ping()
            _redis_bytes_client = client
        except Exception as exc:
            logger.warning("terminal_manager: binary Redis client unavailable "
                           "(%s) — terminals stay worker-owned", exc)
            _redis_bytes_client = None
    return _redis_bytes_client


def streams_enabled() -> bool:
    """Whether terminals are stream-backed (and therefore cross-worker).

    ``False`` means no reachable Redis, which is single-worker behaviour: the
    owner reads its PTY straight into its own subscribers and writes
    keystrokes straight to the fd, exactly as this module did before any of
    this existed.
    """
    return _get_redis_bytes() is not None


def _reset_redis_client() -> None:
    """Drop the cached clients so the next call re-resolves the URL. Tests
    only — they point ``AW_REDIS_URL`` at a throwaway instance after this
    module has already been imported."""
    global _redis_client, _redis_bytes_client
    with _redis_lock:
        _redis_client = None
        _redis_bytes_client = None


class SessionMetaStore:
    """Per-session terminal metadata, one Redis hash per session.

    This is the piece that makes a terminal discoverable from a worker that
    did not create it: the PTY fd stays process-local forever, but the stream
    names (derived from the session id) and ``shell_pid`` — the only things a
    second worker needs in order to relay bytes and read the process tree —
    do not.

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


def _claim_creation(session_id: str) -> bool:
    """``SET …:term:creating:<id> NX EX 30`` — True if THIS caller won the
    race to fork the shell for ``session_id``.

    Still earns its place after the screen backing went away: ``create()`` is
    called with an EXPLICIT session_id from ``restart()``, so two workers
    racing one restart would otherwise each fork a shell and one of them would
    be orphaned — the same bug the screen-name claim prevented, one level down.

    Best-effort by design: with no Redis it always claims, which is the
    single-worker behaviour that ships. The TTL (not a delete-on-success)
    is what makes a worker that dies mid-creation self-healing — the claim
    simply expires and the next create retries, instead of the id being
    permanently unclaimable.
    """
    client = _get_redis()
    if client is None:
        return True
    try:
        return bool(client.set(_term_key(_CREATING_SUFFIX, session_id), "1",
                               nx=True, ex=_CREATING_TTL))
    except Exception as exc:
        logger.warning("_claim_creation(%s) failed: %s", session_id, exc)
        return True


def _release_creation(session_id: str) -> None:
    """Drop a session's creation claim, so the id can be created again.

    Without this a ``restart`` is broken for the length of the claim TTL: it
    ends the old shell and immediately re-creates under the SAME id, the
    still-held claim makes ``create`` take the "another worker is making it"
    branch, and it then waits for an owner nobody is going to publish. The
    claim only ever means "a creation for this id is in flight"; once the old
    session is gone, none is.
    """
    client = _get_redis()
    if client is None or not session_id:
        return
    try:
        client.delete(_term_key(_CREATING_SUFFIX, session_id))
    except Exception as exc:
        logger.warning("_release_creation(%s) failed: %s", session_id, exc)


# ---------------------------------------------------------------------------
# Owner heartbeat — the liveness signal that replaced `screen -ls`
# ---------------------------------------------------------------------------


def _set_owner(session_id: str) -> None:
    """Publish/refresh ``…:term:owner:<id>`` = this worker's pid, EX 30."""
    client = _get_redis()
    if client is None or not session_id:
        return
    try:
        client.set(_term_key(_OWNER_SUFFIX, session_id), str(os.getpid()),
                   ex=_OWNER_TTL)
    except Exception as exc:
        logger.warning("_set_owner(%s) failed: %s", session_id, exc)


def _clear_owner(session_id: str) -> None:
    """Drop the owner key immediately, rather than waiting out its TTL.

    Called on the paths where we KNOW the session ended (remove, restart, the
    shell EOF'ing, a clean worker shutdown), so the rest of the fleet stops
    listing it at once instead of up to ``_OWNER_TTL`` seconds later. The TTL
    remains the backstop for the paths we don't get to run — a crash, a
    SIGKILL, a host reboot.
    """
    client = _get_redis()
    if client is None or not session_id:
        return
    try:
        client.delete(_term_key(_OWNER_SUFFIX, session_id))
    except Exception as exc:
        logger.warning("_clear_owner(%s) failed: %s", session_id, exc)


def _owner_alive(session_id: str) -> bool:
    """Whether some worker is currently holding ``session_id``'s PTY.

    An inconclusive read degrades to "no" — i.e. the same recoverable
    "session not found" its callers already handle, which the next attempt
    corrects. The callers that PRUNE use ``_owner_map`` instead, which can
    say "I don't know".
    """
    client = _get_redis()
    if client is None or not session_id:
        return False
    try:
        return client.get(_term_key(_OWNER_SUFFIX, session_id)) is not None
    except Exception as exc:
        logger.warning("_owner_alive(%s) failed: %s", session_id, exc)
        return False


def _owner_map() -> dict[str, int] | None:
    """Every session with a live owner: ``{session_id: owner worker pid}``.

    ``None`` — not ``{}`` — when the read did not complete. The two are not
    the same fact and callers that prune on this MUST tell them apart: ``{}``
    says "no session has an owner", ``None`` says "I don't know". Conflating
    them is how ``list_sessions()`` used to delete the Redis meta of every
    terminal in the workspace on one failed subprocess, silently (W5b).

    Unlike the ``screen -ls`` it replaces, the only way this is inconclusive
    is a Redis call actually raising — there is no subprocess to time out, no
    fork to fail, and no parse to get wrong.
    """
    client = _get_redis()
    if client is None:
        return {}
    prefix = _term_key(_OWNER_SUFFIX, "")
    found: dict[str, int] = {}
    try:
        for key in client.scan_iter(match=f"{prefix}*"):
            value = client.get(key)
            if value is None:
                continue  # expired between SCAN and GET — conclusively gone
            found[key[len(prefix):]] = int(value) if str(value).isdigit() else 0
    except Exception as exc:
        logger.warning("owner-key scan failed (%s) — terminal liveness is "
                       "unknown for this read", exc)
        return None
    return found


def _pid_alive(pid: int | None) -> bool:
    """Local, non-forking, cannot-time-out liveness for a shell.

    Every uvicorn worker lives in the SAME container and the SAME PID
    namespace (``_ps_snapshot`` already sees every process regardless of which
    worker forked it), so this answers for a shell owned by any worker, not
    just ours. Together with the owner heartbeat key this is what replaced
    ``screen -ls``: no subprocess, so no 5s timeout to lose under load, which
    was the whole of the W5b incident.

    A ZOMBIE is dead. ``/proc/<pid>`` still exists for one until its parent
    reaps it, and counting that as alive would keep a session another worker
    just killed in the SPA's list for as long as the reap took — a terminal
    that opens onto nothing.
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    # "<pid> (comm) <state> …" — comm is arbitrary and can contain spaces and
    # parens, so anchor on the LAST ')' rather than splitting on whitespace.
    close = raw.rfind(b")")
    if close < 0 or close + 2 >= len(raw):
        return False
    return raw[close + 2:close + 3] != b"Z"


# ---------------------------------------------------------------------------
# The two byte streams
# ---------------------------------------------------------------------------


def _out_key(session_id: str) -> str:
    return _term_key(_OUT_SUFFIX, session_id)


def _in_key(session_id: str) -> str:
    return _term_key(_IN_SUFFIX, session_id)


def _xadd(key: str, frame: bytes, data: bytes, maxlen: int) -> bool:
    """One ``XADD … MAXLEN ~ <maxlen>``. False (logged) if it didn't land."""
    client = _get_redis_bytes()
    if client is None:
        return False
    try:
        client.xadd(key, {b"t": frame, b"d": data},
                    maxlen=maxlen, approximate=True)
        return True
    except Exception as exc:
        logger.warning("terminal stream XADD to %s failed: %s", key, exc)
        return False


def _delete_streams(session_id: str) -> None:
    """Drop both streams for a session that has genuinely ended.

    Not optional bookkeeping: a restart re-creates under the SAME id, and a
    surviving output stream would replay the PREVIOUS shell's scrollback into
    the new one.
    """
    client = _get_redis_bytes()
    if client is None or not session_id:
        return
    try:
        client.delete(_out_key(session_id), _in_key(session_id))
    except Exception as exc:
        logger.warning("_delete_streams(%s) failed: %s", session_id, exc)


def _expire_streams(session_id: str, ttl: int) -> None:
    """Put a TTL on both of a session's streams (see ``_STREAM_EOF_TTL``)."""
    client = _get_redis_bytes()
    if client is None or not session_id:
        return
    try:
        client.expire(_out_key(session_id), ttl)
        client.expire(_in_key(session_id), ttl)
    except Exception as exc:
        logger.warning("_expire_streams(%s) failed: %s", session_id, exc)


def _stream_last_id(key: str) -> bytes:
    """Id of the newest entry on ``key``, or ``b"0"`` for an empty stream —
    the "start from now, skip the backlog" cursor."""
    client = _get_redis_bytes()
    if client is None:
        return b"0"
    try:
        entries = client.xrevrange(key, count=1)
    except Exception as exc:
        logger.warning("_stream_last_id(%s) failed: %s", key, exc)
        return b"0"
    return entries[0][0] if entries else b"0"


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
    """A single terminal session, seen from ONE worker.

    Owner or not, the surface ``terminal.py`` uses is identical — ``write``,
    ``resize``, ``subscribe``/``unsubscribe``, ``get_scrollback``,
    ``child_procs``, ``start_reader``, ``alive``, and the id/name/type fields.
    The only difference is whether this worker holds the master fd
    (``is_owner`` / ``fd is not None``); everything else is relayed through
    the two Redis streams, so there is one code path rather than two.
    """

    _exit_callback = None

    def __init__(self, session_id: str, fd: int | None, pid: int | None, name: str,
                 session_type: str = "terminal", command: str | None = None,
                 insecure: bool = False, agent_session_id: str | None = None,
                 shell_pid: int | None = None, is_owner: bool = True):
        self.id = session_id
        self.fd = fd
        self.pid = pid
        self.name = name
        self.type = session_type
        self.command = command
        self.insecure = insecure
        self.agent_session_id = agent_session_id
        #: The forked login shell's pid, recorded in the session's Redis meta
        #: so ANY worker can read this session's process tree — every worker
        #: shares one PID namespace, so only the FD is process-local.
        self.shell_pid = shell_pid if shell_pid is not None else pid
        self.is_owner = is_owner
        self.alive = True
        self._subscribers: set[asyncio.Queue] = set()
        self._reader_started = False
        self._scrollback: list[bytes] = []
        self._scrollback_max = _OUT_MAXLEN
        self._scrollback_lock = _threading_mod.Lock()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        #: Bytes read off the PTY and not yet XADDed (owner only).
        self._out_pending: deque[bytes] = deque()
        self._out_lock = _threading_mod.Lock()
        self._out_wake = _threading_mod.Event()
        self._out_eof = False
        self._out_cursor: bytes = b"0"
        self._in_cursor: bytes = b"0"
        self._threads: list[_threading_mod.Thread] = []
        self._threads_lock = _threading_mod.Lock()
        self._out_consumer_started = False
        self._owner_io_started = False

    # ---- subscriber fan-out (identical on every worker) -----------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def get_scrollback(self) -> bytes:
        """Replay buffer for a WS client that just connected.

        Kept as a pure in-memory read (``terminal.py`` calls it straight from
        an ``async def``, where a blocking Redis round trip would be the
        2026-09-02 event-loop freeze again). It is nonetheless correct on a
        worker that never created the session, because the buffer is primed
        from the output stream's tail in ``prime_scrollback()`` — which runs
        on the ``asyncio.to_thread``'d ``create``/``get`` path — and kept
        current after that by this worker's own output consumer.
        """
        with self._scrollback_lock:
            return b"".join(self._scrollback)

    def _fan_out(self, data: bytes):
        """Deliver one chunk to this worker's subscribers. Loop thread only."""
        if data:
            with self._scrollback_lock:
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

    def _deliver(self, data: bytes):
        """Hop a consumer thread's chunk onto the event loop.

        ``asyncio.Queue.put_nowait`` is not thread-safe, and the output
        consumer is a plain daemon thread — before W7 ``_fan_out`` only ever
        ran from ``loop.add_reader``.
        """
        loop = self._loop
        if loop is None:
            self._fan_out(data)
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, data)
        except RuntimeError:
            pass  # loop closed underneath us (shutdown)

    # ---- scrollback priming --------------------------------------------

    def prime_scrollback(self) -> None:
        """Fill the replay buffer from the output stream's tail, and set this
        worker's consumer cursor to the newest entry it saw.

        Blocking, and called only from ``create``/``_adopt`` — both already
        off the event loop via ``asyncio.to_thread``.
        """
        client = _get_redis_bytes()
        if client is None:
            return
        try:
            entries = client.xrevrange(_out_key(self.id), count=self._scrollback_max)
        except Exception as exc:
            logger.warning("prime_scrollback(%s) failed: %s", self.id, exc)
            return
        chunks: list[bytes] = []
        for entry_id, fields in entries:
            if self._out_cursor == b"0":
                self._out_cursor = entry_id  # xrevrange is newest-first
            if fields.get(b"t") == _F_DATA:
                chunks.append(fields.get(b"d") or b"")
        chunks.reverse()
        with self._scrollback_lock:
            self._scrollback = chunks

    # ---- input: every writer XADDs, only the owner touches the fd -------

    def write(self, data: bytes):
        """Queue input for the shell.

        On the input stream even when this worker IS the owner: two writers
        into one shell is W5b's doubled-keystroke bug, and a "fast path for
        the owner" is that bug wearing a different hat.
        """
        if streams_enabled():
            _xadd(_in_key(self.id), _F_INPUT, data, _IN_MAXLEN)
            return
        self._write_fd(data)

    def resize(self, rows: int, cols: int):
        """Resize the PTY window — last writer wins.

        A behaviour change worth stating: ``screen -x`` used to size the
        window to the SMALLEST attached client, so two browsers on one
        terminal both saw the smaller geometry. A resize is now just another
        typed frame on the input stream, so the most recent client to send one
        sets the geometry for everyone.
        """
        if streams_enabled():
            _xadd(_in_key(self.id), _F_RESIZE, f"{rows}x{cols}".encode(), _IN_MAXLEN)
            return
        self._resize_fd(rows, cols)

    def _write_fd(self, data: bytes):
        """Write input to the PTY, chunked to avoid buffer overflow."""
        if self.fd is None:
            return
        try:
            CHUNK = 128
            for i in range(0, len(data), CHUNK):
                os.write(self.fd, data[i:i + CHUNK])
        except OSError:
            self.alive = False

    def _resize_fd(self, rows: int, cols: int):
        if self.fd is None:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    # ---- readers / consumers -------------------------------------------

    def start_reader(self, loop: asyncio.AbstractEventLoop):
        """Ensure THIS worker is delivering this session's output.

        On the owner that means the PTY fd reader (which feeds the output
        stream); on every worker, including the owner, it means the output
        consumer that reads that stream back and fans it out locally.
        Idempotent — ``terminal.py`` calls it on create, on restart and on
        every WS connect.
        """
        self._loop = loop
        self._start_out_consumer()
        if not self.is_owner or self.fd is None:
            return
        self._start_owner_io()
        if self._reader_started:
            return
        self._reader_started = True

        def on_readable():
            try:
                data = os.read(self.fd, 65536)
                if data:
                    if self._owner_io_started:
                        self._queue_output(data)
                    else:
                        # Degraded (no Redis): there is no pump to hand these
                        # to, so fan out in place. Byte-for-byte the pre-W7
                        # single-worker path — and the ONLY place the owner
                        # ever delivers its own bytes without the stream.
                        self._fan_out(data)
                else:
                    self._on_eof(loop)
            except OSError:
                self._on_eof(loop)

        loop.add_reader(self.fd, on_readable)

    def _start_owner_io(self) -> None:
        """Owner-side threads: the output pump and the input consumer.

        Started from ``create()`` as well as ``start_reader()`` — the input
        consumer must be running before any loop exists, because
        ``_send_prompt`` (a plain daemon thread) and the ``/api/terminals/
        <id>/write`` REST fallback can both land keystrokes on the stream
        without a WebSocket ever having been opened.
        """
        if self._owner_io_started or not streams_enabled() or self.fd is None:
            return
        self._owner_io_started = True
        # Skip whatever is already on the input stream: this is a fresh shell,
        # and replaying a previous one's keystrokes into it would be worse
        # than losing them.
        self._in_cursor = _stream_last_id(_in_key(self.id))
        self._spawn(self._out_pump_loop, "out-pump")
        self._spawn(self._in_consumer_loop, "in-consumer")

    def _start_out_consumer(self) -> None:
        if self._out_consumer_started or not streams_enabled():
            return
        self._out_consumer_started = True
        self._spawn(self._out_consumer_loop, "out-consumer")

    def _spawn(self, target, label: str) -> None:
        thread = _threading_mod.Thread(
            target=target, name=f"term-{label}-{self.id[:8]}", daemon=True)
        with self._threads_lock:
            self._threads.append(thread)
        thread.start()

    def _queue_output(self, data: bytes) -> None:
        """Hand PTY bytes to the pump. Loop thread, so it must not block."""
        with self._out_lock:
            self._out_pending.append(data)
        self._out_wake.set()

    def _out_pump_loop(self) -> None:
        """Coalesce PTY reads and XADD them to the output stream.

        A naive port XADDs once per readable event, which on a `cat` of a
        large file or a `yes` is thousands of XADDs/sec. Buffer instead and
        flush on ``_FLUSH_INTERVAL`` (~8ms) or ``_FLUSH_BYTES`` (64 KiB),
        whichever comes first, then split the flush into ``_OUT_CHUNK_MAX``
        entries so the stream's ``MAXLEN ~`` implies a byte bound too.
        """
        key = _out_key(self.id)
        while not self._stopping:
            self._out_wake.wait(0.5)
            self._out_wake.clear()
            buf = bytearray()
            deadline = time.monotonic() + _FLUSH_INTERVAL
            while True:
                with self._out_lock:
                    while self._out_pending:
                        buf += self._out_pending.popleft()
                if not buf or self._stopping:
                    break
                if len(buf) >= _FLUSH_BYTES or time.monotonic() >= deadline:
                    break
                time.sleep(0.001)
            for i in range(0, len(buf), _OUT_CHUNK_MAX):
                _xadd(key, _F_DATA, bytes(buf[i:i + _OUT_CHUNK_MAX]), _OUT_MAXLEN)
            if self._out_eof and not self._out_pending:
                # The shell is gone. The EOF frame is what flips `alive` on
                # every worker (this one included) — single delivery path, so
                # the last real bytes are guaranteed to be delivered first.
                _xadd(key, _F_EOF, b"", _OUT_MAXLEN)
                _expire_streams(self.id, _STREAM_EOF_TTL)
                _clear_owner(self.id)
                return

    def _in_consumer_loop(self) -> None:
        """Owner only: drain the input stream into the PTY."""
        key = _in_key(self.id)
        while not self._stopping:
            client = _get_redis_bytes()
            if client is None:
                time.sleep(0.5)
                continue
            try:
                result = client.xread({key: self._in_cursor}, count=256,
                                      block=_XREAD_BLOCK_MS)
            except Exception as exc:
                logger.warning("terminal %s input XREAD failed: %s", self.id, exc)
                time.sleep(0.5)
                continue
            for _stream, entries in result or []:
                for entry_id, fields in entries:
                    self._in_cursor = entry_id
                    frame = fields.get(b"t")
                    data = fields.get(b"d") or b""
                    if frame == _F_INPUT:
                        self._write_fd(data)
                    elif frame == _F_RESIZE:
                        rows, _, cols = data.decode(errors="replace").partition("x")
                        try:
                            self._resize_fd(int(rows), int(cols))
                        except ValueError:
                            logger.warning("terminal %s: bad resize frame %r",
                                           self.id, data)

    def _out_consumer_loop(self) -> None:
        """Every worker: read the output stream and fan out locally."""
        key = _out_key(self.id)
        while not self._stopping:
            client = _get_redis_bytes()
            if client is None:
                time.sleep(0.5)
                continue
            try:
                result = client.xread({key: self._out_cursor}, count=64,
                                      block=_XREAD_BLOCK_MS)
            except Exception as exc:
                logger.warning("terminal %s output XREAD failed: %s", self.id, exc)
                time.sleep(0.5)
                continue
            for _stream, entries in result or []:
                for entry_id, fields in entries:
                    self._out_cursor = entry_id
                    frame = fields.get(b"t")
                    if frame == _F_DATA:
                        self._deliver(fields.get(b"d") or b"")
                    elif frame == _F_EOF:
                        self.alive = False
                        self._deliver(b"")  # terminal.py's end-of-stream sentinel
                        self._fire_exit_callback()
                        return

    def _fire_exit_callback(self) -> None:
        if TerminalSession._exit_callback:
            try:
                TerminalSession._exit_callback(self.id, self.type)
            except Exception:
                pass

    def _on_eof(self, loop):
        """The PTY hit EOF — the shell is gone. Owner only.

        Deliberately does NOT flip ``alive`` or fan out here when
        stream-backed: the EOF frame the pump publishes is what does that, on
        this worker and every other one alike, so the last real bytes cannot
        be dropped by ``alive`` going false ahead of them.
        """
        try:
            loop.remove_reader(self.fd)
        except Exception:
            pass
        self._reader_started = False
        if streams_enabled():
            self._out_eof = True
            self._out_wake.set()
            return
        # Degraded (no Redis): byte-for-byte the pre-W7 single-worker path.
        self._fan_out(b"")
        self.alive = False
        self._fire_exit_callback()

    def stop_reader(self, loop: asyncio.AbstractEventLoop):
        if not self._reader_started or self.fd is None:
            return
        try:
            loop.remove_reader(self.fd)
        except Exception:
            pass
        self._reader_started = False

    def close_streams(self) -> None:
        """Ask this session's consumer/pump threads to stop.

        They park in ``XREAD BLOCK`` for at most ``_XREAD_BLOCK_MS``, so this
        returns immediately and they exit within a second. Daemon threads, so
        even a missed stop cannot hold up process shutdown.
        """
        self._stopping = True
        self._out_wake.set()

    # ---- process tree ---------------------------------------------------

    def proc_root_pid(self) -> int | None:
        """The pid whose descendants are "the processes in this terminal".

        The forked login shell — recorded in Redis as ``shell_pid``, so this
        answers on a worker that never created the session. Every uvicorn
        worker lives in the same container and the same PID namespace
        (``_ps_snapshot`` shells one ``ps -eo`` and sees every process
        regardless of which worker forked it), so the process TREE is
        host-global; only the FD is process-local. Answering "session not
        found" for proc queries on a non-owner worker would empty the SPA's
        per-terminal process badge and make its kill action refuse every pid,
        9 times out of 10.
        """
        return self.shell_pid

    def child_procs(self, procs: dict[int, dict] | None = None) -> list[dict]:
        """Processes running in this terminal, from any worker."""
        root = self.proc_root_pid()
        if root is None:
            return []
        return session_child_procs(root, procs)

    def kill(self):
        """Terminate this worker's PTY (owner only; a no-op elsewhere)."""
        self.alive = False
        self.close_streams()
        if self.fd is None:
            return
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
    """Manages PTY sessions.

    Stream-backed (and therefore reachable from every worker) wherever Redis
    is reachable; a direct per-process PTY where it isn't. The per-worker
    ``self.sessions`` dict is a CACHE of this worker's own handles in either
    case — never the source of truth for which sessions exist. That role
    belongs to ``self._meta`` plus the owner heartbeat keys, which is what
    lets ``get()`` serve a session another worker created.
    """

    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}
        self._meta = SessionMetaStore()
        # W5b: serializes the check-then-act cache-miss -> adopt -> store
        # sequence on `self.sessions`, per session_id. Every caller in
        # terminal.py reaches `get`/`create`/`restart`/`remove` through
        # `asyncio.to_thread` — real OS threadpool threads, not just
        # interleaved coroutines — so two concurrent callers racing the same
        # cold session_id used to both cache-miss and each build their own
        # handle, and only one would win the dict slot: the other was silently
        # handed back to its own caller as a second live writer into the same
        # shell, which is what produced the reported keystroke duplication.
        # RLock, not Lock: `restart()` holds the lock for its whole
        # pop/kill/recreate sequence and calls `create()` — which locks the
        # same session_id — on the same thread.
        self._session_locks: dict[str, _threading_mod.RLock] = {}
        self._session_locks_guard = _threading_mod.Lock()
        self._heartbeat: _threading_mod.Thread | None = None
        self._heartbeat_guard = _threading_mod.Lock()

    def _lock_for(self, session_id: str) -> _threading_mod.RLock:
        """Per-session_id RLock, created on first use.

        Never removed — a long-running workspace accumulates one small RLock
        per session_id it has ever seen, bounded by how many terminals this
        box has ever opened, not by anything unbounded.
        """
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = _threading_mod.RLock()
                self._session_locks[session_id] = lock
            return lock

    # ---- owner heartbeat ------------------------------------------------

    def _ensure_heartbeat(self) -> None:
        """One thread per manager refreshing the owner key of every session
        this worker owns — not one thread per session.

        It exits once this worker has owned nothing for a few ticks, so a
        process that opens and closes terminals does not accumulate idle
        threads; the next create starts it again.
        """
        with self._heartbeat_guard:
            if self._heartbeat is not None and self._heartbeat.is_alive():
                return
            self._heartbeat = _threading_mod.Thread(
                target=self._heartbeat_loop, name="term-owner-heartbeat",
                daemon=True)
            self._heartbeat.start()

    def _heartbeat_loop(self) -> None:
        idle_ticks = 0
        while True:
            time.sleep(_OWNER_HEARTBEAT)
            owned = [s for s in list(self.sessions.values())
                     if s.is_owner and s.alive]
            if not owned:
                idle_ticks += 1
                if idle_ticks >= 3:
                    return
                continue
            idle_ticks = 0
            for session in owned:
                _set_owner(session.id)

    # ---- PTY --------------------------------------------------------------

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

    def _remote_session(self, session_id: str, meta: dict) -> TerminalSession:
        """A handle onto a session whose PTY belongs to ANOTHER worker.

        Holds no fd: input goes out on the input stream, output arrives on the
        output stream, and the process tree is read from ``shell_pid`` in the
        shared PID namespace. Its scrollback is primed from the output
        stream's tail so a WS connect here replays the same bytes it would on
        the owner.
        """
        shell_pid = _meta_int(meta, "shell_pid")
        command = meta.get("command") or None
        session = TerminalSession(
            session_id, None, shell_pid, meta.get("name") or session_id,
            session_type=meta.get("type") or "terminal", command=command,
            insecure=_is_insecure_command(command, meta.get("type") or "terminal"),
            agent_session_id=meta.get("agent_session_id") or None,
            shell_pid=shell_pid, is_owner=False,
        )
        session.prime_scrollback()
        self.sessions[session_id] = session
        logger.info("Adopted remote terminal: %s (owner shell pid=%s)",
                    session_id, shell_pid)
        return session

    def _adopt(self, session_id: str, meta: dict) -> TerminalSession | None:
        """Attach to a session THIS worker never created, from its Redis meta.

        The cross-worker path in one method. Returns ``None`` when there is
        nothing to relay to — no live owner key, or a ``shell_pid`` that is no
        longer in the process table — so the caller still answers "session not
        found" rather than handing back a terminal onto nothing.
        """
        if not _owner_alive(session_id):
            return None
        shell_pid = _meta_int(meta, "shell_pid")
        if shell_pid is not None and not _pid_alive(shell_pid):
            return None
        return self._remote_session(session_id, meta)

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

        # Locked for the same reason as get()'s cold-miss path: `create()` is
        # called with an explicit (not freshly-generated) session_id from
        # restart(), and two concurrent callers storing for that same id would
        # race `self.sessions` exactly like two get()s would.
        with self._lock_for(session_id):
            if _claim_creation(session_id):
                master_fd, pid = self._fork_exec(["bash", "-lc", inner], rows, cols)
                session = TerminalSession(
                    session_id, master_fd, pid, name,
                    session_type=session_type, command=command,
                    insecure=_is_insecure_command(command, session_type),
                    agent_session_id=_extract_agent_session_id(command),
                    shell_pid=pid, is_owner=True,
                )
                self.sessions[session_id] = session
                # Meta BEFORE the owner key, always: a racing _adopt() gates on
                # the owner key, so publishing that first would let another
                # worker read an empty hash and build a handle with no
                # shell_pid.
                self._meta.update(
                    session_id, name=name, type=session_type,
                    command=command or "", shell_pid=pid,
                    insecure=session.insecure,
                    agent_session_id=session.agent_session_id or "",
                )
                _set_owner(session_id)
                self._ensure_heartbeat()
                # Before returning: the input consumer must be up, or the
                # `initial_prompt` thread below (and the REST /write fallback)
                # would XADD keystrokes nobody is reading yet.
                session._start_owner_io()
                logger.info("Terminal created: %s (%s, type=%s, shell pid=%d, "
                            "streams=%s)", session_id, name, session_type, pid,
                            streams_enabled())
            else:
                # Another worker won the race for this id. Wait for its owner
                # key rather than forking a second shell — without the wait
                # this worker would hand back a terminal onto nothing.
                logger.info("Skipping shell creation for %s — another worker "
                            "is creating it", session_id)
                for _ in range(30):
                    if _owner_alive(session_id):
                        break
                    time.sleep(0.1)
                else:
                    # Returning a handle anyway reads in the SPA as a terminal
                    # that opens blank and never responds — the exact
                    # silent-degradation shape this workspace's AGENTS.md
                    # warns about. Say so.
                    logger.error(
                        "create: waited 3s for an owner of %s and none "
                        "appeared — the worker that claimed it likely died "
                        "mid-creation. This terminal will be dead; its claim "
                        "expires in %ds.", session_id, _CREATING_TTL)
                meta = self._meta.get(session_id) or {
                    "name": name, "type": session_type, "command": command or "",
                }
                session = self._remote_session(session_id, meta)

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

    def _end_session(self, session_id: str, session: TerminalSession | None,
                     shell_pid: int | None,
                     loop: asyncio.AbstractEventLoop | None,
                     kill_tree: bool) -> None:
        """Tear one session down from whichever worker was asked to.

        The owner and a non-owner get different levers, and each is the
        strongest one available on its side — this is teardown, not the data
        path, so it is not the "local fast path" the delivery rule forbids:

        * Owner: ``session.kill()`` — close the master fd (SIGHUP to the
          foreground process group) and SIGTERM the shell, exactly as the
          direct-PTY backing always did.
        * Non-owner: no fd exists, so the only conclusive lever is the shared
          PID namespace — ``kill_proc_tree(shell_pid)``. That is what makes a
          DELETE landing on a non-owning worker actually end the session
          instead of leaving an unreachable shell running forever.

        ``kill_tree`` additionally SIGKILLs the whole subtree even on the
        owner: ``restart()`` REPLACES what is running, so a command that
        ignores SIGTERM must not survive into the new session. ``remove()``
        does not pass it, preserving the SIGTERM-then-reap race
        ``test_closed_terminal_does_not_leak_a_defunct_shell`` exists to pin.

        A ``shell_pid`` this worker did NOT fork is only ever acted on while
        the session still has a live owner key. Without that check a stale meta
        row — owner dead, its heartbeat expired, ``list_sessions`` not yet run
        — would have us SIGKILL a pid the OS has since handed to an unrelated
        process. It costs one GET, and there is no doubt in the other
        direction: a PTY dies with its owning worker, so no owner means there
        is nothing left to kill anyway.
        """
        if session is not None:
            _stop_reader(session, loop)
            session.close_streams()
            if session.is_owner:
                session.kill()
        owns_locally = session is not None and session.is_owner
        if not shell_pid or not (kill_tree or not owns_locally):
            return
        if not owns_locally and streams_enabled() and not _owner_alive(session_id):
            logger.info("end_session(%s): shell pid %s came from meta with no "
                        "live owner — its worker is already gone, so there is "
                        "nothing to kill", session_id, shell_pid)
            return
        kill_proc_tree(shell_pid)

    def restart(self, session_id: str, command: str | None = None, name: str | None = None,
                rows: int = 24, cols: int = 80, new_session: bool = False,
                is_insecure: bool | None = None,
                loop: asyncio.AbstractEventLoop | None = None) -> TerminalSession | None:
        """Kill the existing session and spawn a fresh one with the same ID."""
        # Locked for the whole pop -> kill -> recreate sequence: two concurrent
        # restarts of the same session_id would otherwise both pop/kill the old
        # session and then both race create(), the same class of bug get() had.
        # create() re-acquires the same session_id's lock (RLock, same thread)
        # below — not a deadlock.
        with self._lock_for(session_id):
            old = self.sessions.pop(session_id, None)
            # Fall back to Redis for a restart that landed on a worker holding no
            # PTY for this session — without it the restart would silently spawn a
            # plain login shell instead of re-running the old command.
            meta = self._meta.get(session_id) if old is None else {}
            shell_pid = old.shell_pid if old else _meta_int(meta, "shell_pid")
            self._end_session(session_id, old, shell_pid, loop, kill_tree=True)
            # Both streams go too: the id is about to be reused, and a
            # surviving output stream would replay the OLD shell's scrollback
            # into the new one.
            _clear_owner(session_id)
            _delete_streams(session_id)
            _release_creation(session_id)
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
        """This worker's handle for ``session_id``, building one if it has none.

        The local dict first, because that is the common case and costs
        nothing. On a miss, fall back to Redis and adopt the session — that
        miss is precisely what a multi-worker deployment produces N-1 times
        out of N, and answering "not found" there is the whole W5 bug.
        """
        session = self.sessions.get(session_id)
        if session is not None:
            # `_pid_alive` as well as `alive`: another worker can have killed
            # the shell in the shared PID namespace without this worker's EOF
            # having fired yet, and serving the handle anyway hands the SPA a
            # terminal onto nothing.
            if session.alive and (session.shell_pid is None
                                  or _pid_alive(session.shell_pid)):
                return session
            # `alive` flips on the EOF frame, i.e. the shell is genuinely gone.
            # Serving the stale object anyway would hand the SPA a terminal
            # onto a closed fd. Drop it and re-resolve: if the session is
            # really over the honest answer is "not found", and if it is not,
            # we simply build a fresh handle below.
            self.sessions.pop(session_id, None)
        # Cold miss: serialize meta-lookup -> adopt -> store per session_id
        # (see __init__) so a second concurrent caller waits for and reuses
        # the first handle instead of building its own.
        with self._lock_for(session_id):
            session = self.sessions.get(session_id)
            if session is not None and session.alive:
                return session
            meta = self._meta.get(session_id)
            if not meta:
                return None
            return self._adopt(session_id, meta)

    def remove(self, session_id: str,
               loop: asyncio.AbstractEventLoop | None = None):
        """End the session everywhere — not just on this worker.

        ``remove`` is the user closing a terminal, so the shell has to go too;
        leaving it would keep whatever it is running alive forever with no
        window pointing at it. Deliberately resolves ``shell_pid`` from Redis
        when this worker holds no PTY, so a delete that lands on a non-owning
        worker still works.
        """
        # Locked so a remove() racing a get()/create() cold-miss for the same
        # session_id can't interleave — e.g. get() adopting a session in the
        # instant after remove() read a stale meta but before it deleted it,
        # leaving a freshly-built handle onto a shell remove() just killed.
        with self._lock_for(session_id):
            session = self.sessions.pop(session_id, None)
            meta = self._meta.get(session_id)
            shell_pid = session.shell_pid if session else _meta_int(meta, "shell_pid")
            self._end_session(session_id, session, shell_pid, loop, kill_tree=False)
            _clear_owner(session_id)
            _delete_streams(session_id)
            _release_creation(session_id)
            self._meta.delete(session_id)
            if session or shell_pid:
                logger.info("Terminal removed: %s", session_id)

    def list_sessions(self, include_hidden: bool = False) -> list[dict]:
        """Every live session in the FLEET, not just this worker's.

        Local handles plus anything in Redis whose owner heartbeat is still
        being refreshed, so the SPA's terminal list is the same on whichever
        worker serves it. An entry with no live owner is dropped and its meta
        and streams deleted — the owner key expiring is how a session whose
        worker died really ends.

        That liveness check is only allowed to DELETE anything when the read
        it rests on actually completed. A Redis call that RAISED comes back as
        ``None`` from ``_owner_map()``, and every prune below is skipped for
        it: listing a session whose owner has since gone is a stale row the
        next successful read corrects, while deleting the meta of a session
        that is still running is unrecoverable. Loud, because the pre-existing
        failure mode was silent (W5b): at ``AW_WORKSPACE_WORKERS`` > 1 every
        worker runs this on every ``terminal_update`` broadcast, so a read
        that starts failing under that load would otherwise freeze the SPA's
        terminal list with nothing logged anywhere.
        """
        owners = _owner_map()
        conclusive = owners is not None
        if not conclusive:
            owners = {}
            logger.warning(
                "Terminal list: the owner-key read was inconclusive — keeping "
                "all %d local session(s) and every session's metadata, pruning "
                "nothing this pass", len(self.sessions))

        # A local handle is stale three ways: the shell exited (``alive``,
        # which the EOF frame drives), the shell is simply not in the process
        # table any more, or ANOTHER worker ended the session out from under
        # this handle. The first two are local reads, so they still prune on
        # an inconclusive pass; the owner-map half does not.
        #
        # The ``_pid_alive`` check is what stops a ghost terminal on the
        # OWNER: a remove() that landed on another worker kills the shell
        # directly in the shared PID namespace, and this worker's EOF only
        # fires if its PTY reader happens to be running. A session this worker
        # owns is deliberately never pruned by the owner MAP — its own shell
        # is the authority, and with no Redis at all that map is legitimately
        # empty.
        dead = [
            sid for sid, s in self.sessions.items()
            if not s.alive
            or (s.shell_pid is not None and not _pid_alive(s.shell_pid))
            or (conclusive and not s.is_owner and sid not in owners)
        ]
        for sid in dead:
            session = self.sessions.pop(sid, None)
            if session is not None:
                session.close_streams()

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
        for sid, meta in self._meta.all().items():
            if sid in listed:
                continue
            if sid not in owners:
                if conclusive:
                    self._meta.delete(sid)
                    _delete_streams(sid)
                    continue
                # Inconclusive: keep listing it from its last known meta
                # rather than deleting the only record of it.
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
        """Rename, write-through to Redis so every worker sees it at once. A
        rename that only touched ``self.sessions`` would show the new name on
        one worker and the old one on the rest."""
        session = self.sessions.get(session_id)
        if session:
            session.name = name
        self._meta.update(session_id, name=name)

    def cleanup(self, loop: asyncio.AbstractEventLoop | None = None):
        """Shutdown: end every session this worker owns.

        A PTY now dies with its owning worker — that is the price of dropping
        the ``screen`` dependency, and it is not softened by pretending
        otherwise. So a session this worker owns is retired here (owner key,
        streams and meta deleted) rather than left advertised to the fleet as
        a terminal nobody can serve for the next ``_OWNER_TTL`` seconds. The
        TTL stays the backstop for the shutdowns that never reach this code —
        a crash, a SIGKILL, a host reboot.
        """
        for session in list(self.sessions.values()):
            _stop_reader(session, loop)
            session.close_streams()
            if session.is_owner:
                session.kill()
                _clear_owner(session.id)
                _delete_streams(session.id)
                self._meta.delete(session.id)
        self.sessions.clear()


def _meta_int(meta: dict, field: str) -> int | None:
    """Read an int field out of a session's Redis meta hash, or ``None``."""
    raw = (meta or {}).get(field)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _sh_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _stop_reader(session: "TerminalSession", loop=None) -> None:
    """Remove ``session``'s fd reader from the loop that installed it.

    ``loop`` is passed in by callers that now run off the event-loop thread
    (see terminal.py — creating/removing a session does enough blocking work
    to need ``asyncio.to_thread``). From a worker thread
    ``asyncio.get_event_loop()`` raises, and silently swallowing that would
    leave an ``add_reader`` callback installed on a closed fd, which the loop
    then wakes on forever.
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
