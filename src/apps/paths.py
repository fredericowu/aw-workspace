"""Persistent workspace-side paths for F4 contribution points.

Everything an app installs that must **survive a container recreation** lives
under ``AW_WORKSPACE_HOME`` (default ``~/.aw-workspace``) — the workspace fs is
host-mounted (see ``aw-workspace-base-dir-host-mount``), so this tree persists.
The apps reconciler re-applies volatile side effects (system-CLI installs) on
every boot; the durable ones (app ``bin`` shims, secrets) simply persist here.

Layout (ADR Decision 8):

* ``<home>/bin``     — app command shims (``<slug>-*``), on PATH, survive restart.
* ``<home>/secrets`` — the workspace-side secure secret store (F4; the
  zero-knowledge store is a separate deferred card).
* ``<home>/.env``    — mirrors ``AW_WORKSPACE_API_KEY`` (0600) so sibling
  processes like the ``aw-workspace-cli`` can authenticate without DB access
  (written at boot by ``src.api.workspace_api_key``; read by ``local_client.py``).

App-contributed skills (``contributes.skills``) are the one exception to the
"lives under ``<home>``" rule: they're copied into ``<workspace root>/skills/``
(top-level, alongside this repo's own built-in skills) instead, because that's
the path Claude Code and other CLI agents actually auto-discover from their
project cwd — a location under ``AW_WORKSPACE_HOME`` is invisible to them.
``_register_skills()`` re-copies on every app ``activate()`` (boot/reconcile),
so this self-heals if a core-image update ever touches ``skills/``.
"""
from __future__ import annotations

import os

DEFAULT_WORKSPACE_CONTAINER_DIR = "/opt/aw-workspace"


def workspace_home_path() -> str:
    """Resolve the workspace home dir WITHOUT creating it — for read-only
    callers (the ``aw-workspace-cli``) that must not fail if the dir isn't
    theirs to ``makedirs``.

    When ``AW_WORKSPACE_HOME`` is unset the fallback is
    ``<container dir>/.aw-workspace`` (the host-mounted workspace tree), NOT
    ``~/.aw-workspace``: the CLI is often invoked from a spawned agent-runner
    container whose ``$HOME`` (``/home/ubuntu``) is its own, but which shares
    the workspace mount at ``/opt/aw-workspace`` — that's where the server's
    ``.env`` (API key + external URL) actually lives."""
    home = os.environ.get("AW_WORKSPACE_HOME")
    if home:
        return home
    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )
    return os.path.join(root, ".aw-workspace")


def workspace_home() -> str:
    home = workspace_home_path()
    os.makedirs(home, exist_ok=True)
    return home


def env_file() -> str:
    """``<home>/.env`` — the shared file the server writes secrets/config into
    (``AW_WORKSPACE_API_KEY``, ``AW_WORKSPACE_API_URL``, …) so sibling
    processes with no DB access, like the ``aw-workspace-cli``, can read them."""
    return os.path.join(workspace_home(), ".env")


def upsert_workspace_env(name: str, value: str) -> None:
    """Upsert ``name=value`` into ``<home>/.env`` (0600), preserving every
    other line — several secrets/config values share this one file, so this
    is the single writer for all of them."""
    path = env_file()
    prefix = f"{name}="
    lines: list[str] = []
    found = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(prefix):
                    lines.append(f"{prefix}{value}\n")
                    found = True
                else:
                    lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{prefix}{value}\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # O_CREAT's mode only applies on first creation; force 0600 every write so
    # a pre-existing (looser) secrets file is always tightened.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(lines)


def bin_dir() -> str:
    d = os.path.join(workspace_home(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def secrets_dir() -> str:
    d = os.path.join(workspace_home(), "secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def skills_dir() -> str:
    """Top-level ``skills/`` at the workspace root, not under ``AW_WORKSPACE_HOME``.

    Claude Code (and other CLI agents) auto-discover ``<cwd>/skills/<id>/SKILL.md``
    from the workspace's project root — the same place this repo's own
    ``skills/aw-workspace/SKILL.md`` already lives. A path under the
    ``.aw-workspace`` state dir would be durable but invisible to that
    discovery mechanism.
    """
    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )
    d = os.path.join(root, "skills")
    os.makedirs(d, exist_ok=True)
    return d


def workspace_root() -> str:
    """The workspace tree's own root — ``/opt/aw-workspace`` by default.

    The parent of ``skills_dir()``/``repos_dir()`` and of the
    ``.aw-workspace`` state dir, i.e. everything a workspace terminal sees
    at its cwd. Unlike the two below there is no ``makedirs``: this
    directory is the checkout itself, so if it's missing something is
    catastrophically wrong and conjuring an empty one would only hide it.
    """
    return os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )


def repos_dir() -> str:
    """Top-level ``repos/`` at the workspace root — where a user/agent working
    from a workspace terminal clones repos for general dev work (the git
    app's uncommitted-changes watchdog scans every immediate child of this
    directory too). Same "workspace root, not durable-state-dir" placement
    as ``skills_dir()`` — visible from any workspace terminal at
    ``/opt/aw-workspace/repos``, not tucked away under ``AW_WORKSPACE_HOME``.
    """
    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )
    d = os.path.join(root, "repos")
    os.makedirs(d, exist_ok=True)
    return d
