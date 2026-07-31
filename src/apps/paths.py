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
* ``<home>/skills``  — symlink index of app-contributed skills (``contributes.skills``),
  one entry per ``<app_id>__<skill_id>`` pointing at the installed app's own
  ``skills/<id>/`` dir — no content duplication.
* ``<home>/cli-token`` — random secret (0600) the ``./aw`` CLI presents to
  prove it's running on the same machine as the server; see ``local_client.py``.
"""
from __future__ import annotations

import os
import secrets

LOCAL_CLI_HEADER = "X-AW-Local-Cli-Token"


def workspace_home() -> str:
    home = os.environ.get("AW_WORKSPACE_HOME")
    if not home:
        home = os.path.join(os.path.expanduser("~"), ".aw-workspace")
    os.makedirs(home, exist_ok=True)
    return home


def bin_dir() -> str:
    d = os.path.join(workspace_home(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def secrets_dir() -> str:
    d = os.path.join(workspace_home(), "secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def skills_dir() -> str:
    d = os.path.join(workspace_home(), "skills")
    os.makedirs(d, exist_ok=True)
    return d


def get_or_create_cli_token() -> str:
    """Return the local-CLI shared secret, generating it on first use.

    Anyone who can read this file already has filesystem access to the
    workspace container — the token only proves "same machine", not "is a
    real user"; it exists so ``./aw`` can call the workspace's own identity-
    gated API without a browser-issued ``aw_id_jwt``.
    """
    path = os.path.join(workspace_home(), "cli-token")
    try:
        with open(path, "r") as f:
            token = f.read().strip()
            if token:
                return token
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token
