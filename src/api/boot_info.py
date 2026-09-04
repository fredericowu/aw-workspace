"""Boot identity for ``/api/health`` — ``boot_id``/``git_head``/``started_at``.

Minted ONCE by the parent process, before ``uvicorn.run(workers=N)`` forks or
spawns worker processes (see ``src/start/workspace.py``), and read back from
``os.environ`` by every worker on every request. ``AW_WORKSPACE_WORKERS=10``
is live on this deployment — if each worker minted its own ``boot_id``, a
poller watching for a restart would see a different value per request and
could never converge, so the parent sets these into ``os.environ`` before
spawning and children only ever read them.

``git_head`` in particular must be captured at process START, not read live
per request: reading the live worktree sha inside the handler would report
the CURRENT bind-mounted code even on a stale process that hasn't picked it
up yet — precisely the lie this field exists to catch. Callers that want
"what does the checkout currently say" should call :func:`compute_git_head`
directly, not go through the cached getters here.

These three fields, once added, are a public contract other services poll
(``aw-workspace-cli restart core --wait``; aw-console and aw-backend's
``_wait_for_workspace_version`` read ``/api/health`` too) — add fields here,
never repurpose or remove one.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid

_BOOT_ID_ENV = "AW_WORKSPACE_BOOT_ID"
_GIT_HEAD_ENV = "AW_WORKSPACE_GIT_HEAD"
_STARTED_AT_ENV = "AW_WORKSPACE_STARTED_AT"


def compute_git_head(container_dir: str | None = None) -> str:
    """``git rev-parse HEAD`` of the tree at ``container_dir`` (default
    ``AW_WORKSPACE_CONTAINER_DIR``, falling back to ``/opt/aw-workspace``).

    Degrades to ``""`` — never raises — when git is unavailable or the tree
    isn't a checkout; this field exists to detect a stale-vs-fresh restart,
    not to assert the workspace is a git repo."""
    root = container_dir or os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace")
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def mint_boot_identity() -> None:
    """Set boot_id/git_head/started_at into ``os.environ``. Call exactly
    once, in the parent process, before ``uvicorn.run(workers=N)`` — see
    ``src/start/workspace.py::main``."""
    os.environ[_BOOT_ID_ENV] = uuid.uuid4().hex
    os.environ[_GIT_HEAD_ENV] = compute_git_head()
    os.environ[_STARTED_AT_ENV] = str(int(time.time()))


def boot_id() -> str:
    return os.environ.get(_BOOT_ID_ENV, "")


def git_head() -> str:
    return os.environ.get(_GIT_HEAD_ENV, "")


def started_at() -> int:
    try:
        return int(os.environ.get(_STARTED_AT_ENV, "0"))
    except ValueError:
        return 0
