"""``aw-workspace-cli restart <component>`` — port of the monolith's ``./aw restart``.

Takes a bare app slug (``kb``) or a full component key
(``docker:aw-kb``); see ``src/cli/lifecycle.py``.
"""
from __future__ import annotations

import argparse

from src.cli.lifecycle import act

COMMAND = "restart"
DESCRIPTION = "Restart a workspace component (app container or managed service)"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli restart", description=DESCRIPTION,
    )
    parser.add_argument("component", help="App slug or component key")
    ns = parser.parse_args(args)
    return act("restart", ns.component)
