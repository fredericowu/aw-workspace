"""``aw-workspace-cli start <component>`` — port of the monolith's ``./aw start``.

Takes a bare app slug (``kb``) or a full component key
(``docker:aw-kb``); see ``src/cli/lifecycle.py``.
"""
from __future__ import annotations

import argparse

from src.cli.lifecycle import act

COMMAND = "start"
DESCRIPTION = "Start a workspace component (app container or managed service)"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli start", description=DESCRIPTION,
    )
    parser.add_argument("component", help="App slug or component key")
    ns = parser.parse_args(args)
    return act("start", ns.component)
