"""One-shot W7 migration cleanup — DELETE THIS FILE ONE RELEASE AFTER W7.

W5 relayed terminal sessions between uvicorn workers through GNU ``screen``
servers; W7 replaced that with two Redis Streams per session and dropped the
dependency (see ``src/api/terminal_manager.py``). ``/opt/aw-workspace`` is a
bind mount, so a core deploy lands new code on a **running** container (see
MIGRATION.md's deploy path) — which means the screen SERVERS the old code
created keep running, with the user's shells inside them, and nothing in the
new code lists, attaches to or kills them. They would leak until the container
is recreated.

So: quit them once, on the leader only, at boot. Deliberately kept out of
``terminal_manager.py`` — that module is now screen-free, and this is
throwaway code with a shorter life than anything in it.

**To delete:** remove this file and its single call site in
``src/api/app.py``'s ``_on_watchdog_lease_acquire``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("terminal")

#: Only names this workspace's own pre-W7 code ever minted
#: (``aw-<session_type>-<session_id>``). A screen a human started by hand is
#: not ours to kill.
_NAME_PREFIX = "aw-"


def sweep_orphaned_screens() -> int:
    """Quit every pre-W7 ``aw-*`` screen still running, and wipe the sockets.

    Bounded and idempotent, and a total no-op where no ``screen`` binary
    exists — which is every container built from the post-W7 Dockerfile.
    Returns the number of screens quit, for the log line.
    """
    screen_bin = shutil.which("screen")
    if screen_bin is None:
        return 0
    try:
        out = subprocess.run([screen_bin, "-ls"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception as exc:
        logger.warning("orphaned-screen sweep: `screen -ls` failed (%s)", exc)
        return 0

    quit_count = 0
    # `screen -ls` prints one tab-indented line per session,
    # "\t12345.aw-terminal-abc\t(date)\t(Detached)" — its exit code is
    # non-zero whenever sessions exist, so it is deliberately never checked.
    for line in out.splitlines():
        entry = line.strip()
        if not entry or not entry[0].isdigit():
            continue
        _pid, _, name = entry.split()[0].partition(".")
        if not name.startswith(_NAME_PREFIX):
            continue
        try:
            subprocess.run([screen_bin, "-S", name, "-X", "quit"],
                           capture_output=True, timeout=5)
            quit_count += 1
        except Exception as exc:
            logger.warning("orphaned-screen sweep: could not quit %s (%s)", name, exc)
    try:
        subprocess.run([screen_bin, "-wipe"], capture_output=True, timeout=5)
    except Exception:
        pass
    if quit_count:
        logger.warning("orphaned-screen sweep: quit %d pre-W7 screen session(s) "
                       "the previous code left running", quit_count)
    return quit_count
