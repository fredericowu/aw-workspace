"""``aw-workspace-cli stop <component>`` — port of the monolith's ``./aw stop``.

Takes a bare app slug (``kb``) or a full component key
(``docker:aw-kb``); see ``src/cli/lifecycle.py``.
"""
from __future__ import annotations

import argparse

from src.cli.lifecycle import act

COMMAND = "stop"
DESCRIPTION = "Stop a workspace component (app container or managed service)"


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="aw-workspace-cli stop", description=DESCRIPTION,
    )
    parser.add_argument("component", help="App slug or component key")
    ns = parser.parse_args(args)
    return act("stop", ns.component)
