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
"""
from __future__ import annotations

import os


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
