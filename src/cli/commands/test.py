"""``aw-workspace-cli test`` — port of the monolith's ``./aw test``.

Runs this repo's own pytest suite from the workspace root, so an agent or a
terminal session doesn't have to know where the tests live or which
interpreter to use. Extra arguments are passed straight through to pytest
(``aw-workspace-cli test -k folders -x``).
"""
from __future__ import annotations

import os
import subprocess
import sys

from src.apps.paths import DEFAULT_WORKSPACE_CONTAINER_DIR

COMMAND = "test"
DESCRIPTION = "Run this workspace's pytest suite (extra args go to pytest)"


def run(args: list[str]) -> int:
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    root = os.path.realpath(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", DEFAULT_WORKSPACE_CONTAINER_DIR)
    )
    target = args or ["src/tests"]
    # sys.executable, not a bare `pytest`: the CLI may be invoked from a
    # sibling container whose PATH has a different (or no) pytest, while its
    # interpreter is already the one that can import this repo.
    return subprocess.call(
        [sys.executable, "-m", "pytest", *target],
        cwd=root,
        env={**os.environ, "PYTHONPATH": root},
    )
